#!/usr/bin/env python3
"""Controller-owned timing, token accounting, verification, and hard-stop enforcement."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path, PurePosixPath
from typing import Any, Callable


REPO = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO / "speedrun" / "config.json"
PRICING_PATH = REPO / "speedrun" / "pricing.v1.json"
ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
FORBIDDEN_RE = re.compile(r"\b(?:sorry|admit|native_decide|set_option)\b")
NINE_PLACES = Decimal("0.000000001")
CGROUP_NAME_RE = re.compile(r"^lean-eval-[1-9][0-9]*-[0-9a-f]{24}$")
MOUNTINFO_ESCAPE_RE = re.compile(r"\\([0-7]{3})")
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class LaunchBoundaryClosed(SystemExit):
    """The STOP sentinel or configured hard stop closed the launch boundary."""


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(canonical_bytes(value))
    os.replace(temporary, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def iso_from_ns(wall_ns: int) -> str:
    return datetime.fromtimestamp(wall_ns / 1_000_000_000, tz=timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def parse_iso(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def parse_iso_ns(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp lacks timezone: {value!r}")
    delta = parsed.astimezone(timezone.utc) - UNIX_EPOCH
    return ((delta.days * 86400 + delta.seconds) * 1_000_000_000) + delta.microseconds * 1000


def config() -> dict[str, Any]:
    return read_json(CONFIG_PATH)


def raw_root() -> Path:
    return Path(config()["raw_root"])


def stop_path() -> Path:
    return raw_root() / "STOP"


def hard_stop_epoch() -> float:
    return parse_iso(config()["hard_stop"])


def hard_stop_wall_ns() -> int:
    return parse_iso_ns(config()["hard_stop"])


def race_start_epoch() -> float:
    return parse_iso(config()["race_start"])


def assert_identifier(label: str, value: str) -> None:
    if not ID_RE.fullmatch(value):
        raise SystemExit(f"invalid {label}: {value!r}")


def assert_launch_allowed(problem: str, setup_test: bool) -> None:
    now = time.time()
    if stop_path().exists():
        raise SystemExit(f"speedrun stopped: {stop_path()}")
    if now >= hard_stop_epoch():
        raise SystemExit("hard stop has passed")
    if now < race_start_epoch() and not (setup_test and problem.startswith("_toy")):
        raise SystemExit("race has not started; only an explicitly marked _toy setup test is allowed")


def launch_boundary_reason(stop_file: Path | str | None, hard_stop_ns: int) -> str | None:
    """Return why no untrusted exec may begin, using only captured launch inputs."""
    if stop_file is not None:
        try:
            os.lstat(stop_file)
        except FileNotFoundError:
            pass
        except OSError as error:
            return f"cannot safely inspect STOP sentinel {stop_file}: {error}"
        else:
            return f"speedrun stopped: {stop_file}"
    if time.time_ns() >= hard_stop_ns:
        return "hard stop has passed"
    return None


def assert_launch_boundary_open(stop_file: Path, hard_stop_ns: int) -> None:
    reason = launch_boundary_reason(stop_file, hard_stop_ns)
    if reason is not None:
        raise LaunchBoundaryClosed(reason)


def exec_gate_argv(
    release_fd: int,
    stop_file: Path,
    hard_stop_ns: int,
    target_argv: list[str],
) -> list[str]:
    if not 2 < release_fd <= 1_000_000 or not target_argv:
        raise ValueError("trusted exec gate requires a private release fd and target argv")
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "_exec_gate",
        "--release-fd",
        str(release_fd),
        "--stop-file",
        str(stop_file),
        "--hard-stop-ns",
        str(hard_stop_ns),
        "--",
        *target_argv,
    ]


def close_exec_gate(descriptor: int) -> None:
    if descriptor < 0:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def release_exec_gate(descriptor: int) -> None:
    try:
        if os.write(descriptor, b"1") != 1:
            raise RuntimeError("short write while releasing trusted exec gate")
    finally:
        os.close(descriptor)


def command_exec_gate(args: argparse.Namespace) -> int:
    """Wait for controller evidence, recheck the boundary, then exec the target."""
    target_argv = list(args.target_argv)
    if target_argv and target_argv[0] == "--":
        target_argv = target_argv[1:]
    if args.release_fd <= 2 or args.hard_stop_ns <= 0 or not target_argv:
        return 125
    stop_file = Path(args.stop_file)
    if not stop_file.is_absolute():
        return 125
    executable = shutil.which(target_argv[0])
    if executable is None:
        return 127
    try:
        executable_path = Path(executable).resolve(strict=True)
    except OSError:
        return 127
    if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
        return 127
    try:
        release = os.read(args.release_fd, 2)
    except OSError:
        return 125
    finally:
        close_exec_gate(args.release_fd)
    if release != b"1":
        return 125
    if launch_boundary_reason(stop_file, args.hard_stop_ns) is not None:
        return 125
    try:
        os.execve(executable_path, target_argv, os.environ)
    except OSError as error:
        print(f"trusted exec gate could not exec target: {error}", file=sys.stderr)
        return 127
    raise AssertionError("os.execvpe unexpectedly returned")


def empty_usage() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }


def normalize_usage(raw: dict[str, Any] | None) -> dict[str, int]:
    raw = raw or {}
    aliases = {
        "input_tokens": ("input_tokens",),
        "cached_input_tokens": ("cached_input_tokens",),
        "cache_write_input_tokens": ("cache_write_input_tokens",),
        "output_tokens": ("output_tokens",),
        "reasoning_output_tokens": ("reasoning_output_tokens",),
    }
    result: dict[str, int] = {}
    for target, names in aliases.items():
        value = 0
        for name in names:
            if name in raw:
                value = int(raw[name])
                break
        if value < 0:
            raise ValueError(f"negative token counter {target}")
        result[target] = value
    if result["cached_input_tokens"] + result["cache_write_input_tokens"] > result["input_tokens"]:
        raise ValueError("cached plus cache-write input exceeds total input")
    if result["reasoning_output_tokens"] > result["output_tokens"]:
        raise ValueError("reasoning output exceeds total output")
    return result


def sum_usage(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Return the componentwise sum of normalized token-usage records."""
    total = empty_usage()
    for row in rows:
        for key, value in normalize_usage(row).items():
            total[key] += value
    return normalize_usage(total)


def price_usage(model: str, usage: dict[str, int], context_class: str = "short") -> Decimal:
    pricing = read_json(PRICING_PATH)
    try:
        rates = pricing["models"][model][context_class]
    except KeyError as error:
        raise ValueError(f"no pinned pricing for {model}/{context_class}") from error
    uncached = usage["input_tokens"] - usage["cached_input_tokens"] - usage["cache_write_input_tokens"]
    total = (
        Decimal(uncached) * Decimal(rates["input"])
        + Decimal(usage["cached_input_tokens"]) * Decimal(rates["cached_input"])
        + Decimal(usage["cache_write_input_tokens"]) * Decimal(rates["cache_write_input"])
        + Decimal(usage["output_tokens"]) * Decimal(rates["output"])
    ) / Decimal(1_000_000)
    return total


def decimal_string(value: Decimal) -> str:
    return format(value.quantize(NINE_PLACES, rounding=ROUND_HALF_UP), "f")


def parse_codex_jsonl(path: Path) -> tuple[list[str], dict[str, int], bool, int]:
    thread_ids: list[str] = []
    terminal_usage = empty_usage()
    completed = False
    parsed_events = 0
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            parsed_events += 1
            event_type = event.get("type")
            if event_type == "thread.started" and isinstance(event.get("thread_id"), str):
                thread_ids.append(event["thread_id"])
            if event_type == "turn.completed":
                completed = True
                one_turn = normalize_usage(event.get("usage"))
                for key, value in one_turn.items():
                    terminal_usage[key] += value
    return sorted(set(thread_ids)), terminal_usage, completed, parsed_events


def interrupted_agent_result(result: dict[str, Any]) -> bool:
    """Whether the model turn ended by containment before emitting terminal usage.

    Independent verification may subsequently replace the controller status with
    ``verified`` or ``verification_failed``.  The immutable interruption facts
    remain ``turn_completed = false`` and a nonzero agent exit code.
    """
    exit_code = result.get("exit_code")
    return (
        result.get("status") in {"incomplete", "verified", "verification_failed"}
        and result.get("turn_completed") is False
        and isinstance(exit_code, int)
        and exit_code != 0
    )


def accounted_usage(
    result: dict[str, Any], rollout_usage: dict[str, Any]
) -> tuple[dict[str, int], str]:
    """Choose billable usage without pretending an interrupted turn completed.

    A normal ``codex exec --json`` run ends with ``turn.completed`` and its
    terminal aggregate must exactly match the persisted rollout.  SIGTERM and
    timeout paths have no terminal event, but the rollout still contains
    monotone cumulative token counters.  In that one narrowly identified case
    those counters are the authoritative accounting source.
    """
    terminal = normalize_usage(result.get("terminal_usage"))
    persisted = normalize_usage(rollout_usage)
    if terminal == persisted:
        return terminal, "terminal-jsonl"
    if interrupted_agent_result(result):
        return persisted, "persisted-rollout-cumulative-after-interruption"
    raise ValueError("terminal JSONL usage differs from persisted rollout usage")


def accounted_rollout_usage(
    result: dict[str, Any], rollout_usages: list[dict[str, Any]]
) -> tuple[dict[str, int], str]:
    """Reconcile controller usage with one or more persisted rollout sessions.

    A single rollout retains the interrupted-turn recovery policy in
    ``accounted_usage``.  Multiple rollouts arise only from sequential account
    failover, so every persisted session must be represented in the
    controller's terminal aggregate.  Require exact componentwise equality;
    there is no multi-rollout interrupted-turn fallback.
    """
    if not rollout_usages:
        raise ValueError("expected at least one persisted rollout")
    if len(rollout_usages) == 1:
        return accounted_usage(result, rollout_usages[0])
    terminal = normalize_usage(result.get("terminal_usage"))
    persisted = sum_usage(rollout_usages)
    if terminal != persisted:
        raise ValueError(
            "terminal JSONL usage differs from componentwise sum of persisted rollout usage"
        )
    return terminal, "terminal-jsonl-componentwise-sum-of-persisted-rollouts"


def decode_mountinfo_path(value: str) -> str:
    return MOUNTINFO_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), value)


def unified_cgroup_path(pid: int | str = "self") -> PurePosixPath:
    try:
        lines = (Path("/proc") / str(pid) / "cgroup").read_text(encoding="ascii").splitlines()
    except OSError as error:
        raise RuntimeError(f"cannot read unified cgroup membership for PID {pid}") from error
    matches: list[str] = []
    for line in lines:
        hierarchy, separator, remainder = line.partition(":")
        controllers, second_separator, path = remainder.partition(":")
        if separator and second_separator and hierarchy == "0" and controllers == "":
            matches.append(path)
    if len(matches) != 1 or not matches[0].startswith("/"):
        raise RuntimeError(f"PID {pid} does not have exactly one unified cgroup-v2 membership")
    result = PurePosixPath(matches[0])
    if any(part in ("", ".", "..") for part in result.parts[1:]):
        raise RuntimeError(f"PID {pid} has an unsafe unified cgroup path")
    return result


def current_cgroup2_directory() -> tuple[Path, PurePosixPath]:
    """Return the controller's current cgroup-v2 directory and procfs path."""
    proc_path = unified_cgroup_path()
    candidates: list[Path] = []
    try:
        mount_lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RuntimeError("cannot inspect cgroup-v2 mounts") from error
    for line in mount_lines:
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if separator + 1 >= len(fields) or fields[separator + 1] != "cgroup2" or len(fields) < 6:
            continue
        mount_root = PurePosixPath(decode_mountinfo_path(fields[3]))
        mount_point = Path(decode_mountinfo_path(fields[4]))
        relative: PurePosixPath | None = None
        if proc_path == PurePosixPath("/"):
            relative = PurePosixPath(".")
        elif mount_root == PurePosixPath("/"):
            relative = proc_path.relative_to("/")
        else:
            try:
                relative = proc_path.relative_to(mount_root)
            except ValueError:
                continue
        candidate = mount_point / Path(*relative.parts)
        try:
            resolved = candidate.resolve(strict=True)
            required = ("cgroup.procs", "cgroup.events", "cgroup.kill")
            if not all((resolved / name).exists() for name in required):
                continue
            members = {int(row) for row in (resolved / "cgroup.procs").read_text().split()}
            if os.getpid() not in members:
                continue
        except (OSError, ValueError):
            continue
        candidates.append(resolved)
    if not candidates:
        raise RuntimeError("the controller's delegated cgroup-v2 directory is unavailable")
    candidates = sorted(
        set(candidates),
        key=lambda path: (path != Path("/sys/fs/cgroup") and Path("/sys/fs/cgroup") not in path.parents, len(path.parts)),
    )
    return candidates[0], proc_path


class ProcessCgroup:
    """A unique cgroup-v2 subtree containing exactly one launched process tree."""

    def __init__(
        self,
        parent: Path,
        parent_proc_path: PurePosixPath,
        path: Path,
        device: int,
        inode: int,
        owner_uid: int,
    ) -> None:
        self.parent = parent
        self.parent_proc_path = parent_proc_path
        self.path = path
        self.device = device
        self.inode = inode
        self.owner_uid = owner_uid
        self.removed = False

    @classmethod
    def create(cls) -> "ProcessCgroup":
        parent, parent_proc_path = current_cgroup2_directory()
        last_error: OSError | None = None
        for _ in range(8):
            name = f"lean-eval-{os.getpid()}-{secrets.token_hex(12)}"
            path = parent / name
            try:
                path.mkdir(mode=0o755)
            except FileExistsError as error:
                last_error = error
                continue
            containment: ProcessCgroup | None = None
            try:
                stat = path.lstat()
                containment = cls(parent, parent_proc_path, path, stat.st_dev, stat.st_ino, stat.st_uid)
                containment.validate_identity()
                if containment.populated():
                    raise RuntimeError("new cgroup unexpectedly started populated")
                # Opening and writing the empty group's kill switch proves that
                # the operation required on every exceptional path is delegated.
                containment.kill()
                if containment.populated():
                    raise RuntimeError("new cgroup became populated during preflight")
                return containment
            except BaseException:
                if containment is not None:
                    try:
                        if not containment.populated():
                            containment.remove_empty()
                    except BaseException:
                        pass
                raise
        raise RuntimeError("could not allocate a unique process cgroup") from last_error

    @property
    def proc_path(self) -> PurePosixPath:
        if self.parent_proc_path == PurePosixPath("/"):
            return PurePosixPath("/") / self.path.name
        return self.parent_proc_path / self.path.name

    def validate_identity(self) -> None:
        if self.removed:
            raise RuntimeError("process cgroup was already removed")
        if self.path.parent != self.parent or not CGROUP_NAME_RE.fullmatch(self.path.name):
            raise RuntimeError("process cgroup is not a safe direct child")
        try:
            stat = self.path.lstat()
        except OSError as error:
            raise RuntimeError(f"process cgroup disappeared: {self.path}") from error
        if not self.path.is_dir() or self.path.is_symlink():
            raise RuntimeError(f"process cgroup is not a real directory: {self.path}")
        if (stat.st_dev, stat.st_ino, stat.st_uid) != (self.device, self.inode, self.owner_uid):
            raise RuntimeError(f"process cgroup identity changed: {self.path}")
        if self.owner_uid != os.getuid():
            raise RuntimeError(f"process cgroup has unexpected owner: {self.path}")
        current_parent, current_proc_path = current_cgroup2_directory()
        if current_parent != self.parent or current_proc_path != self.parent_proc_path:
            raise RuntimeError("controller moved out of the cgroup that owns the process child")

    def _open(self, name: str, flags: int) -> int:
        self.validate_identity()
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
            flags |= os.O_NOFOLLOW
        directory = os.open(self.path, directory_flags)
        try:
            metadata = os.fstat(directory)
            if (metadata.st_dev, metadata.st_ino, metadata.st_uid) != (
                self.device,
                self.inode,
                self.owner_uid,
            ):
                raise RuntimeError(f"opened process cgroup identity changed: {self.path}")
            return os.open(name, flags | os.O_CLOEXEC, dir_fd=directory)
        finally:
            os.close(directory)

    def events(self) -> dict[str, int]:
        descriptor = self._open("cgroup.events", os.O_RDONLY)
        try:
            raw = os.read(descriptor, 4096).decode("ascii")
        finally:
            os.close(descriptor)
        result: dict[str, int] = {}
        try:
            for line in raw.splitlines():
                key, value = line.split()
                result[key] = int(value)
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"malformed cgroup.events in {self.path}") from error
        if "populated" not in result:
            raise RuntimeError(f"cgroup.events lacks populated in {self.path}")
        return result

    def populated(self) -> bool:
        return self.events()["populated"] != 0

    def members(self) -> set[int]:
        descriptor = self._open("cgroup.procs", os.O_RDONLY)
        try:
            raw = os.read(descriptor, 1024 * 1024).decode("ascii")
        finally:
            os.close(descriptor)
        try:
            return {int(row) for row in raw.split()}
        except ValueError as error:
            raise RuntimeError(f"malformed cgroup.procs in {self.path}") from error

    def kill(self) -> None:
        descriptor = self._open("cgroup.kill", os.O_WRONLY)
        try:
            written = os.write(descriptor, b"1")
        finally:
            os.close(descriptor)
        if written != 1:
            raise RuntimeError(f"short write to cgroup.kill in {self.path}")

    def preexec_join(
        self,
        stop_file: Path | None = None,
        hard_stop_ns: int | None = None,
    ) -> Callable[[], None]:
        procs_path = str(self.path / "cgroup.procs")
        stop_name = os.fspath(stop_file) if stop_file is not None else None

        def join() -> None:
            descriptor = os.open(procs_path, os.O_WRONLY | os.O_CLOEXEC)
            try:
                payload = f"{os.getpid()}\n".encode("ascii")
                if os.write(descriptor, payload) != len(payload):
                    raise OSError("short write while joining process cgroup")
            finally:
                os.close(descriptor)
            if hard_stop_ns is not None:
                reason = launch_boundary_reason(
                    stop_name,
                    hard_stop_ns,
                )
                if reason is not None:
                    raise OSError(reason)

        return join

    def validate_process(self, process: subprocess.Popen[Any]) -> None:
        self.validate_identity()
        if process.poll() is not None:
            # Popen only returns after the preexec callback and exec handoff;
            # a very short command may already have exited by this point.
            return
        try:
            observed = unified_cgroup_path(process.pid)
        except RuntimeError:
            if process.poll() is not None:
                return
            raise
        if observed != self.proc_path or process.pid not in self.members():
            if process.poll() is None:
                raise RuntimeError(f"launched PID {process.pid} did not join {self.path}")

    def evidence(self) -> dict[str, Any]:
        self.validate_identity()
        return {"path": str(self.path), "inode": self.inode, "owner_uid": self.owner_uid}

    def remove_empty(self) -> None:
        self.validate_identity()
        if self.populated():
            raise RuntimeError(f"refusing to remove populated cgroup: {self.path}")
        # A program may create nested cgroups. cgroup.events on the root covers
        # their processes; after it reaches zero, remove empty descendants first.
        descendants = sorted(
            (path for path in self.path.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for descendant in descendants:
            if descendant.is_symlink():
                raise RuntimeError(f"refusing to traverse cgroup symlink: {descendant}")
            raw = (descendant / "cgroup.events").read_text(encoding="ascii")
            values = dict(line.split() for line in raw.splitlines())
            if values.get("populated") != "0":
                raise RuntimeError(f"refusing to remove populated descendant cgroup: {descendant}")
            descendant.rmdir()
        self.validate_identity()
        if self.populated():
            raise RuntimeError(f"process cgroup repopulated before removal: {self.path}")
        self.path.rmdir()
        self.removed = True


def registered_process(
    job_dir: Path,
    process: subprocess.Popen[Any],
    argv: list[str],
    containment: ProcessCgroup,
    bootstrap_argv: list[str] | None = None,
) -> None:
    containment.validate_process(process)
    evidence = {
        "schema": "lean-eval-active-process-v2",
        "pid": process.pid,
        "pgid": process.pid,
        "argv": argv,
        "cgroup": containment.evidence(),
        "registered_at": iso_from_ns(time.time_ns()),
    }
    if bootstrap_argv is not None:
        evidence["bootstrap_argv"] = bootstrap_argv
    atomic_json(
        job_dir / "active.json",
        evidence,
    )


def finish_registered_process(job_dir: Path) -> None:
    active = job_dir / "active.json"
    if active.exists():
        os.replace(active, job_dir / "process.json")


def proc_identity(pid: int) -> tuple[int, int, str, int, int] | None:
    """Return ``(ppid, pgrp, state, start_ticks, uid)`` from procfs."""
    if pid <= 1:
        return None
    directory = Path("/proc") / str(pid)
    try:
        raw = (directory / "stat").read_text(encoding="ascii")
        close = raw.rfind(")")
        open_paren = raw.find("(")
        if open_paren <= 0 or close <= open_paren or close + 2 > len(raw):
            return None
        parsed_pid = int(raw[:open_paren].strip())
        fields = raw[close + 2 :].split()
        if parsed_pid != pid or len(fields) < 20:
            return None
        return int(fields[1]), int(fields[2]), fields[0], int(fields[19]), directory.stat().st_uid
    except (OSError, ValueError):
        return None


def proc_snapshot() -> dict[int, tuple[int, int, str, int, int]]:
    result: dict[int, tuple[int, int, str, int, int]] = {}
    try:
        entries = sorted(
            (entry for entry in Path("/proc").iterdir() if entry.name.isdigit()),
            key=lambda entry: int(entry.name),
        )
    except OSError:
        return result
    for entry in entries:
        pid = int(entry.name)
        identity = proc_identity(pid)
        if identity is not None:
            result[pid] = identity
    return result


def descendant_depths(
    table: dict[int, tuple[int, int, str, int, int]], root: int
) -> dict[int, int]:
    children: dict[int, list[int]] = defaultdict(list)
    for pid, identity in table.items():
        children[identity[0]].append(pid)
    result: dict[int, int] = {}
    stack = [(root, 0)]
    while stack:
        pid, depth = stack.pop()
        if pid in result or pid not in table:
            continue
        result[pid] = depth
        for child in sorted(children.get(pid, []), reverse=True):
            stack.append((child, depth + 1))
    return result


def signal_exact_process(pid: int, start_ticks: int, sig: signal.Signals) -> str:
    if pid <= 1 or not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return "refused"
    descriptor = None
    try:
        descriptor = os.pidfd_open(pid, 0)
        current = proc_identity(pid)
        if current is None or current[2] == "Z":
            return "gone"
        if current[3] != start_ticks or current[4] != os.getuid():
            return "identity-changed"
        signal.pidfd_send_signal(descriptor, sig)
        return "sent"
    except ProcessLookupError:
        return "gone"
    except (OSError, PermissionError):
        return "error"
    finally:
        if descriptor is not None:
            os.close(descriptor)


def terminate_group(process: subprocess.Popen[Any], settle_seconds: float = 3.0) -> dict[str, Any]:
    """Stop and kill an exact procfs snapshot of a wrapper's whole process tree.

    The account dispatcher launches ``codex-real`` in a detached session, so a
    signal to the wrapper's process group is insufficient.  We first SIGSTOP
    every PID-reuse-checked descendant, rescan while parents cannot fork, and
    then SIGKILL the deepest processes first via pidfds.
    """
    targets: dict[int, tuple[int, int]] = {}
    stop_sent = 0
    kill_sent = 0
    for _ in range(3):
        table = proc_snapshot()
        depths = descendant_depths(table, process.pid)
        for pid, depth in depths.items():
            identity = table[pid]
            if identity[4] == os.getuid():
                previous = targets.get(pid)
                if previous is None or previous[0] == identity[3]:
                    targets[pid] = (identity[3], max(depth, previous[1] if previous else 0))
        for pid, (start_ticks, _) in sorted(targets.items(), key=lambda row: row[1][1], reverse=True):
            if signal_exact_process(pid, start_ticks, signal.SIGSTOP) == "sent":
                stop_sent += 1
        time.sleep(0.02)

    for pid, (start_ticks, depth) in sorted(
        targets.items(), key=lambda row: row[1][1], reverse=True
    ):
        if signal_exact_process(pid, start_ticks, signal.SIGKILL) == "sent":
            kill_sent += 1

    deadline = time.monotonic() + settle_seconds
    live: list[int] = []
    while True:
        live = []
        for pid, (start_ticks, _) in targets.items():
            current = proc_identity(pid)
            if current is not None and current[2] != "Z" and current[3] == start_ticks:
                live.append(pid)
        if not live or time.monotonic() >= deadline:
            break
        for pid in live:
            signal_exact_process(pid, targets[pid][0], signal.SIGKILL)
        time.sleep(0.02)
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    complete = not live and process.poll() is not None
    return {
        "complete": complete,
        "kill_method": "pidfd-sigstop-rescan-sigkill",
        "sigkill_sent": kill_sent,
        "sigstop_sent": stop_sent,
        "snapshot_processes": len(targets),
    }


def wait_cgroup_empty_and_wrapper_reaped(
    process: subprocess.Popen[Any],
    containment: ProcessCgroup,
    settle_seconds: float,
) -> tuple[bool, bool, bool]:
    deadline = time.monotonic() + settle_seconds
    while True:
        wrapper_reaped = process.poll() is not None
        populated = containment.populated()
        if wrapper_reaped and not populated:
            return True, wrapper_reaped, populated
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, wrapper_reaped, populated
        if not wrapper_reaped:
            try:
                process.wait(timeout=min(0.02, remaining))
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(min(0.02, remaining))


def terminate_contained_process(
    process: subprocess.Popen[Any],
    containment: ProcessCgroup,
    reason: str,
    settle_seconds: float = 5.0,
) -> dict[str, Any]:
    """Kill a complete cgroup and require both kernel and waitpid evidence."""
    started = time.monotonic()
    kill_attempts = 0
    kill_errors: list[str] = []

    def cgroup_kill() -> None:
        nonlocal kill_attempts
        kill_attempts += 1
        try:
            containment.kill()
        except (OSError, RuntimeError) as error:
            kill_errors.append(f"{type(error).__name__}: {error}")

    cgroup_kill()
    first_wait = min(0.5, settle_seconds)
    complete, wrapper_reaped, populated = wait_cgroup_empty_and_wrapper_reaped(
        process, containment, first_wait
    )
    fallback: dict[str, Any] | None = None
    if not complete:
        # This can help on older or partially broken hosts, but it never changes
        # the authoritative completion condition: populated=0 plus waitpid.
        fallback = terminate_group(process, settle_seconds=min(1.0, settle_seconds))
        cgroup_kill()
        remaining = max(0.0, settle_seconds - (time.monotonic() - started))
        complete, wrapper_reaped, populated = wait_cgroup_empty_and_wrapper_reaped(
            process, containment, remaining
        )
    return {
        "complete": complete,
        "kill_method": "cgroup.kill",
        "reason": reason,
        "cgroup_path": str(containment.path),
        "cgroup_kill_attempts": kill_attempts,
        "cgroup_kill_succeeded": kill_attempts > len(kill_errors),
        "cgroup_kill_errors": kill_errors,
        "cgroup_populated": populated,
        "wrapper_reaped": wrapper_reaped,
        "procfs_fallback": fallback,
    }


def cleanup_failed_launch(containment: ProcessCgroup) -> None:
    """Fail closed if Popen raised after its preexec callback may have run."""
    containment.kill()
    deadline = time.monotonic() + 5.0
    while containment.populated():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"failed launch left a populated cgroup: {containment.path}")
        time.sleep(0.02)
    containment.remove_empty()


def finalize_containment(
    process: subprocess.Popen[Any],
    containment: ProcessCgroup,
    termination: dict[str, Any] | None,
    reason: str,
) -> dict[str, Any] | None:
    """Enforce the empty/reaped barrier, then and only then remove the cgroup."""
    complete, _, _ = wait_cgroup_empty_and_wrapper_reaped(process, containment, 0.0)
    if not complete:
        termination = terminate_contained_process(process, containment, reason)
    if termination is not None and termination.get("complete") is not True:
        raise RuntimeError("process cgroup did not terminate completely")
    complete, wrapper_reaped, populated = wait_cgroup_empty_and_wrapper_reaped(
        process, containment, 0.0
    )
    if not complete or not wrapper_reaped or populated:
        raise RuntimeError("process cgroup is not empty with its wrapper reaped")
    return termination


def terminate_nominal_residuals(
    process: subprocess.Popen[Any], containment: ProcessCgroup
) -> tuple[bool, dict[str, Any] | None]:
    if process.poll() is None:
        raise RuntimeError("cannot evaluate residuals before the wrapper exits")
    if not containment.populated():
        return False, None
    return True, terminate_contained_process(
        process, containment, "residual-processes-after-wrapper-exit"
    )


def deadline_timeout(hard_stop_ns: int | None = None) -> float:
    deadline = hard_stop_wall_ns() if hard_stop_ns is None else hard_stop_ns
    remaining_ns = deadline - time.time_ns()
    if remaining_ns <= 0:
        raise LaunchBoundaryClosed("hard stop has passed")
    return remaining_ns / 1_000_000_000


def command_run(args: argparse.Namespace) -> int:
    assert_identifier("problem id", args.problem)
    assert_identifier("job id", args.job_id)
    if not math.isfinite(args.max_agent_seconds) or args.max_agent_seconds <= 0:
        raise SystemExit("max agent seconds must be a positive finite number")
    assert_launch_allowed(args.problem, args.setup_test)
    root = raw_root()
    boundary_stop = root / "STOP"
    boundary_hard_stop_ns = hard_stop_wall_ns()
    root.mkdir(parents=True, exist_ok=True)
    job_dir = root / args.job_id
    try:
        job_dir.mkdir()
    except FileExistsError as error:
        raise SystemExit(f"job directory already exists: {job_dir}") from error

    prompt_path = Path(args.prompt_file).resolve()
    prompt_bytes = prompt_path.read_bytes().replace(b"{{PROBLEM_ID}}", args.problem.encode())
    started_wall_ns = time.time_ns()
    started_monotonic_ns = time.monotonic_ns()
    launch = {
        "schema": "lean-eval-job-launch-v1",
        "job_id": args.job_id,
        "problem_id": args.problem,
        "requested_model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "prompt_sha256": sha256_bytes(prompt_bytes),
        "cwd": str(REPO),
        "controller_pid": os.getpid(),
        "started_at_utc": iso_from_ns(started_wall_ns),
        "started_wall_ns": started_wall_ns,
        "started_monotonic_ns": started_monotonic_ns,
        "pricing_sha256": sha256_file(PRICING_PATH),
        "setup_test": bool(args.setup_test),
        "max_agent_seconds": float(args.max_agent_seconds),
    }
    atomic_json(job_dir / "launch.json", launch)

    stdout_path = job_dir / "stdout.jsonl"
    stderr_path = job_dir / "stderr.log"
    argv = [
        "cx",
        "auto",
        "--",
        "exec",
        "--json",
        "-m",
        args.model,
        "-c",
        f'model_reasoning_effort="{args.reasoning_effort}"',
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        str(REPO),
        "-",
    ]
    exit_code = 125
    timed_out_at_hard_stop = False
    timed_out_at_job_limit = False
    termination: dict[str, Any] | None = None
    residual_processes_after_exit = False
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        containment = ProcessCgroup.create()
        process: subprocess.Popen[Any] | None = None
        gate_read_fd = -1
        gate_release_fd = -1
        try:
            try:
                gate_read_fd, gate_release_fd = os.pipe()
                bootstrap_argv = exec_gate_argv(
                    gate_read_fd,
                    boundary_stop,
                    boundary_hard_stop_ns,
                    argv,
                )
                assert_launch_boundary_open(boundary_stop, boundary_hard_stop_ns)
                try:
                    process = subprocess.Popen(
                        bootstrap_argv,
                        cwd=REPO,
                        stdin=subprocess.PIPE,
                        stdout=stdout,
                        stderr=stderr,
                        start_new_session=True,
                        close_fds=True,
                        pass_fds=(gate_read_fd,),
                        preexec_fn=containment.preexec_join(
                            boundary_stop,
                            boundary_hard_stop_ns,
                        ),
                    )
                finally:
                    close_exec_gate(gate_read_fd)
                    gate_read_fd = -1
            except BaseException:
                cleanup_failed_launch(containment)
                raise
            if process.poll() is not None:
                raise RuntimeError("trusted exec gate exited before active evidence was published")
            registered_process(
                job_dir,
                process,
                argv,
                containment,
                bootstrap_argv,
            )
            try:
                assert_launch_boundary_open(boundary_stop, boundary_hard_stop_ns)
                release_descriptor = gate_release_fd
                gate_release_fd = -1
                release_exec_gate(release_descriptor)
                hard_stop_seconds = deadline_timeout(boundary_hard_stop_ns)
                job_seconds = max(
                    0.001,
                    float(args.max_agent_seconds)
                    - (time.monotonic_ns() - started_monotonic_ns) / 1_000_000_000,
                )
                process.communicate(input=prompt_bytes, timeout=min(hard_stop_seconds, job_seconds))
                exit_code = int(process.returncode)
            except LaunchBoundaryClosed as error:
                timed_out_at_hard_stop = time.time_ns() >= boundary_hard_stop_ns
                close_exec_gate(gate_release_fd)
                gate_release_fd = -1
                termination = terminate_contained_process(
                    process,
                    containment,
                    f"post-registration-launch-boundary: {error}",
                )
                exit_code = int(process.returncode if process.returncode is not None else 124)
            except subprocess.TimeoutExpired:
                timed_out_at_hard_stop = time.time_ns() >= boundary_hard_stop_ns - 1_000_000
                timed_out_at_job_limit = not timed_out_at_hard_stop
                termination = terminate_contained_process(process, containment, "timeout")
                exit_code = int(process.returncode if process.returncode is not None else 124)
            else:
                residual_processes_after_exit, termination = terminate_nominal_residuals(
                    process, containment
                )
            finally:
                if process.poll() is None and termination is None:
                    # This only runs while another exception is unwinding.
                    termination = terminate_contained_process(process, containment, "base-exception")
        except BaseException:
            if process is not None:
                termination = terminate_contained_process(process, containment, "base-exception")
            raise
        finally:
            close_exec_gate(gate_read_fd)
            close_exec_gate(gate_release_fd)
            if process is not None:
                termination = finalize_containment(
                    process, containment, termination, "containment-finalizer"
                )
                # Keep active evidence available to the hard-stop guardian until
                # the cgroup is proven empty and waitpid has reaped the wrapper.
                finish_registered_process(job_dir)
                containment.remove_empty()
            elif not containment.removed:
                cleanup_failed_launch(containment)

    finished_wall_ns = time.time_ns()
    finished_monotonic_ns = time.monotonic_ns()
    thread_ids, usage, completed, parsed_events = parse_codex_jsonl(stdout_path)
    cost = price_usage(args.model, usage, "short")
    status = (
        "agent_completed"
        if exit_code == 0 and completed and not residual_processes_after_exit
        else "incomplete"
    )
    result = {
        "schema": "lean-eval-job-result-v1",
        "job_id": args.job_id,
        "problem_id": args.problem,
        "status": status,
        "requested_model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "thread_ids": thread_ids,
        "exit_code": exit_code,
        "timed_out_at_hard_stop": timed_out_at_hard_stop,
        "timed_out_at_job_limit": timed_out_at_job_limit,
        "residual_processes_after_exit": residual_processes_after_exit,
        "termination": termination,
        "turn_completed": completed,
        "parsed_json_events": parsed_events,
        "finished_at_utc": iso_from_ns(finished_wall_ns),
        "finished_wall_ns": finished_wall_ns,
        "duration_ns": finished_monotonic_ns - started_monotonic_ns,
        "terminal_usage": usage,
        "pricing_context_class": "short",
        "pricing_method": "terminal aggregate with pinned short-context rates",
        "api_equivalent_token_cost_usd": decimal_string(cost),
        "cost_basis": read_json(PRICING_PATH)["cost_basis"],
        "evidence": {
            "launch_sha256": sha256_file(job_dir / "launch.json"),
            "stdout_sha256": sha256_file(stdout_path),
            "stderr_sha256": sha256_file(stderr_path),
        },
    }
    atomic_json(job_dir / "result.json", result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if status == "agent_completed" else 1


def submission_files(cwd: Path, explicit: list[str] | None) -> list[Path]:
    if explicit:
        return [Path(item).resolve() for item in explicit]
    files: list[Path] = []
    main = cwd / "Submission.lean"
    if main.is_file():
        files.append(main)
    helpers = cwd / "Submission"
    if helpers.is_dir():
        files.extend(sorted(helpers.rglob("*.lean")))
    return files


def forbidden_hits(files: list[Path]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), 1):
            match = FORBIDDEN_RE.search(line)
            if match:
                hits.append({"path": str(path), "line": line_number, "token": match.group(0)})
    return hits


def disallowed_workspace_changes(problem: str) -> list[str]:
    if problem.startswith("_toy"):
        return []
    prefix = f"generated/{problem}/"
    commands = [
        ["git", "diff", "--name-only", "HEAD", "--", prefix],
        ["git", "ls-files", "--others", "--exclude-standard", "--", prefix],
    ]
    changed: set[str] = set()
    for argv in commands:
        completed = subprocess.run(argv, cwd=REPO, check=True, capture_output=True, text=True)
        changed.update(line for line in completed.stdout.splitlines() if line)
    allowed_main = prefix + "Submission.lean"
    allowed_helpers = prefix + "Submission/"
    return sorted(
        path
        for path in changed
        if path != allowed_main and not (path.startswith(allowed_helpers) and path.endswith(".lean"))
    )


def command_verify(args: argparse.Namespace) -> int:
    assert_identifier("problem id", args.problem)
    assert_identifier("job id", args.job_id)
    if time.time() < race_start_epoch() and not (args.setup_test and args.problem.startswith("_toy")):
        raise SystemExit("race has not started")
    if stop_path().exists() or time.time() >= hard_stop_epoch():
        raise SystemExit("verification refused after stop")
    root = raw_root()
    boundary_stop = root / "STOP"
    boundary_hard_stop_ns = hard_stop_wall_ns()
    job_dir = root / args.job_id
    result_path = job_dir / "result.json"
    if not result_path.is_file():
        raise SystemExit(f"missing job result: {result_path}")
    result = read_json(result_path)
    if result.get("problem_id") != args.problem:
        raise SystemExit("job/problem mismatch")

    cwd = (REPO / args.cwd).resolve() if not Path(args.cwd).is_absolute() else Path(args.cwd).resolve()
    files = submission_files(cwd, args.files)
    hits = forbidden_hits(files)
    disallowed_changes = disallowed_workspace_changes(args.problem)
    argv = list(args.command)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        argv = ["lake", "test"]
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join(config()["tool_path_prefixes"] + [environment.get("PATH", "")])
    stdout_path = job_dir / "verify.stdout.log"
    stderr_path = job_dir / "verify.stderr.log"
    started_wall_ns = time.time_ns()
    started_monotonic_ns = time.monotonic_ns()
    exit_code = 125
    timed_out = False
    termination: dict[str, Any] | None = None
    residual_processes_after_exit = False
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        containment = ProcessCgroup.create()
        process: subprocess.Popen[Any] | None = None
        gate_read_fd = -1
        gate_release_fd = -1
        try:
            try:
                gate_read_fd, gate_release_fd = os.pipe()
                bootstrap_argv = exec_gate_argv(
                    gate_read_fd,
                    boundary_stop,
                    boundary_hard_stop_ns,
                    argv,
                )
                assert_launch_boundary_open(boundary_stop, boundary_hard_stop_ns)
                try:
                    process = subprocess.Popen(
                        bootstrap_argv,
                        cwd=cwd,
                        stdout=stdout,
                        stderr=stderr,
                        env=environment,
                        start_new_session=True,
                        close_fds=True,
                        pass_fds=(gate_read_fd,),
                        preexec_fn=containment.preexec_join(
                            boundary_stop,
                            boundary_hard_stop_ns,
                        ),
                    )
                finally:
                    close_exec_gate(gate_read_fd)
                    gate_read_fd = -1
            except BaseException:
                cleanup_failed_launch(containment)
                raise
            if process.poll() is not None:
                raise RuntimeError("trusted exec gate exited before active evidence was published")
            registered_process(
                job_dir,
                process,
                argv,
                containment,
                bootstrap_argv,
            )
            try:
                assert_launch_boundary_open(boundary_stop, boundary_hard_stop_ns)
                release_descriptor = gate_release_fd
                gate_release_fd = -1
                release_exec_gate(release_descriptor)
                process.wait(timeout=deadline_timeout(boundary_hard_stop_ns))
                exit_code = int(process.returncode)
            except LaunchBoundaryClosed as error:
                timed_out = True
                close_exec_gate(gate_release_fd)
                gate_release_fd = -1
                termination = terminate_contained_process(
                    process,
                    containment,
                    f"post-registration-launch-boundary: {error}",
                )
                exit_code = int(process.returncode if process.returncode is not None else 124)
            except subprocess.TimeoutExpired:
                timed_out = True
                termination = terminate_contained_process(process, containment, "timeout")
                exit_code = int(process.returncode if process.returncode is not None else 124)
            else:
                residual_processes_after_exit, termination = terminate_nominal_residuals(
                    process, containment
                )
            finally:
                if process.poll() is None and termination is None:
                    termination = terminate_contained_process(process, containment, "base-exception")
        except BaseException:
            if process is not None:
                termination = terminate_contained_process(process, containment, "base-exception")
            raise
        finally:
            close_exec_gate(gate_read_fd)
            close_exec_gate(gate_release_fd)
            if process is not None:
                termination = finalize_containment(
                    process, containment, termination, "containment-finalizer"
                )
                finish_registered_process(job_dir)
                containment.remove_empty()
            elif not containment.removed:
                cleanup_failed_launch(containment)
    finished_wall_ns = time.time_ns()
    finished_monotonic_ns = time.monotonic_ns()
    verified = (
        exit_code == 0
        and not hits
        and not disallowed_changes
        and not timed_out
        and not residual_processes_after_exit
    )
    verification = {
        "schema": "lean-eval-verification-v1",
        "job_id": args.job_id,
        "problem_id": args.problem,
        "argv": argv,
        "cwd": str(cwd),
        "checked_files": [str(path) for path in files],
        "forbidden_hits": hits,
        "disallowed_workspace_changes": disallowed_changes,
        "exit_code": exit_code,
        "timed_out_at_hard_stop": timed_out,
        "residual_processes_after_exit": residual_processes_after_exit,
        "termination": termination,
        "verified": verified,
        "started_at_utc": iso_from_ns(started_wall_ns),
        "finished_at_utc": iso_from_ns(finished_wall_ns),
        "finished_wall_ns": finished_wall_ns,
        "duration_ns": finished_monotonic_ns - started_monotonic_ns,
        "stdout_sha256": sha256_file(stdout_path),
        "stderr_sha256": sha256_file(stderr_path),
    }
    atomic_json(job_dir / "verification.json", verification)
    result["status"] = "verified" if verified else "verification_failed"
    result["elapsed_wall_ns"] = finished_wall_ns - int(read_json(job_dir / "launch.json")["started_wall_ns"])
    result["verified_at_utc"] = verification["finished_at_utc"] if verified else None
    result["verification_sha256"] = sha256_file(job_dir / "verification.json")
    atomic_json(result_path, result)
    print(json.dumps(verification, indent=2, sort_keys=True))
    return 0 if verified else 1


def command_solve(args: argparse.Namespace) -> int:
    run_code = command_run(args)
    if run_code != 0:
        return run_code
    verify_args = argparse.Namespace(
        problem=args.problem,
        job_id=args.job_id,
        cwd=f"generated/{args.problem}",
        files=None,
        setup_test=False,
        command=["lake", "test"],
    )
    return command_verify(verify_args)


def reduction_usage_and_cost(
    job_dir: Path, result: dict[str, Any]
) -> tuple[dict[str, int], Decimal]:
    """Use a passing audit's accounted usage when one is available."""
    usage = normalize_usage(result.get("terminal_usage"))
    cost = Decimal(str(result["api_equivalent_token_cost_usd"]))
    audit_path = job_dir / "audit.json"
    interrupted = interrupted_agent_result(result)
    if not audit_path.is_file():
        if interrupted:
            raise ValueError(f"interrupted job lacks a passing accounting audit: {result.get('job_id')}")
        return usage, cost
    audit = read_json(audit_path)
    if not (
        isinstance(audit, dict)
        and audit.get("schema") == "lean-eval-job-audit-v1"
        and audit.get("job_id") == result.get("job_id")
        and audit.get("problem_id") == result.get("problem_id")
        and audit.get("passed") is True
        and isinstance(audit.get("accounted_usage"), dict)
    ):
        if interrupted:
            raise ValueError(f"interrupted job lacks a passing accounting audit: {result.get('job_id')}")
        return usage, cost
    usage = normalize_usage(audit["accounted_usage"])
    return usage, price_usage(str(result["requested_model"]), usage, "short")


def render_reduction() -> bytes:
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]]] = defaultdict(list)
    root = raw_root()
    if not root.exists():
        return b""
    for job_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        launch_path = job_dir / "launch.json"
        result_path = job_dir / "result.json"
        if not launch_path.is_file() or not result_path.is_file():
            continue
        launch = read_json(launch_path)
        result = read_json(result_path)
        verification_path = job_dir / "verification.json"
        verification = read_json(verification_path) if verification_path.is_file() else None
        grouped[result["problem_id"]].append((launch, result, verification))
    lines: list[bytes] = []
    for problem in sorted(grouped):
        jobs = sorted(grouped[problem], key=lambda row: (row[0]["started_wall_ns"], row[0]["job_id"]))
        usage = empty_usage()
        total_cost = Decimal(0)
        verified_rows = []
        for launch, result, verification in jobs:
            job_dir = root / str(result["job_id"])
            job_usage, job_cost = reduction_usage_and_cost(job_dir, result)
            for key, value in job_usage.items():
                usage[key] += int(value)
            total_cost += job_cost
            if verification and verification.get("verified"):
                verified_rows.append((launch, result, verification))
        earliest_start = min(int(row[0]["started_wall_ns"]) for row in jobs)
        first_verified = min((int(row[2]["finished_wall_ns"]) for row in verified_rows), default=None)
        aggregate = {
            "schema": "lean-eval-problem-aggregate-v1",
            "problem_id": problem,
            "job_ids": [row[0]["job_id"] for row in jobs],
            "verified": bool(verified_rows),
            "first_started_at_utc": iso_from_ns(earliest_start),
            "first_verified_at_utc": iso_from_ns(first_verified) if first_verified is not None else None,
            "elapsed_wall_ns": first_verified - earliest_start if first_verified is not None else None,
            "terminal_usage": usage,
            "api_equivalent_token_cost_usd": decimal_string(total_cost),
        }
        lines.append(canonical_bytes(aggregate))
    return b"".join(lines)


def command_reduce(args: argparse.Namespace) -> int:
    first = render_reduction()
    if args.check_determinism:
        second = render_reduction()
        if first != second:
            raise SystemExit("reducer is not byte-deterministic")
    output = Path(args.output)
    if not output.is_absolute():
        output = REPO / output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_bytes(first)
    os.replace(temporary, output)
    print(f"wrote {len(first)} bytes to {output}")
    return 0


def inspect_rollout(path: Path) -> dict[str, Any]:
    model: str | None = None
    final_usage: dict[str, int] | None = None
    timestamps: list[str] = []
    cumulative_samples: list[int] = []
    with path.open("r", encoding="utf-8", errors="strict") as stream:
        for line in stream:
            event = json.loads(line)
            timestamp = event.get("timestamp")
            if isinstance(timestamp, str):
                timestamps.append(timestamp)
            payload = event.get("payload", {})
            if event.get("type") == "turn_context" and isinstance(payload.get("model"), str):
                model = payload["model"]
            if event.get("type") == "event_msg" and payload.get("type") == "token_count":
                raw_usage = payload.get("info", {}).get("total_token_usage")
                if isinstance(raw_usage, dict):
                    final_usage = normalize_usage(raw_usage)
                    cumulative_samples.append(int(raw_usage.get("total_tokens", 0)))
    if not timestamps:
        raise ValueError(f"rollout has no timestamps: {path}")
    if any(right < left for left, right in zip(cumulative_samples, cumulative_samples[1:])):
        raise ValueError(f"non-monotone cumulative usage: {path}")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "model": model,
        "final_usage": final_usage or empty_usage(),
        "first_timestamp": timestamps[0],
        "last_timestamp": timestamps[-1],
        "token_count_samples": len(cumulative_samples),
        "unique_cumulative_samples": len(set(cumulative_samples)),
    }


def command_audit(args: argparse.Namespace) -> int:
    assert_identifier("job id", args.job_id)
    job_dir = raw_root() / args.job_id
    launch = read_json(job_dir / "launch.json")
    result = read_json(job_dir / "result.json")
    verification_path = job_dir / "verification.json"
    verification = read_json(verification_path) if verification_path.is_file() else None
    errors: list[str] = []

    artifact_checks = {
        "launch": sha256_file(job_dir / "launch.json") == result["evidence"]["launch_sha256"],
        "stdout": sha256_file(job_dir / "stdout.jsonl") == result["evidence"]["stdout_sha256"],
        "stderr": sha256_file(job_dir / "stderr.log") == result["evidence"]["stderr_sha256"],
    }
    pricing_sha256_matches = launch.get("pricing_sha256") == sha256_file(PRICING_PATH)
    if verification:
        artifact_checks["verify_stdout"] = (
            sha256_file(job_dir / "verify.stdout.log") == verification["stdout_sha256"]
        )
        artifact_checks["verify_stderr"] = (
            sha256_file(job_dir / "verify.stderr.log") == verification["stderr_sha256"]
        )
    if not all(artifact_checks.values()):
        errors.append("one or more artifact hashes do not match")
    if not pricing_sha256_matches:
        errors.append("launch pricing SHA-256 does not match the current pinned pricing")

    rollouts: list[dict[str, Any]] = []
    for thread_id in result["thread_ids"]:
        matches = sorted(Path("/data/codex/accounts").glob(f"*/sessions/**/rollout-*{thread_id}.jsonl"))
        if len(matches) != 1:
            errors.append(f"thread {thread_id} has {len(matches)} persisted rollouts")
            continue
        rollouts.append(inspect_rollout(matches[0]))
    if not result["thread_ids"]:
        errors.append("expected at least one thread for an isolated job")
    observed_models = sorted({row["model"] for row in rollouts if row["model"] is not None})
    if not rollouts or any(row["model"] != result["requested_model"] for row in rollouts):
        errors.append(f"requested/observed model mismatch: {observed_models}")
    accounted = normalize_usage(result["terminal_usage"])
    usage_source = "terminal-jsonl"
    if rollouts and len(rollouts) == len(result["thread_ids"]):
        try:
            accounted, usage_source = accounted_rollout_usage(
                result, [row["final_usage"] for row in rollouts]
            )
        except ValueError as error:
            errors.append(str(error))
    if int(result["duration_ns"]) <= 0:
        errors.append("non-positive agent duration")
    launch_epoch = int(launch["started_wall_ns"]) / 1_000_000_000
    result_epoch = int(result["finished_wall_ns"]) / 1_000_000_000
    for row in rollouts:
        first = parse_iso(row["first_timestamp"])
        last = parse_iso(row["last_timestamp"])
        if not (launch_epoch <= first <= last <= result_epoch):
            errors.append("rollout timestamps are not bracketed by controller timestamps")
    if args.require_verified and not (verification and verification.get("verified")):
        errors.append("independent verification is not successful")

    audit = {
        "schema": "lean-eval-job-audit-v1",
        "job_id": args.job_id,
        "problem_id": result["problem_id"],
        "passed": not errors,
        "errors": errors,
        "artifact_hashes_match": artifact_checks,
        "pricing_sha256_matches": pricing_sha256_matches,
        "requested_model": result["requested_model"],
        "observed_models": observed_models,
        "terminal_usage": result["terminal_usage"],
        "accounted_usage": accounted,
        "accounted_api_equivalent_token_cost_usd": decimal_string(
            price_usage(result["requested_model"], accounted, "short")
        ),
        "usage_source": usage_source,
        "rollouts": rollouts,
        "verified": bool(verification and verification.get("verified")),
    }
    atomic_json(job_dir / "audit.json", audit)
    print(json.dumps(audit, indent=2, sort_keys=True))
    return 0 if audit["passed"] else 1


def write_stop(reason: str) -> None:
    root = raw_root()
    root.mkdir(parents=True, exist_ok=True)
    if not stop_path().exists():
        atomic_json(stop_path(), {"schema": "lean-eval-stop-v1", "at": iso_from_ns(time.time_ns()), "reason": reason})


def kill_registered_processes() -> list[int]:
    killed: list[int] = []
    root = raw_root()
    if not root.exists():
        return killed
    entries: list[tuple[Path, int]] = []
    for active in sorted(root.glob("*/active.json")):
        try:
            pgid = int(read_json(active)["pgid"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if pgid > 1 and pgid != os.getpgrp():
            entries.append((active, pgid))
    for _, pgid in entries:
        try:
            os.killpg(pgid, signal.SIGTERM)
            killed.append(pgid)
        except ProcessLookupError:
            pass
    time.sleep(2)
    for active, pgid in entries:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            continue
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if active.exists():
            os.replace(active, active.with_name("killed-at-stop.json"))
    return killed


def command_stop(args: argparse.Namespace) -> int:
    write_stop(args.reason)
    killed = kill_registered_processes()
    print(json.dumps({"stop": str(stop_path()), "killed_process_groups": killed}, sort_keys=True))
    return 0


def command_watchdog(_: argparse.Namespace) -> int:
    root = raw_root()
    root.mkdir(parents=True, exist_ok=True)
    while True:
        remaining = hard_stop_epoch() - time.time()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 30.0))
    write_stop("configured hard stop reached")
    killed = kill_registered_processes()
    atomic_json(root / "watchdog-result.json", {"at": iso_from_ns(time.time_ns()), "killed_process_groups": killed})
    return 0


def command_arm(_: argparse.Namespace) -> int:
    root = raw_root()
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "watchdog.json"
    if state_path.exists():
        state = read_json(state_path)
        pid = int(state.get("pid", -1))
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            pass
        else:
            print(json.dumps(state, indent=2, sort_keys=True))
            return 0
    stdout = (root / "watchdog.stdout.log").open("ab")
    stderr = (root / "watchdog.stderr.log").open("ab")
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_watchdog"],
        cwd=REPO,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
        close_fds=True,
    )
    stdout.close()
    stderr.close()
    state = {
        "schema": "lean-eval-watchdog-v1",
        "pid": process.pid,
        "armed_at": iso_from_ns(time.time_ns()),
        "hard_stop": config()["hard_stop"],
    }
    atomic_json(state_path, state)
    print(json.dumps(state, indent=2, sort_keys=True))
    return 0


def command_status(_: argparse.Namespace) -> int:
    state = {
        "now": iso_from_ns(time.time_ns()),
        "race_start": config()["race_start"],
        "hard_stop": config()["hard_stop"],
        "seconds_to_race_start": max(0.0, race_start_epoch() - time.time()),
        "seconds_to_hard_stop": max(0.0, hard_stop_epoch() - time.time()),
        "stopped": stop_path().exists(),
        "active": [str(path) for path in sorted(raw_root().glob("*/active.json"))] if raw_root().exists() else [],
    }
    print(json.dumps(state, indent=2, sort_keys=True))
    return 0


def command_prepare(args: argparse.Namespace) -> int:
    assert_identifier("problem id", args.problem)
    workspace = REPO / "generated" / args.problem
    if not (workspace / "lakefile.toml").is_file():
        raise SystemExit(f"unknown generated workspace: {workspace}")
    lake_dir = workspace / ".lake"
    lake_dir.mkdir(exist_ok=True)
    packages = lake_dir / "packages"
    expected = REPO / ".lake" / "packages"
    if packages.is_symlink() and packages.resolve() == expected.resolve():
        pass
    elif packages.exists() or packages.is_symlink():
        raise SystemExit(f"refusing to replace existing package path: {packages}")
    else:
        packages.symlink_to(expected)
    seed = REPO / "generated" / "symplectic_matrix_det" / "lake-manifest.json"
    if seed.is_file() and workspace != seed.parent:
        shutil.copyfile(seed, workspace / "lake-manifest.json")
    print(workspace)
    return 0


def launch_cgroup_self_test(script: str) -> tuple[subprocess.Popen[Any], ProcessCgroup]:
    containment = ProcessCgroup.create()
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=containment.preexec_join(),
        )
    except BaseException:
        cleanup_failed_launch(containment)
        raise
    try:
        containment.validate_process(process)
    except BaseException:
        termination = terminate_contained_process(process, containment, "self-test-launch-failure")
        if termination.get("complete") is True:
            containment.remove_empty()
        raise
    return process, containment


def cleanup_cgroup_self_test(
    process: subprocess.Popen[Any] | None, containment: ProcessCgroup | None
) -> None:
    if containment is None or containment.removed:
        return
    if process is None:
        cleanup_failed_launch(containment)
        return
    termination = terminate_contained_process(process, containment, "self-test-cleanup")
    if termination.get("complete") is not True:
        raise RuntimeError(f"self-test process cgroup did not become empty: {containment.path}")
    containment.remove_empty()


def self_test_preexec_boundary_refusal() -> None:
    process: subprocess.Popen[Any] | None = None
    containment: ProcessCgroup | None = None
    try:
        containment = ProcessCgroup.create()
        try:
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                preexec_fn=containment.preexec_join(None, time.time_ns() - 1),
            )
        except subprocess.SubprocessError:
            cleanup_failed_launch(containment)
        else:
            raise RuntimeError("expired preexec boundary allowed untrusted exec")
        if not containment.removed or containment.path.exists():
            raise RuntimeError("expired preexec boundary left its cgroup behind")
    finally:
        cleanup_cgroup_self_test(process, containment)


def self_test_trusted_exec_gate() -> None:
    def exercise(deadline_ns: int, expect_target: bool) -> None:
        process: subprocess.Popen[Any] | None = None
        containment: ProcessCgroup | None = None
        gate_read_fd = -1
        gate_release_fd = -1
        scratch_root = Path(os.environ.get("TMPDIR", "/data"))
        with tempfile.TemporaryDirectory(prefix="lean-eval-gate-self-test-", dir=scratch_root) as temporary:
            job_dir = Path(temporary)
            target_argv = [
                sys.executable,
                "-c",
                "print('target-executed', flush=True)",
            ]
            boundary_stop = job_dir / "STOP"
            try:
                containment = ProcessCgroup.create()
                gate_read_fd, gate_release_fd = os.pipe()
                bootstrap_argv = exec_gate_argv(
                    gate_read_fd,
                    boundary_stop,
                    deadline_ns,
                    target_argv,
                )
                try:
                    process = subprocess.Popen(
                        bootstrap_argv,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                        close_fds=True,
                        pass_fds=(gate_read_fd,),
                        preexec_fn=containment.preexec_join(
                            boundary_stop,
                            deadline_ns,
                        )
                        if expect_target
                        else containment.preexec_join(),
                    )
                finally:
                    close_exec_gate(gate_read_fd)
                    gate_read_fd = -1
                if process.poll() is not None:
                    raise RuntimeError("trusted exec gate exited before its release")
                registered_process(
                    job_dir,
                    process,
                    target_argv,
                    containment,
                    bootstrap_argv,
                )
                active = read_json(job_dir / "active.json")
                if active.get("bootstrap_argv") != bootstrap_argv or active.get("argv") != target_argv:
                    raise RuntimeError("trusted exec gate active evidence is incomplete")
                live_cmdline = tuple(
                    part.decode("utf-8", errors="strict")
                    for part in (Path("/proc") / str(process.pid) / "cmdline").read_bytes().split(b"\0")
                    if part
                )
                if tuple(bootstrap_argv) != live_cmdline:
                    raise RuntimeError("target executed before active evidence and gate release")
                release_descriptor = gate_release_fd
                gate_release_fd = -1
                release_exec_gate(release_descriptor)
                output, _ = process.communicate(timeout=3.0)
                if expect_target:
                    if process.returncode != 0 or output != b"target-executed\n":
                        raise RuntimeError("released trusted exec gate did not execute its target")
                elif process.returncode != 125 or output:
                    raise RuntimeError("expired trusted exec gate did not fail closed")
                residual, termination = terminate_nominal_residuals(process, containment)
                if residual:
                    raise RuntimeError("trusted exec gate self-test left residual processes")
                finalize_containment(process, containment, termination, "self-test-gate-finalizer")
                finish_registered_process(job_dir)
                containment.remove_empty()
            finally:
                close_exec_gate(gate_read_fd)
                close_exec_gate(gate_release_fd)
                cleanup_cgroup_self_test(process, containment)

    exercise(time.time_ns() + 5_000_000_000, True)
    exercise(time.time_ns() - 1, False)


def self_test_double_fork_containment() -> None:
    script = """
import os
import time

first = os.fork()
if first == 0:
    os.setsid()
    second = os.fork()
    if second == 0:
        time.sleep(60)
    os._exit(0)
os.waitpid(first, 0)
time.sleep(60)
"""
    process: subprocess.Popen[Any] | None = None
    containment: ProcessCgroup | None = None
    try:
        process, containment = launch_cgroup_self_test(script)
        deadline = time.monotonic() + 3.0
        escaped_process_group = False
        reparented = False
        while time.monotonic() < deadline:
            for pid in containment.members() - {process.pid}:
                identity = proc_identity(pid)
                if identity is None or identity[2] == "Z":
                    continue
                reparented = reparented or identity[0] != process.pid
                escaped_process_group = escaped_process_group or identity[1] != process.pid
            if reparented and escaped_process_group:
                break
            if process.poll() is not None:
                raise RuntimeError("double-fork self-test wrapper exited unexpectedly")
            time.sleep(0.02)
        if not reparented or not escaped_process_group:
            raise RuntimeError("double-fork self-test did not observe setsid and reparenting")
        termination = terminate_contained_process(
            process, containment, "self-test-double-fork-setsid-reparent"
        )
        if not (
            termination.get("complete") is True
            and termination.get("kill_method") == "cgroup.kill"
            and termination.get("wrapper_reaped") is True
            and termination.get("cgroup_populated") is False
        ):
            raise RuntimeError(f"double-fork cgroup termination failed: {termination}")
        finalize_containment(process, containment, termination, "self-test-finalizer")
        containment.remove_empty()
    finally:
        cleanup_cgroup_self_test(process, containment)


def self_test_nominal_residual_is_incomplete() -> None:
    script = """
import os
import time

if os.fork() == 0:
    time.sleep(60)
os._exit(0)
"""
    process: subprocess.Popen[Any] | None = None
    containment: ProcessCgroup | None = None
    try:
        process, containment = launch_cgroup_self_test(script)
        process.wait(timeout=3.0)
        if process.returncode != 0:
            raise RuntimeError("nominal-residual wrapper did not exit zero")
        residual, termination = terminate_nominal_residuals(process, containment)
        would_be_complete = process.returncode == 0 and not residual
        if not residual or would_be_complete or termination is None:
            raise RuntimeError("nominal residual was not classified as incomplete")
        if not (
            termination.get("complete") is True
            and termination.get("kill_method") == "cgroup.kill"
            and termination.get("wrapper_reaped") is True
            and termination.get("cgroup_populated") is False
        ):
            raise RuntimeError(f"nominal-residual cgroup termination failed: {termination}")
        finalize_containment(process, containment, termination, "self-test-finalizer")
        containment.remove_empty()
    finally:
        cleanup_cgroup_self_test(process, containment)


def command_self_test(_: argparse.Namespace) -> int:
    fixture = {
        "input_tokens": 1000,
        "cached_input_tokens": 600,
        "cache_write_input_tokens": 0,
        "output_tokens": 100,
        "reasoning_output_tokens": 20,
    }
    actual = decimal_string(price_usage("gpt-5.6-luna", fixture, "short"))
    expected = "0.000212000"
    if actual != expected:
        raise SystemExit(f"pricing fixture mismatch: {actual} != {expected}")
    cfg = config()
    if parse_iso(cfg["race_start"]) - parse_iso(cfg["schedule_origin"]) != 3600:
        raise SystemExit("race-start arithmetic mismatch")
    if parse_iso(cfg["hard_stop"]) - parse_iso(cfg["race_start"]) != 86400:
        raise SystemExit("hard-stop arithmetic mismatch")
    sample = {"b": 2, "a": 1}
    if canonical_bytes(sample) != canonical_bytes(json.loads(canonical_bytes(sample))):
        raise SystemExit("canonical JSON roundtrip mismatch")
    interrupted = {
        "status": "incomplete",
        "turn_completed": False,
        "exit_code": 143,
        "terminal_usage": empty_usage(),
    }
    recovered, source = accounted_usage(interrupted, fixture)
    if recovered != fixture or source != "persisted-rollout-cumulative-after-interruption":
        raise SystemExit("interrupted accounting fixture failed")
    verified_after_interruption = dict(interrupted)
    verified_after_interruption["status"] = "verified"
    recovered, source = accounted_usage(verified_after_interruption, fixture)
    if recovered != fixture or source != "persisted-rollout-cumulative-after-interruption":
        raise SystemExit("verified interrupted accounting fixture failed")
    failover_second = {
        "input_tokens": 250,
        "cached_input_tokens": 100,
        "cache_write_input_tokens": 25,
        "output_tokens": 40,
        "reasoning_output_tokens": 10,
    }
    failover_terminal = sum_usage([fixture, failover_second])
    combined, source = accounted_rollout_usage(
        {"terminal_usage": failover_terminal}, [fixture, failover_second]
    )
    if (
        combined != failover_terminal
        or source != "terminal-jsonl-componentwise-sum-of-persisted-rollouts"
    ):
        raise SystemExit("sequential failover accounting fixture failed")
    mismatched_terminal = dict(failover_terminal)
    mismatched_terminal["output_tokens"] += 1
    try:
        accounted_rollout_usage(
            {"terminal_usage": mismatched_terminal}, [fixture, failover_second]
        )
    except ValueError:
        pass
    else:
        raise SystemExit("sequential failover mismatch fixture failed")
    try:
        self_test_preexec_boundary_refusal()
        self_test_trusted_exec_gate()
        self_test_double_fork_containment()
        self_test_nominal_residual_is_incomplete()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"cgroup-v2 containment self-test failed: {error}") from error
    print(json.dumps({
        "canonical_json": "ok",
        "cgroup_v2_expired_preexec_refused": "ok",
        "cgroup_v2_trusted_exec_gate": "ok",
        "cgroup_v2_double_fork_setsid_reparent": "ok",
        "cgroup_v2_nominal_residual_incomplete": "ok",
        "interrupted_accounting": "ok",
        "verified_interrupted_accounting": "ok",
        "sequential_failover_accounting": "ok",
        "pricing_fixture_usd": actual,
        "schedule": "ok",
    }, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="subcommand", required=True)

    run = sub.add_parser("run")
    run.add_argument("--problem", required=True)
    run.add_argument("--job-id", required=True)
    run.add_argument("--model", required=True, choices=("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"))
    run.add_argument("--reasoning-effort", default="xhigh", choices=("low", "medium", "high", "xhigh", "max", "ultra"))
    run.add_argument("--prompt-file", required=True)
    run.add_argument("--max-agent-seconds", type=float, default=3600.0)
    run.add_argument("--setup-test", action="store_true")
    run.set_defaults(handler=command_run)

    solve = sub.add_parser("solve")
    solve.add_argument("--problem", required=True)
    solve.add_argument("--job-id", required=True)
    solve.add_argument("--model", required=True, choices=("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"))
    solve.add_argument("--reasoning-effort", default="xhigh", choices=("low", "medium", "high", "xhigh", "max", "ultra"))
    solve.add_argument("--prompt-file", required=True)
    solve.add_argument("--max-agent-seconds", type=float, default=3600.0)
    solve.set_defaults(handler=command_solve, setup_test=False)

    verify = sub.add_parser("verify")
    verify.add_argument("--problem", required=True)
    verify.add_argument("--job-id", required=True)
    verify.add_argument("--cwd", required=True)
    verify.add_argument("--files", nargs="*")
    verify.add_argument("--setup-test", action="store_true")
    verify.add_argument("command", nargs=argparse.REMAINDER)
    verify.set_defaults(handler=command_verify)

    reduce_parser = sub.add_parser("reduce")
    reduce_parser.add_argument("--output", default="speedrun/problems.jsonl")
    reduce_parser.add_argument("--check-determinism", action="store_true")
    reduce_parser.set_defaults(handler=command_reduce)

    audit = sub.add_parser("audit")
    audit.add_argument("--job-id", required=True)
    audit.add_argument("--require-verified", action="store_true")
    audit.set_defaults(handler=command_audit)

    stop = sub.add_parser("stop")
    stop.add_argument("--reason", default="manual stop")
    stop.set_defaults(handler=command_stop)

    sub.add_parser("arm").set_defaults(handler=command_arm)
    sub.add_parser("status").set_defaults(handler=command_status)
    sub.add_parser("prepare").add_argument("--problem", required=True)
    sub.choices["prepare"].set_defaults(handler=command_prepare)
    sub.add_parser("self-test").set_defaults(handler=command_self_test)
    sub.add_parser("_watchdog").set_defaults(handler=command_watchdog)
    gate = sub.add_parser("_exec_gate", help=argparse.SUPPRESS)
    gate.add_argument("--release-fd", type=int, required=True)
    gate.add_argument("--stop-file", required=True)
    gate.add_argument("--hard-stop-ns", type=int, required=True)
    gate.add_argument("target_argv", nargs=argparse.REMAINDER)
    gate.set_defaults(handler=command_exec_gate)
    return result


def main() -> int:
    args = parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
