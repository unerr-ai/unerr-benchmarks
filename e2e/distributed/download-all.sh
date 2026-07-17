#!/usr/bin/env bash
# download-all.sh — pull results + traces for EVERY fleet a bench.sh matrix
# launched (or an explicit set of fleets), in one pass. Each combo's bundle is
# fetched with the existing tools/pull_results.sh (fleet lookup + sftp get +
# extract), then summarized (resolved/total, artifacts, dead) so a whole matrix
# is one command instead of one pull_results call per (label, app).
#
# Fleet set comes from ONE of:
#   --matrix <id>       read out/bench-<id>/manifest.tsv (bench.sh's authoritative
#                       record of the resolved label+app per combo)
#   --manifest <path>   read an explicit manifest.tsv
#   <label> <app> ...   explicit (label, app) pairs on the command line
#
# Options:
#   --submission        also emit the leaderboard submission (tools/make_submission.py)
#                       for each resolve_then_grade combo (skipped for Terminal — no patch)
#   --model-name <n>    model_name_or_path stamped on submission rows (see make_submission.py)
#   --keep-going        don't stop if one fleet's pull fails (default: keep going anyway)
#
# Examples:
#   ./download-all.sh --matrix mtx-1784207188
#   ./download-all.sh --matrix mtx-1784207188 --submission --model-name unerr-claude-openmodels
#   ./download-all.sh testmtx-econ swebench-agent-dist testmtx-claude-pro swebench-agent-dist-claude
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"

MATRIX=""
MANIFEST=""
SUBMISSION=0
MODEL_NAME=""
PAIRS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --matrix)     MATRIX="${2:?--matrix needs a value}"; shift 2 ;;
    --manifest)   MANIFEST="${2:?--manifest needs a value}"; shift 2 ;;
    --submission) SUBMISSION=1; shift ;;
    --model-name) MODEL_NAME="${2:?--model-name needs a value}"; shift 2 ;;
    --keep-going) shift ;;   # accepted for clarity; this script always keeps going
    -h|--help)    sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)           echo "ERROR: unknown flag '$1'" >&2; exit 2 ;;
    *)            PAIRS+=("$1"); shift ;;
  esac
done

[ -n "$MATRIX" ] && [ -z "$MANIFEST" ] && MANIFEST="$HERE/out/bench-$MATRIX/manifest.tsv"

# Build the work list as parallel arrays: arms[i]/benches[i]/labels[i]/apps[i].
arms=(); benches=(); labels=(); apps=()
if [ -n "$MANIFEST" ]; then
  [ -f "$MANIFEST" ] || { echo "ERROR: manifest not found: $MANIFEST" >&2; exit 1; }
  echo "==> reading fleet set from $MANIFEST"
  while IFS=$'\t' read -r a b l ap _rest; do
    case "$a" in ''|'#'*) continue ;; esac      # skip blank + comment/header rows
    [ -n "${l:-}" ] && [ -n "${ap:-}" ] || continue
    arms+=("$a"); benches+=("$b"); labels+=("$l"); apps+=("$ap")
  done <"$MANIFEST"
elif [ "${#PAIRS[@]}" -gt 0 ]; then
  [ $(( ${#PAIRS[@]} % 2 )) -eq 0 ] || { echo "ERROR: explicit args must be <label> <app> pairs (even count)" >&2; exit 2; }
  i=0
  while [ "$i" -lt "${#PAIRS[@]}" ]; do
    arms+=("?"); benches+=("?"); labels+=("${PAIRS[$i]}"); apps+=("${PAIRS[$((i+1))]}")
    i=$(( i + 2 ))
  done
else
  echo "ERROR: give --matrix <id>, --manifest <path>, or explicit <label> <app> pairs" >&2
  exit 2
fi

n="${#labels[@]}"
[ "$n" -gt 0 ] || { echo "ERROR: no fleets to pull" >&2; exit 1; }
echo "==> pulling $n fleet bundle(s)"

# flow(benchmark) -> resolve_then_grade | harness_run (single source of truth: benchmarks.py).
flow_of() {
  [ "$1" = "?" ] && { echo "resolve_then_grade"; return; }
  "$PY" -c "import sys; sys.path.insert(0,'$HERE/tools'); import benchmarks; print(benchmarks.get('$1')['flow'])" 2>/dev/null \
    || echo "resolve_then_grade"
}

# Summarize one extracted bundle: resolved/total (reports/merged.<label>.json),
# artifact dirs, dead rows. Pure-python so the host needs no jq.
summarize() {  # $1=label
  local label="$1" bundle="$HERE/out/dist-$1/bundle"
  "$PY" - "$bundle" "$label" <<'PY'
import json, os, sys, glob
bundle, label = sys.argv[1], sys.argv[2]
merged = os.path.join(bundle, "reports", f"merged.{label}.json")
resolved = total = None
if os.path.isfile(merged):
    try:
        d = json.load(open(merged))
        resolved = d.get("resolved_instances")
        total = d.get("submitted_instances") or d.get("total_instances")
    except Exception:
        pass
res_dir = os.path.join(bundle, "results", label)
n_art = len(glob.glob(os.path.join(res_dir, "artifacts", "*")))
dead = os.path.join(res_dir, "dead.jsonl")
n_dead = sum(1 for _ in open(dead)) if os.path.isfile(dead) else 0
preds = os.path.join(res_dir, "preds.json")
n_preds = 0
if os.path.isfile(preds):
    try:
        p = json.load(open(preds)); n_preds = len(p) if isinstance(p, (dict, list)) else 0
    except Exception:
        pass
print(f"{resolved if resolved is not None else '?'}\t{total if total is not None else '?'}\t{n_preds}\t{n_art}\t{n_dead}")
PY
}

rows=()   # for the closing table: "arm bench label resolved/total preds artifacts dead"
i=0
while [ "$i" -lt "$n" ]; do
  a="${arms[$i]}"; b="${benches[$i]}"; l="${labels[$i]}"; ap="${apps[$i]}"
  echo
  echo "── [$((i+1))/$n] ${a}:${b}  label=$l  app=$ap ─────────────────────────────"
  if "$HERE/tools/pull_results.sh" "$l" "$ap"; then
    s="$(summarize "$l")"
    IFS=$'\t' read -r r t np na nd <<<"$s"
    echo "   resolved=$r/$t  preds=$np  artifacts=$na  dead=$nd  ->  out/dist-$l/bundle/"
    rows+=("$a	$b	$l	$r/$t	$np	$na	$nd")
    if [ "$SUBMISSION" = 1 ]; then
      fl="$(flow_of "$b")"
      if [ "$fl" = "harness_run" ]; then
        echo "   (submission skipped — $b is a fused harness run, no git patch)"
      elif [ -f "$HERE/out/dist-$l/bundle/results/$l/preds.json" ]; then
        echo "   building submission ..."
        "$PY" "$HERE/tools/make_submission.py" "$HERE/out/dist-$l/bundle/results/$l" \
          ${MODEL_NAME:+--model-name "$MODEL_NAME"} || echo "   (submission had empty/missing patches — see above)"
      else
        echo "   (submission skipped — no preds.json in bundle)"
      fi
    fi
  else
    echo "   PULL FAILED for label=$l app=$ap — fleet down, wrong app, or nothing to pull"
    rows+=("$a	$b	$l	PULL-FAILED	-	-	-")
  fi
  i=$(( i + 1 ))
done

echo
echo "==================== matrix download summary ===================="
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' arm benchmark label resolved preds artifacts dead
for r in "${rows[@]}"; do printf '%s\n' "$r"; done | { command -v column >/dev/null && column -t -s$'\t' || cat; }
echo "================================================================"
echo "bundles under: $HERE/out/dist-<label>/bundle/   (traces: results/<label>/artifacts/<iid>/)"
