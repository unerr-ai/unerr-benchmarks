# unerr benchmark suite

This repo benchmarks **econ** — an [opencode](https://opencode.ai)-based coding
agent that routes across OSS models (minimax / deepseek / glm) with
[unerr](https://github.com/unerr-ai/unerr-cli) compiled directly into its tool
registry — against reference agents **Claude Code** and **Codex** on
[SWE-bench Verified](https://www.swebench.com/). The question this repo answers:
does routing to cheap OSS models plus an embedded code-navigation layer resolve
competitively, and at what fraction of a frontier agent's per-instance cost?

## The benchmark

One execution mode: a work-stealing fleet of fly.io machines
(`e2e/distributed/run-distributed.sh`) resolves + grades SWE-bench instances in
parallel instead of one machine serially. Running it with `MACHINES=1` is the
sequential case — there is no separate sequential runner.

- **Agent under test** — `e2e/econ/`: econ, unerr embedded (not attached, not
  toggleable — there's no on/off flip for it). Routes each step to one of three
  tiers (conductor / oracle / executor) on different OSS models via a self-hosted
  LiteLLM gateway.
- **Reference arms** — `e2e/reference/codex/` and `e2e/reference/claude/`:
  Codex and Claude Code, run as comparison baselines through their own native
  CLIs.
- **Grading** — the standard SWE-bench harness (`swebench.harness.run_evaluation`)
  applies each predicted patch and runs the gold tests; it's agent-agnostic, so
  the same grader scores every arm.

Full methodology (dataset, one-variable protocol, scoring): [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md).

## Results so far

| Metric | econ | Claude Code | Caveat |
|---|--:|--:|---|
| SWE-bench Verified Mini-10 resolved | 5/10 | 10/10 | n=10 is a small, noisy snapshot — directional, not a resolve-rate estimate |
| $ / instance (same Mini-10) | $0.094 | $0.312 | econ ~70% cheaper per instance here, but resolves fewer; some misses are transient OSS-gateway stalls, not modeling failures |
| Navigation token savings (unerr vs grep+read) | 86–92.6% | — | retrieval-slice reduction (nav + read/outline), not a whole-session token discount; fidelity-gated |

Full breakdown, sources, and regeneration notes: [`docs/RESULTS.md`](docs/RESULTS.md).

## Reproduce

```bash
cd e2e/distributed

# required env
export FLY_ORG=<your-fly-org>
export FLY_APP=<your-fly-app>              # fixed app name; each run is scoped by fleet=<LABEL> metadata
export LITELLM_BASE_URL=<your-model-gateway-base-url>
export LITELLM_API_KEY=<your-litellm-key>

# smoke first, always
MACHINES=2 ARM=econ LABEL=dist-smoke \
  TASKS="django__django-11880,django__django-11951,django__django-11790" \
  CPU_KIND=performance ./run-distributed.sh

# a real tranche
MACHINES=5 ARM=econ LABEL=mini SUITE=mini CAMPAIGN=<name> CPU_KIND=performance ./run-distributed.sh
```

- `CPU_KIND=performance` is recommended — shared CPUs starve the agent process
  and inflate wall time (and therefore cost).
- `CAMPAIGN=<name>` pins one built image across every tranche of a multi-run
  campaign, so a 500-instance run isn't scored against a moving target.
- Full launch/monitor/pull/teardown detail: [`e2e/distributed/README.md`](e2e/distributed/README.md).

## Repo layout

```
e2e/
  distributed/     the runner — work-stealing fly.io fleet (resolve + grade + report)
  econ/             agent-under-test harness — econ (unerr embedded) on SWE-bench
  reference/
    codex/          Codex reference arm (baseline)
    claude/         Claude Code reference arm (baseline)
  common/           shared scoring (SWE-Effi) + offline-Pro entitlement helpers
docs/               methodology, results, submission-gap analysis, reference scores
results/            committed navigation/compression benchmark reports (.md)
```

## Limitations

- **Small n.** Headline numbers above are a 10-instance snapshot, not a
  statistically powered resolve-rate estimate.
- **Self-reported reference scores.** External frontier-model numbers
  (`docs/REFERENCE-SCORES.md`) are the vendors' own reported figures —
  scaffold-dependent, not independently re-run by us except through our own
  reference arms.
- **OSS-model variance.** econ's routed models run through a shared gateway with
  no client-side request timeout yet; transient upstream stalls can turn a
  resolvable instance into an empty patch, which shows up as a resolve-rate loss
  that isn't a modeling failure.
- **No leaderboard submission.** The SWE-bench Verified leaderboard requires an
  academic-affiliated author and a published technical report — we have neither,
  so this is framed as a reproducible, self-hosted run, not a leaderboard entry
  (`docs/SUBMISSION.md` has the full gap analysis).
