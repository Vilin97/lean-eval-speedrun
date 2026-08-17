# Speedrun metrics

Generated from the immutable controller results and passed audits at `2026-08-17T16:35:45Z`. Lean line deltas use pre-race Git commit `8cdf39e15cda5b001ad8e4416829e748b70bb2c2` as their baseline.

The canonical per-problem data is in [`metrics.csv`](metrics.csv), and [`trace_index.csv`](trace_index.csv) maps all 421 archived sessions to their parents and owning problem trees. [`metrics.svg`](metrics.svg) is a visual dashboard. The [analysis index](README.md) collects logarithmic, real-time, full-24-hour, solve-time, and budget views of the 79 verified solves.

## Headline results

| Metric | Value |
|---|---:|
| Attempted real problems | 86 |
| Verified solves | 79 |
| Solve rate | 91.9% |
| Controller-launched problem solver runs | 97 |
| Primary problem rollout sessions | 98 |
| Nested problem-subagent sessions | 262 |
| Total problem-subtree sessions | 360 |
| All archived model threads | 421 |
| Problems retried | 10 |
| API-equivalent cost, full process | $3,398.53 |
| API-equivalent cost, attributable problem subtrees | $1,824.84 |
| API-equivalent cost, primary controller rollouts | $571.62 |
| API-equivalent cost, top-level orchestration/support | $1,573.69 |
| API-equivalent cost, setup test | $0.0037 |
| Attributable subtree cost for solved problems | $1,460.79 |
| Controller-measured active solver time, all attempts | 33.62 h |
| Independent verification time | 2.04 h |
| Primary-controller input tokens | 792,775,339 |
| Primary-controller cached input tokens | 774,138,112 (97.6% of input) |
| Primary-controller output tokens | 3,075,519 |
| Primary-controller reasoning output tokens | 1,504,138 |
| Problem-subtree input tokens | 2,460,368,048 |
| Problem-subtree cached input tokens | 2,397,220,864 (97.4% of input) |
| Problem-subtree output tokens | 10,386,492 |
| Problem-subtree reasoning output tokens | 4,796,815 |
| Full-process input tokens | 4,793,039,210 |
| Full-process cached input tokens | 4,681,223,936 (97.7% of input) |
| Full-process output tokens | 16,667,699 |
| Full-process reasoning output tokens | 7,539,263 |
| Added Lean code LOC across verified solutions | 87,778 |
| Median added code LOC per solve | 460 |
| Median attributable subtree cost per solve | $10.47 |
| Median active agent time per solve | 18.4 min |
| 90th percentile active agent time | 39.8 min |
| Final solve | `halmos_generic_weak_mixing` at 14.71 h |

Cost is computed with the repository's pinned, setup-time (2026-08-16) API-equivalent Standard text-token rates. It is **not** an actual ChatGPT subscription charge. Per-problem costs include primary controller rollouts and every nested subagent session below their roots. Top-level orchestration and cross-problem support are reported separately rather than guessed into problem rows.

## Full-process trace accounting

| Non-overlapping trace category | Sessions | Input tokens | Cached input | Output tokens | API-eq. cost |
|---|---:|---:|---:|---:|---:|
| Primary problem rollouts | 98 | 792,775,339 | 774,138,112 | 3,075,519 | $571.62 |
| Nested problem agents | 262 | 1,667,592,709 | 1,623,082,752 | 7,310,973 | $1,253.22 |
| Setup test | 1 | 38,632 | 24,064 | 267 | $0.0037 |
| Root orchestration + support agents | 60 | 2,332,632,530 | 2,283,979,008 | 6,280,940 | $1,573.69 |
| **Full process** | **421** | **4,793,039,210** | **4,681,223,936** | **16,667,699** | **$3,398.53** |

Forked session files contain inherited parent history, so final cumulative counters cannot safely be summed per file. The canonical reconciliation globally deduplicates each nonzero `last_token_usage` by turn ID and cumulative usage, prices it under that turn's model, and uses the parent graph for ownership. It found 43,398 unique model calls, 321 parent edges, and 92 turn IDs copied into descendants (maximum 25 files). The largest call had 244,849 input tokens; 0 calls required long-context pricing. The public archive manifest used for this reconciliation is `6d6148a256ac6c91453ec02f5f000add3f5f2e4e6047d335567b13c4a26fcfd1`.

## Notable extremes

- Fastest verified solution by controller active time: `symplectic_matrix_det` (66.4 s).
- Shortest verified solution delta: `symplectic_matrix_det` (1 added non-comment code LOC).
- Longest verified solution delta: `chudnovsky_formula_for_pi_inv` (16,132 added non-comment code LOC).
- Aggregate controller efficiency: 2.35 solves per measured active solver-hour.
- Gross attributable problem-subtree cost per verified solve: $23.10; primary-controller-only cost per verified solve: $7.24.
- Full-process cost per verified solve, including orchestration and setup: $43.02.
- Controller-job parallelism factor: 1.40 active agent-hours per wall-clock race hour.

## Highest API-equivalent cost

| Problem | Value | Controller runs | Nested agents | Added code LOC |
|---|---:|---:|---:|---:|
| `hopf_umlaufsatz` | $135.99 | 2 | 6 | — |
| `halmos_generic_weak_mixing` | $105.87 | 2 | 7 | 6,838 |
| `dehn_sommerville` | $90.18 | 1 | 7 | — |
| `contractibleSpace_houseWithTwoRooms` | $57.34 | 1 | 10 | 3,262 |
| `levi_civita_exists_unique` | $56.66 | 2 | 6 | 923 |
| `mergelyan_theorem` | $54.57 | 1 | 6 | — |
| `wiener_levy_analytic_calculus` | $52.97 | 1 | 3 | 2,294 |
| `frobenius_group_determinant` | $50.79 | 1 | 3 | 1,366 |
| `glAction_range_eq_centralizer_symAction` | $46.96 | 2 | 3 | 2,110 |
| `permute_to_unimodal` | $45.85 | 1 | 3 | 2,537 |

## Longest active agent time

| Problem | Value | Controller runs | Nested agents | Added code LOC |
|---|---:|---:|---:|---:|
| `hopf_umlaufsatz` | 120.0 min | 2 | 6 | — |
| `halmos_generic_weak_mixing` | 75.4 min | 2 | 7 | 6,838 |
| `oppenheim_inequality` | 66.2 min | 1 | 0 | 245 |
| `frobenius_kernel_isNormal` | 65.2 min | 1 | 0 | — |
| `dehn_sommerville` | 60.0 min | 1 | 7 | — |
| `contractibleSpace_houseWithTwoRooms` | 60.0 min | 1 | 10 | 3,262 |
| `mergelyan_theorem` | 60.0 min | 1 | 6 | — |
| `balanceable_bounded_partitions` | 44.9 min | 1 | 4 | 624 |
| `pi1_circle_mulEquiv_int` | 44.1 min | 1 | 0 | 17 |
| `normal_spectral_theorem` | 40.9 min | 1 | 3 | 137 |

## Largest proof deltas

| Problem | Value | Controller runs | Nested agents | Added code LOC |
|---|---:|---:|---:|---:|
| `chudnovsky_formula_for_pi_inv` | 16,132 LOC | 2 | 6 | 16,132 |
| `halmos_generic_weak_mixing` | 6,838 LOC | 2 | 7 | 6,838 |
| `nash_equilibrium_exists` | 4,435 LOC | 1 | 3 | 4,435 |
| `sard_theorem` | 4,065 LOC | 1 | 3 | 4,065 |
| `regular_value_ae` | 4,050 LOC | 1 | 3 | 4,050 |
| `schauder_fixed_point` | 3,753 LOC | 1 | 3 | 3,753 |
| `contractibleSpace_houseWithTwoRooms` | 3,262 LOC | 1 | 10 | 3,262 |
| `kakutani_fixed_point` | 3,200 LOC | 1 | 2 | 3,200 |
| `brouwer_fixed_point` | 3,111 LOC | 1 | 6 | 3,111 |
| `permute_to_unimodal` | 2,537 LOC | 1 | 3 | 2,537 |

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
