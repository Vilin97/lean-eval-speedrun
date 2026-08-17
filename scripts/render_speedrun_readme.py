#!/usr/bin/env python3
"""Render the public experiment README from the aggregate metrics CSV."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


ORIGINAL_PROMPT = """we are speedrunning lean-eval. solve as many lean-eval problems in 24 hours as you can. You are allowed and encouraged to use anything (e.g. mathlib, LeanPool, TauCeti, anything else) except straight-up copying existing solutions (do not try to search for existing solutions - unfortunately many of them are leaked). You can gauge the difficulty of the problem by how many solutions it has in the leaderboard. For easier problems, try to just solve them fast. For harder problems, make a detailed informal proof and scout the existing Lean repos like mathlib, Lean pool, Tau Ceti and others for what's already built that's useful, and make a detailed blueprint before formalizing. Stay under 128gb ram. Do not use any set_options, do not use native_decide. Use subagents aggressively, up to 16 concurrent subagents. You can choose the model and reasoning level for subagents. For each problem you must log how long it took and its approximate token cost using official API pricing. Set up this logging to be deterministic and not self-reported by the agents (test it on a toy problem to make sure it works before going all-in). After exactly 24 hours from the start, stop all work immediately, push the solutions and logs to the repo, and submit all your solutions to lean-eval under name Vasily-24-hour-gpt-5.6-speedrun. Make a graph of solves over time, with time on logarithmic scale. You are alllowed up to 1 hour to set up whatever you need to make the speedrun successful, e.g. fetch mathlib cache, decide on the order of problems (I recommend easier to harder), write a logging script, anything else that's helpful (but don't overthink it). After exactly 1 hour, the 24h speedrun starts. Be careful not to run out of disk space if each agent gets its own worktree. I expect you to be mostly orchestrating a bunch of subagents. Save all the logs from this chat somewhere, where they can be analyzed later on."""


def duration(seconds: float) -> str:
    rounded = round(seconds)
    if rounded < 120:
        return f"{rounded}s"
    minutes, secs = divmod(rounded, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--solution-commit", required=True)
    parser.add_argument("--logs-commit", required=True)
    parser.add_argument("--submission-url", required=True)
    args = parser.parse_args()
    rows = list(csv.DictReader(args.metrics.open(encoding="utf-8")))
    metrics_payload = json.loads(args.metrics.with_suffix(".json").read_text(encoding="utf-8"))
    archived = metrics_payload["rollout_archive"]["all"]
    archived_categories = metrics_payload["rollout_archive"]["categories"]
    solved = [row for row in rows if row["solved"] == "True"]
    problem_cost = sum(float(row["problem_subtree_cost_usd"]) for row in rows)
    controller_cost = sum(float(row["controller_cost_usd"]) for row in rows)
    total_runs = sum(int(row["controller_runs"]) for row in rows)
    controller_rollouts = sum(int(row["controller_rollout_sessions"]) for row in rows)
    nested_agents = sum(int(row["nested_subagent_sessions"]) for row in rows)
    problem_sessions = sum(int(row["problem_subtree_sessions"]) for row in rows)
    total_runtime = sum(float(row["controller_agent_seconds_total"]) for row in rows)
    total_loc = sum(int(row["added_code_loc"]) for row in solved)
    full_cost = float(archived["api_equivalent_cost_usd"])
    full_input = int(archived["input_tokens"])
    full_cached = int(archived["cached_input_tokens"])
    full_output = int(archived["output_tokens"])
    full_threads = int(archived["sessions"])
    orchestration_cost = float(
        archived_categories["orchestration_support"]["api_equivalent_cost_usd"]
    )
    prompt_quote = "\n".join(f"> {line}" if line else ">" for line in ORIGINAL_PROMPT.splitlines())

    table = [
        "| Status | Problem | Wall time | Active solver time | Attributable API-eq. cost | Runs | Nested agents | Added Lean LOC |",
        "|:--:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in sorted(rows, key=lambda value: value["problem_id"].lower()):
        if row["solved"] == "True":
            status = f"✅ #{row['solve_number']}"
            loc = f"{int(row['added_code_loc']):,}"
        else:
            status = "◻ attempted"
            loc = "—"
        problem = row["problem_id"]
        table.append(
            f"| {status} | [`{problem}`](generated/{problem}) | "
            f"{duration(float(row['first_attempt_to_final_outcome_seconds']))} | "
            f"{duration(float(row['controller_agent_seconds_total']))} | "
            f"${float(row['problem_subtree_cost_usd']):.2f} | {row['controller_runs']} | "
            f"{row['nested_subagent_sessions']} | {loc} |"
        )

    readme = f"""<div align="center">

# 24 Hours of Lean

**A GPT-5.6 LeanEval speedrun**

**{len(solved)} verified solves** · {len(rows)} attempted · {total_runs} controller runs + {nested_agents} nested problem agents · {full_threads} archived sessions

[LeanEval submission]({args.submission_url}) · [rollout traces](https://huggingface.co/datasets/Vilin97/Vasily-24-hour-gpt-5.6-speedrun-logs) · [analysis](analysis/README.md) · [audit manifest](speedrun-logs/manifest.json)

</div>

## The experiment

One hour of setup, followed by a hard 24-hour solve window from **2026-08-16 08:09:44 UTC** to **2026-08-17 08:09:44 UTC**. Every claimed solution was independently replayed through LeanEval comparator and nanoda; agent self-reports never counted as solves.

| Verified | Attempted | Full-process cost | Problem-tree cost | Primary-controller cost | Full input | Added Lean code |
|---:|---:|---:|---:|---:|---:|---:|
| **{len(solved)}** | **{len(rows)}** | **${full_cost:,.2f}** | **${problem_cost:,.2f}** | **${controller_cost:,.2f}** | **{full_input/1e9:,.2f}B** ({full_cached/full_input:.1%} cached) | **{total_loc:,} LOC** |

> Cost uses pinned, setup-time (2026-08-16) API-equivalent Standard text-token pricing. It is an accounting estimate, not a ChatGPT subscription charge. The table totals ${problem_cost:,.2f} across exact problem session trees. The ${full_cost:,.2f} full-process figure adds ${orchestration_cost:,.2f} of top-level orchestration/support and the setup test.

The controller recorded {total_runtime/3600:.2f} active solver-hours across {total_runs} runs and {controller_rollouts} primary rollout sessions. Those roots spawned {nested_agents} attributable nested sessions, for {problem_sessions} problem-tree sessions total. Full trace-level reconciliation is in the [metrics report](analysis/metrics.md), and the [trace index](analysis/trace_index.csv) maps every archived session to its parent and owning problem.

## Audit trail

- **GitHub** preserves the exact submitted Lean files, deterministic controller evidence, independent verification records, pricing snapshot, and aggregate metrics.
- **Hugging Face** preserves **{full_threads} compressed Codex rollout JSONL traces** from the root orchestrator, solver jobs, and support agents, plus a content-addressed manifest. Credential material and redundant environment snapshots are intentionally excluded.
- Primary-rollout totals reconcile passed controller audits exactly. Nested-agent tokens and costs are recovered from the archived parent graph with global turn-level deduplication, never from agent self-reporting.

## Solves over time

Time is logarithmic, so both the opening sprint and the long tail remain visible.

![Cumulative verified solves over logarithmic elapsed time](analysis/solves-over-log-time.svg)

The [analysis gallery](analysis/README.md) adds linear real-time and full-24-hour views, solve-time and budget distributions, and the [aggregate dashboard](analysis/metrics.svg). Exact per-problem tokens, timings, costs, retries, nested-agent counts, and LOC are in [`analysis/metrics.csv`](analysis/metrics.csv) and [`analysis/metrics.json`](analysis/metrics.json).

<details open>
<summary><strong>Original prompt</strong></summary>

{prompt_quote}

</details>

## Problems

“Wall time” runs from first launch to the final result/verification and includes retry gaps. “Active solver time” is the sum of controller-measured run durations. “Runs” counts controller jobs; “Nested agents” counts exact archived descendants of their rollout roots. Cost includes each full problem subtree and all retries, while top-level orchestration remains separately reported overhead. “Added Lean LOC” counts nonblank, non-comment code introduced relative to the pre-race commit.

{chr(10).join(table)}

---

Exact solutions: [`{args.solution_commit[:12]}`](https://github.com/Vilin97/lean-eval-speedrun/tree/{args.solution_commit}) · Aggregated evidence: [`{args.logs_commit[:12]}`](https://github.com/Vilin97/lean-eval-speedrun/tree/{args.logs_commit}) · Submission name: **Vasily-24-hour-gpt-5.6-speedrun**
"""
    args.output.write_text(readme, encoding="utf-8")
    print(f"README_OK rows={len(rows)} solved={len(solved)} output={args.output}")


if __name__ == "__main__":
    main()
