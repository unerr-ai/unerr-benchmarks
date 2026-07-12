# SWE-bench Verified — difficulty tiers (for targeted runs)

SWE-bench Verified ships a per-instance human **`difficulty`** annotation (OpenAI
had 93 devs estimate fix-time per task). This directory buckets all 500 instance
ids by that annotation so any arm's runner can target a tier directly via `IDS=`.

## Tier counts (of 500)

| Tier | Dataset value | Count | Gold patch (approx) |
|---|---|---|---|
| `easy`     | `<15 min fix`     | **194** | ~5 lines |
| `medium`   | `15 min - 1 hour` | **261** | ~14 lines |
| `hard`     | `1-4 hours`       | **42**  | ~50 lines, often multi-file |
| `veryhard` | `>4 hours`        | **3**   | large / multi-file |

**~91% are ≤1 hour.** The genuinely tough set is the **45 instances rated >1 hr**
(`hard` + `veryhard`), which correlate with multi-file / larger diffs.

**The 3 very-hard (>4 hr):** `pydata__xarray-6992`, `sphinx-doc__sphinx-7590`,
`sympy__sympy-13878`.

**Hard tier (1–4 hr) by repo:** django 22 · sympy 6 · sphinx 4 · astropy 3 ·
pytest 3 · pylint 2 · xarray 1 · scikit-learn 1. This is where the non-django,
harder-to-navigate repos appear — the Mini-10/Mini-50 subsets contain none of
them (django/sphinx only).

## Files

- `easy.ids.txt` / `medium.ids.txt` / `hard.ids.txt` / `veryhard.ids.txt` — one
  instance_id per line.
- `easy.csv` / `medium.csv` / `hard.csv` / `veryhard.csv` — comma-separated on one
  line; paste straight into `IDS=`.
- `all.tsv` — `instance_id · repo · difficulty · tier` for arbitrary slicing
  (e.g. "hard sympy only": `awk -F'\t' '$4=="hard" && $2 ~ /sympy/ {print $1}' all.tsv`).
- `gen-tiers.py` — regenerates everything above from the HF datasets-server
  (stdlib only, no `datasets`/pip deps). Run `python3 gen-tiers.py` after a
  dataset revision.

## Running a tier targetedly

Every arm's runner accepts an `IDS=` override (comma-separated instance_ids) that
**bypasses the default Mini-10** and drives exactly those instances. Example — the
3 very-hard as an ultimate stress test, on the econ fly full-resolve runner:

```bash
cd ../econ/fly-remote/fullresolve
env -u EXA_API_KEY WEBSEARCH=0 \
  IDS="$(cat ../../../swebench-verified-difficulty/veryhard.csv)" \
  LABEL=veryhard-3 MEM=16384 CPUS=8 PARALLEL=1 KEEP=1 MAXWAIT=7200 HOLD=5400 ./run.sh
```

Swap `veryhard.csv` → `hard.csv` for all 42, or hand-pick a cross-repo subset
from `all.tsv`. See `../econ/fly-remote/fullresolve/RUNBOOK.md` §2b for the full
flow + the hard-tier caveats (bump `STALL_SECS`; non-django images are larger and
econ is untuned there → expect lower resolve rates + more thrash).

> **Any Verified id works**, not just ones we've run before — the runner pulls the
> official `swebench/sweb.eval.x86_64.<id>` image on demand. `IDS=` also overrides
> the `INSTANCES` first-N cap.
