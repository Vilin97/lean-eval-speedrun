#!/usr/bin/env python3
"""Arm a detached, fail-closed post-stop LeanEval finalization run.

The scheduler has one pinned submission identity. It never invokes the
finalizer before the configured hard stop, and after the deadline it still
waits for a valid STOP marker, a successful hard-stop guardian result, the
guardian/watchdog processes to exit, all active job markers to disappear, and
the raw evidence tree to become stable.

State and logs live below the repository's private Git directory. Keeping
them outside the raw evidence tree prevents the archival pass from racing the
scheduler's own growing log. Arm is idempotent only for the same live,
identity-checked scheduler; stale state is never replaced automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
DEFAULT_CONFIG = REPO / "speedrun" / "config.json"
STATE_DIR_NAME = "lean-eval-speedrun-poststop"
STATE_NAME = "state.json"
RESULT_NAME = "result.json"
STDOUT_NAME = "scheduler.stdout.log"
STDERR_NAME = "scheduler.stderr.log"
STATE_SCHEMA = "lean-eval-poststop-finalizer-state-v1"
RESULT_SCHEMA = "lean-eval-poststop-finalizer-result-v1"
GUARD_STATE_SCHEMA = "lean-eval-hardstop-guard-state-v1"
GUARD_RESULT_SCHEMA = "lean-eval-hardstop-guard-result-v1"
STOP_SCHEMA = "lean-eval-stop-v1"
NS = 1_000_000_000
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

REMOTE = "origin"
PUSH_BRANCH = "main"
SOURCE_REPO = "Vilin97/lean-eval-speedrun"
ISSUE_REPO = "leanprover/lean-eval-submissions"
ISSUE_TITLE = "[submission] Vasily-24-hour-gpt-5.6-speedrun"
MODEL = "Vasily-24-hour-gpt-5.6-speedrun"
PUBLICATION_STATUS = "public"
PUBLICATION_DATE = "2026-08-17"

MAX_ATTEMPTS = 8
RETRY_DELAYS = (15, 30, 60, 120, 240, 300, 300)
QUIESCENCE_TIMEOUT_SECONDS = 900
STABILITY_SECONDS = 5.0
POLL_SECONDS = 1.0

CRITICAL_FILES = (
    "scripts/archive_speedrun_logs.py",
    "scripts/finalize_speedrun.py",
    "scripts/graph_speedrun.py",
    "scripts/hardstop_guard.py",
    "scripts/poststop_finalize.py",
    "scripts/speedrun.py",
    "speedrun/config.json",
    "speedrun/pricing.v1.json",
    "speedrun/schema.v1.json",
)


class SchedulerError(RuntimeError):
    """A scheduling, evidence, or identity precondition failed."""


@dataclass(frozen=True)
class SchedulerConfig:
    path: Path
    raw_root: Path
    hard_stop_ns: int
    hard_stop_text: str
    sha256: str


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    pgrp: int
    session: int
    state: str
    start_ticks: int
    uid: int
    cwd: str
    cmdline: tuple[str, ...]


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise SchedulerError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise SchedulerError(f"JSON evidence is missing or unsafe: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SchedulerError(f"cannot read valid JSON from {path}: {error}") from error


def write_all(descriptor: int, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        written = os.write(descriptor, value[offset:])
        if written <= 0:
            raise SchedulerError("short write while persisting scheduler state")
        offset += written


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise SchedulerError(f"scheduler state directory is unsafe: {path}")
    os.chmod(path, 0o700)


def open_private_append(path: Path) -> int:
    if path.is_symlink():
        raise SchedulerError(f"scheduler log path is an unsafe symlink: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    return descriptor


def create_json_exclusive(path: Path, value: Any) -> bool:
    private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        write_all(descriptor, canonical_bytes(value))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        fsync_directory(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def replace_json(path: Path, value: Any) -> None:
    private_directory(path.parent)
    if path.is_symlink():
        raise SchedulerError(f"refusing to replace symlink state: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        write_all(descriptor, canonical_bytes(value))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def parse_iso_ns(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SchedulerError(f"invalid ISO timestamp {value!r}") from error
    if parsed.tzinfo is None:
        raise SchedulerError(f"timestamp lacks timezone: {value!r}")
    delta = parsed.astimezone(timezone.utc) - EPOCH
    return ((delta.days * 86400 + delta.seconds) * NS) + delta.microseconds * 1000


def boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError as error:
        raise SchedulerError(f"cannot read kernel boot identity: {error}") from error
    if not value:
        raise SchedulerError("kernel boot identity is empty")
    return value


def iso_from_ns(value: int) -> str:
    seconds, nanoseconds = divmod(value, NS)
    parsed = EPOCH + timedelta(seconds=seconds, microseconds=nanoseconds // 1000)
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load_config(path: Path) -> SchedulerConfig:
    resolved = path.resolve(strict=True)
    value = read_json(resolved)
    if not isinstance(value, dict) or value.get("schema") != "lean-eval-speedrun-config-v1":
        raise SchedulerError("unrecognized speedrun configuration schema")
    if Path(str(value.get("default_cwd"))).resolve() != REPO.resolve():
        raise SchedulerError("speedrun default_cwd is not this repository")
    raw_value = value.get("raw_root")
    hard_stop = value.get("hard_stop")
    if not isinstance(raw_value, str) or not Path(raw_value).is_absolute():
        raise SchedulerError("speedrun raw_root must be absolute")
    if not isinstance(hard_stop, str):
        raise SchedulerError("configuration has no hard_stop")
    raw_root = Path(raw_value)
    if raw_root.is_symlink():
        raise SchedulerError("speedrun raw_root may not be a symlink")
    hard_stop_ns = parse_iso_ns(hard_stop)
    if iso_from_ns(hard_stop_ns)[:10] != PUBLICATION_DATE:
        raise SchedulerError(
            "pinned public publication date is not the configured hard-stop UTC date"
        )
    return SchedulerConfig(
        path=resolved,
        raw_root=raw_root.resolve(),
        hard_stop_ns=hard_stop_ns,
        hard_stop_text=hard_stop,
        sha256=hash_file(resolved),
    )


def git_output(*arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=REPO,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise SchedulerError(f"git command could not start: {error}") from error
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout).strip()
        raise SchedulerError(f"git {' '.join(arguments)} failed: {details}")
    return completed.stdout.strip()


def state_directory() -> Path:
    git_directory = Path(git_output("rev-parse", "--absolute-git-dir")).resolve()
    raw = Path(git_output("rev-parse", "--git-path", STATE_DIR_NAME))
    result = (raw if raw.is_absolute() else REPO / raw).resolve()
    if result != git_directory and git_directory not in result.parents:
        raise SchedulerError(f"scheduler state escaped the Git directory: {result}")
    return result


def state_paths() -> tuple[Path, Path, Path, Path]:
    root = state_directory()
    return (
        root / STATE_NAME,
        root / RESULT_NAME,
        root / STDOUT_NAME,
        root / STDERR_NAME,
    )


def critical_hashes() -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in CRITICAL_FILES:
        path = REPO / relative
        if path.is_symlink() or not path.is_file():
            raise SchedulerError(f"critical finalization file is missing/unsafe: {relative}")
        result[relative] = hash_file(path)
    return result


def finalizer_argv() -> list[str]:
    return [
        sys.executable,
        str(REPO / "scripts" / "finalize_speedrun.py"),
        "--execute",
        "--remote",
        REMOTE,
        "--push-branch",
        PUSH_BRANCH,
        "--source-repo",
        SOURCE_REPO,
        "--issue-repo",
        ISSUE_REPO,
        "--issue-title",
        ISSUE_TITLE,
        "--model",
        MODEL,
        "--publication-status",
        PUBLICATION_STATUS,
        "--publication-date",
        PUBLICATION_DATE,
    ]


def parse_proc_stat(raw: str) -> tuple[int, int, int, int, str, int]:
    close = raw.rfind(")")
    open_paren = raw.find("(")
    if open_paren <= 0 or close <= open_paren or close + 2 > len(raw):
        raise ValueError("malformed /proc stat record")
    pid = int(raw[:open_paren].strip())
    fields = raw[close + 2 :].split()
    if len(fields) < 20:
        raise ValueError("short /proc stat record")
    return pid, int(fields[1]), int(fields[2]), int(fields[3]), fields[0], int(fields[19])


def read_process(pid: int) -> ProcessInfo | None:
    if pid <= 1:
        return None
    directory = Path("/proc") / str(pid)
    try:
        parsed_pid, ppid, pgrp, session, state, start_ticks = parse_proc_stat(
            (directory / "stat").read_text(encoding="ascii")
        )
        if parsed_pid != pid:
            return None
        cmdline = tuple(
            part.decode("utf-8", errors="surrogateescape")
            for part in (directory / "cmdline").read_bytes().split(b"\0")
            if part
        )
        uid = directory.stat().st_uid
        cwd = os.path.realpath(os.readlink(directory / "cwd"))
    except (OSError, ValueError):
        return None
    return ProcessInfo(pid, ppid, pgrp, session, state, start_ticks, uid, cwd, cmdline)


def script_invocation_matches(
    info: ProcessInfo,
    script: Path,
    command: str,
    expected_tail: list[str],
) -> bool:
    if info.uid != os.getuid() or info.cwd != str(REPO.resolve()):
        return False
    for index, item in enumerate(info.cmdline):
        candidate = Path(item)
        if not candidate.is_absolute():
            candidate = Path(info.cwd) / candidate
        try:
            if candidate.resolve() != script.resolve():
                continue
        except OSError:
            continue
        return list(info.cmdline[index + 1 :]) == [command, *expected_tail]
    return False


def scheduler_identity(state: dict[str, Any], config: SchedulerConfig) -> ProcessInfo | None:
    pid = state.get("pid")
    start_ticks = state.get("start_ticks")
    token = state.get("arm_token")
    if (
        not isinstance(pid, int)
        or not isinstance(start_ticks, int)
        or not isinstance(token, str)
        or state.get("boot_id") != boot_id()
    ):
        return None
    info = read_process(pid)
    if info is None or info.start_ticks != start_ticks:
        return None
    tail = ["--config", str(config.path), "--arm-token", token]
    return info if script_invocation_matches(info, SCRIPT, "_run", tail) else None


def process_matches_guard_state(value: dict[str, Any]) -> bool:
    pid = value.get("pid")
    start_ticks = value.get("start_ticks")
    token = value.get("arm_token")
    config_path = value.get("config")
    if (
        not isinstance(pid, int)
        or not isinstance(start_ticks, int)
        or not isinstance(token, str)
        or not isinstance(config_path, str)
    ):
        return False
    info = read_process(pid)
    if info is None or info.start_ticks != start_ticks:
        return False
    tail = ["--config", config_path, "--arm-token", token]
    return script_invocation_matches(
        info, REPO / "scripts" / "hardstop_guard.py", "_run", tail
    )


def process_is_watchdog(pid: int) -> bool:
    info = read_process(pid)
    if info is None or info.uid != os.getuid() or info.cwd != str(REPO.resolve()):
        return False
    invocation = list(info.cmdline)
    try:
        script_index = next(
            index
            for index, item in enumerate(invocation)
            if Path(item).resolve() == (REPO / "scripts" / "speedrun.py").resolve()
        )
    except (StopIteration, OSError):
        return False
    return invocation[script_index + 1 :] == ["_watchdog"]


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


def validate_stop(config: SchedulerConfig) -> dict[str, Any] | None:
    path = config.raw_root / "STOP"
    if not path.exists():
        return None
    value = read_json(path)
    if (
        not isinstance(value, dict)
        or value.get("schema") != STOP_SCHEMA
        or not isinstance(value.get("at"), str)
        or not isinstance(value.get("reason"), str)
    ):
        raise SchedulerError("STOP evidence is malformed")
    parse_iso_ns(value["at"])
    return value


def guardian_status(config: SchedulerConfig) -> tuple[str, list[str]]:
    state_path = config.raw_root / "hardstop-guard.json"
    result_path = config.raw_root / "hardstop-guard-result.json"
    if not state_path.exists():
        raise SchedulerError("hard-stop guardian state is missing")
    state = read_json(state_path)
    if not isinstance(state, dict) or state.get("schema") != GUARD_STATE_SCHEMA:
        raise SchedulerError("hard-stop guardian state schema is invalid")
    if (
        state.get("hard_stop_ns") != config.hard_stop_ns
        or state.get("hard_stop") != config.hard_stop_text
        or state.get("config_sha256") != config.sha256
        or state.get("config") != str(config.path)
        or state.get("raw_root") != str(config.raw_root)
    ):
        raise SchedulerError("guardian state differs from scheduler configuration")
    if process_matches_guard_state(state):
        return "running", []
    if not result_path.exists():
        return "exited-without-result", []
    result = read_json(result_path)
    if not isinstance(result, dict) or result.get("schema") != GUARD_RESULT_SCHEMA:
        raise SchedulerError("hard-stop guardian result schema is invalid")
    errors = result.get("errors")
    if not isinstance(errors, list) or not all(isinstance(item, str) for item in errors):
        raise SchedulerError("hard-stop guardian errors field is invalid")
    if (
        result.get("complete") is not True
        or errors
        or result.get("config_sha256") != config.sha256
        or result.get("hard_stop") != config.hard_stop_text
        or result.get("hard_stop_ns") != config.hard_stop_ns
        or result.get("guardian_pid") != state.get("pid")
        or result.get("state_sha256")
        != hashlib.sha256(canonical_bytes(state)).hexdigest()
        or not isinstance(result.get("trigger_wall_ns"), int)
        or result["trigger_wall_ns"] < config.hard_stop_ns
        or int(result.get("stable_empty_scans", 0)) < 2
    ):
        return "failed", errors
    return "complete", []


def watchdog_running(config: SchedulerConfig) -> bool:
    path = config.raw_root / "watchdog.json"
    if not path.exists():
        return False
    value = read_json(path)
    if not isinstance(value, dict) or value.get("schema") != "lean-eval-watchdog-v1":
        raise SchedulerError("watchdog state schema is invalid")
    if value.get("hard_stop") != config.hard_stop_text:
        raise SchedulerError("watchdog hard stop differs from scheduler configuration")
    pid = value.get("pid")
    if not isinstance(pid, int):
        raise SchedulerError("watchdog state has no PID")
    return process_is_watchdog(pid)


def active_markers(config: SchedulerConfig) -> list[str]:
    result: list[str] = []
    for path in sorted(config.raw_root.glob("*/active.json")):
        if path.is_symlink() or not path.is_file():
            raise SchedulerError(f"active marker is unsafe: {path}")
        result.append(path.parent.name)
    return result


def quiescence_observation(config: SchedulerConfig) -> dict[str, Any]:
    stop = validate_stop(config)
    guard_state, guard_errors = guardian_status(config)
    return {
        "active_markers": active_markers(config),
        "guardian_errors": guard_errors,
        "guardian_status": guard_state,
        "stop_present": stop is not None,
        "watchdog_running": watchdog_running(config),
    }


def wait_until_hard_stop(config: SchedulerConfig) -> None:
    while True:
        remaining = config.hard_stop_ns - time.time_ns()
        if remaining <= 0:
            return
        if remaining > NS:
            time.sleep(min((remaining - 250_000_000) / NS, 30.0))
        elif remaining > 5_000_000:
            time.sleep(max((remaining - 1_000_000) / NS, 0.0005))
        else:
            time.sleep(min(remaining / NS, 0.001))


def wait_for_quiescence(config: SchedulerConfig) -> dict[str, Any]:
    if time.time_ns() < config.hard_stop_ns:
        raise SchedulerError("internal refusal: quiescence wait started before hard stop")
    deadline = time.monotonic() + QUIESCENCE_TIMEOUT_SECONDS
    stable_since: float | None = None
    prior_snapshot: tuple[tuple[str, int, int], ...] | None = None
    last_observation: dict[str, Any] = {}
    while time.monotonic() < deadline:
        if time.time_ns() < config.hard_stop_ns:
            raise SchedulerError("wall clock moved before hard stop; refusing finalization")
        observation = quiescence_observation(config)
        last_observation = observation
        guard_state = observation["guardian_status"]
        if guard_state in {"failed", "exited-without-result"}:
            raise SchedulerError(
                f"hard-stop guardian did not complete safely: {guard_state}; "
                f"errors={observation['guardian_errors']}"
            )
        ready = (
            observation["stop_present"]
            and guard_state == "complete"
            and not observation["watchdog_running"]
            and not observation["active_markers"]
        )
        snapshot = raw_snapshot(config.raw_root)
        if ready and snapshot == prior_snapshot:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= STABILITY_SECONDS:
                return observation
        else:
            stable_since = None
        prior_snapshot = snapshot
        time.sleep(POLL_SECONDS)
    raise SchedulerError(
        "post-stop evidence did not quiesce within "
        f"{QUIESCENCE_TIMEOUT_SECONDS}s: {last_observation}"
    )


def pinned_state_matches(state: dict[str, Any], config: SchedulerConfig) -> bool:
    return (
        state.get("schema") == STATE_SCHEMA
        and state.get("config_sha256") == config.sha256
        and state.get("hard_stop_ns") == config.hard_stop_ns
        and state.get("finalizer_argv") == finalizer_argv()
        and state.get("critical_hashes") == critical_hashes()
    )


def persist_phase(
    state_path: Path,
    state: dict[str, Any],
    phase: str,
    **fields: Any,
) -> None:
    state["phase"] = phase
    state["updated_at"] = iso_from_ns(time.time_ns())
    state.update(fields)
    replace_json(state_path, state)


def publish_result(result_path: Path, value: dict[str, Any]) -> None:
    if create_json_exclusive(result_path, value):
        return
    existing = read_json(result_path)
    if existing != value:
        raise SchedulerError(f"conflicting scheduler result already exists: {result_path}")


def execute_finalizer(
    state_path: Path,
    result_path: Path,
    state: dict[str, Any],
    config: SchedulerConfig,
) -> int:
    attempts = state.get("attempts")
    if not isinstance(attempts, list):
        attempts = []
        state["attempts"] = attempts
    for attempt_number in range(1, MAX_ATTEMPTS + 1):
        if time.time_ns() < config.hard_stop_ns:
            raise SchedulerError("finalizer invocation refused before hard stop")
        if (
            hash_file(config.path) != config.sha256
            or critical_hashes() != state["critical_hashes"]
        ):
            raise SchedulerError("reviewed configuration/finalization code changed after arm")
        observation = wait_for_quiescence(config)
        if time.time_ns() < config.hard_stop_ns:
            raise SchedulerError("wall clock moved before hard stop; finalizer was not invoked")
        started_ns = time.time_ns()
        attempt = {
            "attempt": attempt_number,
            "argv_sha256": hashlib.sha256(
                b"\0".join(item.encode("utf-8") for item in finalizer_argv())
            ).hexdigest(),
            "quiescence": observation,
            "started_at": iso_from_ns(started_ns),
            "started_wall_ns": started_ns,
        }
        attempts.append(attempt)
        persist_phase(
            state_path,
            state,
            "finalizer-running",
            current_attempt=attempt_number,
        )
        print(
            json.dumps(
                {
                    "attempt": attempt_number,
                    "event": "starting-pinned-finalizer",
                    "started_at": attempt["started_at"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
        try:
            completed = subprocess.run(
                finalizer_argv(),
                cwd=REPO,
                stdin=subprocess.DEVNULL,
                check=False,
            )
            exit_code = int(completed.returncode)
        except OSError as error:
            exit_code = 127
            attempt["spawn_error"] = str(error)
        finished_ns = time.time_ns()
        attempt["exit_code"] = exit_code
        attempt["finished_at"] = iso_from_ns(finished_ns)
        attempt["finished_wall_ns"] = finished_ns
        persist_phase(
            state_path,
            state,
            "attempt-finished",
            current_attempt=attempt_number,
            last_exit_code=exit_code,
        )
        if exit_code == 0:
            result = {
                "attempts": attempts,
                "complete": True,
                "completed_at": iso_from_ns(finished_ns),
                "finalizer_argv": finalizer_argv(),
                "hard_stop": config.hard_stop_text,
                "schema": RESULT_SCHEMA,
            }
            publish_result(result_path, result)
            persist_phase(
                state_path,
                state,
                "complete",
                completed_at=result["completed_at"],
            )
            return 0
        if attempt_number < MAX_ATTEMPTS:
            delay = RETRY_DELAYS[attempt_number - 1]
            persist_phase(
                state_path,
                state,
                "retry-wait",
                next_attempt=attempt_number + 1,
                retry_delay_seconds=delay,
            )
            time.sleep(delay)

    failed_at = iso_from_ns(time.time_ns())
    result = {
        "attempts": attempts,
        "complete": False,
        "failed_at": failed_at,
        "finalizer_argv": finalizer_argv(),
        "hard_stop": config.hard_stop_text,
        "schema": RESULT_SCHEMA,
    }
    publish_result(result_path, result)
    persist_phase(state_path, state, "failed", failed_at=failed_at)
    return 2


def command_run(args: argparse.Namespace) -> int:
    try:
        handshake = os.read(0, 1)
    except OSError as error:
        raise SchedulerError(f"scheduler arm handshake failed: {error}") from error
    if handshake != b"1":
        raise SchedulerError("scheduler arm handshake was not completed")
    config = load_config(Path(args.config))
    state_path, result_path, _, _ = state_paths()
    state = read_json(state_path)
    if (
        not isinstance(state, dict)
        or state.get("arm_token") != args.arm_token
        or state.get("pid") != os.getpid()
        or state.get("boot_id") != boot_id()
        or not pinned_state_matches(state, config)
    ):
        raise SchedulerError("scheduler state/identity handshake failed")
    own = read_process(os.getpid())
    if own is None or own.start_ticks != state.get("start_ticks"):
        raise SchedulerError("scheduler /proc start identity differs from armed state")
    persist_phase(state_path, state, "waiting-hard-stop")
    wait_until_hard_stop(config)
    if time.time_ns() < config.hard_stop_ns:
        raise SchedulerError("scheduler woke before hard stop")
    persist_phase(state_path, state, "waiting-quiescence")
    try:
        return execute_finalizer(state_path, result_path, state, config)
    except Exception as error:
        failed_at = iso_from_ns(time.time_ns())
        result = {
            "complete": False,
            "error": f"{type(error).__name__}: {error}",
            "failed_at": failed_at,
            "finalizer_argv": finalizer_argv(),
            "hard_stop": config.hard_stop_text,
            "schema": RESULT_SCHEMA,
        }
        publish_result(result_path, result)
        persist_phase(
            state_path,
            state,
            "failed",
            error=result["error"],
            failed_at=failed_at,
        )
        return 2


def redacted_state(state: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in state.items() if key != "arm_token"}


def command_arm(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    state_path, result_path, stdout_path, stderr_path = state_paths()
    private_directory(state_path.parent)
    if state_path.is_symlink() or result_path.is_symlink():
        raise SchedulerError("scheduler state/result paths may not be symlinks")
    if result_path.exists():
        raise SchedulerError(f"scheduler result already exists: {result_path}")
    if state_path.exists():
        existing = read_json(state_path)
        if (
            isinstance(existing, dict)
            and pinned_state_matches(existing, config)
            and scheduler_identity(existing, config) is not None
        ):
            print(json.dumps(redacted_state(existing), indent=2, sort_keys=True))
            return 0
        raise SchedulerError(f"stale or invalid scheduler state exists: {state_path}")

    token = secrets.token_hex(24)
    read_fd, write_fd = os.pipe()
    stdout_fd = open_private_append(stdout_path)
    stderr_fd = open_private_append(stderr_path)
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT),
                "_run",
                "--config",
                str(config.path),
                "--arm-token",
                token,
            ],
            cwd=REPO,
            stdin=read_fd,
            stdout=stdout_fd,
            stderr=stderr_fd,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        os.close(stdout_fd)
        os.close(stderr_fd)
        os.close(read_fd)

    state: dict[str, Any] | None = None
    published = False
    child: ProcessInfo | None = None
    try:
        for _ in range(100):
            child = read_process(process.pid)
            if child is not None:
                break
            time.sleep(0.01)
        if child is None:
            raise SchedulerError("scheduler child exited before identity capture")
        state = {
            "arm_token": token,
            "armed_at": iso_from_ns(time.time_ns()),
            "attempts": [],
            "boot_id": boot_id(),
            "config": str(config.path),
            "config_sha256": config.sha256,
            "critical_hashes": critical_hashes(),
            "finalizer_argv": finalizer_argv(),
            "hard_stop": config.hard_stop_text,
            "hard_stop_ns": config.hard_stop_ns,
            "phase": "armed",
            "pid": process.pid,
            "schema": STATE_SCHEMA,
            "start_ticks": child.start_ticks,
        }
        if scheduler_identity(state, config) is None:
            raise SchedulerError("new scheduler failed command/cwd/start identity validation")
        if not create_json_exclusive(state_path, state):
            raise SchedulerError("another scheduler won the arm race")
        published = True
        os.write(write_fd, b"1")
    except Exception:
        try:
            current = read_process(process.pid)
            if current is not None and (child is None or child.start_ticks == current.start_ticks):
                process.kill()
        except (OSError, ProcessLookupError):
            pass
        if published and state is not None:
            try:
                if read_json(state_path) == state:
                    state_path.unlink()
                    fsync_directory(state_path.parent)
            except (OSError, SchedulerError):
                pass
        raise
    finally:
        os.close(write_fd)
    print(json.dumps(redacted_state(state), indent=2, sort_keys=True))
    return 0


def command_status(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    state_path, result_path, stdout_path, stderr_path = state_paths()
    if state_path.is_symlink() or result_path.is_symlink():
        raise SchedulerError("scheduler state/result paths may not be symlinks")
    state = read_json(state_path) if state_path.exists() else None
    result = read_json(result_path) if result_path.exists() else None
    identity_valid = (
        isinstance(state, dict)
        and pinned_state_matches(state, config)
        and scheduler_identity(state, config) is not None
    )
    try:
        observation = quiescence_observation(config)
    except SchedulerError as error:
        observation = {"error": str(error)}
    now = time.time_ns()
    value = {
        "attempt_count": len(state.get("attempts", [])) if isinstance(state, dict) else 0,
        "critical_files_unchanged": (
            isinstance(state, dict) and state.get("critical_hashes") == critical_hashes()
        ),
        "finalizer_argv": finalizer_argv(),
        "guardian_watchdog_observation": observation,
        "hard_stop": config.hard_stop_text,
        "logs": {"stderr": str(stderr_path), "stdout": str(stdout_path)},
        "now": iso_from_ns(now),
        "phase": state.get("phase") if isinstance(state, dict) else None,
        "result_complete": result.get("complete") if isinstance(result, dict) else None,
        "result_exists": result is not None,
        "scheduler_identity_valid": identity_valid,
        "scheduler_pid": state.get("pid") if isinstance(state, dict) else None,
        "seconds_to_hard_stop": max(0.0, (config.hard_stop_ns - now) / NS),
        "state_exists": state is not None,
        "state_path": str(state_path),
    }
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


def command_self_test(_: argparse.Namespace) -> int:
    timestamp = "2026-08-17T08:09:44.640Z"
    if iso_from_ns(parse_iso_ns(timestamp)) != timestamp:
        raise SchedulerError("timestamp fixture failed")
    sample = "123 (name with ) paren) S 4 5 6 " + "0 " * 15 + "99 0 0"
    if parse_proc_stat(sample) != (123, 4, 5, 6, "S", 99):
        raise SchedulerError("/proc stat fixture failed")
    expected = [
        sys.executable,
        str(REPO / "scripts" / "finalize_speedrun.py"),
        "--execute",
        "--remote",
        "origin",
        "--push-branch",
        "main",
        "--source-repo",
        "Vilin97/lean-eval-speedrun",
        "--issue-repo",
        "leanprover/lean-eval-submissions",
        "--issue-title",
        "[submission] Vasily-24-hour-gpt-5.6-speedrun",
        "--model",
        "Vasily-24-hour-gpt-5.6-speedrun",
        "--publication-status",
        "public",
        "--publication-date",
        "2026-08-17",
    ]
    if finalizer_argv() != expected:
        raise SchedulerError("pinned finalizer argv fixture failed")
    own = read_process(os.getpid())
    if own is None or own.uid != os.getuid() or own.start_ticks <= 0:
        raise SchedulerError("self /proc identity fixture failed")
    if canonical_bytes({"b": 2, "a": 1}) != b'{"a":1,"b":2}\n':
        raise SchedulerError("canonical JSON fixture failed")
    required_manifests = {"speedrun/pricing.v1.json", "speedrun/schema.v1.json"}
    if not required_manifests.issubset(CRITICAL_FILES):
        raise SchedulerError("critical speedrun manifest fixture failed")
    root = state_directory()
    print(
        json.dumps(
            {
                "canonical_json": "ok",
                "critical_speedrun_manifests": "ok",
                "pinned_finalizer_argv": "ok",
                "proc_identity": "ok",
                "proc_stat_parser": "ok",
                "state_location": str(root),
                "timestamp_arithmetic": "ok",
            },
            sort_keys=True,
        )
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subcommands = result.add_subparsers(dest="subcommand", required=True)
    for name in ("arm", "status"):
        command = subcommands.add_parser(name)
        command.add_argument("--config", default=str(DEFAULT_CONFIG))
    subcommands.choices["arm"].set_defaults(handler=command_arm)
    subcommands.choices["status"].set_defaults(handler=command_status)
    subcommands.add_parser("self-test").set_defaults(handler=command_self_test)
    private = subcommands.add_parser("_run", help=argparse.SUPPRESS)
    private.add_argument("--config", required=True)
    private.add_argument("--arm-token", required=True)
    private.set_defaults(handler=command_run)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.handler(args))
    except SchedulerError as error:
        print(f"post-stop scheduler refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
