<div align="center">

# 24 Hours of Lean

**A GPT-5.6 LeanEval speedrun**

**79 verified solves** · 86 attempted · 97 controller runs + 262 nested problem agents · 421 archived sessions

[LeanEval submission](https://github.com/leanprover/lean-eval-submissions/issues/1081) · [rollout traces](https://huggingface.co/datasets/Vilin97/Vasily-24-hour-gpt-5.6-speedrun-logs) · [analysis](analysis/README.md) · [audit manifest](https://github.com/Vilin97/lean-eval-speedrun-solutions/blob/main/speedrun-logs/manifest.json)

</div>

> **The solutions are not public.** Every proof, solver prompt, and raw run log lives in the private
> [`lean-eval-speedrun-solutions`](https://github.com/Vilin97/lean-eval-speedrun-solutions)
> repository, so that this run does not contaminate the LeanEval benchmark for anyone measuring
> against it later. The problem links in the table below point there and will 404 without access.
> What stays public is the accounting: aggregate metrics, the derived analysis, and the
> orchestration harness under [`scripts/`](scripts).

## The experiment

One hour of setup, followed by a hard 24-hour solve window from **2026-08-16 08:09:44 UTC** to **2026-08-17 08:09:44 UTC**. Every claimed solution was independently replayed through LeanEval comparator and nanoda; agent self-reports never counted as solves.

| Verified | Attempted | Full-process cost | Problem-tree cost | Primary-controller cost | Full input | Added Lean code |
|---:|---:|---:|---:|---:|---:|---:|
| **79** | **86** | **$3,398.53** | **$1,824.84** | **$571.62** | **4.79B** (97.7% cached) | **87,778 LOC** |

> Cost uses pinned, setup-time (2026-08-16) API-equivalent Standard text-token pricing. It is an accounting estimate, not a ChatGPT subscription charge. The table totals $1,824.84 across exact problem session trees. The $3,398.53 full-process figure adds $1,573.69 of top-level orchestration/support and the setup test.

The controller recorded 33.62 active solver-hours across 97 runs and 98 primary rollout sessions. Those roots spawned 262 attributable nested sessions, for 360 problem-tree sessions total. Full trace-level reconciliation is in the [metrics report](analysis/metrics.md), and the [trace index](analysis/trace_index.csv) maps every archived session to its parent and owning problem.

## Audit trail

- **This repository** is public and preserves the aggregate metrics, the derived analysis, and the orchestration harness.
- **[`lean-eval-speedrun-solutions`](https://github.com/Vilin97/lean-eval-speedrun-solutions)** is private and preserves the exact submitted Lean files, the solver prompts, deterministic controller evidence, independent verification records, and the pricing snapshot. Access is restricted so the solutions do not contaminate the LeanEval benchmark.
- **Hugging Face** (access-restricted) preserves **421 compressed Codex rollout JSONL traces** from the root orchestrator, solver jobs, and support agents, plus a content-addressed manifest. Credential material and redundant environment snapshots are intentionally excluded.
- Primary-rollout totals reconcile passed controller audits exactly. Nested-agent tokens and costs are recovered from the archived parent graph with global turn-level deduplication, never from agent self-reporting.

## Solves over time

Time is logarithmic, so both the opening sprint and the long tail remain visible.

![Cumulative verified solves over logarithmic elapsed time](analysis/solves-over-log-time.svg)

The [analysis gallery](analysis/README.md) adds linear real-time and full-24-hour views, solve-time and budget distributions, and the [aggregate dashboard](analysis/metrics.svg). Exact per-problem tokens, timings, costs, retries, nested-agent counts, and LOC are in [`analysis/metrics.csv`](analysis/metrics.csv) and [`analysis/metrics.json`](analysis/metrics.json).

<details open>
<summary><strong>Original prompt</strong></summary>

> we are speedrunning lean-eval. solve as many lean-eval problems in 24 hours as you can. You are allowed and encouraged to use anything (e.g. mathlib, LeanPool, TauCeti, anything else) except straight-up copying existing solutions (do not try to search for existing solutions - unfortunately many of them are leaked). You can gauge the difficulty of the problem by how many solutions it has in the leaderboard. For easier problems, try to just solve them fast. For harder problems, make a detailed informal proof and scout the existing Lean repos like mathlib, Lean pool, Tau Ceti and others for what's already built that's useful, and make a detailed blueprint before formalizing. Stay under 128gb ram. Do not use any set_options, do not use native_decide. Use subagents aggressively, up to 16 concurrent subagents. You can choose the model and reasoning level for subagents. For each problem you must log how long it took and its approximate token cost using official API pricing. Set up this logging to be deterministic and not self-reported by the agents (test it on a toy problem to make sure it works before going all-in). After exactly 24 hours from the start, stop all work immediately, push the solutions and logs to the repo, and submit all your solutions to lean-eval under name Vasily-24-hour-gpt-5.6-speedrun. Make a graph of solves over time, with time on logarithmic scale. You are alllowed up to 1 hour to set up whatever you need to make the speedrun successful, e.g. fetch mathlib cache, decide on the order of problems (I recommend easier to harder), write a logging script, anything else that's helpful (but don't overthink it). After exactly 1 hour, the 24h speedrun starts. Be careful not to run out of disk space if each agent gets its own worktree. I expect you to be mostly orchestrating a bunch of subagents. Save all the logs from this chat somewhere, where they can be analyzed later on.

</details>

## Problems

“Wall time” runs from first launch to the final result/verification and includes retry gaps. “Active solver time” is the sum of controller-measured run durations. “Runs” counts controller jobs; “Nested agents” counts exact archived descendants of their rollout roots. Cost includes each full problem subtree and all retries, while top-level orchestration remains separately reported overhead. “Added Lean LOC” counts nonblank, non-comment code introduced relative to the pre-race commit.

| Status | Problem | Wall time | Active solver time | Attributable API-eq. cost | Runs | Nested agents | Added Lean LOC |
|:--:|---|---:|---:|---:|---:|---:|---:|
| ✅ #30 | [`abel_ruffini`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/abel_ruffini) | 25m 47s | 22m 33s | $6.26 | 1 | 3 | 694 |
| ✅ #43 | [`balanceable_bounded_partitions`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/balanceable_bounded_partitions) | 45m 20s | 44m 57s | $26.03 | 1 | 4 | 624 |
| ✅ #36 | [`banach_alaoglu_bourbaki`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/banach_alaoglu_bourbaki) | 13m 05s | 12m 30s | $6.37 | 1 | 3 | 89 |
| ✅ #24 | [`bauer_extreme_point_uniqueness`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/bauer_extreme_point_uniqueness) | 5m 57s | 4m 44s | $2.14 | 1 | 3 | 33 |
| ✅ #52 | [`boone_higman_embedding`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/boone_higman_embedding) | 23m 01s | 18m 00s | $8.83 | 2 | 3 | 884 |
| ✅ #48 | [`boone_higman_simple`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/boone_higman_simple) | 11m 24s | 11m 00s | $6.14 | 1 | 3 | 449 |
| ✅ #18 | [`brauer_character_in_cyclotomic`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/brauer_character_in_cyclotomic) | 9m 55s | 7m 08s | $4.57 | 1 | 3 | 48 |
| ✅ #46 | [`brauer_fowler`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/brauer_fowler) | 9m 21s | 8m 56s | $3.76 | 1 | 3 | 240 |
| ✅ #26 | [`brouwer_fixed_point`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/brouwer_fixed_point) | 19m 24s | 17m 44s | $20.22 | 1 | 6 | 3,111 |
| ✅ #5 | [`bvp_comparison`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/bvp_comparison) | 4m 40s | 3m 11s | $0.08 | 1 | 0 | 25 |
| ✅ #76 | [`choquet_representation_theorem`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/choquet_representation_theorem) | 27m 54s | 26m 50s | $36.16 | 1 | 3 | 990 |
| ✅ #33 | [`chudnovsky_formula_for_pi_inv`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/chudnovsky_formula_for_pi_inv) | 37m 00s | 21m 36s | $19.32 | 2 | 6 | 16,132 |
| ✅ #29 | [`compact_group_semisimple`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/compact_group_semisimple) | 7m 04s | 6m 02s | $2.62 | 1 | 3 | 82 |
| ✅ #73 | [`contractibleSpace_houseWithTwoRooms`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/contractibleSpace_houseWithTwoRooms) | 1h 02m | 1h 00m | $57.34 | 1 | 10 | 3,262 |
| ✅ #2 | [`cubic_decay_asymptotic`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/cubic_decay_asymptotic) | 2m 42s | 96s | $0.28 | 1 | 0 | 106 |
| ✅ #39 | [`cyclotomic_integer_house_le_two`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/cyclotomic_integer_house_le_two) | 34m 32s | 31m 49s | $26.30 | 1 | 3 | 523 |
| ◻ attempted | [`dehn_sommerville`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/dehn_sommerville) | 1h 00m | 1h 00m | $90.18 | 1 | 7 | — |
| ✅ #35 | [`dirichlet_eigenvalues_eq_nat_sq`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/dirichlet_eigenvalues_eq_nat_sq) | 10m 39s | 9m 38s | $8.90 | 1 | 3 | 213 |
| ✅ #12 | [`euler_lagrange_equation`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/euler_lagrange_equation) | 22m 32s | 20m 29s | $14.70 | 1 | 3 | 174 |
| ✅ #23 | [`exists_complementary_polynomial_on_unit_circle`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/exists_complementary_polynomial_on_unit_circle) | 40m 48s | 38m 57s | $41.18 | 1 | 4 | 990 |
| ✅ #74 | [`fang_xia_tiling_partition_transitive`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/fang_xia_tiling_partition_transitive) | 34m 27s | 33m 57s | $23.10 | 1 | 3 | 1,633 |
| ✅ #8 | [`finite_graph_ramsey_theorem`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/finite_graph_ramsey_theorem) | 12m 47s | 12m 24s | $1.45 | 1 | 0 | 62 |
| ✅ #40 | [`fourier_dirichlet_fejer`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/fourier_dirichlet_fejer) | 31m 00s | 28m 58s | $37.22 | 1 | 4 | 561 |
| ✅ #77 | [`fraser_kakeya_fourier_decay`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/fraser_kakeya_fourier_decay) | 21m 36s | 19m 06s | $19.94 | 1 | 3 | 824 |
| ✅ #78 | [`frobenius_group_determinant`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/frobenius_group_determinant) | 39m 02s | 37m 06s | $50.79 | 1 | 3 | 1,366 |
| ◻ attempted | [`frobenius_kernel_isNormal`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/frobenius_kernel_isNormal) | 1h 05m | 1h 05m | $18.20 | 1 | 0 | — |
| ✅ #47 | [`furstenberg_topological`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/furstenberg_topological) | 8m 39s | 8m 07s | $6.12 | 1 | 3 | 350 |
| ✅ #63 | [`glAction_range_eq_centralizer_symAction`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/glAction_range_eq_centralizer_symAction) | 35m 07s | 33m 56s | $46.96 | 2 | 3 | 2,110 |
| ✅ #79 | [`halmos_generic_weak_mixing`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/halmos_generic_weak_mixing) | 1h 17m | 1h 15m | $105.87 | 2 | 7 | 6,838 |
| ✅ #71 | [`hausdorff_absolute_continuity`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/hausdorff_absolute_continuity) | 18m 21s | 17m 13s | $21.68 | 1 | 4 | 863 |
| ✅ #70 | [`hausdorff_hildebrandt_schoenberg`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/hausdorff_hildebrandt_schoenberg) | 25m 15s | 24m 10s | $27.05 | 1 | 3 | 1,409 |
| ✅ #69 | [`hausdorff_positivity_criterion`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/hausdorff_positivity_criterion) | 27m 49s | 26m 43s | $31.11 | 1 | 5 | 1,000 |
| ✅ #38 | [`heat_kernel_solves_heat_equation`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/heat_kernel_solves_heat_equation) | 25m 49s | 23m 51s | $35.76 | 1 | 3 | 460 |
| ✅ #11 | [`hippocrates_lunes`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/hippocrates_lunes) | 18m 46s | 17m 31s | $3.08 | 1 | 0 | 375 |
| ◻ attempted | [`hopf_umlaufsatz`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/hopf_umlaufsatz) | 7h 38m | 2h 00m | $135.99 | 2 | 6 | — |
| ✅ #19 | [`irreducible_nonnegative_matrix_has_positive_eigenvector_at_spectralRadius`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/irreducible_nonnegative_matrix_has_positive_eigenvector_at_spectralRadius) | 12m 22s | 11m 21s | $7.04 | 1 | 3 | 618 |
| ◻ attempted | [`isoperimetric_inequality`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/isoperimetric_inequality) | 16m 46s | 16m 46s | $22.87 | 1 | 3 | — |
| ✅ #67 | [`jordan_normal_form`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/jordan_normal_form) | 33m 07s | 31m 04s | $44.78 | 1 | 3 | 482 |
| ✅ #55 | [`kakutani_fixed_point`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/kakutani_fixed_point) | 3m 21s | 2m 40s | $0.78 | 1 | 2 | 3,200 |
| ✅ #53 | [`kirk_normal_structure`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/kirk_normal_structure) | 12m 47s | 11m 56s | $5.60 | 1 | 4 | 310 |
| ✅ #3 | [`koszul_formula`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/koszul_formula) | 2m 45s | 72s | $0.10 | 1 | 0 | 21 |
| ✅ #56 | [`landsberg_schaar`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/landsberg_schaar) | 37m 28s | 35m 08s | $25.65 | 1 | 3 | 899 |
| ✅ #66 | [`levi_civita_exists_unique`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/levi_civita_exists_unique) | 47m 17s | 39m 46s | $56.66 | 2 | 6 | 923 |
| ◻ attempted | [`lidskii_last`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/lidskii_last) | 14m 02s | 7m 21s | $8.36 | 2 | 6 | — |
| ✅ #49 | [`lindemann`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/lindemann) | 35m 36s | 33m 07s | $42.50 | 1 | 4 | 538 |
| ✅ #57 | [`lindemann_weierstrass`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/lindemann_weierstrass) | 29m 27s | 23m 53s | $29.78 | 3 | 3 | 1,030 |
| ✅ #44 | [`linear_ode_asymptotic_stability`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/linear_ode_asymptotic_stability) | 20m 46s | 18m 38s | $33.14 | 1 | 3 | 276 |
| ✅ #13 | [`lp_maximum_principle`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/lp_maximum_principle) | 40m 25s | 38m 46s | $2.95 | 1 | 0 | 83 |
| ✅ #9 | [`mem_convexHull_finset_extremePoints_of_mem_compact_convex`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/mem_convexHull_finset_extremePoints_of_mem_compact_convex) | 20m 30s | 19m 36s | $5.04 | 1 | 0 | 195 |
| ◻ attempted | [`mergelyan_theorem`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/mergelyan_theorem) | 1h 00m | 1h 00m | $54.57 | 1 | 6 | — |
| ✅ #31 | [`monge_kantorovich`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/monge_kantorovich) | 13m 27s | 12m 23s | $6.30 | 1 | 3 | 189 |
| ✅ #60 | [`morley_theorem`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/morley_theorem) | 7m 33s | 6m 45s | $2.97 | 1 | 3 | 546 |
| ✅ #65 | [`mountain_pass`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/mountain_pass) | 32m 18s | 30m 21s | $32.72 | 1 | 5 | 759 |
| ✅ #4 | [`mulCayley_connected_iff_closure_eq_top`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/mulCayley_connected_iff_closure_eq_top) | 3m 36s | 3m 15s | $0.25 | 1 | 0 | 46 |
| ✅ #21 | [`nash_equilibrium_exists`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/nash_equilibrium_exists) | 22m 37s | 22m 02s | $17.12 | 1 | 3 | 4,435 |
| ✅ #15 | [`normal_spectral_theorem`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/normal_spectral_theorem) | 42m 10s | 40m 57s | $17.51 | 1 | 3 | 137 |
| ✅ #32 | [`nyquist_shannon_sampling`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/nyquist_shannon_sampling) | 16m 54s | 14m 03s | $9.33 | 1 | 3 | 246 |
| ✅ #20 | [`oppenheim_inequality`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/oppenheim_inequality) | 1h 07m | 1h 06m | $8.27 | 1 | 0 | 245 |
| ✅ #42 | [`pascal`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/pascal) | 15m 09s | 10m 02s | $4.27 | 2 | 3 | 385 |
| ✅ #59 | [`peano_existence`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/peano_existence) | 21m 23s | 19m 29s | $25.82 | 1 | 3 | 315 |
| ✅ #17 | [`pell_solution_convergent`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/pell_solution_convergent) | 4m 38s | 3m 44s | $0.60 | 1 | 0 | 96 |
| ✅ #50 | [`permute_to_unimodal`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/permute_to_unimodal) | 39m 19s | 38m 50s | $45.85 | 1 | 3 | 2,537 |
| ✅ #16 | [`pi1_circle_mulEquiv_int`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/pi1_circle_mulEquiv_int) | 44m 52s | 44m 05s | $1.00 | 1 | 0 | 17 |
| ✅ #6 | [`posSemidef_map_exp`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/posSemidef_map_exp) | 7m 59s | 6m 34s | $0.86 | 1 | 0 | 27 |
| ✅ #34 | [`radon_transform_inversion`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/radon_transform_inversion) | 14m 54s | 12m 37s | $12.35 | 1 | 3 | 160 |
| ✅ #27 | [`regular_value_ae`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/regular_value_ae) | 20m 43s | 18m 24s | $13.72 | 1 | 3 | 4,050 |
| ✅ #25 | [`rising_sun_lemma`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/rising_sun_lemma) | 12m 08s | 11m 34s | $10.18 | 1 | 4 | 367 |
| ✅ #75 | [`rokhlin_lemma`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/rokhlin_lemma) | 2m 01s | 84s | $0.58 | 1 | 1 | 613 |
| ✅ #22 | [`rouche_zero_count_eq`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/rouche_zero_count_eq) | 23m 39s | 21m 51s | $25.63 | 1 | 3 | 381 |
| ✅ #72 | [`runge_theorem`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/runge_theorem) | 9m 10s | 7m 34s | $3.77 | 1 | 5 | 1,982 |
| ✅ #28 | [`sard_theorem`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/sard_theorem) | 11m 19s | 9m 08s | $7.73 | 1 | 3 | 4,065 |
| ✅ #41 | [`schauder_fixed_point`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/schauder_fixed_point) | 9m 33s | 8m 52s | $6.75 | 1 | 3 | 3,753 |
| ✅ #61 | [`shannon_capacity_pentagon`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/shannon_capacity_pentagon) | 10m 51s | 10m 11s | $7.56 | 1 | 3 | 472 |
| ✅ #68 | [`solvable_by_radicals_converse`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/solvable_by_radicals_converse) | 24m 52s | 23m 30s | $30.05 | 1 | 4 | 310 |
| ✅ #54 | [`sturm`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/sturm) | 22m 17s | 21m 40s | $21.93 | 1 | 3 | 1,273 |
| ✅ #10 | [`sturm_separation`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/sturm_separation) | 23m 03s | 21m 32s | $5.15 | 1 | 3 | 414 |
| ✅ #7 | [`substInv_X_sub_X_sq_eq_catalan`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/substInv_X_sub_X_sq_eq_catalan) | 12m 38s | 12m 03s | $7.36 | 1 | 3 | 31 |
| ✅ #62 | [`symAction_range_eq_centralizer_glAction`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/symAction_range_eq_centralizer_glAction) | 32m 06s | 30m 53s | $44.98 | 1 | 5 | 726 |
| ✅ #1 | [`symplectic_matrix_det`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/symplectic_matrix_det) | 110s | 66s | $0.01 | 1 | 0 | 1 |
| ✅ #14 | [`trace_cayley_hamilton_newton`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/trace_cayley_hamilton_newton) | 41m 25s | 40m 09s | $37.08 | 1 | 3 | 190 |
| ◻ attempted | [`turing_recursive_equiv`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/turing_recursive_equiv) | 25m 00s | 25m 00s | $33.88 | 1 | 3 | — |
| ✅ #51 | [`tverberg_theorem`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/tverberg_theorem) | 19m 58s | 18m 14s | $16.39 | 1 | 3 | 551 |
| ✅ #45 | [`vonNeumann_doubleCommutant_tfae`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/vonNeumann_doubleCommutant_tfae) | 18m 17s | 17m 15s | $12.31 | 1 | 3 | 335 |
| ✅ #37 | [`wiener_atom_detection`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/wiener_atom_detection) | 10m 24s | 9m 16s | $5.58 | 1 | 3 | 298 |
| ✅ #58 | [`wiener_inverse_closed`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/wiener_inverse_closed) | 17m 45s | 14m 04s | $10.47 | 2 | 3 | 437 |
| ✅ #64 | [`wiener_levy_analytic_calculus`](https://github.com/Vilin97/lean-eval-speedrun-solutions/tree/main/generated/wiener_levy_analytic_calculus) | 41m 48s | 39m 49s | $52.97 | 1 | 3 | 2,294 |

---

Exact solutions and run evidence: [`lean-eval-speedrun-solutions`](https://github.com/Vilin97/lean-eval-speedrun-solutions) (private) · Submission name: **Vasily-24-hour-gpt-5.6-speedrun**
