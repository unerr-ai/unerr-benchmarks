# e2e — SWE-bench Verified benchmark

Answers: **does econ (unerr embedded) resolve competitively against reference
coding agents, and at what cost?**

One execution mode: a work-stealing fleet of fly.io machines resolves + grades
SWE-bench instances in parallel. Running it with `MACHINES=1` is the sequential
case — there is no separate sequential runner.

```
SWE-bench Verified instances
         │
         ├── e2e/econ                agent-under-test — econ (unerr embedded), no on/off flip
         ├── e2e/reference/codex      reference arm — Codex CLI, run native
         └── e2e/reference/claude     reference arm — Claude Code CLI, run native
                     │
                     ▼
         swebench.harness.run_evaluation (same grader, every arm)
                     │
                     ▼
         e2e/common/scoring  (SWE-Effi)  →  resolve rate, $/instance, tokens, turns
```

## Directory map

| Path | Role |
|---|---|
| `distributed/` | The runner — `run-distributed.sh` launches a coordinator + N worker machines on fly.io, each claiming and resolving+grading one instance at a time, then merges results into one bundle. |
| `econ/` | Agent-under-test harness — econ (unerr compiled in) on SWE-bench. Single-arm: there's nothing to attach or flip, so this measures econ's own resolve rate/cost/tokens. |
| `reference/codex/` | Codex reference arm (`local-docker/` + `fly-remote/` backends) — comparison baseline. |
| `reference/claude/` | Claude Code reference arm (`local-docker/` + `fly-remote/` backends) — comparison baseline. |
| `common/` | Shared offline-Pro entitlement (`dev-entitlement.mjs`, `lib.sh`), preflight health-check, and SWE-Effi scoring (`scoring/`). |

## Quick-start

```bash
cd distributed

# smoke first, always
MACHINES=2 ARM=econ LABEL=dist-smoke \
  TASKS="django__django-11880,django__django-11951,django__django-11790" \
  ./run-distributed.sh

# a real tranche
MACHINES=5 ARM=econ LABEL=mini SUITE=mini ./run-distributed.sh
```

Full launch/monitor/pull/teardown detail, invariants, and the env contract
(`FLY_ORG`, `FLY_APP`, `LITELLM_BASE_URL`, `LITELLM_API_KEY`, `CPU_KIND`,
`CAMPAIGN`): [`distributed/README.md`](distributed/README.md). Methodology and
results: [`../docs/METHODOLOGY.md`](../docs/METHODOLOGY.md),
[`../docs/RESULTS.md`](../docs/RESULTS.md).

## Scoring: SWE-Effi (`common/scoring/`)

Raw "fewer tokens" can be won by *failing faster*. SWE-Effi (arXiv 2509.09853)
scores resolve rate *against* resources: the normalized AUC of the token-bounded
effectiveness curve is the headline. "More resolves per token" is the defensible
claim. See `common/scoring/README.md` for the trajectory schema and metric
definitions.

## Shared runtime: offline Pro entitlement

The reference arms (`reference/codex/`, `reference/claude/`) run unerr at the Pro
tier with **no login and no cloud** via two env vars injected before any agent
run:

- `UNERR_ENTITLEMENT_KID` / `UNERR_ENTITLEMENT_PUBKEY` — minted by
  `common/dev-entitlement.mjs mint pro`; lifts the free-tier 1-repo cap so unerr
  runs in every instance repo.
- `UNERR_TOKEN` — any non-blank value satisfies the login-presence wall; no
  cloud call is ever made.

These vars are set by `unerr_offline_pro()` in `common/lib.sh`, sourced by both
reference backends. econ doesn't need any of this — unerr is compiled directly
into its tool registry, not attached externally.
