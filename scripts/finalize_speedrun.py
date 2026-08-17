#!/usr/bin/env python3
"""Safely finalize a stopped speedrun and submit its verified solutions.

The command is a dry-run unless ``--execute`` is present.  Execution is only
allowed after both the configured hard stop and the controller-owned ``STOP``
marker.  Git commits use explicit path lists; this script never stages a whole
workspace, the repository root, ignored files, or raw controller logs.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import zlib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


REPO = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO / "speedrun" / "config.json"
DEFAULT_ISSUE_REPO = "leanprover/lean-eval-submissions"
STATE_SCHEMA = "lean-eval-speedrun-finalize-state-v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
FORBIDDEN_RE = re.compile(r"\b(?:sorry|admit|native_decide|set_option)\b")

ADMIN_EXACT = {
    "speedrun/README.md",
    "speedrun/config.json",
    "speedrun/pricing.v1.json",
    "speedrun/problems.jsonl",
    "speedrun/queue.tsv",
    "speedrun/schema.v1.json",
    "speedrun/solver_prompt.md",
    "analysis/solves.csv",
    "analysis/solves-over-log-time.svg",
    "analysis/solves-over-real-time.svg",
    "analysis/solves-over-24-hours.svg",
    "analysis/solves-over-solve-time.svg",
    "analysis/solves-over-budget.svg",
    "speedrun/toy_prompt.md",
    "scripts/archive_speedrun_logs.py",
    "scripts/finalize_speedrun.py",
    "scripts/graph_speedrun.py",
    "scripts/hardstop_guard.py",
    "scripts/launch_wave.py",
    "scripts/poststop_finalize.py",
    "scripts/speedrun.py",
}

PUBLICATION_LABELS = {
    "public": "Public",
    "planned": "Private, but publication is planned",
    "private": "Private, with no current publication plan",
}

ACKNOWLEDGEMENTS = (
    "I understand that the lean-eval CI will fetch my submission URL and run comparator "
    "on every lakefile.toml whose name matches a benchmark problem id.",
    "I understand that only the set of solved problem IDs, along with the metadata I entered "
    "above, will be published to the public leaderboard results store.",
    "I understand that an encrypted copy of the submission source tree (compressed gzipped "
    "tar, ≤ 10 MiB) is retained indefinitely in the private `leanprover/lean-eval-audit` "
    "repository for audit purposes, decryptable only by a small set of benchmark maintainers "
    "listed in `.audit/recipients.txt`. See "
    "[`docs/audit-archive.md`](https://github.com/leanprover/lean-eval-submissions/blob/main/"
    "docs/audit-archive.md).",
)


class FinalizeError(RuntimeError):
    """A safety precondition or administrative action failed."""


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise FinalizeError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(canonical_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FinalizeError(f"cannot read valid JSON from {path}: {error}") from error


def parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise FinalizeError(f"invalid ISO timestamp {value!r}") from error
    if parsed.tzinfo is None:
        raise FinalizeError(f"timestamp lacks a timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def parse_date(value: str, label: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise FinalizeError(f"{label} must use YYYY-MM-DD: {value!r}") from error
    if parsed.isoformat() != value:
        raise FinalizeError(f"{label} must use canonical YYYY-MM-DD: {value!r}")
    return parsed


def display_command(argv: Iterable[str], cwd: Path = REPO) -> None:
    relative = "." if cwd == REPO else str(cwd)
    print(f"[{relative}] $ {shlex.join(list(argv))}")


def run_command(
    argv: list[str],
    *,
    cwd: Path = REPO,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: float | None = None,
    check: bool = True,
    announce: bool = True,
) -> subprocess.CompletedProcess[str]:
    if announce:
        display_command(argv, cwd)
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FinalizeError(f"command could not complete: {shlex.join(argv)}: {error}") from error
    if check and completed.returncode != 0:
        details = (completed.stderr or completed.stdout).strip()
        if len(details) > 4000:
            details = details[-4000:]
        suffix = f"\n{details}" if details else ""
        raise FinalizeError(
            f"command exited {completed.returncode}: {shlex.join(argv)}{suffix}"
        )
    return completed


def git(*args: str, check: bool = True, announce: bool = False) -> subprocess.CompletedProcess[str]:
    return run_command(["git", *args], check=check, announce=announce)


def git_output(*args: str) -> str:
    return git(*args).stdout.strip()


def ensure_repo() -> None:
    root = Path(git_output("rev-parse", "--show-toplevel")).resolve()
    if root != REPO.resolve():
        raise FinalizeError(f"script repository mismatch: {root} != {REPO.resolve()}")


def assert_clean_index() -> None:
    if git("diff", "--cached", "--quiet", check=False).returncode != 0:
        raise FinalizeError("git index is not clean; refusing to mix pre-staged user changes")
    if git_output("ls-files", "--unmerged"):
        raise FinalizeError("repository has unmerged index entries")


def assert_no_git_operation() -> None:
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG"):
        raw = git_output("rev-parse", "--git-path", name)
        path = Path(raw) if Path(raw).is_absolute() else REPO / raw
        if path.exists():
            raise FinalizeError(f"git operation is in progress ({name})")


def config_fingerprint(configuration: dict[str, Any], args: argparse.Namespace) -> str:
    value = {
        "hard_stop": configuration["hard_stop"],
        "issue_repo": args.issue_repo,
        "issue_title": normalized_issue_title(args.issue_title),
        "model": args.model,
        "production_description": args.production_description,
        "publication_date": args.publication_date,
        "publication_status": args.publication_status,
        "intended_publication_date": args.intended_publication_date,
        "logs_commit_message": args.logs_commit_message,
        "max_submission_archive_bytes": args.max_submission_archive_bytes,
        "push_branch": args.push_branch,
        "remote": args.remote,
        "solution_commit_message": args.solution_commit_message,
        "source_repo": args.source_repo,
    }
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def state_path() -> Path:
    raw = git_output("rev-parse", "--git-path", "lean-eval-speedrun-finalize.json")
    return (Path(raw) if Path(raw).is_absolute() else REPO / raw).resolve()


def load_state(path: Path, fingerprint: str) -> dict[str, Any]:
    if not path.exists():
        return {"schema": STATE_SCHEMA, "fingerprint": fingerprint}
    value = read_json(path)
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
        raise FinalizeError(f"unrecognized finalization state: {path}")
    if value.get("fingerprint") != fingerprint:
        raise FinalizeError(
            "existing finalization state was created with different remote/submission options"
        )
    return value


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    atomic_json(path, state)


def process_is_expected_watchdog(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        command_line = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return False
    return b"scripts/speedrun.py" in command_line and b"_watchdog" in command_line


def active_markers(raw_root: Path) -> list[Path]:
    return sorted(raw_root.glob("*/active.json")) if raw_root.exists() else []


def raw_snapshot(raw_root: Path) -> tuple[tuple[str, int, int], ...]:
    rows: list[tuple[str, int, int]] = []
    if not raw_root.exists():
        return ()
    for directory, directory_names, file_names in os.walk(raw_root, followlinks=False):
        directory_names[:] = sorted(
            name for name in directory_names if not (Path(directory) / name).is_symlink()
        )
        for name in sorted(file_names):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
            rows.append((path.relative_to(raw_root).as_posix(), stat.st_size, stat.st_mtime_ns))
    return tuple(rows)


def wait_for_quiescence(
    raw_root: Path, watchdog_path: Path, wait_seconds: float, poll_seconds: float, stability_seconds: float
) -> None:
    deadline = time.monotonic() + wait_seconds
    while True:
        markers = active_markers(raw_root)
        watchdog_running = False
        if watchdog_path.is_file():
            try:
                watchdog_running = process_is_expected_watchdog(int(read_json(watchdog_path)["pid"]))
            except (FinalizeError, KeyError, TypeError, ValueError):
                watchdog_running = False
        if not markers and not watchdog_running:
            break
        if time.monotonic() >= deadline:
            names = [path.relative_to(raw_root).as_posix() for path in markers]
            raise FinalizeError(
                f"workers did not quiesce within {wait_seconds:g}s; active={names}, "
                f"watchdog_running={watchdog_running}"
            )
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))

    while True:
        before = raw_snapshot(raw_root)
        if stability_seconds > 0:
            time.sleep(stability_seconds)
        after = raw_snapshot(raw_root)
        if before == after:
            break
        if time.monotonic() >= deadline:
            raise FinalizeError("raw job evidence continued changing after workers disappeared")

    incomplete = []
    for launch in sorted(raw_root.glob("*/launch.json")) if raw_root.exists() else []:
        if not (launch.parent / "result.json").is_file():
            incomplete.append(launch.parent.name)
            verification_path = launch.parent / "verification.json"
            if verification_path.is_file():
                verification = read_json(verification_path)
                if not isinstance(verification, dict):
                    raise FinalizeError(
                        f"inconsistent non-object verification artifact: {launch.parent.name}"
                    )
                if verification.get("verified") is True:
                    raise FinalizeError(
                        "inconsistent evidence: verified artifact has no matching result: "
                        f"{launch.parent.name}"
                    )
    if incomplete:
        print(
            "preserving hard-stop-incomplete jobs as archival evidence; skipping audit/qualification: "
            + ", ".join(incomplete)
        )


def assert_stopped(configuration: dict[str, Any], args: argparse.Namespace) -> tuple[Path, datetime]:
    hard_stop = parse_iso(str(configuration["hard_stop"]))
    now = datetime.now(timezone.utc)
    if now < hard_stop:
        remaining = (hard_stop - now).total_seconds()
        raise FinalizeError(
            f"administrative finalization is forbidden before hard stop {configuration['hard_stop']} "
            f"({remaining:.3f}s remain)"
        )
    raw_root = Path(str(configuration["raw_root"])).resolve()
    stop = raw_root / "STOP"
    if stop.is_symlink() or not stop.is_file():
        raise FinalizeError(f"missing controller STOP marker: {stop}")
    stop_record = read_json(stop)
    if not isinstance(stop_record, dict) or stop_record.get("schema") != "lean-eval-stop-v1":
        raise FinalizeError(f"invalid controller STOP marker: {stop}")
    if not isinstance(stop_record.get("at"), str):
        raise FinalizeError("STOP marker has no timestamp")
    parse_iso(stop_record["at"])
    wait_for_quiescence(
        raw_root,
        raw_root / "watchdog.json",
        args.worker_wait_seconds,
        args.poll_seconds,
        args.stability_seconds,
    )
    return raw_root, hard_stop


def changed_paths() -> set[str]:
    tracked = git("diff", "--name-only", "-z", "HEAD", "--").stdout.split("\0")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z", "--").stdout.split("\0")
    return {path for path in tracked + untracked if path}


def current_submission_files(problem: str) -> set[str]:
    workspace = REPO / "generated" / problem
    main = workspace / "Submission.lean"
    if main.is_symlink() or not main.is_file():
        raise FinalizeError(f"missing regular Submission.lean for verified problem {problem}")
    paths = {main.relative_to(REPO).as_posix()}
    helpers = workspace / "Submission"
    if helpers.exists():
        if helpers.is_symlink() or not helpers.is_dir():
            raise FinalizeError(f"unsafe Submission directory for {problem}: {helpers}")
        for path in sorted(helpers.rglob("*.lean")):
            if path.is_symlink() or not path.is_file():
                raise FinalizeError(f"unsafe helper path: {path}")
            paths.add(path.relative_to(REPO).as_posix())
    return paths


def is_solution_path(path: str, problem: str) -> bool:
    main = f"generated/{problem}/Submission.lean"
    prefix = f"generated/{problem}/Submission/"
    return path == main or (path.startswith(prefix) and path.endswith(".lean"))


def normalized_checked_files(verification: dict[str, Any], problem: str) -> set[str]:
    raw_files = verification.get("checked_files")
    if not isinstance(raw_files, list) or not raw_files:
        raise FinalizeError(f"verified job for {problem} has no checked_files")
    result: set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, str):
            raise FinalizeError(f"non-string checked file for {problem}")
        path = Path(raw)
        if not path.is_absolute():
            raise FinalizeError(f"checked file is not absolute: {raw}")
        try:
            relative = path.resolve(strict=True).relative_to(REPO.resolve()).as_posix()
        except (OSError, ValueError) as error:
            raise FinalizeError(f"checked file escapes or is missing: {raw}") from error
        if not is_solution_path(relative, problem):
            raise FinalizeError(f"verified checked file is outside solution allowlist: {relative}")
        result.add(relative)
    return result


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise FinalizeError(f"{path}:{line_number}: expected JSON object")
                records.append(value)
    except (OSError, json.JSONDecodeError) as error:
        raise FinalizeError(f"cannot parse {path}: {error}") from error
    return records


def timestamp_ns(value: str) -> int:
    return int(parse_iso(value).timestamp() * 1_000_000_000)


def discover_verified(
    raw_root: Path, race_start: datetime, hard_stop: datetime, *, require_audits: bool
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    by_problem: dict[str, list[dict[str, Any]]] = {}
    audit_notes: list[str] = []
    for result_path in sorted(raw_root.glob("*/result.json")):
        job_dir = result_path.parent
        result = read_json(result_path)
        problem = result.get("problem_id")
        if not isinstance(problem, str) or problem.startswith("_toy"):
            continue
        launch_path = job_dir / "launch.json"
        if not launch_path.is_file():
            raise FinalizeError(f"verified candidate has no launch record: {job_dir.name}")
        launch = read_json(launch_path)
        verification_path = job_dir / "verification.json"
        if not verification_path.is_file():
            continue
        verification = read_json(verification_path)
        if verification.get("verified") is not True:
            continue
        if result.get("schema") != "lean-eval-job-result-v1":
            raise FinalizeError(f"unrecognized result schema for {job_dir.name}")
        if launch.get("schema") != "lean-eval-job-launch-v1":
            raise FinalizeError(f"unrecognized launch schema for {job_dir.name}")
        if verification.get("schema") != "lean-eval-verification-v1":
            raise FinalizeError(f"unrecognized verification schema for {job_dir.name}")
        if result.get("status") != "verified":
            raise FinalizeError(f"verification/result status mismatch for {job_dir.name}")
        if result.get("job_id") != job_dir.name or launch.get("job_id") != job_dir.name:
            raise FinalizeError(f"launch/result identity mismatch for {job_dir.name}")
        if launch.get("problem_id") != problem:
            raise FinalizeError(f"launch/result problem mismatch for {job_dir.name}")
        if verification.get("problem_id") != problem or verification.get("job_id") != job_dir.name:
            raise FinalizeError(f"verification identity mismatch for {job_dir.name}")
        if result.get("verification_sha256") != hash_file(verification_path):
            raise FinalizeError(f"verification hash mismatch for {job_dir.name}")
        if verification.get("argv") != ["lake", "test"]:
            raise FinalizeError(f"verified job did not run the controller's exact lake test: {job_dir.name}")
        expected_cwd = (REPO / "generated" / problem).resolve()
        try:
            verification_cwd = Path(str(verification.get("cwd"))).resolve(strict=True)
        except OSError as error:
            raise FinalizeError(f"verified job has an invalid cwd: {job_dir.name}") from error
        if verification_cwd != expected_cwd:
            raise FinalizeError(f"verified job used the wrong workspace: {job_dir.name}")
        if (
            verification.get("exit_code") != 0
            or verification.get("forbidden_hits") != []
            or verification.get("disallowed_workspace_changes") != []
        ):
            raise FinalizeError(f"verified job has inconsistent verification details: {job_dir.name}")
        if verification.get("timed_out_at_hard_stop") is True:
            raise FinalizeError(f"verified job claims a hard-stop timeout: {job_dir.name}")
        finished_text = verification.get("finished_at_utc")
        if not isinstance(finished_text, str):
            raise FinalizeError(f"verified job has no finish timestamp: {job_dir.name}")
        finished = parse_iso(finished_text)
        if finished < race_start or finished > hard_stop:
            raise FinalizeError(
                f"verified job {job_dir.name} falls outside the race window: {finished_text}"
            )
        launch_wall_ns = launch.get("started_wall_ns")
        verified_wall_ns = verification.get("finished_wall_ns")
        if not isinstance(launch_wall_ns, int) or not isinstance(verified_wall_ns, int):
            raise FinalizeError(f"verified job has invalid controller wall times: {job_dir.name}")
        race_start_ns = timestamp_ns(race_start.isoformat())
        hard_stop_ns = timestamp_ns(hard_stop.isoformat())
        rendered_finished_ns = timestamp_ns(finished_text)
        if not (
            race_start_ns <= launch_wall_ns <= verified_wall_ns <= hard_stop_ns
            and rendered_finished_ns <= verified_wall_ns < rendered_finished_ns + 1_000_000
        ):
            raise FinalizeError(f"verified job has inconsistent/deadline-unsafe wall times: {job_dir.name}")
        audit_path = job_dir / "audit.json"
        audit = read_json(audit_path) if audit_path.is_file() else None
        if require_audits:
            if (
                not isinstance(audit, dict)
                or audit.get("schema") != "lean-eval-job-audit-v1"
                or audit.get("job_id") != job_dir.name
                or audit.get("problem_id") != problem
                or audit.get("passed") is not True
                or audit.get("verified") is not True
                or not isinstance(audit.get("artifact_hashes_match"), dict)
                or not all(audit["artifact_hashes_match"].values())
            ):
                raise FinalizeError(f"verified job lacks a passing verified audit: {job_dir.name}")
        elif not isinstance(audit, dict) or audit.get("passed") is not True:
            audit_notes.append(f"{job_dir.name}: audit will be generated/rechecked")
        try:
            checked = normalized_checked_files(verification, problem)
        except FinalizeError as error:
            audit_notes.append(f"{job_dir.name}: skipped because {error}")
            continue
        current = current_submission_files(problem)
        if checked != current:
            missing = sorted(checked - current)
            added = sorted(current - checked)
            audit_notes.append(
                f"{job_dir.name}: skipped because current paths differ from verification; "
                f"missing={missing}, added={added}"
            )
            continue
        verified_ns = verified_wall_ns
        too_new = []
        for relative in sorted(current):
            stat = (REPO / relative).stat()
            if max(stat.st_mtime_ns, stat.st_ctime_ns) > verified_ns:
                too_new.append(relative)
        if too_new:
            audit_notes.append(
                f"{job_dir.name}: skipped because source metadata changed after verification: {too_new}"
            )
            continue
        by_problem.setdefault(problem, []).append(
            {
                "job_id": job_dir.name,
                "verified_at": finished_text,
                "verified_ns": verified_ns,
                "files": sorted(current),
            }
        )

    selected: dict[str, dict[str, Any]] = {}
    for problem, rows in by_problem.items():
        selected[problem] = min(rows, key=lambda row: (row["verified_ns"], row["job_id"]))

    aggregates = {
        str(row["problem_id"])
        for row in read_jsonl(REPO / "speedrun" / "problems.jsonl")
        if row.get("verified") is True and not str(row.get("problem_id", "")).startswith("_toy")
    }
    if require_audits and aggregates != set(selected):
        raise FinalizeError(
            "verified reducer output does not match source/audit-qualified jobs: "
            f"aggregate_only={sorted(aggregates - set(selected))}, "
            f"qualified_only={sorted(set(selected) - aggregates)}"
        )
    return selected, audit_notes


def assert_solution_changes(
    selected: dict[str, dict[str, Any]], changes: set[str]
) -> list[str]:
    solution_paths: set[str] = set()
    for problem, row in sorted(selected.items()):
        prefix = f"generated/{problem}/"
        disallowed = sorted(
            path for path in changes if path.startswith(prefix) and not is_solution_path(path, problem)
        )
        if disallowed:
            raise FinalizeError(
                f"verified workspace {problem} has non-solution changes: {disallowed}"
            )
        one_problem = {path for path in changes if is_solution_path(path, problem)}
        if not one_problem:
            raise FinalizeError(f"verified problem has no solution change relative to HEAD: {problem}")
        solution_paths.update(one_problem)
        for relative in row["files"]:
            text = (REPO / relative).read_text(encoding="utf-8")
            hit = FORBIDDEN_RE.search(text)
            if hit:
                raise FinalizeError(f"forbidden token {hit.group(0)!r} in {relative}")
    if not solution_paths:
        raise FinalizeError("no audited, verified real solutions are available for commit S")
    return sorted(solution_paths)


def administrative_jobs(raw_root: Path) -> list[tuple[str, bool]]:
    jobs: list[tuple[str, bool]] = []
    for launch in sorted(raw_root.glob("*/launch.json")):
        job_dir = launch.parent
        result = job_dir / "result.json"
        if not result.is_file():
            verification_path = job_dir / "verification.json"
            if verification_path.is_file():
                verification = read_json(verification_path)
                if not isinstance(verification, dict):
                    raise FinalizeError(
                        f"inconsistent non-object verification artifact: {job_dir.name}"
                    )
                if verification.get("verified") is True:
                    raise FinalizeError(
                        "inconsistent evidence: verified artifact has no matching result: "
                        f"{job_dir.name}"
                    )
            continue
        verification_path = job_dir / "verification.json"
        verified = verification_path.is_file() and read_json(verification_path).get("verified") is True
        jobs.append((job_dir.name, verified))
    return jobs


def run_administrative_pipeline(raw_root: Path) -> None:
    for job_id, verified in administrative_jobs(raw_root):
        argv = [sys.executable, str(REPO / "scripts" / "speedrun.py"), "audit", "--job-id", job_id]
        if verified:
            argv.append("--require-verified")
        run_command(argv)
    run_command(
        [sys.executable, str(REPO / "scripts" / "speedrun.py"), "reduce", "--output",
         "speedrun/problems.jsonl", "--check-determinism"]
    )
    run_command([sys.executable, str(REPO / "scripts" / "graph_speedrun.py")])
    run_command([sys.executable, str(REPO / "scripts" / "archive_speedrun_logs.py")])


def validation_environment(configuration: dict[str, Any]) -> dict[str, str]:
    environment = os.environ.copy()
    prefixes = configuration.get("tool_path_prefixes")
    if not isinstance(prefixes, list) or not all(isinstance(item, str) for item in prefixes):
        raise FinalizeError("config tool_path_prefixes is not a list of strings")
    environment["PATH"] = os.pathsep.join([*prefixes, environment.get("PATH", "")])
    return environment


def validate_current_solutions(
    selected: dict[str, dict[str, Any]], solution_paths: list[str], configuration: dict[str, Any], timeout: float
) -> None:
    environment = validation_environment(configuration)
    for problem in sorted(selected):
        run_command(
            ["lake", "test"],
            cwd=REPO / "generated" / problem,
            env=environment,
            timeout=timeout,
        )
    for relative in solution_paths:
        run_command(
            ["lake", "exe", "lean-eval", "validate-submission", "--file", relative, "--json"],
            timeout=timeout,
        )


def nul_paths(output: str) -> set[str]:
    return {item for item in output.split("\0") if item}


def staged_paths() -> set[str]:
    return nul_paths(git("diff", "--cached", "--name-only", "-z").stdout)


def commit_paths(commit: str) -> set[str]:
    return nul_paths(
        git("diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", commit).stdout
    )


def unstage_exact(paths: list[str]) -> None:
    if paths:
        git("restore", "--staged", "--", *paths, check=False, announce=True)


def commit_exact(paths: list[str], message: str) -> str:
    if not paths:
        raise FinalizeError("refusing to create an empty commit")
    assert_clean_index()
    before = git_output("rev-parse", "HEAD")
    run_command(["git", "add", "--", *paths])
    actual_staged = staged_paths()
    if actual_staged != set(paths):
        unstage_exact(paths)
        raise FinalizeError(
            f"staged path set differs from allowlist: expected={paths}, actual={sorted(actual_staged)}"
        )
    try:
        run_command(["git", "commit", "--no-gpg-sign", "--only", "-m", message, "--", *paths])
    except FinalizeError:
        unstage_exact(paths)
        raise
    commit = git_output("rev-parse", "HEAD")
    parents = git_output("rev-list", "--parents", "-n", "1", commit).split()
    if len(parents) != 2 or parents[1] != before:
        raise FinalizeError(f"unexpected commit parent for {commit}")
    actual = commit_paths(commit)
    if actual != set(paths):
        raise FinalizeError(
            f"commit contains paths outside its allowlist: expected={paths}, actual={sorted(actual)}"
        )
    assert_clean_index()
    return commit


def validate_solution_commit(commit: str, expected_paths: list[str], timeout: float) -> None:
    if not SHA_RE.fullmatch(commit):
        raise FinalizeError(f"invalid solution commit SHA: {commit}")
    actual = commit_paths(commit)
    if actual != set(expected_paths):
        raise FinalizeError("saved solution commit no longer matches its recorded path allowlist")
    parent = f"{commit}^"
    run_command(
        ["lake", "exe", "lean-eval", "validate-submission", "--base", parent, "--head", commit, "--json"],
        timeout=timeout,
    )


def compressed_git_archive_size(commit: str) -> int:
    """Return a close, conservative preflight for the hosted source.tar.gz size."""
    display_command(["git", "archive", "--format=tar", commit])
    try:
        process = subprocess.Popen(
            ["git", "archive", "--format=tar", commit],
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise FinalizeError(f"could not start git archive: {error}") from error
    if process.stdout is None or process.stderr is None:
        raise FinalizeError("git archive pipes were not created")
    compressor = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=31)
    size = 0
    for block in iter(lambda: process.stdout.read(1024 * 1024), b""):
        size += len(compressor.compress(block))
    size += len(compressor.flush())
    stderr = process.stderr.read().decode("utf-8", errors="replace")
    return_code = process.wait()
    if return_code != 0:
        raise FinalizeError(f"git archive exited {return_code}: {stderr.strip()}")
    return size


def assert_submission_archive_size(commit: str, cap: int) -> int:
    size = compressed_git_archive_size(commit)
    if size > cap:
        raise FinalizeError(
            f"compressed tree for commit S is approximately {size} bytes, above safety cap {cap}; "
            "the hosted audit archive would reject it"
        )
    print(f"submission archive preflight: {size} compressed bytes (safety cap {cap})")
    return size


def is_admin_path(path: str) -> bool:
    if path in ADMIN_EXACT:
        return True
    if re.fullmatch(r"speedrun/wave[0-9]+\.tsv", path):
        return True
    if path.startswith("speedrun/prompts/") and path.endswith(".md"):
        return len(Path(path).parts) == 3
    if path == "speedrun/toy/Toy.lean":
        return True
    if path.startswith("speedrun/toy-evidence/") and path.endswith(".json"):
        return len(Path(path).parts) == 3
    if path == "speedrun-logs/manifest.json":
        return True
    if path.startswith("speedrun-logs/jobs/") or path.startswith("speedrun-logs/rollouts/"):
        return path.endswith(".gz")
    return False


def file_matches_head(relative: str) -> bool:
    current = git("hash-object", "--", relative, check=False)
    committed = git("rev-parse", "--verify", f"HEAD:{relative}", check=False)
    return (
        current.returncode == 0
        and committed.returncode == 0
        and current.stdout.strip() == committed.stdout.strip()
    )


def administrative_paths(changes: set[str], args: argparse.Namespace, hard_stop: datetime) -> list[str]:
    required = (
        REPO / "speedrun" / "problems.jsonl",
        REPO / "analysis" / "solves.csv",
        REPO / "analysis" / "solves-over-log-time.svg",
        REPO / "analysis" / "solves-over-real-time.svg",
        REPO / "analysis" / "solves-over-24-hours.svg",
        REPO / "analysis" / "solves-over-solve-time.svg",
        REPO / "analysis" / "solves-over-budget.svg",
        REPO / "speedrun-logs" / "manifest.json",
    )
    for path in required:
        if path.is_symlink() or not path.is_file():
            raise FinalizeError(f"missing final administrative output: {path}")
    manifest = read_json(REPO / "speedrun-logs" / "manifest.json")
    if manifest.get("schema") != "lean-eval-speedrun-log-archive-v1":
        raise FinalizeError("unrecognized speedrun log archive manifest")
    if parse_iso(str(manifest.get("hard_stop"))) != hard_stop:
        raise FinalizeError("archive manifest hard stop differs from active configuration")
    rows = manifest.get("files")
    if not isinstance(rows, list):
        raise FinalizeError("archive manifest files field is not a list")
    manifest_paths: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("archive"), str):
            raise FinalizeError("malformed file entry in archive manifest")
        raw_relative = row["archive"]
        archive_relative = Path(raw_relative)
        if (
            archive_relative.is_absolute()
            or ".." in archive_relative.parts
            or archive_relative.as_posix() != raw_relative
        ):
            raise FinalizeError(f"unsafe archive path in manifest: {raw_relative!r}")
        relative = (Path("speedrun-logs") / archive_relative).as_posix()
        if not (
            relative.endswith(".gz")
            and (
                relative.startswith("speedrun-logs/jobs/")
                or relative.startswith("speedrun-logs/rollouts/")
            )
        ):
            raise FinalizeError(f"archive path is outside the log allowlist: {relative}")
        if relative in manifest_paths:
            raise FinalizeError(f"duplicate archive path in manifest: {relative}")
        manifest_paths.add(relative)
        archive = REPO / relative
        if archive.is_symlink() or not archive.is_file():
            raise FinalizeError(f"archive manifest references missing/unsafe file: {archive}")
        expected_hash = row.get("archive_sha256")
        if not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
            raise FinalizeError(f"archive manifest has an invalid hash for {relative}")
        if hash_file(archive) != expected_hash:
            raise FinalizeError(f"archive hash differs from manifest: {relative}")
        if relative not in changes and not file_matches_head(relative):
            raise FinalizeError(
                f"manifest archive is neither a visible change nor identical to HEAD: {relative}"
            )

    paths = sorted(
        path
        for path in changes
        if is_admin_path(path)
        and (
            not path.startswith(("speedrun-logs/jobs/", "speedrun-logs/rollouts/"))
            or path in manifest_paths
        )
    )
    if not paths:
        raise FinalizeError("no changed administrative artifacts are available for commit L")
    total = 0
    for relative in paths:
        path = REPO / relative
        if path.is_symlink() or not path.is_file():
            raise FinalizeError(f"administrative allowlist contains a missing/non-regular file: {relative}")
        size = path.stat().st_size
        if size > args.max_admin_file_bytes:
            raise FinalizeError(
                f"administrative file exceeds {args.max_admin_file_bytes} byte cap: {relative} ({size})"
            )
        total += size
    if total > args.max_admin_total_bytes:
        raise FinalizeError(
            f"administrative commit would contain {total} bytes, above cap {args.max_admin_total_bytes}"
        )
    return paths


def parse_github_remote(url: str) -> str | None:
    if url.startswith("git@github.com:"):
        candidate = url.removeprefix("git@github.com:")
    elif url.startswith("ssh://git@github.com/"):
        candidate = url.removeprefix("ssh://git@github.com/")
    else:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            return None
        if parsed.username is not None or parsed.password is not None:
            raise FinalizeError("refusing a GitHub remote URL containing embedded credentials")
        candidate = parsed.path.lstrip("/")
    candidate = candidate.removesuffix(".git").rstrip("/")
    return candidate if REPO_RE.fullmatch(candidate) else None


def assert_remote_matches(remote: str, source_repo: str) -> None:
    url = git_output("remote", "get-url", "--push", remote)
    parsed = parse_github_remote(url)
    if parsed is None or parsed.casefold() != source_repo.casefold():
        raise FinalizeError(
            f"push remote {remote!r} is not the declared GitHub repository {source_repo!r}"
        )


def gh_json(argv: list[str]) -> Any:
    completed = run_command(["gh", *argv])
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise FinalizeError(f"gh returned non-JSON output for {shlex.join(argv)}") from error


def assert_issue_template(issue_repo: str) -> None:
    value = gh_json(
        ["api", f"repos/{issue_repo}/contents/.github/ISSUE_TEMPLATE/submit.yml"]
    )
    encoded = value.get("content") if isinstance(value, dict) else None
    if not isinstance(encoded, str):
        raise FinalizeError("hosted submission template API response has no content")
    try:
        template = base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise FinalizeError("cannot decode hosted submission issue template") from error
    required = (
        'title: "[submission] ',
        "id: source_url",
        "label: Submission URL",
        "id: model",
        "label: Model",
        "id: solution_publication_status",
        "label: Exact solution publication status",
        "id: publication_date",
        "id: intended_publication_date",
        "id: production_description",
        "id: acknowledgements",
        "lean-eval CI will fetch my submission URL",
        "only the set of solved problem IDs",
        "encrypted copy of the submission source",
    )
    missing = [snippet for snippet in required if snippet not in template]
    if missing:
        raise FinalizeError(f"hosted submission issue schema has drifted; missing {missing}")


def assert_repo_visibility(source_repo: str, publication_status: str) -> str:
    value = gh_json(["repo", "view", source_repo, "--json", "nameWithOwner,visibility"])
    visibility = str(value.get("visibility", "")).upper()
    actual_repo = str(value.get("nameWithOwner", ""))
    if actual_repo.casefold() != source_repo.casefold():
        raise FinalizeError("GitHub repository identity differs from --source-repo")
    if publication_status == "public" and visibility != "PUBLIC":
        raise FinalizeError("publication status is Public but source repository is not public")
    if publication_status != "public" and visibility == "PUBLIC":
        raise FinalizeError(
            "publication status is private/planned but pushing exact solutions to a public "
            "repository would publish them"
        )
    return visibility


def external_preflight(args: argparse.Namespace) -> str:
    run_command(["gh", "auth", "status", "--hostname", "github.com", "--active"])
    assert_remote_matches(args.remote, args.source_repo)
    visibility = assert_repo_visibility(args.source_repo, args.publication_status)
    assert_issue_template(args.issue_repo)
    return visibility


def remote_branch_sha(remote: str, branch: str) -> str | None:
    completed = run_command(
        ["git", "ls-remote", "--heads", remote, f"refs/heads/{branch}"]
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) != 1:
        raise FinalizeError(f"remote branch lookup returned multiple rows for {branch}")
    sha = lines[0].split()[0]
    if not SHA_RE.fullmatch(sha):
        raise FinalizeError(f"remote returned invalid SHA for {branch}: {sha!r}")
    return sha


def is_ancestor(ancestor: str, descendant: str) -> bool:
    if git("cat-file", "-e", f"{descendant}^{{commit}}", check=False).returncode != 0:
        return False
    return git("merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0


def push_commit(remote: str, branch: str, commit: str) -> None:
    existing = remote_branch_sha(remote, branch)
    if existing == commit or (existing is not None and is_ancestor(commit, existing)):
        print(f"remote {remote}/{branch} already contains {commit}")
        return
    run_command(["git", "push", remote, f"{commit}:refs/heads/{branch}"])
    observed = remote_branch_sha(remote, branch)
    if observed != commit:
        raise FinalizeError(
            f"remote branch did not resolve to pushed commit: expected {commit}, observed {observed}"
        )


def normalized_issue_title(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise FinalizeError("issue title must be one line")
    stripped = value.strip()
    if not stripped:
        raise FinalizeError("issue title cannot be empty")
    return stripped if stripped.startswith("[submission] ") else f"[submission] {stripped}"


def issue_body(
    args: argparse.Namespace, source_commit: str, logs_commit: str, verified: list[str]
) -> str:
    source_url = f"https://github.com/{args.source_repo}/tree/{source_commit}"
    publication_date = args.publication_date or "_No response_"
    intended_date = args.intended_publication_date or "_No response_"
    production = args.production_description.strip() if args.production_description else ""
    if not production:
        production = (
            f"24-hour LeanEval speedrun; {len(verified)} independently verified problem(s): "
            + ", ".join(verified)
            + ". Timing, token, audit, and graph artifacts are recorded in "
            + f"[log/setup commit {logs_commit[:12]}]"
            + f"(https://github.com/{args.source_repo}/tree/{logs_commit})."
        )
    if len(production) > 4000:
        raise FinalizeError("rendered production description exceeds the hosted 4000-character limit")
    acknowledgements = "\n".join(f"- [x] {text}" for text in ACKNOWLEDGEMENTS)
    return (
        f"### Submission URL\n\n{source_url}\n\n"
        f"### Model\n\n{args.model}\n\n"
        "### Exact solution publication status\n\n"
        f"{PUBLICATION_LABELS[args.publication_status]}\n\n"
        f"### Publication date (if public)\n\n{publication_date}\n\n"
        f"### Intended publication date (if planned)\n\n{intended_date}\n\n"
        f"### How this solution was produced (optional)\n\n{production}\n\n"
        f"### Acknowledgements\n\n{acknowledgements}\n"
    )


def find_existing_issue(
    issue_repo: str, source_url: str, model: str, expected_body: str
) -> str | None:
    matches: list[str] = []
    page = 1
    while True:
        values = gh_json(
            ["api", f"repos/{issue_repo}/issues?state=all&per_page=100&page={page}"]
        )
        if not isinstance(values, list):
            raise FinalizeError("GitHub issues API returned a non-list")
        for value in values:
            if not isinstance(value, dict) or "pull_request" in value:
                continue
            body = value.get("body")
            if not isinstance(body, str):
                continue
            if (
                f"### Submission URL\n\n{source_url}" in body
                and f"### Model\n\n{model}" in body
            ):
                if body != expected_body:
                    raise FinalizeError(
                        "an existing submission issue uses this source commit/model but different metadata"
                    )
                url = value.get("html_url")
                if isinstance(url, str):
                    matches.append(url)
        if len(values) < 100:
            break
        page += 1
    unique = sorted(set(matches))
    if len(unique) > 1:
        raise FinalizeError(f"multiple existing issues point to solution commit: {unique}")
    return unique[0] if unique else None


def create_or_find_issue(
    args: argparse.Namespace, source_commit: str, logs_commit: str, verified: list[str]
) -> str:
    source_url = f"https://github.com/{args.source_repo}/tree/{source_commit}"
    body = issue_body(args, source_commit, logs_commit, verified)
    existing = find_existing_issue(args.issue_repo, source_url, args.model, body)
    if existing is not None:
        print(f"reusing existing submission issue {existing}")
        return existing
    completed = run_command(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            args.issue_repo,
            "--title",
            normalized_issue_title(args.issue_title),
            "--body-file",
            "-",
        ],
        input_text=body,
    )
    candidates = [line.strip() for line in completed.stdout.splitlines() if "/issues/" in line]
    if len(candidates) != 1:
        raise FinalizeError(f"could not identify created issue URL from gh output: {completed.stdout!r}")
    issue_url = candidates[0]
    viewed = gh_json(
        ["issue", "view", issue_url, "--json", "body,title,url"]
    )
    if viewed.get("body") != body or viewed.get("title") != normalized_issue_title(args.issue_title):
        raise FinalizeError("created issue does not exactly match the requested title/body")
    return issue_url


def validate_arguments(args: argparse.Namespace) -> None:
    if not REPO_RE.fullmatch(args.source_repo):
        raise FinalizeError(f"invalid --source-repo: {args.source_repo!r}")
    if not REPO_RE.fullmatch(args.issue_repo):
        raise FinalizeError(f"invalid --issue-repo: {args.issue_repo!r}")
    if not BRANCH_RE.fullmatch(args.push_branch) or ".." in args.push_branch or args.push_branch.endswith("/"):
        raise FinalizeError(f"unsafe --push-branch: {args.push_branch!r}")
    if not args.model.strip() or "\n" in args.model or "\r" in args.model:
        raise FinalizeError("--model must be a nonempty single line")
    normalized_issue_title(args.issue_title)
    if args.worker_wait_seconds < 0 or args.poll_seconds <= 0 or args.stability_seconds < 0:
        raise FinalizeError("worker/stability timing options must be nonnegative (poll must be positive)")
    if args.validation_timeout <= 0:
        raise FinalizeError("--validation-timeout must be positive")
    if args.max_admin_file_bytes <= 0 or args.max_admin_total_bytes <= 0:
        raise FinalizeError("administrative byte caps must be positive")
    if not 0 < args.max_submission_archive_bytes <= 10 * 1024 * 1024:
        raise FinalizeError(
            "--max-submission-archive-bytes must be positive and no larger than the hosted 10 MiB cap"
        )
    if args.production_description is not None and len(args.production_description.strip()) > 4000:
        raise FinalizeError("--production-description exceeds the hosted 4000-character limit")
    if args.publication_status == "public":
        if not args.publication_date:
            raise FinalizeError("--publication-date is required for public exact solutions")
        if args.intended_publication_date:
            raise FinalizeError("--intended-publication-date is invalid for public exact solutions")
        if parse_date(args.publication_date, "publication date") > date.today():
            raise FinalizeError("public solution publication date cannot be in the future")
    elif args.publication_status == "planned":
        if args.publication_date:
            raise FinalizeError("--publication-date is invalid for planned publication")
        if not args.intended_publication_date:
            raise FinalizeError("--intended-publication-date is required for planned publication")
        parse_date(args.intended_publication_date, "intended publication date")
    else:
        if args.publication_date or args.intended_publication_date:
            raise FinalizeError("publication dates must be omitted for private/no-plan status")


def dry_run_summary(
    args: argparse.Namespace,
    raw_root: Path,
    hard_stop: datetime,
    state: dict[str, Any],
) -> None:
    race_start = parse_iso(str(read_json(Path(args.config).resolve())["race_start"]))
    selected, notes = discover_verified(
        raw_root, race_start, hard_stop, require_audits=False
    )
    changes = changed_paths()
    potential = sorted(
        path for problem in selected for path in changes if is_solution_path(path, problem)
    )
    planned = {
        "mode": "dry-run",
        "external_mutations": False,
        "hard_stop": hard_stop.isoformat().replace("+00:00", "Z"),
        "stop_marker": str(raw_root / "STOP"),
        "verified_candidates": sorted(selected),
        "potential_solution_paths": potential,
        "audit_notes": notes,
        "resume_state": state,
        "commands_on_execute": [
            "reduce deterministically",
            "audit every completed job; require verified audit for successful jobs",
            "render the analysis solve ledger and graph suite",
            "write deterministic sanitized speedrun-logs archives",
            "rerun lake test and validate-submission for each qualified solution",
            "commit/push exact solution allowlist as S",
            "commit/push closed administrative allowlist as L",
            "create/reuse hosted submission issue whose URL is pinned to S",
        ],
        "remote": args.remote,
        "push_branch": args.push_branch,
        "source_repo": args.source_repo,
        "issue_repo": args.issue_repo,
    }
    print(json.dumps(planned, indent=2, sort_keys=True))


def execute(args: argparse.Namespace) -> int:
    validate_arguments(args)
    ensure_repo()
    if git("check-ref-format", "--branch", args.push_branch, check=False).returncode != 0:
        raise FinalizeError(f"git rejects --push-branch: {args.push_branch!r}")
    configuration = read_json(Path(args.config).resolve())
    if not isinstance(configuration, dict):
        raise FinalizeError("speedrun config must be a JSON object")
    raw_root, hard_stop = assert_stopped(configuration, args)
    race_start = parse_iso(str(configuration["race_start"]))
    if race_start >= hard_stop:
        raise FinalizeError("configured race window is empty or reversed")
    assert_no_git_operation()
    assert_clean_index()
    fingerprint = config_fingerprint(configuration, args)
    local_state_path = state_path()
    state = load_state(local_state_path, fingerprint)

    if not args.execute:
        dry_run_summary(args, raw_root, hard_stop, state)
        return 0

    visibility = external_preflight(args)
    git("var", "GIT_AUTHOR_IDENT")
    run_administrative_pipeline(raw_root)
    selected, _ = discover_verified(
        raw_root, race_start, hard_stop, require_audits=True
    )
    verified = sorted(selected)
    if not verified:
        raise FinalizeError("no real problem has both successful verification and a passing audit")

    solution_commit = state.get("solution_commit")
    solution_paths_value = state.get("solution_paths")
    if solution_commit is None:
        solution_paths = assert_solution_changes(selected, changed_paths())
        validate_current_solutions(
            selected, solution_paths, configuration, args.validation_timeout
        )
        solution_commit = commit_exact(solution_paths, args.solution_commit_message)
        state["solution_commit"] = solution_commit
        state["solution_paths"] = solution_paths
        state["verified_problems"] = verified
        save_state(local_state_path, state)
    else:
        if not isinstance(solution_commit, str) or not SHA_RE.fullmatch(solution_commit):
            raise FinalizeError("invalid solution commit in resume state")
        if not isinstance(solution_paths_value, list) or not all(
            isinstance(path, str) for path in solution_paths_value
        ):
            raise FinalizeError("invalid solution path allowlist in resume state")
        solution_paths = sorted(solution_paths_value)
    validate_solution_commit(solution_commit, solution_paths, args.validation_timeout)
    archive_size = assert_submission_archive_size(
        solution_commit, args.max_submission_archive_bytes
    )
    state["submission_archive_bytes"] = archive_size
    save_state(local_state_path, state)

    if state.get("solution_pushed") is not True:
        push_commit(args.remote, args.push_branch, solution_commit)
        state["solution_pushed"] = True
        save_state(local_state_path, state)
    else:
        remote_sha = remote_branch_sha(args.remote, args.push_branch)
        if remote_sha is None or not (
            remote_sha == solution_commit or is_ancestor(solution_commit, remote_sha)
        ):
            raise FinalizeError("resume state says S was pushed, but the remote branch does not contain S")

    logs_commit = state.get("logs_commit")
    logs_paths_value = state.get("logs_paths")
    if logs_commit is None:
        logs_paths = administrative_paths(changed_paths(), args, hard_stop)
        logs_commit = commit_exact(logs_paths, args.logs_commit_message)
        state["logs_commit"] = logs_commit
        state["logs_paths"] = logs_paths
        save_state(local_state_path, state)
    else:
        if not isinstance(logs_commit, str) or not SHA_RE.fullmatch(logs_commit):
            raise FinalizeError("invalid logs commit in resume state")
        if not isinstance(logs_paths_value, list) or not all(
            isinstance(path, str) for path in logs_paths_value
        ):
            raise FinalizeError("invalid logs path allowlist in resume state")
        logs_paths = sorted(logs_paths_value)
        if commit_paths(logs_commit) != set(logs_paths):
            raise FinalizeError("saved logs commit differs from its recorded path allowlist")
        newly_changed_admin = sorted(path for path in changed_paths() if is_admin_path(path))
        if newly_changed_admin:
            raise FinalizeError(
                "administrative artifacts changed after saved commit L; refusing to submit stale logs: "
                f"{newly_changed_admin}"
            )
    if not is_ancestor(solution_commit, logs_commit):
        raise FinalizeError("logs/setup commit L is not a descendant of solution commit S")

    if state.get("logs_pushed") is not True:
        push_commit(args.remote, args.push_branch, logs_commit)
        state["logs_pushed"] = True
        save_state(local_state_path, state)
    else:
        remote_sha = remote_branch_sha(args.remote, args.push_branch)
        if remote_sha is None or not (
            remote_sha == logs_commit or is_ancestor(logs_commit, remote_sha)
        ):
            raise FinalizeError("resume state says L was pushed, but the remote branch does not contain L")

    issue_url = state.get("issue_url")
    if issue_url is None:
        issue_url = create_or_find_issue(args, solution_commit, logs_commit, verified)
        state["issue_url"] = issue_url
        save_state(local_state_path, state)
    elif not isinstance(issue_url, str) or "/issues/" not in issue_url:
        raise FinalizeError("invalid issue URL in resume state")

    summary = {
        "schema": STATE_SCHEMA,
        "solution_commit": solution_commit,
        "logs_commit": logs_commit,
        "issue_url": issue_url,
        "source_url": f"https://github.com/{args.source_repo}/tree/{solution_commit}",
        "remote_visibility": visibility,
        "submission_archive_bytes": archive_size,
        "verified_problems": verified,
        "state_file": str(local_state_path),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--execute", action="store_true", help="perform local commits and external mutations")
    result.add_argument("--config", default=str(DEFAULT_CONFIG))
    result.add_argument("--remote", default="origin", help="Git remote whose push URL matches --source-repo")
    result.add_argument("--push-branch", required=True, help="explicit non-force destination branch")
    result.add_argument("--source-repo", required=True, help="GitHub owner/repo that will host commits S and L")
    result.add_argument("--issue-repo", default=DEFAULT_ISSUE_REPO)
    result.add_argument("--issue-title", default="LeanEval 24-hour speedrun")
    result.add_argument("--model", required=True, help="free-form hosted leaderboard model/system label")
    result.add_argument(
        "--publication-status", required=True, choices=tuple(PUBLICATION_LABELS),
        help="public, planned, or private",
    )
    result.add_argument("--publication-date")
    result.add_argument("--intended-publication-date")
    result.add_argument("--production-description")
    result.add_argument("--worker-wait-seconds", type=float, default=300.0)
    result.add_argument("--poll-seconds", type=float, default=1.0)
    result.add_argument("--stability-seconds", type=float, default=5.0)
    result.add_argument("--validation-timeout", type=float, default=1800.0)
    result.add_argument("--max-admin-file-bytes", type=int, default=95_000_000)
    result.add_argument("--max-admin-total-bytes", type=int, default=1_000_000_000)
    result.add_argument(
        "--max-submission-archive-bytes",
        type=int,
        default=9_500_000,
        help="conservative cap below the hosted service's absolute 10 MiB limit",
    )
    result.add_argument("--solution-commit-message", default="speedrun: add verified solutions")
    result.add_argument("--logs-commit-message", default="speedrun: archive run evidence and setup")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return execute(args)
    except FinalizeError as error:
        print(f"finalization refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
