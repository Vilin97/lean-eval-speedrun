#!/usr/bin/env python3
"""Render deterministic cumulative-solve data and analysis SVGs."""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable


REPO = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO / "speedrun" / "config.json"
DEFAULT_PROBLEMS = REPO / "speedrun" / "problems.jsonl"
DEFAULT_METRICS = REPO / "analysis" / "metrics.csv"
DEFAULT_OUTPUT = REPO / "analysis"
MIN_LOG_SECONDS = 1.0
RACE_SECONDS = 86_400.0
SOLVE_TIME_MAX_MINUTES = 78.0
BUDGET_CAP_USD = 135.99


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp lacks a timezone: {value!r}")
    return parsed


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            records.append(value)
    return records


def read_metrics(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def verified_timestamp(record: dict[str, Any]) -> str | None:
    for key in ("first_verified_at_utc", "verified_at_utc", "accepted_at_utc"):
        value = record.get(key)
        if isinstance(value, str):
            return value
    return None


def solve_rows(
    records: list[dict[str, Any]], race_start: datetime, hard_stop: datetime
) -> list[dict[str, Any]]:
    earliest: dict[str, tuple[datetime, str]] = {}
    for record in records:
        problem_id = record.get("problem_id")
        if not isinstance(problem_id, str) or problem_id.startswith("_toy"):
            continue
        if record.get("verified") is not True:
            continue
        timestamp_text = verified_timestamp(record)
        if timestamp_text is None:
            continue
        timestamp = parse_iso(timestamp_text)
        if timestamp < race_start or timestamp > hard_stop:
            continue
        candidate = (timestamp, timestamp_text)
        if problem_id not in earliest or candidate < earliest[problem_id]:
            earliest[problem_id] = candidate

    ordered = sorted(
        (
            (timestamp, problem_id, timestamp_text)
            for problem_id, (timestamp, timestamp_text) in earliest.items()
        ),
        key=lambda item: (item[0], item[1]),
    )
    rows: list[dict[str, Any]] = []
    for solve_number, (timestamp, problem_id, timestamp_text) in enumerate(ordered, 1):
        elapsed = (timestamp - race_start).total_seconds()
        rows.append(
            {
                "solve_number": solve_number,
                "problem_id": problem_id,
                "verified_at_utc": timestamp_text,
                "elapsed_seconds": elapsed,
                "plot_elapsed_seconds": min(
                    RACE_SECONDS, max(MIN_LOG_SECONDS, elapsed)
                ),
            }
        )
    return rows


def render_csv(rows: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "solve_number",
            "problem_id",
            "verified_at_utc",
            "elapsed_seconds",
            "plot_elapsed_seconds",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["solve_number"],
                row["problem_id"],
                row["verified_at_utc"],
                f'{row["elapsed_seconds"]:.3f}',
                f'{row["plot_elapsed_seconds"]:.3f}',
            ]
        )
    return output.getvalue().encode("utf-8")


def y_ticks(maximum: int) -> list[int]:
    ticks = list(range(0, maximum + 1, 10))
    if not ticks or ticks[-1] != maximum:
        ticks.append(maximum)
    return ticks


def render_step_svg(
    events: list[dict[str, Any]],
    *,
    title: str,
    subtitle: str,
    x_label: str,
    x_min: float,
    x_max: float,
    ticks: list[tuple[float, str]],
    transform: Callable[[float], float] = lambda value: value,
    overflow_note: str | None = None,
) -> bytes:
    width, height = 1000, 620
    left, right, top, bottom_margin = 90, 34, 78, 82
    plot_width = width - left - right
    plot_height = height - top - bottom_margin
    bottom = top + plot_height
    maximum = max(1, len(events))
    transformed_min = transform(x_min)
    transformed_max = transform(x_max)

    def x_position(value: float) -> float:
        bounded = min(x_max, max(x_min, value))
        ratio = (transform(bounded) - transformed_min) / (
            transformed_max - transformed_min
        )
        return left + ratio * plot_width

    def y_position(count: int) -> float:
        return bottom - count / maximum * plot_height

    pieces = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="620" viewBox="0 0 1000 620">',
        "<style>",
        "text{font-family:ui-sans-serif,system-ui,sans-serif;fill:#1f2937}",
        ".grid{stroke:#d1d5db;stroke-width:1}.axis{stroke:#111827;stroke-width:1.5}",
        ".curve{fill:none;stroke:#2563eb;stroke-width:3;stroke-linejoin:round}",
        ".point{fill:#2563eb;stroke:#fff;stroke-width:1.4}",
        ".overflow{fill:#f59e0b;stroke:#fff;stroke-width:1.4}",
        ".note{fill:#6b7280}",
        "</style>",
        '<rect width="1000" height="620" fill="#fff"/>',
        f'<text x="500" y="27" text-anchor="middle" font-size="20" font-weight="650">{html.escape(title)}</text>',
        f'<text class="note" x="500" y="50" text-anchor="middle" font-size="12">{html.escape(subtitle)}</text>',
    ]

    for value, label in ticks:
        x_value = x_position(value)
        pieces.append(
            f'<line class="grid" x1="{x_value:.3f}" y1="{top}" x2="{x_value:.3f}" y2="{bottom}"/>'
        )
        pieces.append(
            f'<text x="{x_value:.3f}" y="{bottom + 24}" text-anchor="middle" font-size="12">{html.escape(label)}</text>'
        )

    for tick in y_ticks(maximum):
        y_value = y_position(tick)
        pieces.append(
            f'<line class="grid" x1="{left}" y1="{y_value:.3f}" x2="{width-right}" y2="{y_value:.3f}"/>'
        )
        pieces.append(
            f'<text x="{left-12}" y="{y_value+4:.3f}" text-anchor="end" font-size="12">{tick}</text>'
        )

    pieces.extend(
        [
            f'<line class="axis" x1="{left}" y1="{bottom}" x2="{width-right}" y2="{bottom}"/>',
            f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{bottom}"/>',
            f'<text x="{left + plot_width/2:.3f}" y="{height-22}" text-anchor="middle" font-size="14">{html.escape(x_label)}</text>',
            f'<text x="22" y="{top + plot_height/2:.3f}" text-anchor="middle" font-size="14" transform="rotate(-90 22 {top + plot_height/2:.3f})">Cumulative distinct verified solves</text>',
        ]
    )

    path = [f"M {x_position(x_min):.3f} {y_position(0):.3f}"]
    for count, event in enumerate(events, 1):
        x_value = x_position(float(event["x"]))
        path.extend([f"H {x_value:.3f}", f"V {y_position(count):.3f}"])
    path.append(f"H {x_position(x_max):.3f}")
    pieces.append(f'<path class="curve" d="{" ".join(path)}"/>')

    for count, event in enumerate(events, 1):
        x_raw = float(event["x"])
        x_value = x_position(x_raw)
        y_value = y_position(count)
        css_class = "overflow" if x_raw > x_max else "point"
        detail = html.escape(str(event["detail"]), quote=True)
        pieces.append(
            f'<circle class="{css_class}" cx="{x_value:.3f}" cy="{y_value:.3f}" r="4"><title>{detail}</title></circle>'
        )

    if overflow_note:
        pieces.append(
            f'<text x="{width-right-3}" y="{top+17}" text-anchor="end" font-size="12" fill="#b45309">{html.escape(overflow_note)}</text>'
        )
    pieces.append("</svg>")
    return ("\n".join(pieces) + "\n").encode("utf-8")


def timeline_events(rows: list[dict[str, Any]], divisor: float) -> list[dict[str, Any]]:
    return [
        {
            "x": float(row["elapsed_seconds"]) / divisor,
            "detail": (
                f'{row["problem_id"]} — solve #{row["solve_number"]}, '
                f'{row["verified_at_utc"]}'
            ),
        }
        for row in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--problems", default=str(DEFAULT_PROBLEMS))
    parser.add_argument("--metrics", default=str(DEFAULT_METRICS))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    configuration = json.loads(Path(args.config).read_text(encoding="utf-8"))
    race_start = parse_iso(configuration["race_start"])
    hard_stop = parse_iso(configuration["hard_stop"])
    if (hard_stop - race_start).total_seconds() != RACE_SECONDS:
        raise ValueError("configured race window is not exactly 24 hours")

    rows = solve_rows(read_records(Path(args.problems)), race_start, hard_stop)
    metrics = read_metrics(Path(args.metrics))
    solved = [row for row in metrics if row["solved"] == "True"]
    solved.sort(key=lambda row: int(row["solve_number"]))
    if [row["problem_id"] for row in solved] != [row["problem_id"] for row in rows]:
        raise ValueError("metrics and deterministic solve ledger disagree")

    output = Path(args.output_dir)
    atomic_write(output / "solves.csv", render_csv(rows))

    log_events = timeline_events(rows, 1.0)
    atomic_write(
        output / "solves-over-log-time.svg",
        render_step_svg(
            log_events,
            title="Verified solves over logarithmic race time",
            subtitle="The original speedrun view preserves both the opening sprint and long tail",
            x_label="Elapsed race time (logarithmic)",
            x_min=MIN_LOG_SECONDS,
            x_max=RACE_SECONDS,
            ticks=[
                (1, "1s"),
                (10, "10s"),
                (60, "1m"),
                (600, "10m"),
                (3_600, "1h"),
                (14_400, "4h"),
                (86_400, "24h"),
            ],
            transform=math.log10,
        ),
    )

    final_hours = float(rows[-1]["elapsed_seconds"]) / 3_600
    real_tick_hours = [final_hours * index / 5 for index in range(6)]
    real_ticks = [
        (
            value,
            (race_start + timedelta(hours=value)).strftime("%H:%M"),
        )
        for value in real_tick_hours
    ]
    atomic_write(
        output / "solves-over-real-time.svg",
        render_step_svg(
            timeline_events(rows, 3_600),
            title="Verified solves over real UTC time",
            subtitle="Observed window from race start through the final verified solve",
            x_label="UTC on 2026-08-16",
            x_min=0,
            x_max=final_hours,
            ticks=real_ticks,
        ),
    )

    atomic_write(
        output / "solves-over-24-hours.svg",
        render_step_svg(
            timeline_events(rows, 3_600),
            title="Verified solves across the full 24-hour race",
            subtitle="Linear elapsed time; the final solve landed at 14.71 hours",
            x_label="Elapsed race time (hours)",
            x_min=0,
            x_max=24,
            ticks=[(value, f"{value}h") for value in (0, 4, 8, 12, 16, 20, 24)],
        ),
    )

    by_solve_time = sorted(
        solved, key=lambda row: float(row["first_attempt_to_verified_seconds"])
    )
    solve_time_events = [
        {
            "x": float(row["first_attempt_to_verified_seconds"]) / 60,
            "detail": (
                f'{row["problem_id"]} — '
                f'{float(row["first_attempt_to_verified_seconds"])/60:.3f} min'
            ),
        }
        for row in by_solve_time
    ]
    solve_overflow = sum(
        float(event["x"]) > SOLVE_TIME_MAX_MINUTES for event in solve_time_events
    )
    atomic_write(
        output / "solves-over-solve-time.svg",
        render_step_svg(
            solve_time_events,
            title="Verified solves by per-problem solve time",
            subtitle="Empirical CDF of first launch to independent verification",
            x_label="Per-problem wall time (minutes)",
            x_min=0,
            x_max=SOLVE_TIME_MAX_MINUTES,
            ticks=[
                (value, f"{value:g}m")
                for value in (0, 10, 20, 30, 40, 50, 60, 70, 78)
            ],
        ),
    )

    by_budget = sorted(solved, key=lambda row: float(row["problem_subtree_cost_usd"]))
    budget_events = [
        {
            "x": float(row["problem_subtree_cost_usd"]),
            "detail": (
                f'{row["problem_id"]} — '
                f'${float(row["problem_subtree_cost_usd"]):.6f}'
            ),
        }
        for row in by_budget
    ]
    atomic_write(
        output / "solves-over-budget.svg",
        render_step_svg(
            budget_events,
            title="Verified solves by attributable per-problem budget",
            subtitle="Problem-subtree API-equivalent cost, including retries and nested agents",
            x_label="Per-problem budget (USD, API-equivalent)",
            x_min=0,
            x_max=BUDGET_CAP_USD,
            ticks=[
                (0, "$0"),
                (25, "$25"),
                (50, "$50"),
                (75, "$75"),
                (100, "$100"),
                (BUDGET_CAP_USD, "$135.99"),
            ],
            overflow_note="The $135.99 domain maximum is an attempted, unsolved problem",
        ),
    )

    generated = [
        "solves.csv",
        "solves-over-log-time.svg",
        "solves-over-real-time.svg",
        "solves-over-24-hours.svg",
        "solves-over-solve-time.svg",
        "solves-over-budget.svg",
    ]
    print(
        json.dumps(
            {
                "generated": generated,
                "output_dir": str(output),
                "solve_time_overflow": solve_overflow,
                "solve_time_max_minutes": SOLVE_TIME_MAX_MINUTES,
                "solves": len(rows),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
