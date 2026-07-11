# econ SWE-bench Mini-10 — full runbook (build → fly → run → grade → analyze)

End-to-end procedure to reproduce the econ Verified Mini-10 benchmark on fly.io and compare it
against the saved baselines. All commands run from `e2e/econ/fly-remote/fullresolve/` unless noted.

## 0. Invariants (do not violate)
- **Never print API keys/tokens** — only their lengths. Keys: `LITELLM_API_KEY`, `EXA_API_KEY`, `FLY_API_TOKEN`.
- **Fly org = `vamsee-k-933` (team)** — never the personal org. App `unerr-bench-econ-fullresolve`, region `iad`, volume `bench_data_econ` (200 GB).
- **Web search OFF** (baseline-comparable): always launch with `env -u EXA_API_KEY WEBSEARCH=0`. On SWE-bench the fixes are public on GitHub → an enabled web search is answer-lookup.
- **econ-coding-agent is reads-only.** The only allowed write is the build output under `packages/opencode/dist/` (gitignored) — building latest source touches no tracked files.
- **NEVER re-run the Claude baseline.** It is permanent (see §5).

## 1. Build econ (local, on darwin)
```bash
cd ~/IdeaProjects/econ-coding-agent && bun install && bun run --cwd packages/opencode build
```
This cross-compiles ALL targets, including the one the fly VM needs:
`packages/opencode/dist/opencode-linux-x64-baseline/bin/opencode` (glibc, NOT musl — SWE-bench images are Debian).
`run.sh` auto-vendors that binary + `opencode.json` + `.opencode/` into the toolbox build context.
`--single` is NOT enough (host-only). If you skip this and no baseline binary exists, `run.sh` aborts with the exact rebuild command.

## 2. Launch the run
```bash
env -u EXA_API_KEY WEBSEARCH=0 INSTANCES=10 PARALLEL=1 LABEL=econ-v4 KEEP=1 MAXWAIT=7200 HOLD=5400 ./run.sh
```
- **Smoke first** (always): `INSTANCES=1 LABEL=smoke` — one instance, verify green before the full 10.
- `run.sh` does: vendor binary → `flyctl deploy --build-only --remote-only --push` (image built on fly remote builder, context = `e2e/econ`) → `flyctl machine run` one-shot DinD machine (`--volume bench_data_econ:/data --restart no`) → stream logs → poll for the `"bundle_ready"` beacon → sftp-pull `/data/bundle.tgz` → extract to `out/bundle/`.
- `LABEL` MUST be unique per run (it names the results + grade dirs). Use `econ-v4`, `econ-v5`, …
- `PARALLEL=1` for a clean comparison. Parallelism works but the single LiteLLM→Fireworks gateway is the bottleneck (5-wide ≈ 8% wall saving, 3–6× per-instance slowdown); cap at 2–3 if used. `PARALLEL>1` auto-scales the VM to 16 GB/8 cpu.
- `KEEP=1` (default) leaves the machine up so you can pull DBs/traces after. Destroy later: `flyctl machine destroy <MID> -a unerr-bench-econ-fullresolve --force` (or `tools/bench-ctl.sh destroy`).
- `LITELLM_API_KEY` is read from `e2e/econ/.env.local` if not exported.
- The host-side babysitter in `run.sh` can exit early on a `flyctl status` blip — the in-VM job continues regardless. Use `bench-ctl` (below) to monitor independently.

## 3. Monitor / pull / audit — use the permanent tools (don't write ad-hoc scripts)
```bash
tools/bench-ctl.sh machines                 # list fly machines
tools/bench-ctl.sh status  econ-v4          # in-VM: resolve stage, rows done, bundle/grade state
tools/bench-ctl.sh watch   econ-v4          # poll until a FRESH graded bundle lands → out/econ-v4-bundle/ + audit
tools/bench-ctl.sh pull    econ-v4          # one-shot pull now
tools/bench-ctl.sh audit   out/econ-v4-bundle          # resolve% + total/per-instance/per-tier cost + tokens
tools/bench-ctl.sh trace   out/econ-v4-bundle --all    # per-instance: tools, test runs, edited-tests, verdict
tools/bench-ctl.sh trace   out/econ-v4-bundle django__django-11885
```
Gotchas the tools already handle: `fly ssh sftp get` REFUSES to overwrite (they `rm -f` first); `watch` has a freshness guard (bundle mtime > watch-start) so it never false-triggers on a stale `/data/bundle.tgz`. If you pull `meta.jsonl` by hand, `rm -f` the local copy first or you re-audit stale rows.

## 4. Deep analysis (gold-vs-econ + tier routing)
- **Localization/exploration mining (per-tier):** `tools/bench-explore.py --db out/econ-v4-dbs/<iid>/opencode.db` (or `--bundle DIR --all`). Each tier is its own session; shows every search + hit-count (⌀=empty), reads, time-to-first-edit, edit-thrash, test verdicts. The `opencode.db` for each instance is in its artifact dir (`OPENCODE_DB`); checkpoint the WAL (`sqlite3 db 'PRAGMA wal_checkpoint(TRUNCATE)'`) before pulling.
- **Gold-vs-econ + which test failed:** the swebench grade report is `/data/logs/grade-<label>/econ.<label>.json` (has `resolved_ids`, `unresolved_ids`, per-instance `report.json` with `tests_status.FAIL_TO_PASS/PASS_TO_PASS` success/failure). The gold patch + test_patch + problem_statement come from the SWE-bench_Verified parquet (HF cache on the VM). Run extraction with the swebench venv: **`/work/.venv/bin/python3`** (base python lacks pandas). See `scratchpad/extract-v3.py` in the session archive for the exact combine-into-one-JSON script.
- **F2P fail = didn't fix the bug; P2P fail = fixed target but broke a regression** (often from editing a test the grader then resets).

## 5. Cross-check against baselines (same 10 django instances)
Mini-10 ids: `11790 11815 11848 11880 11885 11951 11964 11999 12039 12050`.

| Run | Resolved | Cost | Notes |
|---|---|---|---|
| **Claude Code (PERMANENT — never re-run)** | **10/10** | **$3.1181** | claude-opus-4-8 |
| econ-v2 | 8/10 | $0.32 | conductor-only; failed 11885, 12039 |
| econ-v3 (HEAD 7553842f4) | 6/10 | $0.6881 | regression: +12039, −11790/−11815/−11964; 11885 still fails |
| econ-v4 (HEAD a763d16fa) | 5/10 @8GB → **6/10** reasoning-fair | $0.7643 (+$0.1776 re-run) | 11999 OOM'd @MEM=8192 (v4 runtime heavier than v3) → RESOLVED @16GB, so reasoning-fair 6/10. vs v3: lateral count, +11790 recovered / −11848 NEW regression, higher cost. Oracle ~52% spend but mostly READ-ONLY; reasoner never fired. **Bump default MEM to 16384 for v4+.** Gold-verified per-failure in memory `econ-v4-mini10-result`. |

**econ-v3 failure taxonomy (what to check a new run against):**
- 11790, 11815 — **format near-miss**: correct localization + concept, wrong output form (`str()` cast; `Status.GOOD` vs `Status['GOOD']`). Oracle fired but read-only, never ran the real test.
- 11964 — **wrong localization**: patched the effect layer (descriptor) not the cause (enum `__str__`). Oracle never fired.
- 11885 — **over-broad + test-editing**: broke regression query-counts, edited the test to hide it; grader reset the test → P2P regression.

**Fixes being tracked (ranked):** (1) self-verify against the real FAIL_TO_PASS test before submit (recovers 11790+11815 → ~8/10); (2) escalate on thrash/test-edits; (3) oracle closes the loop (run test, fix to green, not advise); (4) no-test-edits guardrail; (5) conductor semantic-search-first. See the memory notes `econ-v3-failure-taxonomy`, `econ-v3-exploration-findings`.

## 6. What "good" looks like
A new econ build should (a) beat econ-v2's 8/10, (b) recover the two near-misses (11790/11815), (c) not thrash conductor-only past ~30 turns without escalating, and (d) never edit a test file. Cost target: well under Claude's $3.12 (econ runs ~$0.3–0.7).
