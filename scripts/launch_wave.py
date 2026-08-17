#!/usr/bin/env python3
"""Launch a tab-separated wave of independently logged speedrun jobs."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
CONFIG = json.loads((REPO / "speedrun" / "config.json").read_text())
RAW_ROOT = Path(CONFIG["raw_root"])


def parse_iso(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def canonical_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wave")
    args = parser.parse_args()
    wave_path = (REPO / args.wave).resolve() if not Path(args.wave).is_absolute() else Path(args.wave).resolve()
    now = time.time()
    if now < parse_iso(CONFIG["race_start"]):
        raise SystemExit("race has not started")
    if now >= parse_iso(CONFIG["hard_stop"]):
        raise SystemExit("hard stop has passed")
    if (RAW_ROOT / "STOP").exists():
        raise SystemExit("STOP sentinel exists")

    with wave_path.open(newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    required = {"problem_id", "job_id", "model", "reasoning_effort", "prompt_file"}
    if not rows or set(rows[0]) != required:
        raise SystemExit(f"wave columns must be exactly {sorted(required)}")

    controller_root = RAW_ROOT / "_controllers"
    controller_root.mkdir(parents=True, exist_ok=True)
    record_path = controller_root / f"{wave_path.stem}.launch.json"
    if record_path.exists():
        raise SystemExit(f"wave record already exists: {record_path}")

    prompts = []
    for row in rows:
        prompt = (REPO / row["prompt_file"]).resolve()
        if not prompt.is_file():
            raise SystemExit(f"missing prompt: {prompt}")
        prompts.append(prompt)

    launched = []
    for row, prompt in zip(rows, prompts, strict=True):
        argv = [
            sys.executable,
            str(REPO / "scripts" / "speedrun.py"),
            "solve",
            "--problem",
            row["problem_id"],
            "--job-id",
            row["job_id"],
            "--model",
            row["model"],
            "--reasoning-effort",
            row["reasoning_effort"],
            "--prompt-file",
            str(prompt),
        ]
        stdout_path = controller_root / f"{row['job_id']}.stdout.log"
        stderr_path = controller_root / f"{row['job_id']}.stderr.log"
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            process = subprocess.Popen(
                argv,
                cwd=REPO,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                close_fds=True,
            )
        launched.append({**row, "pid": process.pid, "argv": argv})

    record = {
        "schema": "lean-eval-wave-launch-v1",
        "wave_sha256": __import__("hashlib").sha256(wave_path.read_bytes()).hexdigest(),
        "launched_wall_ns": time.time_ns(),
        "jobs": launched,
    }
    canonical_write(record_path, record)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
