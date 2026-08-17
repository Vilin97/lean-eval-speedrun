#!/usr/bin/env python3
"""Create a deterministic, sanitized, per-file gzip archive of speedrun logs."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable


REPO = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO / "speedrun" / "config.json"
DEFAULT_OUTPUT = REPO / "speedrun-logs"
DEFAULT_ACCOUNTS = Path("/data/codex/accounts")
SESSION_ID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$"
)
EXCLUSIONS_SECTION_RE = re.compile(
    r"(?ims)^[ \t]*(?:#{1,6}[ \t]*)?Persistent[ \t]+user[ \t]+exclusions[ \t]*\r?\n"
    r".*?(?=^[ \t]*(?:---[ \t]*project-doc[ \t]*---|#{1,6}[ \t]+\S)|\Z)"
)
REDACTION = "[redacted exclusions section]\n"


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode(
        "utf-8"
    )


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp lacks a timezone: {value!r}")
    return parsed


def sanitize_string(value: str) -> tuple[str, int]:
    return EXCLUSIONS_SECTION_RE.subn(REDACTION, value)


def sanitize_value(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        return sanitize_string(value)
    if isinstance(value, list):
        output = []
        replacements = 0
        for item in value:
            sanitized, count = sanitize_value(item)
            output.append(sanitized)
            replacements += count
        return output, replacements
    if isinstance(value, dict):
        output: dict[Any, Any] = {}
        replacements = 0
        for key, item in value.items():
            sanitized_key, key_count = sanitize_value(key)
            sanitized_item, item_count = sanitize_value(item)
            if sanitized_key in output:
                raise ValueError("sanitization caused a duplicate JSON object key")
            output[sanitized_key] = sanitized_item
            replacements += key_count + item_count
        return output, replacements
    return value, 0


def is_world_state(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("type") == "world_state":
        return True
    payload = value.get("payload")
    return isinstance(payload, dict) and payload.get("type") == "world_state"


def safe_regular_files(root: Path) -> list[Path]:
    files: list[Path] = []
    if not root.exists():
        return files
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names[:] = sorted(
            name for name in directory_names if not (Path(directory) / name).is_symlink()
        )
        for name in sorted(file_names):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            if any(part.casefold() == "auth.json" for part in path.parts):
                continue
            files.append(path)
    return files


def atomic_gzip(
    destination: Path,
    writer: Callable[[BinaryIO], dict[str, Any]],
    stability_check: Callable[[], bool],
) -> tuple[dict[str, Any], str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as raw_output:
            with gzip.GzipFile(
                filename="", mode="wb", compresslevel=9, fileobj=raw_output, mtime=0
            ) as output:
                statistics = writer(output)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        archive_hash = hash_file(temporary)
        if not stability_check():
            raise RuntimeError("source changed while it was being archived")
        os.replace(temporary, destination)
        return statistics, archive_hash
    finally:
        temporary.unlink(missing_ok=True)


def transform_jsonl(source: Path, output: BinaryIO) -> dict[str, Any]:
    source_digest = hashlib.sha256()
    records_in = 0
    records_out = 0
    malformed_lines = 0
    omitted_world_state = 0
    sanitized_strings = 0
    with source.open("rb") as stream:
        for raw_line in stream:
            source_digest.update(raw_line)
            records_in += 1
            try:
                text = raw_line.decode("utf-8")
                value = json.loads(text)
            except (UnicodeDecodeError, json.JSONDecodeError):
                text = raw_line.decode("utf-8", errors="replace")
                sanitized, count = sanitize_string(text)
                output.write(sanitized.encode("utf-8"))
                if not sanitized.endswith("\n"):
                    output.write(b"\n")
                malformed_lines += 1
                sanitized_strings += count
                records_out += 1
                continue
            if is_world_state(value):
                omitted_world_state += 1
                continue
            sanitized, count = sanitize_value(value)
            output.write(canonical_bytes(sanitized))
            sanitized_strings += count
            records_out += 1
    return {
        "malformed_lines": malformed_lines,
        "omitted_world_state": omitted_world_state,
        "records_in": records_in,
        "records_out": records_out,
        "sanitized_strings": sanitized_strings,
        "source_sha256": source_digest.hexdigest(),
    }


def transform_json(source: Path, output: BinaryIO) -> dict[str, Any]:
    data = source.read_bytes()
    value = json.loads(data.decode("utf-8"))
    sanitized, count = sanitize_value(value)
    output.write(canonical_bytes(sanitized))
    return {
        "malformed_lines": 0,
        "omitted_world_state": 0,
        "records_in": 1,
        "records_out": 1,
        "sanitized_strings": count,
        "source_sha256": hashlib.sha256(data).hexdigest(),
    }


def transform_text(source: Path, output: BinaryIO) -> dict[str, Any]:
    data = source.read_bytes()
    if b"\0" in data:
        raise ValueError(f"refusing to archive an unrecognized binary file: {source}")
    text = data.decode("utf-8")
    sanitized, count = sanitize_string(text)
    output.write(sanitized.encode("utf-8"))
    return {
        "malformed_lines": 0,
        "omitted_world_state": 0,
        "records_in": 1,
        "records_out": 1,
        "sanitized_strings": count,
        "source_sha256": hashlib.sha256(data).hexdigest(),
    }


def transformer_for(path: Path) -> Callable[[Path, BinaryIO], dict[str, Any]]:
    if path.suffix == ".jsonl":
        return transform_jsonl
    if path.suffix == ".json":
        return transform_json
    return transform_text


@dataclass(frozen=True)
class SessionMetadata:
    path: Path
    account: str
    thread_id: str
    parent_thread_id: str | None
    session_id: str | None
    timestamp: datetime
    cwd: str | None


def session_id_from_path(path: Path) -> str | None:
    match = SESSION_ID_RE.search(path.name)
    return match.group(1).lower() if match else None


def read_session_metadata(path: Path, accounts_root: Path) -> SessionMetadata | None:
    own_id = session_id_from_path(path)
    if own_id is None:
        return None
    with path.open("r", encoding="utf-8", errors="strict") as stream:
        for line_number, line in enumerate(stream):
            if line_number >= 64:
                break
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict) or value.get("type") != "session_meta":
                continue
            payload = value.get("payload")
            if not isinstance(payload, dict) or str(payload.get("id", "")).lower() != own_id:
                continue
            relative = path.relative_to(accounts_root)
            parent = payload.get("parent_thread_id")
            session = payload.get("session_id")
            cwd = payload.get("cwd")
            timestamp_text = payload.get("timestamp") or value.get("timestamp")
            if not isinstance(timestamp_text, str):
                raise ValueError(f"session metadata lacks a timestamp: {path}")
            return SessionMetadata(
                path=path,
                account=relative.parts[0],
                thread_id=own_id,
                parent_thread_id=parent.lower() if isinstance(parent, str) else None,
                session_id=session.lower() if isinstance(session, str) else None,
                timestamp=parse_iso(timestamp_text),
                cwd=cwd if isinstance(cwd, str) else None,
            )
    return None


def job_thread_ids(jobs_root: Path) -> set[str]:
    result: set[str] = set()
    for path in sorted(jobs_root.glob("*/result.json")):
        if any(part.casefold() == "auth.json" for part in path.parts):
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        for thread_id in value.get("thread_ids", []):
            if isinstance(thread_id, str):
                result.add(thread_id.lower())
    return result


def relevant_sessions(
    accounts_root: Path,
    jobs_root: Path,
    default_cwd: str,
    origin: datetime,
    hard_stop: datetime,
    explicit_roots: list[str],
) -> tuple[list[SessionMetadata], list[str]]:
    metadata: list[SessionMetadata] = []
    for account in sorted(accounts_root.iterdir()) if accounts_root.exists() else []:
        sessions = account / "sessions"
        if not sessions.is_dir():
            continue
        for path in sorted(sessions.rglob("*.jsonl")):
            if path.is_symlink() or any(part.casefold() == "auth.json" for part in path.parts):
                continue
            item = read_session_metadata(path, accounts_root)
            if item is not None and item.timestamp <= hard_stop:
                metadata.append(item)

    normalized_cwd = os.path.realpath(default_cwd)
    roots = {value.lower() for value in explicit_roots}
    roots.update(job_thread_ids(jobs_root))
    for item in metadata:
        if item.parent_thread_id is not None or item.timestamp < origin:
            continue
        if item.cwd is not None and os.path.realpath(item.cwd) == normalized_cwd:
            roots.add(item.thread_id)

    included = set(roots)
    changed = True
    while changed:
        changed = False
        for item in metadata:
            if item.thread_id in included:
                continue
            if item.parent_thread_id in included:
                included.add(item.thread_id)
                changed = True
    selected = sorted((item for item in metadata if item.thread_id in included), key=lambda item: item.thread_id)
    return selected, sorted(roots)


def atomic_manifest(path: Path, value: Any) -> None:
    data = canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def archive_one(source: Path, destination: Path, source_label: str, output_root: Path) -> dict[str, Any]:
    resolved_output = output_root.resolve()
    resolved_parent = destination.parent.resolve()
    if resolved_output != resolved_parent and resolved_output not in resolved_parent.parents:
        raise ValueError(f"archive destination escapes output directory: {destination}")
    transformer = transformer_for(source)
    initial_stat = source.stat()
    initial_signature = (
        initial_stat.st_dev,
        initial_stat.st_ino,
        initial_stat.st_size,
        initial_stat.st_mtime_ns,
    )

    def writer(output: BinaryIO) -> dict[str, Any]:
        return transformer(source, output)

    def source_is_stable() -> bool:
        final_stat = source.stat()
        return initial_signature == (
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_size,
            final_stat.st_mtime_ns,
        )

    statistics, archive_hash = atomic_gzip(destination, writer, source_is_stable)
    return {
        "archive": destination.relative_to(output_root).as_posix(),
        "archive_sha256": archive_hash,
        "source": source_label,
        **statistics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--accounts-root", default=str(DEFAULT_ACCOUNTS))
    parser.add_argument("--root-thread-id", action="append", default=[])
    args = parser.parse_args()

    configuration = json.loads(Path(args.config).read_text(encoding="utf-8"))
    accounts_root = Path(args.accounts_root).resolve()
    jobs_root = Path(configuration["raw_root"]).resolve()
    output_argument = Path(args.output)
    if output_argument.is_symlink():
        raise SystemExit(f"refusing to archive through a symlink: {output_argument}")
    output_root = output_argument.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    sessions, roots = relevant_sessions(
        accounts_root=accounts_root,
        jobs_root=jobs_root,
        default_cwd=configuration["default_cwd"],
        origin=parse_iso(configuration["schedule_origin"]),
        hard_stop=parse_iso(configuration["hard_stop"]),
        explicit_roots=args.root_thread_id,
    )
    archived: list[dict[str, Any]] = []
    for item in sessions:
        relative = item.path.relative_to(accounts_root)
        destination = output_root / "rollouts" / Path(str(relative) + ".gz")
        archived.append(archive_one(item.path, destination, f"rollout:{relative.as_posix()}", output_root))

    for source in safe_regular_files(jobs_root):
        relative = source.relative_to(jobs_root)
        destination = output_root / "jobs" / Path(str(relative) + ".gz")
        archived.append(archive_one(source, destination, f"job:{relative.as_posix()}", output_root))

    archived.sort(key=lambda item: item["archive"])
    manifest = {
        "schema": "lean-eval-speedrun-log-archive-v1",
        "schedule_origin": configuration["schedule_origin"],
        "hard_stop": configuration["hard_stop"],
        "root_thread_ids": roots,
        "rollout_count": len(sessions),
        "files": archived,
    }
    atomic_manifest(output_root / "manifest.json", manifest)
    print(
        json.dumps(
            {"files": len(archived), "output": str(output_root), "rollouts": len(sessions)},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
