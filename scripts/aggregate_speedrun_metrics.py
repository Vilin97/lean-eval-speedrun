#!/usr/bin/env python3
"""Build evidence-backed per-problem and aggregate speedrun metrics."""

from __future__ import annotations

import argparse
import csv
import difflib
import gzip
import hashlib
import html
import json
import math
import re
import statistics
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


DECL_RE = re.compile(
    r"^\s*(?:private\s+|protected\s+|noncomputable\s+|unsafe\s+)*"
    r"(?:theorem|lemma|def|abbrev|instance|structure|class|inductive)\b"
)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def strip_lean_comments(lines: list[str]) -> tuple[list[bool], list[str]]:
    """Return code-line flags and comment-stripped text, handling nested block comments."""
    flags: list[bool] = []
    stripped_lines: list[str] = []
    depth = 0
    in_string = False
    escaped = False
    for line in lines:
        output: list[str] = []
        i = 0
        while i < len(line):
            pair = line[i : i + 2]
            char = line[i]
            if depth:
                if pair == "/-":
                    depth += 1
                    i += 2
                elif pair == "-/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
                continue
            if in_string:
                output.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                i += 1
                continue
            if pair == "--":
                break
            if pair == "/-":
                depth = 1
                i += 2
                continue
            output.append(char)
            if char == '"':
                in_string = True
            i += 1
        text = "".join(output)
        stripped_lines.append(text)
        flags.append(bool(text.strip()))
    return flags, stripped_lines


def baseline_lines(repo: Path, baseline: str, relative: str) -> list[str]:
    completed = subprocess.run(
        ["git", "show", f"{baseline}:{relative}"],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.stdout.splitlines() if completed.returncode == 0 else []


def solution_metrics(repo: Path, baseline: str, files: list[str]) -> dict[str, int]:
    physical = code = added_physical = added_code = declarations = 0
    extant_files = 0
    for relative in files:
        path = repo / relative
        if path.suffix != ".lean" or not path.is_file():
            continue
        extant_files += 1
        current = path.read_text(encoding="utf-8").splitlines()
        old = baseline_lines(repo, baseline, relative)
        flags, stripped = strip_lean_comments(current)
        physical += len(current)
        code += sum(flags)
        declarations += sum(bool(DECL_RE.match(line)) for line in stripped)
        matcher = difflib.SequenceMatcher(a=old, b=current, autojunk=False)
        for tag, _a0, _a1, b0, b1 in matcher.get_opcodes():
            if tag in {"insert", "replace"}:
                added_physical += b1 - b0
                added_code += sum(flags[b0:b1])
    return {
        "lean_files": extant_files,
        "solution_physical_loc": physical,
        "solution_code_loc": code,
        "added_physical_loc": added_physical,
        "added_code_loc": added_code,
        "declaration_count": declarations,
    }


def archived_rollout_metrics(
    repo: Path, raw: Path
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Price every archived model call exactly once and recover session ownership.

    Forked subagent rollouts embed their parent's history, including cumulative
    token records. Summing each file's final counter would therefore charge the
    copied history repeatedly. Instead, token samples are globally keyed by
    ``(turn_id, total_token_usage)`` and their unique nonzero
    ``last_token_usage`` is priced under the model active for that turn. The
    session parent graph assigns each original turn to its nearest archived
    owner and then to the owner's controller-problem root, setup root, or
    top-level orchestration root.
    """
    archive_root = repo / "speedrun-logs"
    manifest_path = archive_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fields = (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    )
    controller_roots: dict[str, dict[str, str]] = {}
    for directory in sorted(raw.iterdir()):
        if not directory.is_dir():
            continue
        launch_path = directory / "launch.json"
        result_path = directory / "result.json"
        if not (launch_path.is_file() and result_path.is_file()):
            continue
        launch = json.loads(launch_path.read_text(encoding="utf-8"))
        result = json.loads(result_path.read_text(encoding="utf-8"))
        category = "setup" if launch.get("setup_test") else "problem"
        problem_id = str(launch["problem_id"])
        for thread_id in result.get("thread_ids", []):
            if not isinstance(thread_id, str):
                raise RuntimeError(f"non-string thread id in {result_path}")
            key = thread_id.lower()
            value = {"category": category, "problem_id": problem_id}
            if key in controller_roots and controller_roots[key] != value:
                raise RuntimeError(f"controller thread has conflicting ownership: {thread_id}")
            controller_roots[key] = value

    pricing = json.loads((repo / "speedrun/pricing.v1.json").read_text(encoding="utf-8"))
    threshold = int(pricing["context_threshold_input_tokens"])
    category_names = (
        "problem_controller",
        "nested_problem_agents",
        "problem_subtrees",
        "orchestration_support",
        "setup",
    )

    def empty_aggregate() -> dict[str, object]:
        return {
            "sessions": 0,
            "model_calls": 0,
            "long_context_calls": 0,
            "api_equivalent_cost_usd": Decimal("0"),
            **{field: 0 for field in fields},
        }

    def add_usage(target: dict[str, object], usage: dict[str, int]) -> None:
        for field in fields:
            target[field] = int(target[field]) + usage[field]

    def price_usage(model: str, usage: dict[str, int]) -> tuple[Decimal, bool]:
        long_context = usage["input_tokens"] > threshold
        context_class = "long" if long_context else "short"
        try:
            rates = pricing["models"][model][context_class]
        except KeyError as error:
            raise RuntimeError(f"missing pinned pricing for {model}/{context_class}") from error
        uncached = (
            usage["input_tokens"]
            - usage["cached_input_tokens"]
            - usage["cache_write_input_tokens"]
        )
        if uncached < 0 or usage["reasoning_output_tokens"] > usage["output_tokens"]:
            raise RuntimeError(f"invalid usage counters for {model}: {usage}")
        cost = (
            Decimal(uncached) * Decimal(rates["input"])
            + Decimal(usage["cached_input_tokens"]) * Decimal(rates["cached_input"])
            + Decimal(usage["cache_write_input_tokens"])
            * Decimal(rates["cache_write_input"])
            + Decimal(usage["output_tokens"]) * Decimal(rates["output"])
        ) / Decimal(1_000_000)
        return cost, long_context

    categories = {name: empty_aggregate() for name in category_names}
    sessions: dict[str, dict[str, object]] = {}
    turn_sessions: dict[str, set[str]] = defaultdict(set)
    token_variants: dict[
        tuple[str, tuple[int, ...]], set[tuple[str, tuple[int, ...]]]
    ] = defaultdict(set)
    raw_token_records = 0
    prefix_token_records = 0
    rollout_entries = [entry for entry in manifest["files"] if entry["archive"].startswith("rollouts/")]
    for entry in rollout_entries:
        path = archive_root / entry["archive"]
        thread_id: str | None = None
        parent_thread_id: str | None = None
        started_at = ""
        source: object = ""
        current_turn: str | None = None
        current_model: str | None = None
        cumulative_samples: list[int] = []
        with gzip.open(path, "rt", encoding="utf-8", errors="strict") as stream:
            for line in stream:
                event = json.loads(line)
                payload = event.get("payload", {})
                if event.get("type") == "session_meta" and thread_id is None:
                    candidate = payload.get("id")
                    if isinstance(candidate, str):
                        thread_id = candidate.lower()
                        started_at = str(payload.get("timestamp") or event.get("timestamp") or "")
                        source = payload.get("source", "")
                        try:
                            parent = source["subagent"]["thread_spawn"]["parent_thread_id"]
                        except (KeyError, TypeError):
                            parent = None
                        if parent is not None:
                            if not isinstance(parent, str):
                                raise RuntimeError(f"non-string parent thread id in {path}")
                            parent_thread_id = parent.lower()
                if event.get("type") == "turn_context":
                    turn = payload.get("turn_id")
                    model = payload.get("model")
                    current_turn = turn.lower() if isinstance(turn, str) else None
                    current_model = model if isinstance(model, str) else None
                    if thread_id is not None and current_turn is not None:
                        turn_sessions[current_turn].add(thread_id)
                if event.get("type") == "event_msg" and payload.get("type") == "token_count":
                    raw_token_records += 1
                    info = payload.get("info", {})
                    raw_usage = info.get("total_token_usage")
                    last_usage = info.get("last_token_usage")
                    if isinstance(raw_usage, dict):
                        cumulative_samples.append(int(raw_usage.get("total_tokens", 0)))
                    if not (
                        current_turn is not None
                        and current_model is not None
                        and isinstance(raw_usage, dict)
                        and isinstance(last_usage, dict)
                    ):
                        prefix_token_records += 1
                        continue
                    total_key = tuple(int(raw_usage.get(field, 0)) for field in fields)
                    last_value = tuple(int(last_usage.get(field, 0)) for field in fields)
                    token_variants[(current_turn, total_key)].add((current_model, last_value))
        if thread_id is None or thread_id in sessions:
            raise RuntimeError(f"missing or duplicate archived thread id: {path}")
        if any(right < left for left, right in zip(cumulative_samples, cumulative_samples[1:])):
            raise RuntimeError(f"non-monotone cumulative token usage in {path}")
        spawn: dict[str, object] = {}
        if isinstance(source, dict):
            possible = source.get("subagent", {})
            if isinstance(possible, dict):
                possible = possible.get("thread_spawn", {})
                if isinstance(possible, dict):
                    spawn = possible
        sessions[thread_id] = {
            "thread_id": thread_id,
            "parent_thread_id": parent_thread_id,
            "started_at_utc": started_at,
            "source_kind": "subagent" if parent_thread_id else str(source),
            "agent_path": str(spawn.get("agent_path") or ""),
            "agent_nickname": str(spawn.get("agent_nickname") or ""),
            "archive_path": entry["archive"],
            "archive_sha256": str(entry["archive_sha256"]),
        }

    if len(rollout_entries) != int(manifest["rollout_count"]):
        raise RuntimeError("rollout manifest count mismatch")
    if not set(controller_roots).issubset(sessions):
        raise RuntimeError("rollout manifest/thread classification mismatch")
    missing_parents = sorted(
        str(row["parent_thread_id"])
        for row in sessions.values()
        if row["parent_thread_id"] is not None and row["parent_thread_id"] not in sessions
    )
    if missing_parents:
        raise RuntimeError(f"archived sessions have missing parents: {missing_parents}")

    roots: dict[str, str] = {}
    depths: dict[str, int] = {}

    def find_root(thread_id: str) -> tuple[str, int]:
        chain: list[str] = []
        current = thread_id
        seen: set[str] = set()
        while sessions[current]["parent_thread_id"] is not None:
            if current in seen:
                raise RuntimeError(f"cycle in archived session parent graph at {current}")
            seen.add(current)
            chain.append(current)
            current = str(sessions[current]["parent_thread_id"])
        return current, len(chain)

    for thread_id in sessions:
        roots[thread_id], depths[thread_id] = find_root(thread_id)
    if any(sessions[root]["parent_thread_id"] is not None for root in roots.values()):
        raise RuntimeError("session root computation failed")
    if any(root in controller_roots and root != thread_id for thread_id, root in roots.items() if thread_id in controller_roots):
        raise RuntimeError("controller rollout unexpectedly has a parent")

    per_problem: dict[str, dict[str, object]] = defaultdict(empty_aggregate)
    owned_models: dict[str, set[str]] = defaultdict(set)
    owner_by_turn: dict[str, str] = {}
    copied_turn_ids = 0
    max_turn_copies = 0
    for turn_id, candidates in turn_sessions.items():
        if not candidates:
            raise RuntimeError(f"turn has no archived session: {turn_id}")
        candidate_roots = {roots[thread_id] for thread_id in candidates}
        if len(candidate_roots) != 1:
            raise RuntimeError(f"copied turn crosses session roots: {turn_id}")
        minimum = min(depths[thread_id] for thread_id in candidates)
        owners = [thread_id for thread_id in candidates if depths[thread_id] == minimum]
        if len(owners) != 1:
            raise RuntimeError(f"turn has ambiguous nearest owner: {turn_id}")
        owner_by_turn[turn_id] = owners[0]
        copied_turn_ids += int(len(candidates) > 1)
        max_turn_copies = max(max_turn_copies, len(candidates))

    def ownership(thread_id: str) -> tuple[str, str, str]:
        root = roots[thread_id]
        controller = controller_roots.get(root)
        if controller and controller["category"] == "problem":
            problem_id = controller["problem_id"]
            category = "problem_controller" if thread_id == root else "nested_problem_agents"
            return category, problem_id, root
        if controller and controller["category"] == "setup":
            return "setup", controller["problem_id"], root
        return "orchestration_support", "", root

    for thread_id in sessions:
        category, problem_id, _root = ownership(thread_id)
        categories[category]["sessions"] = int(categories[category]["sessions"]) + 1
        if category in {"problem_controller", "nested_problem_agents"}:
            categories["problem_subtrees"]["sessions"] = (
                int(categories["problem_subtrees"]["sessions"]) + 1
            )
            problem_row = per_problem[problem_id]
            problem_row["sessions"] = int(problem_row["sessions"]) + 1
            if category == "nested_problem_agents":
                problem_row["nested_sessions"] = int(problem_row.get("nested_sessions", 0)) + 1
            else:
                problem_row["controller_sessions"] = int(
                    problem_row.get("controller_sessions", 0)
                ) + 1

    zero_duplicate_variants = 0
    max_call_input_tokens = 0
    for (turn_id, _total_usage), variants in token_variants.items():
        nonzero = [variant for variant in variants if any(variant[1])]
        if len(nonzero) != 1:
            raise RuntimeError(
                f"token sample has {len(nonzero)} distinct nonzero last usages: {turn_id}"
            )
        zero_duplicate_variants += int(any(not any(variant[1]) for variant in variants))
        model, raw_last = nonzero[0]
        usage = dict(zip(fields, raw_last, strict=True))
        owner = owner_by_turn.get(turn_id)
        if owner is None:
            raise RuntimeError(f"priced token sample has no turn owner: {turn_id}")
        owned_models[owner].add(model)
        category, problem_id, _root = ownership(owner)
        cost, long_context = price_usage(model, usage)
        max_call_input_tokens = max(max_call_input_tokens, usage["input_tokens"])
        for target in (
            categories[category],
            categories["problem_subtrees"]
            if category in {"problem_controller", "nested_problem_agents"}
            else None,
            per_problem[problem_id]
            if category in {"problem_controller", "nested_problem_agents"}
            else None,
        ):
            if target is None:
                continue
            target["model_calls"] = int(target["model_calls"]) + 1
            target["long_context_calls"] = int(target["long_context_calls"]) + int(
                long_context
            )
            target["api_equivalent_cost_usd"] = Decimal(
                target["api_equivalent_cost_usd"]
            ) + cost
            add_usage(target, usage)

    nonoverlapping = (
        "problem_controller",
        "nested_problem_agents",
        "orchestration_support",
        "setup",
    )
    total = empty_aggregate()
    for name in nonoverlapping:
        row = categories[name]
        total["sessions"] = int(total["sessions"]) + int(row["sessions"])
        total["model_calls"] = int(total["model_calls"]) + int(row["model_calls"])
        total["long_context_calls"] = int(total["long_context_calls"]) + int(
            row["long_context_calls"]
        )
        total["api_equivalent_cost_usd"] = Decimal(
            total["api_equivalent_cost_usd"]
        ) + Decimal(row["api_equivalent_cost_usd"])
        add_usage(total, {field: int(row[field]) for field in fields})

    def serializable(row: dict[str, object]) -> dict[str, object]:
        normalized = dict(row)
        normalized["api_equivalent_cost_usd"] = format(
            Decimal(row["api_equivalent_cost_usd"]), ".9f"
        )
        return normalized

    trace_index: list[dict[str, object]] = []
    for thread_id, row in sessions.items():
        category, problem_id, root = ownership(thread_id)
        if category == "problem_controller":
            role = "primary_problem_rollout"
        elif category == "nested_problem_agents":
            role = "nested_problem_subagent"
        elif category == "setup":
            role = "setup"
        elif thread_id == root:
            role = "root_orchestrator"
        else:
            role = "orchestration_support"
        trace_index.append(
            {
                "thread_id": thread_id,
                "parent_thread_id": row["parent_thread_id"] or "",
                "root_thread_id": root,
                "depth": depths[thread_id],
                "category": category,
                "role": role,
                "owning_problem": problem_id,
                "agent_path": row["agent_path"],
                "agent_nickname": row["agent_nickname"],
                "started_at_utc": row["started_at_utc"],
                "models": ";".join(sorted(owned_models[thread_id])),
                "archive_path": row["archive_path"],
                "archive_sha256": row["archive_sha256"],
            }
        )
    trace_index.sort(key=lambda row: (str(row["started_at_utc"]), str(row["thread_id"])))

    normalized_per_problem = {
        problem_id: serializable(row) for problem_id, row in sorted(per_problem.items())
    }
    result = {
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "rollout_count": len(rollout_entries),
        "pricing_method": "globally deduplicated nonzero last_token_usage by turn_id and cumulative usage",
        "raw_token_count_records": raw_token_records,
        "inherited_prefix_token_records": prefix_token_records,
        "zero_duplicate_variants": zero_duplicate_variants,
        "copied_turn_ids": copied_turn_ids,
        "max_turn_copies": max_turn_copies,
        "max_call_input_tokens": max_call_input_tokens,
        "session_parent_edges": sum(
            row["parent_thread_id"] is not None for row in sessions.values()
        ),
        "categories": {name: serializable(row) for name, row in categories.items()},
        "per_problem": normalized_per_problem,
        "all": serializable(total),
    }
    return result, trace_index


def svg_text(x: float, y: float, value: str, **attrs: object) -> str:
    encoded = html.escape(value)
    def attribute_name(key: str) -> str:
        return "class" if key == "class_" else key.replace("_", "-")

    extra = " ".join(f'{attribute_name(key)}="{val}"' for key, val in attrs.items())
    return f'<text x="{x:.1f}" y="{y:.1f}" {extra}>{encoded}</text>'


def scale_log(value: float, low: float, high: float, start: float, end: float) -> float:
    value = max(value, low)
    if high <= low:
        return (start + end) / 2
    ratio = (math.log10(value) - math.log10(low)) / (math.log10(high) - math.log10(low))
    return start + ratio * (end - start)


def render_dashboard(rows: list[dict[str, object]], output: Path) -> None:
    solved = sorted((row for row in rows if row["solved"]), key=lambda row: row["solve_number"])
    width, height = 1500, 1100
    panels = {
        "solves": (85, 90, 650, 380),
        "cost": (820, 90, 650, 380),
        "scatter": (85, 610, 650, 380),
        "bars": (820, 610, 650, 380),
    }
    pieces = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:system-ui,-apple-system,sans-serif;fill:#172033}.title{font-size:28px;font-weight:700}.panel{font-size:19px;font-weight:650}.axis{font-size:12px;fill:#526078}.grid{stroke:#d9dfeb;stroke-width:1}.frame{fill:#fbfcff;stroke:#bac4d6}.line{fill:none;stroke:#2463eb;stroke-width:3}.costline{fill:none;stroke:#d14b31;stroke-width:3}.dot{fill:#2463eb;fill-opacity:.68;stroke:#153a8a}.bar{fill:#8a56d6}.note{font-size:12px;fill:#526078}</style>",
        svg_text(60, 45, "Vasily 24-hour GPT-5.6 LeanEval speedrun — aggregate metrics", class_="title"),
    ]
    max_elapsed = max(float(row["race_elapsed_seconds"]) for row in solved)
    min_elapsed = max(1.0, min(float(row["race_elapsed_seconds"]) for row in solved))

    def frame(name: str, title: str) -> tuple[float, float, float, float]:
        x, y, w, h = panels[name]
        pieces.append(f'<rect class="frame" x="{x}" y="{y}" width="{w}" height="{h}" rx="8"/>')
        pieces.append(svg_text(x + 18, y + 30, title, class_="panel"))
        return x + 62, y + 55, w - 88, h - 100

    # Cumulative solves over logarithmic elapsed time.
    x0, y0, w0, h0 = frame("solves", "Cumulative verified solves (logarithmic time)")
    for hour in [1 / 60, 1 / 6, 1, 3, 6, 12, 24]:
        sec = hour * 3600
        if min_elapsed <= sec <= max_elapsed:
            x = scale_log(sec, min_elapsed, max_elapsed, x0, x0 + w0)
            pieces.append(f'<line class="grid" x1="{x:.1f}" y1="{y0}" x2="{x:.1f}" y2="{y0+h0}"/>')
            label = f"{hour * 60:.0f}m" if hour < 1 else f"{hour:g}h"
            pieces.append(svg_text(x, y0 + h0 + 20, label, class_="axis", text_anchor="middle"))
    for count in [0, 20, 40, 60, 80]:
        y = y0 + h0 - count / max(1, len(solved)) * h0
        pieces.append(f'<line class="grid" x1="{x0}" y1="{y:.1f}" x2="{x0+w0}" y2="{y:.1f}"/>')
        pieces.append(svg_text(x0 - 10, y + 4, str(count), class_="axis", text_anchor="end"))
    coords = []
    for row in solved:
        x = scale_log(float(row["race_elapsed_seconds"]), min_elapsed, max_elapsed, x0, x0 + w0)
        y = y0 + h0 - int(row["solve_number"]) / len(solved) * h0
        coords.append((x, y))
    if coords:
        path = f"M {x0:.1f} {y0+h0:.1f} " + " ".join(f"L {x:.1f} {y:.1f}" for x, y in coords)
        pieces.append(f'<path class="line" d="{path}"/>')

    # Cumulative attributable problem-subtree cost by solve time.
    x1, y1, w1, h1 = frame("cost", "Cumulative attributable cost of solved problem trees")
    cumulative = 0.0
    cost_points = []
    for row in solved:
        cumulative += float(row["problem_subtree_cost_usd"])
        cost_points.append((float(row["race_elapsed_seconds"]), cumulative))
    max_cost = max((value for _, value in cost_points), default=1.0)
    for fraction in [0, .25, .5, .75, 1]:
        y = y1 + h1 - fraction * h1
        pieces.append(f'<line class="grid" x1="{x1}" y1="{y:.1f}" x2="{x1+w1}" y2="{y:.1f}"/>')
        pieces.append(svg_text(x1 - 10, y + 4, f"${max_cost*fraction:,.0f}", class_="axis", text_anchor="end"))
    for hour in [1 / 60, 1 / 6, 1, 3, 6, 12, 24]:
        sec = hour * 3600
        if min_elapsed <= sec <= max_elapsed:
            x = scale_log(sec, min_elapsed, max_elapsed, x1, x1 + w1)
            pieces.append(f'<line class="grid" x1="{x:.1f}" y1="{y1}" x2="{x:.1f}" y2="{y1+h1}"/>')
            label = f"{hour * 60:.0f}m" if hour < 1 else f"{hour:g}h"
            pieces.append(svg_text(x, y1 + h1 + 20, label, class_="axis", text_anchor="middle"))
    if cost_points:
        coords = [
            (
                scale_log(seconds, min_elapsed, max_elapsed, x1, x1 + w1),
                y1 + h1 - value / max_cost * h1,
            )
            for seconds, value in cost_points
        ]
        path = f"M {x1:.1f} {y1+h1:.1f} " + " ".join(f"L {x:.1f} {y:.1f}" for x, y in coords)
        pieces.append(f'<path class="costline" d="{path}"/>')

    # Cost vs proof LOC, both logarithmic.
    x2, y2, w2, h2 = frame(
        "scatter", "Attributable problem-tree cost vs added Lean LOC (log–log)"
    )
    scatter = [
        row
        for row in solved
        if int(row["added_code_loc"]) > 0 and float(row["problem_subtree_cost_usd"]) > 0
    ]
    min_loc = min((int(row["added_code_loc"]) for row in scatter), default=1)
    max_loc = max((int(row["added_code_loc"]) for row in scatter), default=2)
    min_scost = min((float(row["problem_subtree_cost_usd"]) for row in scatter), default=.01)
    max_scost = max((float(row["problem_subtree_cost_usd"]) for row in scatter), default=1)
    for value in [10, 30, 100, 300, 1000, 3000, 10000]:
        if min_loc <= value <= max_loc:
            x = scale_log(value, min_loc, max_loc, x2, x2 + w2)
            pieces.append(f'<line class="grid" x1="{x:.1f}" y1="{y2}" x2="{x:.1f}" y2="{y2+h2}"/>')
            pieces.append(svg_text(x, y2 + h2 + 20, str(value), class_="axis", text_anchor="middle"))
    for value in [.1, .3, 1, 3, 10, 30, 100]:
        if min_scost <= value <= max_scost:
            y = scale_log(value, min_scost, max_scost, y2 + h2, y2)
            pieces.append(f'<line class="grid" x1="{x2}" y1="{y:.1f}" x2="{x2+w2}" y2="{y:.1f}"/>')
            pieces.append(svg_text(x2 - 10, y + 4, f"${value:g}", class_="axis", text_anchor="end"))
    label_rows = set(
        row["problem_id"]
        for row in sorted(
            scatter, key=lambda item: float(item["problem_subtree_cost_usd"]), reverse=True
        )[:5]
    )
    for row in scatter:
        x = scale_log(int(row["added_code_loc"]), min_loc, max_loc, x2, x2 + w2)
        y = scale_log(
            float(row["problem_subtree_cost_usd"]), min_scost, max_scost, y2 + h2, y2
        )
        pieces.append(f'<circle class="dot" cx="{x:.1f}" cy="{y:.1f}" r="4"><title>{html.escape(str(row["problem_id"]))}: {row["added_code_loc"]} LOC, ${float(row["problem_subtree_cost_usd"]):.2f}</title></circle>')
        if row["problem_id"] in label_rows:
            pieces.append(svg_text(x + 6, y - 5, str(row["problem_id"])[:28], class_="note"))

    # Top costs.
    x3, y3, w3, h3 = frame(
        "bars", "Most expensive solved problem trees (API-equivalent USD)"
    )
    top = sorted(
        solved, key=lambda row: float(row["problem_subtree_cost_usd"]), reverse=True
    )[:12]
    bar_max = max((float(row["problem_subtree_cost_usd"]) for row in top), default=1)
    bar_h = h3 / max(1, len(top))
    label_w = 255
    for index, row in enumerate(top):
        y = y3 + index * bar_h + 2
        value = float(row["problem_subtree_cost_usd"])
        pieces.append(svg_text(x3 + label_w - 8, y + bar_h * .63, str(row["problem_id"])[:34], class_="axis", text_anchor="end"))
        length = (w3 - label_w - 52) * value / bar_max
        pieces.append(f'<rect class="bar" x="{x3+label_w}" y="{y+3:.1f}" width="{length:.1f}" height="{max(5,bar_h-7):.1f}" rx="2"/>')
        pieces.append(svg_text(x3 + label_w + length + 5, y + bar_h * .63, f"${value:.2f}", class_="axis"))

    pieces.append(svg_text(60, 1065, "Time axes are logarithmic; costs include exact problem-subtree agents and use pinned setup-time API-equivalent rates.", class_="note"))
    pieces.append("</svg>")
    output.write_text("\n".join(pieces) + "\n", encoding="utf-8")


def markdown_summary(
    rows: list[dict[str, object]], generated_at: str, baseline: str, rollout_archive: dict[str, object]
) -> str:
    attempted = rows
    solved = [row for row in rows if row["solved"]]
    subtree_costs = [float(row["problem_subtree_cost_usd"]) for row in solved]
    locs = [int(row["added_code_loc"]) for row in solved]
    runtimes = [float(row["controller_agent_seconds_total"]) for row in solved]
    subtree_cost = sum(float(row["problem_subtree_cost_usd"]) for row in attempted)
    solved_subtree_cost = sum(subtree_costs)
    controller_cost = sum(float(row["controller_cost_usd"]) for row in attempted)
    controller_input = sum(int(row["controller_input_tokens"]) for row in attempted)
    controller_cached = sum(int(row["controller_cached_input_tokens"]) for row in attempted)
    controller_output = sum(int(row["controller_output_tokens"]) for row in attempted)
    controller_reasoning = sum(
        int(row["controller_reasoning_output_tokens"]) for row in attempted
    )
    subtree_input = sum(int(row["problem_subtree_input_tokens"]) for row in attempted)
    subtree_cached = sum(
        int(row["problem_subtree_cached_input_tokens"]) for row in attempted
    )
    subtree_output = sum(int(row["problem_subtree_output_tokens"]) for row in attempted)
    subtree_reasoning = sum(
        int(row["problem_subtree_reasoning_output_tokens"]) for row in attempted
    )
    total_runtime = sum(float(row["controller_agent_seconds_total"]) for row in attempted)
    total_verify = sum(
        float(row["controller_verification_seconds_total"]) for row in attempted
    )
    total_runs = sum(int(row["controller_runs"]) for row in attempted)
    total_rollouts = sum(int(row["controller_rollout_sessions"]) for row in attempted)
    nested_sessions = sum(int(row["nested_subagent_sessions"]) for row in attempted)
    subtree_sessions = sum(int(row["problem_subtree_sessions"]) for row in attempted)
    retried = sum(int(row["controller_runs"]) > 1 for row in attempted)
    controller_cache_rate = controller_cached / controller_input if controller_input else 0
    archived_all = rollout_archive["all"]
    archived_categories = rollout_archive["categories"]
    support = archived_categories["orchestration_support"]
    nested = archived_categories["nested_problem_agents"]
    setup = archived_categories["setup"]
    archived_input = int(archived_all["input_tokens"])
    archived_cached = int(archived_all["cached_input_tokens"])
    archived_output = int(archived_all["output_tokens"])
    archived_reasoning = int(archived_all["reasoning_output_tokens"])
    archived_cost = float(archived_all["api_equivalent_cost_usd"])

    def top_table(field: str, formatter, n: int = 10) -> str:
        top = sorted(attempted, key=lambda row: float(row[field]), reverse=True)[:n]
        lines = [
            "| Problem | Value | Controller runs | Nested agents | Added code LOC |",
            "|---|---:|---:|---:|---:|",
        ]
        for row in top:
            loc = f"{int(row['added_code_loc']):,}" if row["solved"] else "—"
            lines.append(
                f"| `{row['problem_id']}` | {formatter(row[field])} | "
                f"{row['controller_runs']} | {row['nested_subagent_sessions']} | "
                f"{loc} |"
            )
        return "\n".join(lines)

    last = max(solved, key=lambda row: int(row["solve_number"]))
    fastest = min(solved, key=lambda row: float(row["controller_agent_seconds_total"]))
    shortest = min((row for row in solved if int(row["added_code_loc"]) > 0), key=lambda row: int(row["added_code_loc"]))
    longest = max(solved, key=lambda row: int(row["added_code_loc"]))
    return f"""# Speedrun metrics

Generated from the immutable controller results and passed audits at `{generated_at}`. Lean line deltas use pre-race Git commit `{baseline}` as their baseline.

The canonical per-problem data is in [`metrics.csv`](metrics.csv), and [`trace_index.csv`](trace_index.csv) maps all 421 archived sessions to their parents and owning problem trees. [`metrics.svg`](metrics.svg) is a visual dashboard. The [analysis index](README.md) collects logarithmic, real-time, full-24-hour, solve-time, and budget views of the 79 verified solves.

## Headline results

| Metric | Value |
|---|---:|
| Attempted real problems | {len(attempted):,} |
| Verified solves | {len(solved):,} |
| Solve rate | {len(solved)/len(attempted):.1%} |
| Controller-launched problem solver runs | {total_runs:,} |
| Primary problem rollout sessions | {total_rollouts:,} |
| Nested problem-subagent sessions | {nested_sessions:,} |
| Total problem-subtree sessions | {subtree_sessions:,} |
| All archived model threads | {int(archived_all['sessions']):,} |
| Problems retried | {retried:,} |
| API-equivalent cost, full process | ${archived_cost:,.2f} |
| API-equivalent cost, attributable problem subtrees | ${subtree_cost:,.2f} |
| API-equivalent cost, primary controller rollouts | ${controller_cost:,.2f} |
| API-equivalent cost, top-level orchestration/support | ${float(support['api_equivalent_cost_usd']):,.2f} |
| API-equivalent cost, setup test | ${float(setup['api_equivalent_cost_usd']):,.4f} |
| Attributable subtree cost for solved problems | ${solved_subtree_cost:,.2f} |
| Controller-measured active solver time, all attempts | {total_runtime/3600:,.2f} h |
| Independent verification time | {total_verify/3600:,.2f} h |
| Primary-controller input tokens | {controller_input:,} |
| Primary-controller cached input tokens | {controller_cached:,} ({controller_cache_rate:.1%} of input) |
| Primary-controller output tokens | {controller_output:,} |
| Primary-controller reasoning output tokens | {controller_reasoning:,} |
| Problem-subtree input tokens | {subtree_input:,} |
| Problem-subtree cached input tokens | {subtree_cached:,} ({subtree_cached/subtree_input:.1%} of input) |
| Problem-subtree output tokens | {subtree_output:,} |
| Problem-subtree reasoning output tokens | {subtree_reasoning:,} |
| Full-process input tokens | {archived_input:,} |
| Full-process cached input tokens | {archived_cached:,} ({archived_cached/archived_input:.1%} of input) |
| Full-process output tokens | {archived_output:,} |
| Full-process reasoning output tokens | {archived_reasoning:,} |
| Added Lean code LOC across verified solutions | {sum(locs):,} |
| Median added code LOC per solve | {statistics.median(locs):,.0f} |
| Median attributable subtree cost per solve | ${statistics.median(subtree_costs):,.2f} |
| Median active agent time per solve | {statistics.median(runtimes)/60:,.1f} min |
| 90th percentile active agent time | {percentile(runtimes,.9)/60:,.1f} min |
| Final solve | `{last['problem_id']}` at {float(last['race_elapsed_seconds'])/3600:.2f} h |

Cost is computed with the repository's pinned, setup-time (2026-08-16) API-equivalent Standard text-token rates. It is **not** an actual ChatGPT subscription charge. Per-problem costs include primary controller rollouts and every nested subagent session below their roots. Top-level orchestration and cross-problem support are reported separately rather than guessed into problem rows.

## Full-process trace accounting

| Non-overlapping trace category | Sessions | Input tokens | Cached input | Output tokens | API-eq. cost |
|---|---:|---:|---:|---:|---:|
| Primary problem rollouts | {int(archived_categories['problem_controller']['sessions']):,} | {int(archived_categories['problem_controller']['input_tokens']):,} | {int(archived_categories['problem_controller']['cached_input_tokens']):,} | {int(archived_categories['problem_controller']['output_tokens']):,} | ${float(archived_categories['problem_controller']['api_equivalent_cost_usd']):,.2f} |
| Nested problem agents | {int(nested['sessions']):,} | {int(nested['input_tokens']):,} | {int(nested['cached_input_tokens']):,} | {int(nested['output_tokens']):,} | ${float(nested['api_equivalent_cost_usd']):,.2f} |
| Setup test | {int(archived_categories['setup']['sessions']):,} | {int(archived_categories['setup']['input_tokens']):,} | {int(archived_categories['setup']['cached_input_tokens']):,} | {int(archived_categories['setup']['output_tokens']):,} | ${float(archived_categories['setup']['api_equivalent_cost_usd']):,.4f} |
| Root orchestration + support agents | {int(support['sessions']):,} | {int(support['input_tokens']):,} | {int(support['cached_input_tokens']):,} | {int(support['output_tokens']):,} | ${float(support['api_equivalent_cost_usd']):,.2f} |
| **Full process** | **{int(archived_all['sessions']):,}** | **{archived_input:,}** | **{archived_cached:,}** | **{archived_output:,}** | **${archived_cost:,.2f}** |

Forked session files contain inherited parent history, so final cumulative counters cannot safely be summed per file. The canonical reconciliation globally deduplicates each nonzero `last_token_usage` by turn ID and cumulative usage, prices it under that turn's model, and uses the parent graph for ownership. It found {int(archived_all['model_calls']):,} unique model calls, {int(rollout_archive['session_parent_edges']):,} parent edges, and {int(rollout_archive['copied_turn_ids']):,} turn IDs copied into descendants (maximum {int(rollout_archive['max_turn_copies']):,} files). The largest call had {int(rollout_archive['max_call_input_tokens']):,} input tokens; {int(archived_all['long_context_calls']):,} calls required long-context pricing. The public archive manifest used for this reconciliation is `{rollout_archive['manifest_sha256']}`.

## Notable extremes

- Fastest verified solution by controller active time: `{fastest['problem_id']}` ({float(fastest['controller_agent_seconds_total']):.1f} s).
- Shortest verified solution delta: `{shortest['problem_id']}` ({int(shortest['added_code_loc']):,} added non-comment code LOC).
- Longest verified solution delta: `{longest['problem_id']}` ({int(longest['added_code_loc']):,} added non-comment code LOC).
- Aggregate controller efficiency: {len(solved)/(total_runtime/3600):.2f} solves per measured active solver-hour.
- Gross attributable problem-subtree cost per verified solve: ${subtree_cost/len(solved):.2f}; primary-controller-only cost per verified solve: ${controller_cost/len(solved):.2f}.
- Full-process cost per verified solve, including orchestration and setup: ${archived_cost/len(solved):.2f}.
- Controller-job parallelism factor: {total_runtime/(24*3600):.2f} active agent-hours per wall-clock race hour.

## Highest API-equivalent cost

{top_table('problem_subtree_cost_usd', lambda value: f'${float(value):,.2f}')}

## Longest active agent time

{top_table('controller_agent_seconds_total', lambda value: f'{float(value)/60:,.1f} min')}

## Largest proof deltas

{top_table('added_code_loc', lambda value: f'{int(value):,} LOC')}

## Column definitions

- `controller_runs`: launched controller jobs for the problem, including retries.
- `controller_rollout_sessions`: audited primary Codex sessions across those jobs; this can exceed runs after sequential account failover.
- `nested_subagent_sessions`: archived descendant sessions below the problem's primary rollout roots.
- `problem_subtree_sessions`: primary rollout plus nested session count.
- `controller_agent_seconds_total`: sum of controller-measured run durations; parallel runs therefore add together.
- `controller_*_tokens` and `controller_cost_usd`: passed-audit accounting for primary controller rollouts only.
- `problem_subtree_*_tokens` and `problem_subtree_cost_usd`: globally deduplicated accounting for primary rollouts and all exact descendants. Top-level orchestration/support is deliberately unallocated.
- `first_attempt_to_verified_seconds`: wall-clock latency from the first launch to successful independent verification.
- `first_attempt_to_final_outcome_seconds`: wall-clock span from the first launch to the last recorded result or verification, including gaps between retries; unlike active time, this is defined for failed attempts too.
- `race_elapsed_seconds`: successful verification time measured from the configured race start; this drives the chronological race-time graphs.
- Token and cost columns sum **all** attempts for that problem, including failed retries. Cached input is included in total input and priced separately.
- `solution_code_loc`: nonblank, non-comment Lean lines in the independently checked solution closure.
- `added_code_loc`: nonblank, non-comment Lean lines introduced or replaced relative to the pre-race Git `HEAD`; this is the main proof-length measure.
- `declaration_count`: theorem, lemma, definition, instance, structure, class, and inductive declaration headers in the checked solution closure.

[`trace_index.csv`](trace_index.csv) contains one row per archived session: thread and parent IDs, root, depth, ownership, role, agent path/nickname, start timestamp, owned-turn models, and manifest archive hash. It intentionally omits per-file usage because forked files copy parent histories; global turn deduplication is the canonical accounting method.
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", required=True, help="pre-race Git commit for LOC deltas")
    args = parser.parse_args()
    repo = args.repo.resolve()
    raw = args.raw_root.resolve()
    output = args.output.resolve()
    baseline = args.baseline
    subprocess.run(["git", "cat-file", "-e", f"{baseline}^{{commit}}"], cwd=repo, check=True)
    output.mkdir(parents=True, exist_ok=True)

    import sys
    sys.path.insert(0, str(repo))
    from scripts.finalize_speedrun import discover_verified, parse_iso, read_json

    config = read_json(repo / "speedrun/config.json")
    race_start = parse_iso(config["race_start"])
    hard_stop = parse_iso(config["hard_stop"])
    selected, notes = discover_verified(raw, race_start, hard_stop, require_audits=True)
    if notes:
        raise RuntimeError(f"verified discovery notes were not empty: {notes}")

    jobs: dict[str, list[dict[str, object]]] = defaultdict(list)
    for directory in sorted(raw.iterdir()):
        if not directory.is_dir() or directory.name.startswith("_"):
            continue
        launch_path = directory / "launch.json"
        result_path = directory / "result.json"
        audit_path = directory / "audit.json"
        if not (launch_path.is_file() and result_path.is_file() and audit_path.is_file()):
            continue
        launch = json.loads(launch_path.read_text(encoding="utf-8"))
        if launch.get("setup_test"):
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        if not audit.get("passed"):
            raise RuntimeError(f"audit did not pass for {directory.name}")
        verification_path = directory / "verification.json"
        verification = json.loads(verification_path.read_text(encoding="utf-8")) if verification_path.is_file() else None
        jobs[str(launch["problem_id"])].append(
            {"directory": directory, "launch": launch, "result": result, "audit": audit, "verification": verification}
        )

    solve_order = {
        problem: index
        for index, problem in enumerate(
            sorted(selected, key=lambda problem: int(selected[problem]["verified_ns"])), 1
        )
    }
    rows: list[dict[str, object]] = []
    usage_fields = (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    )
    for problem in sorted(jobs):
        attempts = sorted(jobs[problem], key=lambda job: job["launch"]["started_wall_ns"])
        solved = problem in selected
        usage = {field: 0 for field in usage_fields}
        cost = Decimal("0")
        agent_seconds = verification_seconds = 0.0
        rollout_count = 0
        models: set[str] = set()
        statuses: Counter[str] = Counter()
        for job in attempts:
            audit = job["audit"]
            result = job["result"]
            for field in usage_fields:
                usage[field] += int(audit["accounted_usage"].get(field, 0))
            cost += Decimal(str(audit["accounted_api_equivalent_token_cost_usd"]))
            agent_seconds += int(result.get("duration_ns") or 0) / 1e9
            verification = job["verification"]
            if verification:
                verification_seconds += int(verification.get("duration_ns") or 0) / 1e9
            rollout_count += len(audit.get("rollouts", []))
            models.update(str(model) for model in audit.get("observed_models", []))
            statuses[str(result.get("status"))] += 1
        first_started = min(parse_time(str(job["launch"]["started_at_utc"])) for job in attempts)
        final_finished = max(
            parse_time(
                str(
                    job["verification"]["finished_at_utc"]
                    if job["verification"] and job["verification"].get("finished_at_utc")
                    else job["result"]["finished_at_utc"]
                )
            )
            for job in attempts
        )
        outcome_latency = (final_finished - first_started).total_seconds()
        if solved:
            verified_at_text = str(selected[problem]["verified_at"])
            verified_at = parse_time(verified_at_text)
            loc = solution_metrics(repo, baseline, list(selected[problem]["files"]))
            latency = (verified_at - first_started).total_seconds()
            race_elapsed = (verified_at - race_start).total_seconds()
            verified_job_id = str(selected[problem]["job_id"])
        else:
            verified_at_text = ""
            verified_at = None
            loc = {
                "lean_files": 0,
                "solution_physical_loc": 0,
                "solution_code_loc": 0,
                "added_physical_loc": 0,
                "added_code_loc": 0,
                "declaration_count": 0,
            }
            latency = 0.0
            race_elapsed = 0.0
            verified_job_id = ""
        row: dict[str, object] = {
            "problem_id": problem,
            "solved": solved,
            "solve_number": solve_order.get(problem, 0),
            "verified_job_id": verified_job_id,
            "verified_at_utc": verified_at_text,
            "race_elapsed_seconds": round(race_elapsed, 3),
            "first_attempt_to_verified_seconds": round(latency, 3),
            "first_attempt_to_final_outcome_seconds": round(outcome_latency, 3),
            "controller_runs": len(attempts),
            "controller_failed_runs": len(attempts) - (1 if solved else 0),
            "controller_rollout_sessions": rollout_count,
            "controller_models": ";".join(sorted(models)),
            "controller_statuses": ";".join(
                f"{key}:{statuses[key]}" for key in sorted(statuses)
            ),
            "controller_agent_seconds_total": round(agent_seconds, 3),
            "controller_verification_seconds_total": round(verification_seconds, 3),
            "controller_cost_usd": format(cost, ".9f"),
            **{f"controller_{field}": usage[field] for field in usage_fields},
            **loc,
        }
        row["controller_cached_input_fraction"] = (
            round(usage["cached_input_tokens"] / usage["input_tokens"], 6)
            if usage["input_tokens"]
            else 0
        )
        row["controller_cost_per_added_code_loc"] = (
            round(float(cost) / int(row["added_code_loc"]), 6)
            if int(row["added_code_loc"])
            else 0
        )
        rows.append(row)

    rollout_archive, trace_index = archived_rollout_metrics(repo, raw)
    trace_problems = rollout_archive["per_problem"]
    if set(trace_problems) != set(jobs):
        raise RuntimeError(
            "trace problem ownership mismatch: "
            f"trace_only={sorted(set(trace_problems) - set(jobs))}, "
            f"jobs_only={sorted(set(jobs) - set(trace_problems))}"
        )
    for row in rows:
        problem_id = str(row["problem_id"])
        trace = trace_problems[problem_id]
        if int(trace["controller_sessions"]) != int(row["controller_rollout_sessions"]):
            raise RuntimeError(f"primary rollout count mismatch for {problem_id}")
        row.update(
            {
                "nested_subagent_sessions": int(trace.get("nested_sessions", 0)),
                "problem_subtree_sessions": int(trace["sessions"]),
                "problem_subtree_model_calls": int(trace["model_calls"]),
                "problem_subtree_long_context_calls": int(trace["long_context_calls"]),
                **{
                    f"problem_subtree_{field}": int(trace[field]) for field in usage_fields
                },
                "problem_subtree_cost_usd": str(trace["api_equivalent_cost_usd"]),
                "nested_subagent_cost_usd": format(
                    Decimal(str(trace["api_equivalent_cost_usd"]))
                    - Decimal(str(row["controller_cost_usd"])),
                    ".9f",
                ),
            }
        )
        row["problem_subtree_cached_input_fraction"] = round(
            int(row["problem_subtree_cached_input_tokens"])
            / int(row["problem_subtree_input_tokens"]),
            6,
        )
        row["problem_subtree_cost_per_added_code_loc"] = (
            round(
                float(row["problem_subtree_cost_usd"]) / int(row["added_code_loc"]), 6
            )
            if int(row["added_code_loc"])
            else 0
        )

    problem_trace = rollout_archive["categories"]["problem_controller"]
    for field in usage_fields:
        if sum(int(row[f"controller_{field}"]) for row in rows) != int(problem_trace[field]):
            raise RuntimeError(f"problem-job trace reconciliation failed for {field}")
    if sum(Decimal(str(row["controller_cost_usd"])) for row in rows) != Decimal(
        str(problem_trace["api_equivalent_cost_usd"])
    ):
        raise RuntimeError("problem-job trace reconciliation failed for cost")
    subtree_trace = rollout_archive["categories"]["problem_subtrees"]
    for field in usage_fields:
        if sum(int(row[f"problem_subtree_{field}"]) for row in rows) != int(
            subtree_trace[field]
        ):
            raise RuntimeError(f"problem-subtree trace reconciliation failed for {field}")
    if sum(Decimal(str(row["problem_subtree_cost_usd"])) for row in rows) != Decimal(
        str(subtree_trace["api_equivalent_cost_usd"])
    ):
        raise RuntimeError("problem-subtree trace reconciliation failed for cost")

    fields = list(rows[0])
    with (output / "metrics.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    trace_fields = list(trace_index[0])
    with (output / "trace_index.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=trace_fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(trace_index)
    rollout_archive["trace_index_rows"] = len(trace_index)
    rollout_archive["trace_index_sha256"] = hashlib.sha256(
        (output / "trace_index.csv").read_bytes()
    ).hexdigest()
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    payload = {
        "schema": "lean-eval-speedrun-aggregate-metrics-v1",
        "generated_at_utc": generated_at,
        "race_start": config["race_start"],
        "hard_stop": config["hard_stop"],
        "baseline_commit": baseline,
        "cost_basis": read_json(repo / "speedrun/pricing.v1.json")["cost_basis"],
        "rollout_archive": rollout_archive,
        "problems": rows,
    }
    (output / "metrics.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "metrics.md").write_text(
        markdown_summary(rows, generated_at, baseline, rollout_archive), encoding="utf-8"
    )
    render_dashboard(rows, output / "metrics.svg")
    print(f"METRICS_OK problems={len(rows)} solved={sum(bool(row['solved']) for row in rows)} output={output}")


if __name__ == "__main__":
    main()
