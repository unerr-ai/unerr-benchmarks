# bench tools — permanent control/query/audit surface

Reusable tooling for the econ fly full-resolve runs. **Don't re-write ad-hoc monitor/parse
scripts** — everything you need is here. All stdlib/flyctl only; no deps to install.

## Running a benchmark (parallel)

From `e2e/econ/fly-remote/fullresolve/`:

```bash
# smoke (5 instances, 5-wide) — EXA off, latest vendored binary, keep machine for pulls
env -u EXA_API_KEY WEBSEARCH=0 INSTANCES=5 PARALLEL=5 LABEL=smoke5 KEEP=1 ./run.sh

# full Mini-10, 5-wide
env -u EXA_API_KEY WEBSEARCH=0 INSTANCES=10 PARALLEL=5 LABEL=econ-v3 KEEP=1 ./run.sh
```

`PARALLEL>1` auto-scales the fly VM to 16GB/8cpu (override with `MEM=`/`CPUS=`; fly can go
bigger, e.g. `MEM=32768 CPUS=16`). `PARALLEL` runs N instances concurrently in-VM — the model
calls are remote so it's I/O-bound; wall-clock drops ~linearly. Grading auto-uses ≥N workers.

## Controlling / querying / auditing — `bench-ctl.sh`

```bash
tools/bench-ctl.sh machines              # list fly machines
tools/bench-ctl.sh status  smoke5        # live: resolve stage, rows done, bundle/grade state
tools/bench-ctl.sh watch   smoke5        # poll until a FRESH bundle lands → out/smoke5-bundle/ + audit
tools/bench-ctl.sh pull    smoke5        # one-shot pull the bundle now
tools/bench-ctl.sh audit   out/smoke5-bundle       # cost/token/tier/resolution table
tools/bench-ctl.sh trace   out/smoke5-bundle --all # per-instance root-cause (terse)
tools/bench-ctl.sh trace   out/smoke5-bundle django__django-11885   # full trace
tools/bench-ctl.sh destroy               # destroy ALL machines (frees the volume)
```

`watch` has a **freshness guard** (only accepts a bundle newer than watch-start) so it never
false-triggers on a stale `/data/bundle.tgz` from a prior run.

## The underlying tools (callable directly)

- **`bench-audit.py --bundle <dir> [--label L] [--json]`** — resolution, total/per-instance/per-tier
  cost + tokens, cache-hit %, failures. Works pre-grade too (cost only).
- **`bench-trace.py --bundle <dir> (IID | --all)`** — what the agent DID: tool sequence, shell/test
  commands, edited files, tiers/models, and root-cause signals (⚠ edited-test-files, test-run count,
  the last test verdict econ saw). This auto-reproduces the manual failure root-causing.
- **`bench-explore.py --db <opencode.db>`** (or `--bundle <dir> --all`) — mines the full opencode
  SQLite session store (richer than events.jsonl): splits by TIER (each tier is its own session) and
  reconstructs the localization narrative — every `search` query + its hit-count (⌀=empty), reads,
  time-to-first-edit, edit-thrash (same file re-edited N×), test-run verdicts. `--timeline` for the
  full ordered stream. Use it to optimize code-exploration: it's how we found the conductor greps
  (`mode:literal`, misses) while the oracle uses semantic recon. DBs are in each artifact dir
  (`opencode.db`); checkpoint the WAL (`sqlite3 db 'PRAGMA wal_checkpoint(TRUNCATE)'`) before pulling.
