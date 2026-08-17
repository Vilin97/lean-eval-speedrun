#!/usr/bin/env python3
"""Exact-deadline, fail-closed guardian for the LeanEval speedrun.

``arm`` installs a detached guardian but does not stop or signal anything.
The detached process waits until the configured UTC hard stop, atomically
creates ``STOP``, validates job ownership from controller evidence, cgroup v2,
and ``/proc``, then immediately uses ``cgroup.kill`` for recorded live job
groups and PID-reuse-safe SIGKILL as an exact fallback.  Legacy jobs without
cgroup evidence retain the PID-based path.  Completion requires every validated
job cgroup to report ``populated 0``.

No signal is sent before the hard stop.  ``status`` and ``self-test`` are
read-only.  The private ``_run`` entry point is only accepted for an identity
published by ``arm``.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


REPO = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
DEFAULT_CONFIG = REPO / "speedrun" / "config.json"
STATE_NAME = "hardstop-guard.json"
RESULT_NAME = "hardstop-guard-result.json"
STDOUT_NAME = "hardstop-guard.stdout.log"
STDERR_NAME = "hardstop-guard.stderr.log"
STATE_SCHEMA = "lean-eval-hardstop-guard-state-v1"
RESULT_SCHEMA = "lean-eval-hardstop-guard-result-v1"
STOP_SCHEMA = "lean-eval-stop-v1"
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
CGROUP_NAME_RE = re.compile(r"^lean-eval-([1-9][0-9]*)-([0-9a-f]{24})$")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
NS = 1_000_000_000
CGROUP_SETTLE_SECONDS = 2.0
CGROUP_READ_LIMIT = 1024 * 1024
CGROUP_MAX_DEPTH = 32
CGROUP_MAX_GROUPS = 1024
CGROUP_MAX_PIDS = 65536
CGROUP_KILL_SUCCESS_OUTCOMES = frozenset(
    {
        "already-empty",
        "cgroup-kill-written",
        "empty-after-controller-transition",
        "empty-after-marker-change",
    }
)


class GuardError(RuntimeError):
    """A safety or identity condition failed."""


@dataclass(frozen=True)
class GuardConfig:
    path: Path
    raw_root: Path
    race_start_ns: int
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
    exe: str
    cmdline: tuple[str, ...]


@dataclass(frozen=True)
class CgroupRecord:
    path: Path
    inode: int
    owner_uid: int


@dataclass(frozen=True)
class CgroupMount:
    root: PurePosixPath
    mountpoint: Path


@dataclass(frozen=True)
class ActiveRecord:
    path: Path
    pid: int
    pgid: int
    argv: tuple[str, ...]
    registered_ns: int
    sha256: str
    bootstrap_argv: tuple[str, ...] | None = None
    cgroup: CgroupRecord | None = None
    cgroup_error: str | None = None


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    problem_id: str
    directory: Path
    controller_pid: int
    launched_ns: int
    requested_model: str
    reasoning_effort: str
    active: ActiveRecord | None
    result_exists: bool


@dataclass
class OwnedTarget:
    info: ProcessInfo
    roles: set[str] = field(default_factory=set)
    jobs: set[str] = field(default_factory=set)
    depth: int = 0


@dataclass
class CgroupTarget:
    job_id: str
    record: CgroupRecord
    directory_fd: int
    events_fd: int
    kill_fd: int
    procs_fd: int
    device: int
    initial_populated: int
    initial_pids: list[int]
    initial_cgroups: list[str]
    empty_proven: bool
    empty_proof_wall_ns: int | None

    def close(self) -> None:
        for descriptor in (
            self.procs_fd,
            self.kill_fd,
            self.events_fd,
            self.directory_fd,
        ):
            os.close(descriptor)


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise GuardError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GuardError(f"cannot read valid JSON from {path}: {error}") from error


def write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise GuardError("short write while publishing guardian evidence")
        offset += written


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_json_exclusive(path: Path, value: Any) -> bool:
    """Atomically publish JSON without replacing an existing evidence file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
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


def parse_iso_ns(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise GuardError(f"invalid ISO timestamp {value!r}") from error
    if parsed.tzinfo is None:
        raise GuardError(f"timestamp lacks timezone: {value!r}")
    delta = parsed.astimezone(timezone.utc) - EPOCH
    return ((delta.days * 86400 + delta.seconds) * NS) + delta.microseconds * 1000


def iso_from_ns(value: int) -> str:
    seconds, nanoseconds = divmod(value, NS)
    parsed = EPOCH + timedelta(seconds=seconds, microseconds=nanoseconds // 1000)
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load_config(path: Path) -> GuardConfig:
    resolved = path.resolve(strict=True)
    value = read_json(resolved)
    if not isinstance(value, dict) or value.get("schema") != "lean-eval-speedrun-config-v1":
        raise GuardError("unrecognized speedrun configuration schema")
    if Path(str(value.get("default_cwd"))).resolve() != REPO.resolve():
        raise GuardError("speedrun default_cwd is not this repository")
    raw_value = value.get("raw_root")
    if not isinstance(raw_value, str) or not Path(raw_value).is_absolute():
        raise GuardError("speedrun raw_root must be absolute")
    raw_root = Path(raw_value)
    if raw_root.is_symlink():
        raise GuardError("speedrun raw_root may not be a symlink")
    race_text = value.get("race_start")
    hard_text = value.get("hard_stop")
    if not isinstance(race_text, str) or not isinstance(hard_text, str):
        raise GuardError("configuration lacks race_start/hard_stop timestamps")
    race_ns = parse_iso_ns(race_text)
    hard_ns = parse_iso_ns(hard_text)
    if race_ns >= hard_ns:
        raise GuardError("configured race interval is empty or reversed")
    return GuardConfig(
        path=resolved,
        raw_root=raw_root.resolve(),
        race_start_ns=race_ns,
        hard_stop_ns=hard_ns,
        hard_stop_text=hard_text,
        sha256=hash_file(resolved),
    )


def boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError as error:
        raise GuardError(f"cannot read kernel boot identity: {error}") from error
    if not value:
        raise GuardError("kernel boot identity is empty")
    return value


def boot_wall_seconds() -> int:
    try:
        lines = Path("/proc/stat").read_text(encoding="ascii").splitlines()
    except OSError as error:
        raise GuardError(f"cannot read /proc/stat: {error}") from error
    for line in lines:
        if line.startswith("btime "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError) as error:
                raise GuardError("invalid btime in /proc/stat") from error
    raise GuardError("/proc/stat has no btime")


def clock_ticks() -> int:
    value = int(os.sysconf("SC_CLK_TCK"))
    if value <= 0:
        raise GuardError("invalid kernel clock tick rate")
    return value


def require_pidfd_support() -> None:
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise GuardError("Linux pidfd signaling is required for PID-reuse-safe enforcement")


def process_start_wall_ns(info: ProcessInfo, boot_seconds: int, ticks: int) -> int:
    return boot_seconds * NS + (info.start_ticks * NS // ticks)


def parse_proc_stat(raw: str) -> tuple[int, int, int, int, str, int]:
    close = raw.rfind(")")
    open_paren = raw.find("(")
    if open_paren <= 0 or close <= open_paren or close + 2 > len(raw):
        raise ValueError("malformed /proc stat record")
    pid = int(raw[:open_paren].strip())
    fields = raw[close + 2 :].split()
    if len(fields) < 20:
        raise ValueError("short /proc stat record")
    state = fields[0]
    ppid = int(fields[1])
    pgrp = int(fields[2])
    session_id = int(fields[3])
    start_ticks = int(fields[19])
    return pid, ppid, pgrp, session_id, state, start_ticks


def read_process(pid: int) -> ProcessInfo | None:
    if pid <= 1:
        return None
    directory = Path("/proc") / str(pid)
    try:
        raw_stat = (directory / "stat").read_text(encoding="ascii")
        parsed_pid, ppid, pgrp, session_id, state, start_ticks = parse_proc_stat(raw_stat)
        if parsed_pid != pid:
            return None
        cmdline = tuple(
            part.decode("utf-8", errors="surrogateescape")
            for part in (directory / "cmdline").read_bytes().split(b"\0")
            if part
        )
        stat_result = directory.stat()
        cwd = os.path.realpath(os.readlink(directory / "cwd"))
        exe = os.path.realpath(os.readlink(directory / "exe"))
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError, ValueError):
        return None
    return ProcessInfo(
        pid=pid,
        ppid=ppid,
        pgrp=pgrp,
        session=session_id,
        state=state,
        start_ticks=start_ticks,
        uid=stat_result.st_uid,
        cwd=cwd,
        exe=exe,
        cmdline=cmdline,
    )


def process_snapshot() -> dict[int, ProcessInfo]:
    result: dict[int, ProcessInfo] = {}
    try:
        entries = sorted(
            (entry for entry in Path("/proc").iterdir() if entry.name.isdigit()),
            key=lambda entry: int(entry.name),
        )
    except OSError as error:
        raise GuardError(f"cannot enumerate /proc: {error}") from error
    for entry in entries:
        info = read_process(int(entry.name))
        if info is not None:
            result[info.pid] = info
    return result


def decode_mountinfo_path(value: str) -> str:
    """Decode the octal escapes used for mountinfo path fields."""

    def replace(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 8))

    decoded = re.sub(r"\\([0-7]{3})", replace, value)
    if "\\" in decoded or "\0" in decoded:
        raise GuardError("unsupported escape in cgroup2 mountinfo path")
    return decoded


def parse_unified_cgroup(text: str) -> PurePosixPath:
    matches: list[str] = []
    for raw_line in text.splitlines():
        fields = raw_line.split(":", 2)
        if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
            matches.append(fields[2])
    if len(matches) != 1:
        raise GuardError("process has no unique unified cgroup-v2 membership")
    raw_path = matches[0]
    path = PurePosixPath(raw_path)
    if (
        not raw_path.startswith("/")
        or raw_path.startswith("//")
        or str(path) != raw_path
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise GuardError("process has an unsafe unified cgroup-v2 path")
    return path


def read_unified_cgroup(pid: int | None = None) -> PurePosixPath:
    if pid is not None and pid <= 1:
        raise GuardError(f"refusing unsafe cgroup lookup for PID {pid}")
    path = Path("/proc/self/cgroup") if pid is None else Path("/proc") / str(pid) / "cgroup"
    try:
        return parse_unified_cgroup(path.read_text(encoding="ascii"))
    except OSError as error:
        label = "self" if pid is None else str(pid)
        raise GuardError(f"cannot read cgroup membership for PID {label}: {error}") from error


def parse_cgroup2_mounts(text: str) -> list[CgroupMount]:
    mounts: list[CgroupMount] = []
    for raw_line in text.splitlines():
        before, separator, after = raw_line.partition(" - ")
        if not separator:
            continue
        left = before.split()
        right = after.split()
        if len(left) < 6 or not right or right[0] != "cgroup2":
            continue
        root_text = decode_mountinfo_path(left[3])
        mountpoint_text = decode_mountinfo_path(left[4])
        root = PurePosixPath(root_text)
        mountpoint = Path(mountpoint_text)
        if (
            not root_text.startswith("/")
            or root_text.startswith("//")
            or str(root) != root_text
            or not mountpoint.is_absolute()
            or str(mountpoint) != mountpoint_text
        ):
            raise GuardError("unsafe cgroup2 mountinfo path")
        mounts.append(CgroupMount(root=root, mountpoint=mountpoint))
    if not mounts:
        raise GuardError("no cgroup-v2 filesystem is mounted")
    return mounts


def cgroup2_mounts() -> list[CgroupMount]:
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="ascii")
    except OSError as error:
        raise GuardError(f"cannot read cgroup2 mount topology: {error}") from error
    return parse_cgroup2_mounts(text)


def map_unified_cgroup(
    unified: PurePosixPath, mounts: Iterable[CgroupMount]
) -> Path:
    candidates: list[tuple[int, Path]] = []
    for mount in mounts:
        if unified != mount.root and mount.root not in unified.parents:
            continue
        relative = unified.relative_to(mount.root)
        candidates.append((len(mount.root.parts), mount.mountpoint.joinpath(*relative.parts)))
    if not candidates:
        raise GuardError(f"unified cgroup path is outside every cgroup2 mount: {unified}")
    best_depth = max(depth for depth, _ in candidates)
    best = {path for depth, path in candidates if depth == best_depth}
    if len(best) != 1:
        raise GuardError("unified cgroup path maps ambiguously through cgroup2 mounts")
    return next(iter(best))


def current_delegated_cgroup() -> tuple[Path, list[CgroupMount]]:
    mounts = cgroup2_mounts()
    path = map_unified_cgroup(read_unified_cgroup(), mounts)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise GuardError(f"cannot resolve delegated cgroup directory {path}: {error}") from error
    if resolved != path or path.is_symlink() or not path.is_dir():
        raise GuardError(f"delegated cgroup directory is unsafe: {path}")
    return path, mounts


def read_descriptor(descriptor: int, label: str) -> str:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        length = 0
        while True:
            chunk = os.read(descriptor, min(65536, CGROUP_READ_LIMIT + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
            if length > CGROUP_READ_LIMIT:
                raise GuardError(f"{label} exceeds the guardian read limit")
        return b"".join(chunks).decode("ascii")
    except (OSError, UnicodeDecodeError) as error:
        raise GuardError(f"cannot read valid ASCII from {label}: {error}") from error


def parse_cgroup_events(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[0] in result:
            raise GuardError("malformed cgroup.events data")
        try:
            value = int(fields[1])
        except ValueError as error:
            raise GuardError("non-integer cgroup.events value") from error
        if value < 0:
            raise GuardError("negative cgroup.events value")
        result[fields[0]] = value
    if result.get("populated") not in {0, 1}:
        raise GuardError("cgroup.events lacks a Boolean populated field")
    return result


def parse_cgroup_procs(text: str) -> list[int]:
    result: list[int] = []
    for line in text.splitlines():
        if not line or not line.isascii() or not line.isdecimal():
            raise GuardError("malformed cgroup.procs data")
        pid = int(line)
        if pid <= 1 or pid in result:
            raise GuardError("unsafe or duplicate PID in cgroup.procs")
        result.append(pid)
    return sorted(result)


def open_cgroup_control(directory_fd: int, name: str, flags: int) -> int:
    descriptor = os.open(
        name,
        flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_fd,
    )
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise GuardError(f"cgroup control {name} is not a regular file")
    return descriptor


def option_value(argv: tuple[str, ...], option: str) -> str | None:
    indexes = [index for index, value in enumerate(argv[:-1]) if value == option]
    if len(indexes) != 1:
        return None
    return argv[indexes[0] + 1]


def speedrun_invocation(info: ProcessInfo) -> tuple[str, str | None, str | None] | None:
    script_index = None
    for index, token in enumerate(info.cmdline):
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = Path(info.cwd) / candidate
        try:
            if candidate.resolve() == (REPO / "scripts" / "speedrun.py").resolve():
                script_index = index
                break
        except OSError:
            continue
    if script_index is None or script_index + 1 >= len(info.cmdline):
        return None
    command = info.cmdline[script_index + 1]
    if command not in {"run", "solve", "verify"}:
        return None
    tail = info.cmdline[script_index + 2 :]
    return command, option_value(tail, "--job-id"), option_value(tail, "--problem")


def exact_worker_invocation(info: ProcessInfo) -> bool:
    if (
        not info.cmdline
        or Path(info.cmdline[0]).name != "codex-real"
        or Path(info.exe).name not in {"codex", "codex-real"}
        or info.cwd != str(REPO.resolve())
    ):
        return False
    argv = info.cmdline
    try:
        exec_index = argv.index("exec")
        cwd_index = argv.index("-C", exec_index)
    except ValueError:
        return False
    return (
        "--json" in argv[exec_index + 1 :]
        and "--dangerously-bypass-approvals-and-sandbox" in argv[exec_index + 1 :]
        and cwd_index + 1 < len(argv)
        and Path(argv[cwd_index + 1]).resolve() == REPO.resolve()
        and argv[-1] == "-"
    )


def matching_worker_jobs(
    info: ProcessInfo,
    jobs: Iterable[JobRecord],
    config: GuardConfig,
    boot_seconds: int,
    ticks: int,
) -> list[JobRecord]:
    if not exact_worker_invocation(info):
        return []
    try:
        exec_index = info.cmdline.index("exec")
    except ValueError:
        return []
    tail = info.cmdline[exec_index + 1 :]
    model = option_value(tail, "-m")
    effort_values = {
        token.removeprefix('model_reasoning_effort="').removesuffix('"')
        for index, token in enumerate(tail)
        if index > 0
        and tail[index - 1] == "-c"
        and token.startswith('model_reasoning_effort="')
        and token.endswith('"')
    }
    if model is None or len(effort_values) != 1:
        return []
    started_ns = process_start_wall_ns(info, boot_seconds, ticks)
    if not config.race_start_ns - 2 * NS <= started_ns <= config.hard_stop_ns + 2 * NS:
        return []
    effort = next(iter(effort_values))
    return sorted(
        (
            job
            for job in jobs
            if job.active is not None
            and job.active.cgroup is None
            and job.active.cgroup_error is None
            and info.pgrp == job.active.pgid
            and info.session == job.active.pgid
            and job.requested_model == model
            and job.reasoning_effort == effort
            and job.launched_ns <= started_ns + 2 * NS
        ),
        key=lambda job: job.job_id,
    )


def parse_bootstrap_argv(
    active_schema: str,
    value: Any,
    target_argv: tuple[str, ...],
    config: GuardConfig,
) -> tuple[str, ...]:
    """Validate the trusted pre-exec gate command recorded by active v2."""
    if active_schema != "lean-eval-active-process-v2":
        raise GuardError("legacy active schema may not carry bootstrap_argv")
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) for item in value
    ):
        raise GuardError("bootstrap_argv must be a nonempty string list")
    expected_length = 10 + len(target_argv)
    if len(value) != expected_length:
        raise GuardError("bootstrap_argv has an unexpected length")
    interpreter = Path(value[0])
    try:
        resolved_interpreter = interpreter.resolve(strict=True)
        interpreter_matches = (
            interpreter.is_absolute()
            and resolved_interpreter.is_file()
            and os.access(resolved_interpreter, os.X_OK)
        )
    except OSError:
        interpreter_matches = False
    if not interpreter_matches:
        raise GuardError("bootstrap_argv interpreter is not an absolute executable file")
    if value[1:4] != [
        str((REPO / "scripts" / "speedrun.py").resolve()),
        "_exec_gate",
        "--release-fd",
    ]:
        raise GuardError("bootstrap_argv has an unexpected private command prefix")
    release_fd_text = value[4]
    if (
        not release_fd_text.isascii()
        or not release_fd_text.isdecimal()
        or str(int(release_fd_text)) != release_fd_text
        or not 2 < int(release_fd_text) <= 1_000_000
    ):
        raise GuardError("bootstrap_argv release fd is unsafe")
    if value[5:10] != [
        "--stop-file",
        str(config.raw_root / "STOP"),
        "--hard-stop-ns",
        str(config.hard_stop_ns),
        "--",
    ]:
        raise GuardError("bootstrap_argv is not bound to the configured hard stop")
    if tuple(value[10:]) != target_argv:
        raise GuardError("bootstrap_argv target differs from active argv")
    return tuple(value)


def active_argv_matches(info: ProcessInfo, active: ActiveRecord, job: JobRecord) -> bool:
    if active.bootstrap_argv is not None and info.cmdline == active.bootstrap_argv:
        try:
            return Path(info.exe).resolve(strict=True) == Path(
                active.bootstrap_argv[0]
            ).resolve(strict=True)
        except OSError:
            return False
    if info.cmdline == active.argv:
        return True
    recorded = active.argv
    if recorded[:4] == ("cx", "auto", "--", "exec"):
        try:
            recorded_exec = recorded.index("exec")
            actual_exec = info.cmdline.index("exec")
        except ValueError:
            return False
        if info.cmdline[actual_exec:] != recorded[recorded_exec:]:
            return False
        direct = actual_exec >= 1 and Path(info.cmdline[actual_exec - 1]).name == "cx"
        wrapped = (
            actual_exec >= 4
            and Path(info.cmdline[actual_exec - 3]).name == "cx-accounts"
            and info.cmdline[actual_exec - 2 : actual_exec] == ("run", "--")
        )
        return direct or wrapped
    if recorded == ("lake", "test"):
        return (
            len(info.cmdline) >= 2
            and Path(info.cmdline[-2]).name == "lake"
            and info.cmdline[-1] == "test"
            and info.cwd == str((REPO / "generated" / job.problem_id).resolve())
        )
    return False


def parse_cgroup_record(value: Any) -> CgroupRecord:
    if not isinstance(value, dict) or set(value) != {"path", "inode", "owner_uid"}:
        raise GuardError("cgroup evidence must have exactly path/inode/owner_uid")
    raw_path = value.get("path")
    inode = value.get("inode")
    owner_uid = value.get("owner_uid")
    if not isinstance(raw_path, str) or not raw_path:
        raise GuardError("cgroup evidence path is not a nonempty string")
    path = Path(raw_path)
    if (
        not path.is_absolute()
        or raw_path.startswith("//")
        or str(path) != raw_path
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise GuardError("cgroup evidence path is not canonical and absolute")
    if isinstance(inode, bool) or not isinstance(inode, int) or inode <= 0:
        raise GuardError("cgroup evidence inode is invalid")
    if isinstance(owner_uid, bool) or not isinstance(owner_uid, int) or owner_uid < 0:
        raise GuardError("cgroup evidence owner_uid is invalid")
    return CgroupRecord(path=path, inode=inode, owner_uid=owner_uid)


def load_jobs(config: GuardConfig) -> tuple[list[JobRecord], list[str]]:
    jobs: list[JobRecord] = []
    errors: list[str] = []
    if not config.raw_root.exists():
        return jobs, errors
    for directory in sorted(config.raw_root.iterdir(), key=lambda path: path.name):
        if directory.is_symlink() or not directory.is_dir() or directory.name.startswith("_"):
            continue
        launch_path = directory / "launch.json"
        if not launch_path.is_file() or launch_path.is_symlink():
            continue
        try:
            launch = read_json(launch_path)
            if not isinstance(launch, dict) or launch.get("schema") != "lean-eval-job-launch-v1":
                raise GuardError("unrecognized launch schema")
            job_id = launch.get("job_id")
            problem_id = launch.get("problem_id")
            controller_pid = launch.get("controller_pid")
            launched_ns = launch.get("started_wall_ns")
            requested_model = launch.get("requested_model")
            reasoning_effort = launch.get("reasoning_effort")
            if (
                not isinstance(job_id, str)
                or not JOB_ID_RE.fullmatch(job_id)
                or job_id != directory.name
                or not isinstance(problem_id, str)
                or not JOB_ID_RE.fullmatch(problem_id)
                or not isinstance(controller_pid, int)
                or controller_pid <= 1
                or not isinstance(launched_ns, int)
                or not isinstance(requested_model, str)
                or not requested_model
                or not isinstance(reasoning_effort, str)
                or not reasoning_effort
            ):
                raise GuardError("invalid launch identity/timing fields")
            active_path = directory / "active.json"
            active = None
            if active_path.exists():
                if active_path.is_symlink() or not active_path.is_file():
                    raise GuardError("unsafe active marker")
                active_value = read_json(active_path)
                active_schema = (
                    active_value.get("schema")
                    if isinstance(active_value, dict)
                    else None
                )
                bootstrap_present = (
                    isinstance(active_value, dict) and "bootstrap_argv" in active_value
                )
                bootstrap_value = (
                    active_value.get("bootstrap_argv")
                    if isinstance(active_value, dict)
                    else None
                )
                if (
                    not isinstance(active_value, dict)
                    or active_schema
                    not in {"lean-eval-active-process-v1", "lean-eval-active-process-v2"}
                    or isinstance(active_value.get("pid"), bool)
                    or not isinstance(active_value.get("pid"), int)
                    or active_value["pid"] <= 1
                    or isinstance(active_value.get("pgid"), bool)
                    or not isinstance(active_value.get("pgid"), int)
                    or active_value["pgid"] <= 1
                    or not isinstance(active_value.get("argv"), list)
                    or not active_value["argv"]
                    or not all(isinstance(item, str) for item in active_value["argv"])
                    or (
                        bootstrap_present
                        and (
                            not isinstance(bootstrap_value, list)
                            or not bootstrap_value
                            or not all(isinstance(item, str) for item in bootstrap_value)
                        )
                    )
                    or not isinstance(active_value.get("registered_at"), str)
                ):
                    raise GuardError("invalid active marker")
                bootstrap_argv = None
                if bootstrap_present:
                    bootstrap_argv = parse_bootstrap_argv(
                        active_schema,
                        bootstrap_value,
                        tuple(active_value["argv"]),
                        config,
                    )
                cgroup = None
                cgroup_error = None
                if active_schema == "lean-eval-active-process-v1":
                    if "cgroup" in active_value:
                        cgroup_error = "legacy active schema may not carry cgroup evidence"
                else:
                    try:
                        cgroup = parse_cgroup_record(active_value.get("cgroup"))
                    except GuardError as error:
                        cgroup_error = str(error)
                if cgroup_error is not None:
                    errors.append(f"{directory.name}: unsafe active evidence: {cgroup_error}")
                active = ActiveRecord(
                    path=active_path,
                    pid=active_value["pid"],
                    pgid=active_value["pgid"],
                    argv=tuple(active_value["argv"]),
                    registered_ns=parse_iso_ns(active_value["registered_at"]),
                    sha256=hash_file(active_path),
                    bootstrap_argv=bootstrap_argv,
                    cgroup=cgroup,
                    cgroup_error=cgroup_error,
                )
            jobs.append(
                JobRecord(
                    job_id=job_id,
                    problem_id=problem_id,
                    directory=directory,
                    controller_pid=controller_pid,
                    launched_ns=launched_ns,
                    requested_model=requested_model,
                    reasoning_effort=reasoning_effort,
                    active=active,
                    result_exists=(directory / "result.json").is_file(),
                )
            )
        except GuardError as error:
            errors.append(f"{directory.name}: {error}")
    return jobs, errors


def controller_matches(
    info: ProcessInfo,
    job: JobRecord,
    expected_uid: int,
    boot_seconds: int,
    ticks: int,
) -> bool:
    invocation = speedrun_invocation(info)
    if invocation is None:
        return False
    command, job_id, problem_id = invocation
    started_ns = process_start_wall_ns(info, boot_seconds, ticks)
    latest_start_ns = (
        job.active.registered_ns + 5 * NS
        if command == "verify" and job.active is not None
        else job.launched_ns + 5 * NS
    )
    return (
        info.uid == expected_uid
        and info.cwd == str(REPO.resolve())
        and job_id == job.job_id
        and problem_id == job.problem_id
        and job.launched_ns - 60 * NS <= started_ns <= latest_start_ns
    )


def active_matches(
    info: ProcessInfo,
    job: JobRecord,
    expected_uid: int,
    boot_seconds: int,
    ticks: int,
) -> bool:
    active = job.active
    if active is None:
        return False
    started_ns = process_start_wall_ns(info, boot_seconds, ticks)
    return (
        info.pid == active.pid
        and info.uid == expected_uid
        and info.pgrp == active.pgid == active.pid
        and info.session == active.pid
        and job.launched_ns - 5 * NS <= started_ns <= active.registered_ns + 5 * NS
        and active_argv_matches(info, active, job)
    )


def cgroup_creator_matches(
    creator_pid: int,
    job: JobRecord,
    table: dict[int, ProcessInfo],
    expected_uid: int,
    boot_seconds: int,
    ticks: int,
) -> bool:
    """Bind a cgroup name to the live controller that created it."""
    creator = table.get(creator_pid)
    if creator is None or not controller_matches(
        creator, job, expected_uid, boot_seconds, ticks
    ):
        return False
    active = job.active
    if active is None:
        return False
    active_info = table.get(active.pid)
    return (
        active_info is None
        or active_info.state == "Z"
        or active_info.ppid == creator_pid
    )


def descendants(table: dict[int, ProcessInfo], root: int) -> dict[int, int]:
    children: dict[int, list[int]] = {}
    for info in table.values():
        children.setdefault(info.ppid, []).append(info.pid)
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


def guardian_ancestors(table: dict[int, ProcessInfo]) -> set[int]:
    result = {os.getpid()}
    pid = os.getpid()
    while pid in table:
        parent = table[pid].ppid
        if parent <= 1 or parent in result:
            break
        result.add(parent)
        pid = parent
    return result


def collect_owned_targets(
    table: dict[int, ProcessInfo],
    jobs: list[JobRecord],
    config: GuardConfig,
    expected_uid: int,
    boot_seconds: int,
    ticks: int,
) -> tuple[dict[tuple[int, int], OwnedTarget], list[str], list[str]]:
    targets: dict[tuple[int, int], OwnedTarget] = {}
    errors: list[str] = []
    validated_jobs: set[str] = set()
    excluded = guardian_ancestors(table)

    def add_tree(root: ProcessInfo, job_ids: set[str], role: str) -> None:
        for pid, depth in descendants(table, root.pid).items():
            info = table[pid]
            if pid in excluded or info.uid != expected_uid:
                if pid != root.pid:
                    errors.append(
                        f"{','.join(sorted(job_ids))}: descendant {pid} "
                        "failed uid/self safety check"
                    )
                continue
            key = (pid, info.start_ticks)
            target = targets.setdefault(key, OwnedTarget(info=info, depth=depth))
            target.depth = max(target.depth, depth)
            target.jobs.update(job_ids)
            target.roles.add(role if pid == root.pid else "descendant")
            if exact_worker_invocation(info):
                target.roles.add("detached-codex-worker")

    for job in jobs:
        roots: dict[int, tuple[ProcessInfo, str]] = {}
        for candidate_pid in {job.controller_pid, job.active.pid if job.active else -1}:
            info = table.get(candidate_pid)
            if info is None:
                continue
            if controller_matches(info, job, expected_uid, boot_seconds, ticks):
                roots[info.pid] = (info, "controller")
            elif active_matches(info, job, expected_uid, boot_seconds, ticks):
                roots[info.pid] = (info, "registered-active")
            elif candidate_pid > 1:
                errors.append(
                    f"{job.job_id}: live recorded PID {candidate_pid} "
                    "failed identity validation"
                )

        if job.active is not None:
            active_info = table.get(job.active.pid)
            if active_info is not None and active_matches(
                active_info, job, expected_uid, boot_seconds, ticks
            ):
                roots[active_info.pid] = (active_info, "registered-active")
                parent_info = table.get(active_info.ppid)
                if parent_info is not None and controller_matches(
                    parent_info, job, expected_uid, boot_seconds, ticks
                ):
                    roots[parent_info.pid] = (parent_info, "controller")

        for info in table.values():
            invocation = speedrun_invocation(info)
            if invocation is None or invocation[1:] != (job.job_id, job.problem_id):
                continue
            if controller_matches(info, job, expected_uid, boot_seconds, ticks):
                roots[info.pid] = (info, "controller")

        if roots:
            validated_jobs.add(job.job_id)
            for root, role in roots.values():
                add_tree(root, {job.job_id}, role)
        elif job.active is not None:
            errors.append(f"{job.job_id}: active marker has no live, validated process root")

    for info in table.values():
        matches = matching_worker_jobs(info, jobs, config, boot_seconds, ticks)
        if not matches:
            continue
        key = (info.pid, info.start_ticks)
        if key not in targets:
            match_ids = {job.job_id for job in matches}
            add_tree(info, match_ids, "validated-orphan-codex-worker")

    return targets, errors, sorted(validated_jobs)


def cgroup_population_consistent(populated: int, members: dict[int, Path]) -> bool:
    """Relate subtree-wide populated state to recursively collected members."""
    return (populated == 0 and not members) or (populated == 1 and bool(members))


def scan_cgroup_subtree(target: CgroupTarget) -> tuple[dict[int, Path], list[Path]]:
    """Safely and boundedly collect every cgroup.procs entry below target."""
    root_metadata = os.fstat(target.directory_fd)
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_dev != target.device
        or root_metadata.st_ino != target.record.inode
        or root_metadata.st_uid != target.record.owner_uid
    ):
        raise GuardError("recorded cgroup root identity changed during traversal")

    members: dict[int, Path] = {}
    groups: list[Path] = []
    identities: set[tuple[int, int]] = set()

    def visit(
        directory_fd: int,
        relative_parts: tuple[str, ...],
        metadata: os.stat_result,
        depth: int,
        procs_fd: int | None = None,
    ) -> None:
        if depth > CGROUP_MAX_DEPTH:
            raise GuardError("recorded cgroup subtree exceeds the depth limit")
        if len(groups) >= CGROUP_MAX_GROUPS:
            raise GuardError("recorded cgroup subtree exceeds the group-count limit")
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in identities:
            raise GuardError("recorded cgroup subtree repeats a directory identity")
        identities.add(identity)
        group_path = target.record.path.joinpath(*relative_parts)
        groups.append(group_path)

        own_procs_fd = procs_fd
        close_procs = False
        if own_procs_fd is None:
            own_procs_fd = open_cgroup_control(directory_fd, "cgroup.procs", os.O_RDONLY)
            close_procs = True
        if own_procs_fd is None:
            raise GuardError("internal cgroup.procs descriptor invariant failed")
        try:
            for pid in parse_cgroup_procs(
                read_descriptor(own_procs_fd, f"{group_path}/cgroup.procs")
            ):
                if pid in members:
                    raise GuardError(f"PID {pid} appeared in multiple cgroup.procs files")
                if len(members) >= CGROUP_MAX_PIDS:
                    raise GuardError("recorded cgroup subtree exceeds the PID-count limit")
                members[pid] = group_path
        finally:
            if close_procs:
                os.close(own_procs_fd)

        children: list[tuple[str, os.stat_result]] = []
        for name in sorted(os.listdir(directory_fd)):
            child_metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(child_metadata.st_mode):
                raise GuardError(
                    f"recorded cgroup subtree contains a symlink: {group_path / name}"
                )
            if stat.S_ISDIR(child_metadata.st_mode):
                children.append((name, child_metadata))
            elif not stat.S_ISREG(child_metadata.st_mode):
                raise GuardError(
                    f"recorded cgroup subtree contains an unsafe entry: {group_path / name}"
                )

        for name, expected_metadata in children:
            child_fd = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                child_metadata = os.fstat(child_fd)
                if (
                    not stat.S_ISDIR(child_metadata.st_mode)
                    or (child_metadata.st_dev, child_metadata.st_ino)
                    != (expected_metadata.st_dev, expected_metadata.st_ino)
                    or child_metadata.st_dev != target.device
                    or child_metadata.st_uid != target.record.owner_uid
                ):
                    raise GuardError(
                        f"recorded descendant cgroup identity is unsafe: {group_path / name}"
                    )
                visit(
                    child_fd,
                    (*relative_parts, name),
                    child_metadata,
                    depth + 1,
                )
            finally:
                os.close(child_fd)

    visit(target.directory_fd, (), root_metadata, 0, target.procs_fd)
    return members, groups


def cgroup_snapshot(target: CgroupTarget) -> tuple[int, dict[int, Path], list[Path]]:
    """Read a stable subtree-wide populated/procs snapshot."""
    for _ in range(4):
        before = parse_cgroup_events(
            read_descriptor(target.events_fd, f"{target.record.path}/cgroup.events")
        )["populated"]
        members, groups = scan_cgroup_subtree(target)
        after = parse_cgroup_events(
            read_descriptor(target.events_fd, f"{target.record.path}/cgroup.events")
        )["populated"]
        if before == after and cgroup_population_consistent(after, members):
            return after, members, groups
        time.sleep(0.001)
    raise GuardError(
        "cgroup.events populated state disagrees with recursively scanned cgroup.procs"
    )


def process_cgroup_path(pid: int, mounts: Iterable[CgroupMount]) -> Path:
    return map_unified_cgroup(read_unified_cgroup(pid), mounts)


def open_validated_cgroup(
    parent_fd: int,
    delegated: Path,
    mounts: list[CgroupMount],
    job: JobRecord,
    excluded: set[int],
    expected_uid: int,
) -> CgroupTarget:
    active = job.active
    if active is None or active.cgroup is None:
        raise GuardError("internal cgroup validation requested without cgroup evidence")
    record = active.cgroup
    name_match = CGROUP_NAME_RE.fullmatch(record.path.name)
    if (
        record.path.parent != delegated
        or name_match is None
    ):
        raise GuardError(
            "recorded cgroup is not the expected direct lean-eval child of the delegated cgroup"
        )
    creator_pid = int(name_match.group(1))
    if record.owner_uid != expected_uid:
        raise GuardError("recorded cgroup owner does not match guardian uid")

    directory_fd = -1
    events_fd = -1
    kill_fd = -1
    procs_fd = -1
    try:
        directory_fd = os.open(
            record.path.name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        metadata = os.fstat(directory_fd)
        parent_metadata = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != parent_metadata.st_dev
            or metadata.st_ino != record.inode
            or metadata.st_uid != record.owner_uid
        ):
            raise GuardError("recorded cgroup inode/type/owner identity changed")
        events_fd = open_cgroup_control(directory_fd, "cgroup.events", os.O_RDONLY)
        procs_fd = open_cgroup_control(directory_fd, "cgroup.procs", os.O_RDONLY)
        kill_fd = open_cgroup_control(directory_fd, "cgroup.kill", os.O_WRONLY)
        target = CgroupTarget(
            job_id=job.job_id,
            record=record,
            directory_fd=directory_fd,
            events_fd=events_fd,
            kill_fd=kill_fd,
            procs_fd=procs_fd,
            device=metadata.st_dev,
            initial_populated=-1,
            initial_pids=[],
            initial_cgroups=[],
            empty_proven=False,
            empty_proof_wall_ns=None,
        )
        populated, members, groups = cgroup_snapshot(target)
        pids = sorted(members)
        association_table = process_snapshot()
        current_excluded = excluded | guardian_ancestors(association_table)
        if not cgroup_creator_matches(
            creator_pid,
            job,
            association_table,
            expected_uid,
            boot_wall_seconds(),
            clock_ticks(),
        ):
            raise GuardError(
                "recorded cgroup name is not bound to a validated live job controller"
            )
        validated_infos: dict[int, ProcessInfo] = {}
        for pid, member_path in sorted(members.items()):
            if pid in current_excluded:
                raise GuardError(f"cgroup subtree contains guardian/self ancestor PID {pid}")
            info = association_table.get(pid)
            if info is None or info.state == "Z":
                raise GuardError(f"cannot validate live cgroup subtree PID {pid}")
            if info.uid != expected_uid:
                raise GuardError(f"cgroup subtree PID {pid} has a foreign uid")
            if process_cgroup_path(pid, mounts) != member_path:
                raise GuardError(f"cgroup subtree PID {pid} has mismatched /proc membership")
            validated_infos[pid] = info

        live_active = association_table.get(active.pid)
        if live_active is not None and live_active.state != "Z":
            if not active_matches(
                live_active,
                job,
                expected_uid,
                boot_wall_seconds(),
                clock_ticks(),
            ):
                raise GuardError("live active PID failed exact identity validation")
            if active.pid not in members:
                raise GuardError("live active PID is not in the recorded cgroup subtree")
            allowed = set(descendants(association_table, active.pid))
        else:
            worker_roots = {
                pid for pid, info in validated_infos.items() if exact_worker_invocation(info)
            }
            allowed = set()
            for worker_pid in worker_roots:
                allowed.update(descendants(association_table, worker_pid))

        for pid in pids:
            if pid not in allowed:
                if live_active is not None and live_active.state != "Z":
                    raise GuardError(
                        f"cgroup subtree PID {pid} is outside active-process ancestry"
                    )
                raise GuardError(
                    f"orphaned cgroup subtree PID {pid} is outside exact-worker ancestry"
                )

        target.initial_populated = populated
        target.initial_pids = pids
        target.initial_cgroups = [str(path) for path in groups]
        if populated == 0:
            target.empty_proven = True
            target.empty_proof_wall_ns = time.time_ns()
        return target
    except (OSError, GuardError) as error:
        for descriptor in (procs_fd, kill_fd, events_fd, directory_fd):
            if descriptor >= 0:
                os.close(descriptor)
        if isinstance(error, GuardError):
            raise
        raise GuardError(f"cannot safely open recorded cgroup: {error}") from error


def collect_cgroup_targets(
    jobs: list[JobRecord],
    table: dict[int, ProcessInfo],
    expected_uid: int,
) -> tuple[list[CgroupTarget], list[str], Path | None]:
    errors: list[str] = []
    candidates = [
        job
        for job in jobs
        if job.active is not None and job.active.cgroup is not None
    ]
    if not candidates:
        return [], errors, None
    delegated, mounts = current_delegated_cgroup()
    path_counts: dict[Path, int] = {}
    for job in candidates:
        assert job.active is not None and job.active.cgroup is not None
        path_counts[job.active.cgroup.path] = path_counts.get(job.active.cgroup.path, 0) + 1

    parent_fd = -1
    targets: list[CgroupTarget] = []
    try:
        parent_fd = os.open(
            delegated,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        parent_metadata = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_metadata.st_mode) or parent_metadata.st_uid != expected_uid:
            raise GuardError("current delegated cgroup directory has an unsafe owner/type")
        excluded = guardian_ancestors(table)
        for job in candidates:
            assert job.active is not None and job.active.cgroup is not None
            record = job.active.cgroup
            if path_counts[record.path] != 1:
                errors.append(f"{job.job_id}: recorded cgroup path is shared by multiple jobs")
                continue
            try:
                targets.append(
                    open_validated_cgroup(
                        parent_fd,
                        delegated,
                        mounts,
                        job,
                        excluded,
                        expected_uid,
                    )
                )
            except GuardError as error:
                errors.append(f"{job.job_id}: unsafe or mismatched cgroup evidence: {error}")
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)

    identity_counts: dict[tuple[int, int], int] = {}
    for target in targets:
        identity = (target.device, target.record.inode)
        identity_counts[identity] = identity_counts.get(identity, 0) + 1
    unique: list[CgroupTarget] = []
    for target in targets:
        if identity_counts[(target.device, target.record.inode)] != 1:
            errors.append(f"{target.job_id}: recorded cgroup inode is shared by multiple jobs")
            target.close()
        else:
            unique.append(target)
    return unique, errors, delegated


def kill_cgroup(target: CgroupTarget) -> str:
    if target.initial_populated == 0:
        return "already-empty"
    try:
        written = os.write(target.kill_fd, b"1")
        if written != 1:
            return "short-write"
        return "cgroup-kill-written"
    except PermissionError:
        return "permission-denied"
    except OSError as error:
        return f"os-error-{error.errno}"


def cgroup_path_absent(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def hash_safe_regular_file(path: Path) -> str | None:
    """Hash one stable, non-symlink regular file through its opened inode."""
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            return None
        digest = hashlib.sha256()
        length = 0
        while True:
            block = os.read(descriptor, 65536)
            if not block:
                break
            length += len(block)
            if length > CGROUP_READ_LIMIT:
                return None
            digest.update(block)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(before, field) != getattr(after, field)
            or getattr(after, field) != getattr(current, field)
            for field in stable_fields
        ):
            return None
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def completed_transition_facts_valid(
    *,
    active_path_absent: bool,
    process_sha256: str | None,
    expected_sha256: str,
    active_gone_or_zombie: bool,
    cgroup_absent: bool,
) -> bool:
    return (
        active_path_absent
        and process_sha256 == expected_sha256
        and active_gone_or_zombie
        and cgroup_absent
    )


def process_gone_or_zombie(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
        parsed_pid, _, _, _, state, _ = parse_proc_stat(raw)
    except (FileNotFoundError, ProcessLookupError):
        return True
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return parsed_pid == pid and state == "Z"


def controller_completed_empty_transition(
    active: ActiveRecord, target: CgroupTarget
) -> bool:
    return completed_transition_facts_valid(
        active_path_absent=cgroup_path_absent(active.path),
        process_sha256=hash_safe_regular_file(active.path.with_name("process.json")),
        expected_sha256=active.sha256,
        active_gone_or_zombie=process_gone_or_zombie(active.pid),
        cgroup_absent=cgroup_path_absent(target.record.path),
    )


def record_empty_proof(target: CgroupTarget) -> None:
    target.empty_proven = True
    target.empty_proof_wall_ns = time.time_ns()


def wait_cgroups_empty(
    targets: Iterable[CgroupTarget],
    timeout_seconds: float,
    active_records: dict[str, ActiveRecord] | None = None,
) -> dict[str, dict[str, Any]]:
    targets = list(targets)
    deadline = time.monotonic() + timeout_seconds
    observations: dict[str, dict[str, Any]] = {}
    while True:
        remaining = False
        observations = {}
        for target in targets:
            try:
                populated, members, _ = cgroup_snapshot(target)
                if populated == 0:
                    record_empty_proof(target)
                observations[target.job_id] = {
                    "pids": sorted(members),
                    "populated": populated,
                }
                remaining = remaining or populated != 0 or bool(members)
            except GuardError as error:
                active = (
                    active_records.get(target.job_id)
                    if active_records is not None
                    else None
                )
                if (
                    not target.empty_proven
                    and active is not None
                    and controller_completed_empty_transition(active, target)
                ):
                    record_empty_proof(target)
                if target.empty_proven and cgroup_path_absent(target.record.path):
                    observations[target.job_id] = {
                        "empty_proof_wall_ns": target.empty_proof_wall_ns,
                        "pids": [],
                        "populated": 0,
                        "removed_after_empty_proof": True,
                    }
                else:
                    observations[target.job_id] = {"error": str(error)}
                    remaining = True
        if not remaining or time.monotonic() >= deadline:
            return observations
        time.sleep(0.01)


def cmdline_hash(info: ProcessInfo) -> str:
    return hash_bytes(
        b"\0".join(
            item.encode("utf-8", errors="surrogateescape") for item in info.cmdline
        )
    )


def cwd_scope(info: ProcessInfo) -> str:
    repo = str(REPO.resolve())
    if info.cwd == repo:
        return "repository"
    if info.cwd.startswith(repo + os.sep):
        return "repository-descendant"
    return "external-descendant"


def target_evidence(target: OwnedTarget) -> dict[str, Any]:
    info = target.info
    return {
        "cmdline_sha256": cmdline_hash(info),
        "cwd_scope": cwd_scope(info),
        "depth": target.depth,
        "exe_basename": Path(info.exe).name,
        "jobs": sorted(target.jobs),
        "pgrp": info.pgrp,
        "pid": info.pid,
        "ppid": info.ppid,
        "roles": sorted(target.roles),
        "session": info.session,
        "start_ticks": info.start_ticks,
    }


def signal_exact(target: OwnedTarget, excluded: set[int]) -> str:
    expected = target.info
    if expected.pid in excluded or expected.pid <= 1:
        return "refused-self-or-ancestor"
    pidfd = None
    try:
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            return "pidfd-unavailable"
        pidfd = os.pidfd_open(expected.pid, 0)
        current = read_process(expected.pid)
        if current is None or current.state == "Z":
            return "already-gone"
        if current.start_ticks != expected.start_ticks or current.uid != expected.uid:
            return "identity-changed"
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        return "sigkill-sent"
    except ProcessLookupError:
        return "already-gone"
    except PermissionError:
        return "permission-denied"
    except OSError as error:
        if error.errno == errno.ESRCH:
            return "already-gone"
        return f"os-error-{error.errno}"
    finally:
        if pidfd is not None:
            os.close(pidfd)


def uptime_ticks(ticks: int) -> int:
    try:
        text = Path("/proc/uptime").read_text(encoding="ascii").split()[0]
        whole, _, fraction = text.partition(".")
        numerator = int(whole) * ticks
        if fraction:
            numerator += int(fraction) * ticks // (10 ** len(fraction))
        return numerator
    except (OSError, ValueError, IndexError) as error:
        raise GuardError(f"cannot read kernel uptime: {error}") from error


def residual_targets(
    table: dict[int, ProcessInfo],
    known: dict[tuple[int, int], OwnedTarget],
    known_pgrps: set[int],
    known_sessions: set[int],
    expected_uid: int,
    excluded: set[int],
    scan_start_ticks: int,
    lineage_latest_ticks: int,
) -> dict[tuple[int, int], OwnedTarget]:
    residual: dict[tuple[int, int], OwnedTarget] = {}
    for info in table.values():
        if info.pid in excluded or info.pid <= 1 or info.uid != expected_uid or info.state == "Z":
            continue
        key = (info.pid, info.start_ticks)
        previous = known.get(key)
        reason = None
        parent = table.get(info.ppid)
        parent_is_owned = (
            parent is not None and (parent.pid, parent.start_ticks) in known
        )
        group_leader = table.get(info.pgrp)
        group_identity_is_owned = (
            group_leader is None
            or (group_leader.pid, group_leader.start_ticks) in known
        )
        session_leader = table.get(info.session)
        session_identity_is_owned = (
            session_leader is None
            or (session_leader.pid, session_leader.start_ticks) in known
        )
        if previous is not None:
            reason = "identity-survivor"
        elif (
            info.start_ticks <= lineage_latest_ticks
            and (
                parent_is_owned
                or (info.pgrp in known_pgrps and group_identity_is_owned)
                or (info.session in known_sessions and session_identity_is_owned)
            )
        ):
            reason = "owned-lineage-rescan"
        elif (
            scan_start_ticks - 1 <= info.start_ticks <= lineage_latest_ticks
            and exact_worker_invocation(info)
        ):
            reason = "deadline-race-codex-worker"
        if reason is None:
            continue
        target = OwnedTarget(info=info, roles={reason}, jobs=set(), depth=0)
        if previous is not None:
            target.roles.update(previous.roles)
            target.jobs.update(previous.jobs)
            target.depth = previous.depth
        residual[key] = target
    return residual


def rename_active_markers(jobs: Iterable[JobRecord]) -> tuple[list[str], list[str]]:
    renamed: list[str] = []
    errors: list[str] = []
    for job in sorted(jobs, key=lambda item: item.job_id):
        active = job.active
        if active is None or not active.path.exists():
            continue
        try:
            live = read_process(active.pid)
            if live is not None and live.state != "Z":
                errors.append(
                    f"{job.job_id}: active process is still live; marker was deliberately retained"
                )
                continue
            if active.path.is_symlink() or hash_file(active.path) != active.sha256:
                errors.append(f"{job.job_id}: active marker changed during enforcement")
                continue
            destination = active.path.with_name("killed-at-hard-stop.json")
            if destination.is_symlink():
                errors.append(f"{job.job_id}: unsafe killed-at-hard-stop evidence path")
                continue
            if destination.exists():
                if hash_file(destination) == active.sha256:
                    active.path.unlink(missing_ok=True)
                    renamed.append(str(destination.relative_to(job.directory.parent)))
                else:
                    errors.append(f"{job.job_id}: conflicting killed-at-hard-stop evidence")
                continue
            os.replace(active.path, destination)
            fsync_directory(destination.parent)
            renamed.append(str(destination.relative_to(job.directory.parent)))
        except OSError as error:
            errors.append(f"{job.job_id}: cannot preserve active marker: {error}")
    return renamed, errors


def validate_stop(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if (
        not isinstance(value, dict)
        or value.get("schema") != STOP_SCHEMA
        or not isinstance(value.get("at"), str)
        or not isinstance(value.get("reason"), str)
    ):
        raise GuardError(f"invalid existing STOP evidence: {path}")
    parse_iso_ns(value["at"])
    return value


def publish_stop(config: GuardConfig, trigger_wall_ns: int) -> tuple[dict[str, Any], bool]:
    path = config.raw_root / "STOP"
    value = {
        "at": iso_from_ns(trigger_wall_ns),
        "reason": "configured hard stop reached (exact guardian)",
        "schema": STOP_SCHEMA,
    }
    created = create_json_exclusive(path, value)
    if not created and path.is_symlink():
        raise GuardError(f"existing STOP is an unsafe symlink: {path}")
    return (value if created else validate_stop(path)), created


def enforce_hard_stop(config: GuardConfig, state: dict[str, Any]) -> dict[str, Any]:
    trigger_wall_ns = time.time_ns()
    if trigger_wall_ns < config.hard_stop_ns:
        raise GuardError("internal refusal: hard-stop enforcement was invoked before deadline")
    trigger_mono_ns = time.monotonic_ns()
    stop_errors: list[str] = []
    try:
        stop_value, stop_created = publish_stop(config, trigger_wall_ns)
    except GuardError as error:
        stop_path = config.raw_root / "STOP"
        if stop_path.is_symlink():
            stop_value = {
                "path_type": "symlink",
                "schema": "unsafe-existing-stop-evidence",
            }
        elif stop_path.is_file():
            stop_value = {
                "invalid_existing_stop_sha256": hash_file(stop_path),
                "schema": "invalid-existing-stop-evidence",
            }
        else:
            raise
        stop_created = False
        stop_errors.append(str(error))
    expected_uid = os.getuid()
    boot_seconds = boot_wall_seconds()
    ticks = clock_ticks()
    initial_scan_tick = uptime_ticks(ticks)
    lineage_latest_tick = initial_scan_tick + max(2, ticks // 5)
    table = process_snapshot()
    jobs, evidence_errors = load_jobs(config)
    evidence_errors.extend(stop_errors)
    targets, ownership_errors, validated_jobs = collect_owned_targets(
        table, jobs, config, expected_uid, boot_seconds, ticks
    )
    evidence_errors.extend(ownership_errors)
    excluded = guardian_ancestors(table)

    cgroup_targets: list[CgroupTarget] = []
    delegated_cgroup: Path | None = None
    try:
        cgroup_targets, cgroup_errors, delegated_cgroup = collect_cgroup_targets(
            jobs, table, expected_uid
        )
        evidence_errors.extend(cgroup_errors)
    except GuardError as error:
        evidence_errors.append(f"cannot validate recorded cgroups: {error}")
    jobs_by_id = {job.job_id: job for job in jobs}
    cgroup_attempts: list[dict[str, Any]] = []
    for target in sorted(cgroup_targets, key=lambda item: item.job_id):
        active = jobs_by_id[target.job_id].active
        assert active is not None
        kill_wall_ns = time.time_ns()
        try:
            marker_unchanged = (
                not active.path.is_symlink()
                and active.path.is_file()
                and hash_file(active.path) == active.sha256
            )
        except GuardError:
            marker_unchanged = False
        if not marker_unchanged:
            try:
                current_populated, current_members, _ = cgroup_snapshot(target)
            except GuardError:
                if target.empty_proven and cgroup_path_absent(target.record.path):
                    outcome = "empty-after-marker-change"
                elif controller_completed_empty_transition(active, target):
                    outcome = "empty-after-controller-transition"
                    record_empty_proof(target)
                else:
                    outcome = "active-marker-changed"
            else:
                outcome = (
                    "empty-after-marker-change"
                    if current_populated == 0 and not current_members
                    else "active-marker-changed"
                )
                if outcome == "empty-after-marker-change":
                    record_empty_proof(target)
        else:
            outcome = kill_cgroup(target)
        if (
            outcome not in CGROUP_KILL_SUCCESS_OUTCOMES
            and controller_completed_empty_transition(active, target)
        ):
            outcome = "empty-after-controller-transition"
            record_empty_proof(target)
        cgroup_attempts.append(
            {
                "empty_proof_wall_ns": target.empty_proof_wall_ns,
                "initial_cgroups": target.initial_cgroups,
                "initial_pids": target.initial_pids,
                "initial_populated": target.initial_populated,
                "inode": target.record.inode,
                "job_id": target.job_id,
                "kill_offset_ns": kill_wall_ns - trigger_wall_ns,
                "kill_wall_ns": kill_wall_ns,
                "outcome": outcome,
                "owner_uid": target.record.owner_uid,
                "path": str(target.record.path),
            }
        )

    ordered = sorted(
        targets.values(),
        key=lambda target: (
            0 if target.roles.intersection({"controller", "registered-active"}) else 1,
            -target.depth,
            target.info.pid,
        ),
    )
    attempts: list[dict[str, Any]] = []
    for target in ordered:
        signal_wall_ns = time.time_ns()
        outcome = signal_exact(target, excluded)
        attempts.append(
            {
                "outcome": outcome,
                "signal_offset_ns": signal_wall_ns - trigger_wall_ns,
                "signal_wall_ns": signal_wall_ns,
                **target_evidence(target),
            }
        )

    known = dict(targets)
    known_pgrps = {
        target.info.pgrp for target in targets.values() if target.info.pgrp > 1
    }
    known_sessions = {
        target.info.session for target in targets.values() if target.info.session > 1
    }
    empty_scans = 0
    rescan_rounds = 0
    rescan_deadline = time.monotonic() + 2.0
    final_residual: dict[tuple[int, int], OwnedTarget] = {}
    while empty_scans < 2 and time.monotonic() < rescan_deadline:
        time.sleep(0.02)
        rescan_rounds += 1
        current_table = process_snapshot()
        current_excluded = guardian_ancestors(current_table)
        residual = residual_targets(
            current_table,
            known,
            known_pgrps,
            known_sessions,
            expected_uid,
            current_excluded,
            initial_scan_tick,
            lineage_latest_tick,
        )
        if not residual:
            empty_scans += 1
            final_residual = {}
            continue
        empty_scans = 0
        final_residual = residual
        for key, target in sorted(residual.items()):
            signal_wall_ns = time.time_ns()
            outcome = signal_exact(target, current_excluded)
            attempts.append(
                {
                    "outcome": outcome,
                    "rescan_round": rescan_rounds,
                    "signal_offset_ns": signal_wall_ns - trigger_wall_ns,
                    "signal_wall_ns": signal_wall_ns,
                    **target_evidence(target),
                }
            )
            known[key] = target
            known_pgrps.add(target.info.pgrp)
            known_sessions.add(target.info.session)

    attempts_by_job = {row["job_id"]: row for row in cgroup_attempts}
    cgroup_active_records: dict[str, ActiveRecord] = {}
    for target in cgroup_targets:
        active = jobs_by_id[target.job_id].active
        assert active is not None
        cgroup_active_records[target.job_id] = active
        if controller_completed_empty_transition(active, target):
            record_empty_proof(target)
            row = attempts_by_job[target.job_id]
            row["empty_proof_wall_ns"] = target.empty_proof_wall_ns
            if row["outcome"] not in CGROUP_KILL_SUCCESS_OUTCOMES:
                row["outcome"] = "empty-after-controller-transition"
                row["reconciled_after_signal_rescan"] = True

    cgroup_final = wait_cgroups_empty(
        cgroup_targets,
        CGROUP_SETTLE_SECONDS,
        cgroup_active_records,
    )
    for target in cgroup_targets:
        row = attempts_by_job[target.job_id]
        active = cgroup_active_records[target.job_id]
        if (
            row["outcome"] not in CGROUP_KILL_SUCCESS_OUTCOMES
            and controller_completed_empty_transition(active, target)
        ):
            record_empty_proof(target)
            row["empty_proof_wall_ns"] = target.empty_proof_wall_ns
            row["outcome"] = "empty-after-controller-transition"
            row["reconciled_during_cgroup_wait"] = True
        if row["outcome"] not in CGROUP_KILL_SUCCESS_OUTCOMES:
            evidence_errors.append(
                f"{row['job_id']}: atomic cgroup.kill attempt failed: {row['outcome']}"
            )

    for target in cgroup_targets:
        observation = cgroup_final.get(target.job_id, {})
        if observation.get("populated") != 0 or observation.get("pids") != []:
            detail = observation.get("error", json.dumps(observation, sort_keys=True))
            evidence_errors.append(
                f"{target.job_id}: cgroup.events did not reach populated=0: {detail}"
            )
        target.close()

    renamed, marker_errors = rename_active_markers(jobs)
    evidence_errors.extend(marker_errors)
    final_table = process_snapshot()
    unowned_workers = sorted(
        info.pid
        for info in final_table.values()
        if exact_worker_invocation(info)
        and config.race_start_ns
        <= process_start_wall_ns(info, boot_seconds, ticks)
        <= trigger_wall_ns
        and (info.pid, info.start_ticks) not in known
    )
    if unowned_workers:
        evidence_errors.append(
            "exact worker-shaped processes remained but lacked validated controller ancestry: "
            + ",".join(str(pid) for pid in unowned_workers)
        )
    failed_signals = sorted(
        {
            f"pid={row['pid']} outcome={row['outcome']}"
            for row in attempts
            if row["outcome"] not in {"sigkill-sent", "already-gone"}
        }
    )
    if failed_signals:
        evidence_errors.append(
            "one or more exact signal attempts failed: " + "; ".join(failed_signals)
        )
    if final_residual or empty_scans < 2:
        evidence_errors.append("owned process tree did not reach two stable empty rescans")

    return {
        "active_markers_preserved": renamed,
        "cgroup_final": cgroup_final,
        "cgroup_kill_attempts": cgroup_attempts,
        "delegated_cgroup": str(delegated_cgroup) if delegated_cgroup is not None else None,
        "complete": not evidence_errors,
        "config_sha256": config.sha256,
        "errors": sorted(set(evidence_errors)),
        "guardian_pid": os.getpid(),
        "hard_stop": config.hard_stop_text,
        "hard_stop_ns": config.hard_stop_ns,
        "initial_target_count": len(targets),
        "jobs_seen": [job.job_id for job in jobs],
        "rescan_rounds": rescan_rounds,
        "schema": RESULT_SCHEMA,
        "signal_attempts": attempts,
        "stable_empty_scans": empty_scans,
        "state_sha256": hash_bytes(canonical_bytes(state)),
        "stop": stop_value,
        "stop_created_by_guardian": stop_created,
        "trigger_monotonic_ns": trigger_mono_ns,
        "trigger_wall_ns": trigger_wall_ns,
        "triggered_at": iso_from_ns(trigger_wall_ns),
        "unowned_worker_pids": unowned_workers,
        "validated_jobs": validated_jobs,
    }


def state_paths(config: GuardConfig) -> tuple[Path, Path]:
    return config.raw_root / STATE_NAME, config.raw_root / RESULT_NAME


def guardian_identity(state: dict[str, Any], config: GuardConfig) -> ProcessInfo | None:
    pid = state.get("pid")
    start_ticks = state.get("start_ticks")
    token = state.get("arm_token")
    if not isinstance(pid, int) or not isinstance(start_ticks, int) or not isinstance(token, str):
        return None
    info = read_process(pid)
    if info is None or info.start_ticks != start_ticks or info.uid != os.getuid():
        return None
    if info.cwd != str(REPO.resolve()):
        return None
    invocation = list(info.cmdline)
    try:
        script_index = next(
            index
            for index, item in enumerate(invocation)
            if Path(item).resolve() == SCRIPT
        )
    except (StopIteration, OSError):
        return None
    expected_tail = [
        "_run",
        "--config",
        str(config.path),
        "--arm-token",
        token,
    ]
    if invocation[script_index + 1 :] != expected_tail:
        return None
    return info


def wait_until_deadline(hard_stop_ns: int) -> None:
    while True:
        now = time.time_ns()
        remaining = hard_stop_ns - now
        if remaining <= 0:
            return
        if remaining > NS:
            time.sleep(min((remaining - 250_000_000) / NS, 30.0))
        elif remaining > 5_000_000:
            time.sleep(max((remaining - 1_000_000) / NS, 0.0005))
        else:
            time.sleep(min(remaining / NS, 0.001))


def command_run(args: argparse.Namespace) -> int:
    try:
        handshake = os.read(0, 1)
    except OSError as error:
        raise GuardError(f"guardian arm handshake failed: {error}") from error
    if handshake != b"1":
        raise GuardError("guardian arm handshake was not completed")
    require_pidfd_support()
    config = load_config(Path(args.config))
    state_path, result_path = state_paths(config)
    if state_path.is_symlink() or not state_path.is_file():
        raise GuardError(f"guardian state is missing or unsafe: {state_path}")
    state = read_json(state_path)
    if (
        not isinstance(state, dict)
        or state.get("schema") != STATE_SCHEMA
        or state.get("arm_token") != args.arm_token
        or state.get("pid") != os.getpid()
        or state.get("boot_id") != boot_id()
        or state.get("config_sha256") != config.sha256
        or state.get("hard_stop_ns") != config.hard_stop_ns
    ):
        raise GuardError("guardian state/identity handshake failed")
    own = read_process(os.getpid())
    if own is None or state.get("start_ticks") != own.start_ticks:
        raise GuardError("guardian /proc start identity does not match armed state")
    wait_until_deadline(config.hard_stop_ns)
    if time.time_ns() < config.hard_stop_ns:
        raise GuardError("guardian woke before hard stop")
    try:
        result = enforce_hard_stop(config, state)
    except Exception as error:
        failure_wall_ns = time.time_ns()
        stop_summary: dict[str, Any]
        try:
            stop_value, stop_created = publish_stop(config, failure_wall_ns)
            stop_summary = {"created": stop_created, "value": stop_value}
        except Exception as stop_error:
            stop_summary = {
                "created": False,
                "error": f"{type(stop_error).__name__}: {stop_error}",
            }
        result = {
            "complete": False,
            "config_sha256": config.sha256,
            "errors": [f"{type(error).__name__}: {error}"],
            "guardian_pid": os.getpid(),
            "hard_stop": config.hard_stop_text,
            "hard_stop_ns": config.hard_stop_ns,
            "schema": RESULT_SCHEMA,
            "state_sha256": hash_bytes(canonical_bytes(state)),
            "stop": stop_summary,
            "trigger_wall_ns": failure_wall_ns,
            "triggered_at": iso_from_ns(failure_wall_ns),
        }
    if not create_json_exclusive(result_path, result):
        existing = read_json(result_path)
        if existing != result:
            raise GuardError(f"conflicting guardian result already exists: {result_path}")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["complete"] else 2


def command_arm(args: argparse.Namespace) -> int:
    require_pidfd_support()
    config = load_config(Path(args.config))
    if time.time_ns() >= config.hard_stop_ns:
        raise GuardError("refusing to arm at or after the hard stop")
    if (config.raw_root / "STOP").exists():
        raise GuardError("refusing to arm because STOP already exists")
    config.raw_root.mkdir(parents=True, exist_ok=True)
    state_path, result_path = state_paths(config)
    if result_path.is_symlink():
        raise GuardError(f"guardian result path is an unsafe symlink: {result_path}")
    if result_path.exists():
        raise GuardError(f"guardian result already exists: {result_path}")
    if state_path.is_symlink():
        raise GuardError(f"guardian state path is an unsafe symlink: {state_path}")
    if state_path.exists():
        existing = read_json(state_path)
        if isinstance(existing, dict) and guardian_identity(existing, config) is not None:
            print(json.dumps(existing, indent=2, sort_keys=True))
            return 0
        raise GuardError(
            f"stale or invalid guardian state exists; refusing to replace it: {state_path}"
        )

    token = secrets.token_hex(24)
    read_fd, write_fd = os.pipe()
    stdout_path = config.raw_root / STDOUT_NAME
    stderr_path = config.raw_root / STDERR_NAME
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
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
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            close_fds=True,
        )
    os.close(read_fd)
    published = False
    state: dict[str, Any] | None = None
    try:
        child = None
        for _ in range(100):
            child = read_process(process.pid)
            if child is not None:
                break
            time.sleep(0.01)
        if child is None:
            raise GuardError("armed guardian exited before /proc identity could be captured")
        state = {
            "arm_token": token,
            "armed_at": iso_from_ns(time.time_ns()),
            "boot_id": boot_id(),
            "config": str(config.path),
            "config_sha256": config.sha256,
            "hard_stop": config.hard_stop_text,
            "hard_stop_ns": config.hard_stop_ns,
            "pid": process.pid,
            "raw_root": str(config.raw_root),
            "schema": STATE_SCHEMA,
            "start_ticks": child.start_ticks,
        }
        if guardian_identity(state, config) is None:
            raise GuardError("new guardian process failed command/cwd/start identity validation")
        if not create_json_exclusive(state_path, state):
            raise GuardError("another guardian won the arm race; refusing this child")
        published = True
        os.write(write_fd, b"1")
    except Exception:
        try:
            current = read_process(process.pid)
            if current is not None and (child is None or current.start_ticks == child.start_ticks):
                os.kill(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if published and state is not None:
            try:
                if read_json(state_path) == state:
                    state_path.unlink()
                    fsync_directory(state_path.parent)
            except (GuardError, OSError):
                pass
        raise
    finally:
        os.close(write_fd)
    print(json.dumps(state, indent=2, sort_keys=True))
    return 0


def command_status(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    state_path, result_path = state_paths(config)
    if state_path.is_symlink() or result_path.is_symlink():
        raise GuardError("guardian state/result evidence may not be symlinks")
    state = read_json(state_path) if state_path.is_file() else None
    result = read_json(result_path) if result_path.is_file() else None
    identity_valid = isinstance(state, dict) and guardian_identity(state, config) is not None
    jobs, job_errors = load_jobs(config)
    now = time.time_ns()
    value = {
        "active_markers": sorted(job.job_id for job in jobs if job.active is not None),
        "guardian_identity_valid": identity_valid,
        "guardian_pid": state.get("pid") if isinstance(state, dict) else None,
        "hard_stop": config.hard_stop_text,
        "job_evidence_errors": job_errors,
        "now": iso_from_ns(now),
        "result_complete": result.get("complete") if isinstance(result, dict) else None,
        "result_exists": result is not None,
        "seconds_to_hard_stop": max(0.0, (config.hard_stop_ns - now) / NS),
        "state_exists": state is not None,
        "stop_exists": (config.raw_root / "STOP").is_file(),
    }
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


def command_self_test(_: argparse.Namespace) -> int:
    require_pidfd_support()
    descriptor = os.pidfd_open(os.getpid(), 0)
    os.close(descriptor)
    sample = "123 (name with ) paren) S 4 5 6 " + "0 " * 15 + "99 0 0"
    parsed = parse_proc_stat(sample)
    if parsed != (123, 4, 5, 6, "S", 99):
        raise GuardError(f"/proc stat parser fixture failed: {parsed}")
    timestamp = "2026-08-17T08:09:44.640Z"
    if iso_from_ns(parse_iso_ns(timestamp)) != timestamp:
        raise GuardError("timestamp round-trip fixture failed")
    if parse_unified_cgroup("0::/fixture/job\n") != PurePosixPath("/fixture/job"):
        raise GuardError("unified cgroup parser fixture failed")
    mount_fixture = parse_cgroup2_mounts(
        "36 25 0:32 / /sys/fs/cgroup rw,nosuid - cgroup2 cgroup rw\n"
    )
    if map_unified_cgroup(PurePosixPath("/fixture/job"), mount_fixture) != Path(
        "/sys/fs/cgroup/fixture/job"
    ):
        raise GuardError("cgroup2 mount mapping fixture failed")
    if parse_cgroup_events("populated 0\nfrozen 0\n")["populated"] != 0:
        raise GuardError("cgroup.events parser fixture failed")
    if parse_cgroup_procs("19\n7\n") != [7, 19]:
        raise GuardError("cgroup.procs parser fixture failed")
    descendant_only_members = {
        19: Path("/sys/fs/cgroup/lean-eval-123-aaaaaaaaaaaaaaaaaaaaaaaa/nested")
    }
    if not cgroup_population_consistent(1, descendant_only_members):
        raise GuardError("descendant-only populated cgroup fixture failed")
    if cgroup_population_consistent(0, descendant_only_members):
        raise GuardError("populated=0 accepted a nested cgroup member")
    if (
        "empty-after-marker-change" not in CGROUP_KILL_SUCCESS_OUTCOMES
        or "active-marker-changed" in CGROUP_KILL_SUCCESS_OUTCOMES
    ):
        raise GuardError("empty cgroup marker-transition fixture failed")
    transition_sha256 = "9" * 64
    initial_populated = 1
    controller_transition = completed_transition_facts_valid(
        active_path_absent=True,
        process_sha256=transition_sha256,
        expected_sha256=transition_sha256,
        active_gone_or_zombie=True,
        cgroup_absent=True,
    )
    if (
        initial_populated != 1
        or not controller_transition
        or "empty-after-controller-transition"
        not in CGROUP_KILL_SUCCESS_OUTCOMES
    ):
        raise GuardError("initially populated controller-transition fixture failed")
    invalid_transition_facts = (
        {
            "active_path_absent": False,
            "process_sha256": transition_sha256,
            "active_gone_or_zombie": True,
            "cgroup_absent": True,
        },
        {
            "active_path_absent": True,
            "process_sha256": "8" * 64,
            "active_gone_or_zombie": True,
            "cgroup_absent": True,
        },
        {
            "active_path_absent": True,
            "process_sha256": transition_sha256,
            "active_gone_or_zombie": False,
            "cgroup_absent": True,
        },
        {
            "active_path_absent": True,
            "process_sha256": transition_sha256,
            "active_gone_or_zombie": True,
            "cgroup_absent": False,
        },
    )
    if any(
        completed_transition_facts_valid(
            expected_sha256=transition_sha256,
            **facts,
        )
        for facts in invalid_transition_facts
    ):
        raise GuardError("unsafe controller-transition facts were accepted")
    cgroup_fixture = parse_cgroup_record(
        {
            "inode": 42,
            "owner_uid": os.getuid(),
            "path": "/sys/fs/cgroup/lean-eval-123-aaaaaaaaaaaaaaaaaaaaaaaa",
        }
    )
    name_match = CGROUP_NAME_RE.fullmatch(cgroup_fixture.path.name)
    if name_match is None or name_match.group(1) != "123":
        raise GuardError("cgroup evidence/name fixture failed")
    try:
        parse_cgroup_record(
            {
                "inode": 42,
                "owner_uid": os.getuid(),
                "path": "/sys/fs/cgroup/fixture/../unsafe",
            }
        )
    except GuardError:
        pass
    else:
        raise GuardError("unsafe cgroup path fixture was accepted")
    delegated, mounts = current_delegated_cgroup()
    if process_cgroup_path(os.getpid(), mounts) != delegated:
        raise GuardError("self cgroup/mount mapping fixture failed")
    for control in ("cgroup.events", "cgroup.kill", "cgroup.procs"):
        control_path = delegated / control
        if control_path.is_symlink() or not control_path.is_file():
            raise GuardError(f"delegated cgroup control is missing or unsafe: {control}")
    fixture_boot_seconds = 1_000
    fixture_ticks = 100
    verify_active = ActiveRecord(
        path=Path("/fixture/active.json"),
        pid=333,
        pgid=333,
        argv=("lake", "test"),
        registered_ns=1_003 * NS,
        sha256="a" * 64,
        cgroup=CgroupRecord(
            path=Path("/sys/fs/cgroup/lean-eval-222-aaaaaaaaaaaaaaaaaaaaaaaa"),
            inode=42,
            owner_uid=os.getuid(),
        ),
    )
    verify_job = JobRecord(
        job_id="verify-job",
        problem_id="fixture-problem",
        directory=Path("/fixture/verify-job"),
        controller_pid=111,
        launched_ns=1_000 * NS,
        requested_model="fixture-model",
        reasoning_effort="fixture-effort",
        active=verify_active,
        result_exists=False,
    )
    verify_table = {
        222: ProcessInfo(
            222,
            1,
            222,
            222,
            "S",
            200,
            os.getuid(),
            str(REPO.resolve()),
            str(Path(sys.executable).resolve()),
            (
                sys.executable,
                str((REPO / "scripts" / "speedrun.py").resolve()),
                "verify",
                "--job-id",
                "verify-job",
                "--problem",
                "fixture-problem",
            ),
        ),
        333: ProcessInfo(
            333,
            222,
            333,
            333,
            "S",
            201,
            os.getuid(),
            str(REPO.resolve()),
            "/fixture/lake",
            ("lake", "test"),
        ),
    }
    if verify_job.controller_pid == 222 or not cgroup_creator_matches(
        222,
        verify_job,
        verify_table,
        os.getuid(),
        fixture_boot_seconds,
        fixture_ticks,
    ):
        raise GuardError("standalone verify cgroup-controller binding fixture failed")
    fixture = {
        10: ProcessInfo(10, 1, 10, 10, "S", 100, os.getuid(), str(REPO), "/x", ("root",)),
        11: ProcessInfo(11, 10, 11, 11, "S", 101, os.getuid(), str(REPO), "/x", ("child",)),
        12: ProcessInfo(12, 11, 12, 12, "S", 102, os.getuid(), str(REPO), "/x", ("detached",)),
        20: ProcessInfo(20, 1, 20, 20, "S", 200, os.getuid(), str(REPO), "/x", ("unrelated",)),
    }
    if descendants(fixture, 10) != {10: 0, 11: 1, 12: 2}:
        raise GuardError("detached-session descendant fixture failed")
    worker = ProcessInfo(
        30,
        11,
        30,
        30,
        "S",
        103,
        os.getuid(),
        str(REPO.resolve()),
        "/opt/codex",
        (
            "/opt/codex-real",
            "-c",
            "fixture=true",
            "exec",
            "--json",
            "-m",
            "fixture-model",
            "-c",
            'model_reasoning_effort="fixture-effort"',
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(REPO.resolve()),
            "-",
        ),
    )
    if not exact_worker_invocation(worker):
        raise GuardError("detached codex worker classifier fixture failed")
    classifier_config = GuardConfig(
        path=Path("/fixture/config.json"),
        raw_root=Path("/fixture"),
        race_start_ns=0,
        hard_stop_ns=10 * NS,
        hard_stop_text="fixture",
        sha256="c" * 64,
    )
    bootstrap_target = ("lake", "test")
    bootstrap_command = (
        sys.executable,
        str((REPO / "scripts" / "speedrun.py").resolve()),
        "_exec_gate",
        "--release-fd",
        "7",
        "--stop-file",
        str(classifier_config.raw_root / "STOP"),
        "--hard-stop-ns",
        str(classifier_config.hard_stop_ns),
        "--",
        *bootstrap_target,
    )
    if parse_bootstrap_argv(
        "lean-eval-active-process-v2",
        list(bootstrap_command),
        bootstrap_target,
        classifier_config,
    ) != bootstrap_command:
        raise GuardError("active bootstrap parser fixture failed")
    try:
        parse_bootstrap_argv(
            "lean-eval-active-process-v1",
            list(bootstrap_command),
            bootstrap_target,
            classifier_config,
        )
    except GuardError:
        pass
    else:
        raise GuardError("legacy active bootstrap argv fixture was accepted")
    bootstrap_active = ActiveRecord(
        path=Path("/fixture/bootstrap/active.json"),
        pid=31,
        pgid=31,
        argv=bootstrap_target,
        registered_ns=2 * NS,
        sha256="e" * 64,
        bootstrap_argv=bootstrap_command,
    )
    bootstrap_job = JobRecord(
        job_id="bootstrap-job",
        problem_id="fixture-problem",
        directory=Path("/fixture/bootstrap"),
        controller_pid=29,
        launched_ns=NS,
        requested_model="fixture-model",
        reasoning_effort="fixture-effort",
        active=bootstrap_active,
        result_exists=False,
    )
    bootstrap_info = ProcessInfo(
        31,
        29,
        31,
        31,
        "S",
        104,
        os.getuid(),
        str(REPO.resolve()),
        str(Path(sys.executable).resolve()),
        bootstrap_command,
    )
    if not active_argv_matches(bootstrap_info, bootstrap_active, bootstrap_job):
        raise GuardError("active bootstrap argv fixture failed")
    altered_bootstrap = ProcessInfo(
        **{
            **bootstrap_info.__dict__,
            "cmdline": (*bootstrap_command, "unexpected"),
        }
    )
    if active_argv_matches(altered_bootstrap, bootstrap_active, bootstrap_job):
        raise GuardError("altered active bootstrap argv fixture was accepted")
    exact_target_argv = ("cx", "auto", "--", "exec", "--json")
    exact_target_active = ActiveRecord(
        path=Path("/fixture/exact-target/active.json"),
        pid=32,
        pgid=32,
        argv=exact_target_argv,
        registered_ns=2 * NS,
        sha256="f" * 64,
    )
    exact_target_job = JobRecord(
        job_id="exact-target-job",
        problem_id="fixture-problem",
        directory=Path("/fixture/exact-target"),
        controller_pid=29,
        launched_ns=NS,
        requested_model="fixture-model",
        reasoning_effort="fixture-effort",
        active=exact_target_active,
        result_exists=False,
    )
    exact_target_info = ProcessInfo(
        32,
        29,
        32,
        32,
        "S",
        105,
        os.getuid(),
        str(REPO.resolve()),
        "/fixture/cx",
        exact_target_argv,
    )
    if not active_argv_matches(
        exact_target_info, exact_target_active, exact_target_job
    ):
        raise GuardError("exact active target argv fixture failed")
    legacy_active = ActiveRecord(
        path=Path("/fixture/legacy/active.json"),
        pid=30,
        pgid=30,
        argv=("cx", "auto", "--", "exec"),
        registered_ns=2 * NS,
        sha256="b" * 64,
    )
    legacy_job = JobRecord(
        job_id="legacy-job",
        problem_id="fixture-problem",
        directory=Path("/fixture/legacy"),
        controller_pid=29,
        launched_ns=NS,
        requested_model="fixture-model",
        reasoning_effort="fixture-effort",
        active=legacy_active,
        result_exists=False,
    )
    if matching_worker_jobs(worker, [legacy_job], classifier_config, 0, 100) != [
        legacy_job
    ]:
        raise GuardError("legacy orphan-worker binding fixture failed")
    v2_active = ActiveRecord(
        path=Path("/fixture/v2/active.json"),
        pid=30,
        pgid=30,
        argv=("cx", "auto", "--", "exec"),
        registered_ns=2 * NS,
        sha256="d" * 64,
        cgroup=CgroupRecord(
            path=Path("/sys/fs/cgroup/lean-eval-29-aaaaaaaaaaaaaaaaaaaaaaaa"),
            inode=43,
            owner_uid=os.getuid(),
        ),
    )
    v2_job = JobRecord(
        job_id="v2-job",
        problem_id="fixture-problem",
        directory=Path("/fixture/v2"),
        controller_pid=29,
        launched_ns=NS,
        requested_model="fixture-model",
        reasoning_effort="fixture-effort",
        active=v2_active,
        result_exists=False,
    )
    if matching_worker_jobs(worker, [v2_job], classifier_config, 0, 100):
        raise GuardError("v2 worker escaped exclusive cgroup binding")
    encoded = canonical_bytes({"b": 2, "a": 1})
    if encoded != b'{"a":1,"b":2}\n':
        raise GuardError("canonical evidence fixture failed")
    own = read_process(os.getpid())
    if own is None or own.start_ticks <= 0 or own.uid != os.getuid():
        raise GuardError("cannot validate self /proc identity")
    print(
        json.dumps(
            {
                "canonical_evidence": "ok",
                "active_bootstrap_argv": "ok",
                "active_exact_target_argv": "ok",
                "cgroup_control_topology": "ok",
                "cgroup_descendant_only_population": "ok",
                "cgroup_empty_marker_transition": "ok",
                "cgroup_initial_populated_controller_transition": "ok",
                "cgroup_evidence_validation": "ok",
                "cgroup_events_parser": "ok",
                "cgroup_mount_mapping": "ok",
                "cgroup_procs_parser": "ok",
                "cgroup_post_kill_controller_transition": "ok",
                "cgroup_verify_controller_binding": "ok",
                "detached_descendant_selection": "ok",
                "detached_worker_classifier": "ok",
                "legacy_worker_group_binding": "ok",
                "proc_identity": "ok",
                "proc_stat_parser": "ok",
                "pidfd": "ok",
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
    except GuardError as error:
        print(f"hard-stop guardian refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
