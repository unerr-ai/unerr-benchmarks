#!/usr/bin/env bash
# bench.sh — matrix launcher: fire ANY subset of (arm x benchmark) runs, each as its
# OWN independent coordinator + worker fleet, in parallel or sequentially.
#
# Each combo is one `run-distributed.sh <mode>` invocation with ARM + BENCHMARK set and
# a distinct LABEL, so 1, 3, 4, or 6 sets can be triggered together — exactly the
# "trigger off all 3/4/6 sets when we need" shape. Fleets are LABEL-scoped (fly
# metadata fleet=<LABEL>); same-arm combos share the arm's app but never collide.
#
# Usage:
#   ./bench.sh <mode> econ:verified claude:pro econ:terminal        # explicit combos
#   ./bench.sh <mode> --arms econ,claude --benches verified,pro     # cartesian product
#   <mode> = run | prepare | start | destroy
#     run      full one-shot per combo — build+seed+create+arm+poll+pull+teardown (no GPU window).
#              Maps to run-distributed.sh's DEFAULT (no-subcommand) all-in-one mode.
#     prepare  build image + create each fleet WARM (coordinator holds /claim, workers idle),
#              then exit. Use before a GPU window so nothing waits on the meter. (rd `prepare`)
#     start    arm each PREPARED fleet's gate + poll + pull + teardown (the second half after a
#              prepare + GPU flip). Requires a prior `prepare`. (rd `run`)
#     destroy  tear down every combo's fleet by its fleet=<LABEL> metadata. (rd `destroy`)
#   <arm>    any value passes straight through as ARM=<value> (no allowlist here) —
#            econ | claude-<mix> | claude-native, e.g. --arms econ,claude-gpt,claude-open,claude-native.
#            Legacy claude/claude-real are auto-normalized to claude-open/claude-native.
#   (no `status`/`arm` here — a bad mode would risk run-distributed's default all-in-one launching
#    a real fleet; check a live fleet per README §3, or use ./download-all.sh.)
#
#   ./bench.sh prepare --arms econ,claude --benches verified,pro,terminal   # warm 6 fleets
#   # ... raise your GPUs + ./gpu-flip.sh --conductor <id> between prepare and start
#   #     (gpu-flip.sh lives in ../unerr-terminal-bench/infra/litellm/) ...
#   ./bench.sh start   --arms econ,claude --benches verified,pro,terminal   # arm+poll+pull+teardown all 6
#   PLAN_ONLY=1 ./bench.sh run econ:pro claude:terminal                     # preview, no fly calls
#
# Options / env:
#   --suite <s>     SUITE for every combo (full|smoke|mini|pro-mini|...) — default full
#   --seq           run combos sequentially (default: all in parallel, backgrounded)
#   --matrix <id>   shared LABEL prefix for this matrix (default: mtx-<epoch>)
#   MACHINES, DEDICATED_CONDUCTOR, ROOTFS_GB, CPU_KIND, ... — any run-distributed.sh
#                   env is inherited by every combo (set once, applies to all).
#   PLAN_ONLY=1     forwarded to each combo (prints its resolved plan, no fly calls).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODE="${1:-}"
case "$MODE" in
  run|prepare|start|destroy) shift ;;
  ""|-h|--help) sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'; [ -n "$MODE" ] && exit 0 || exit 1 ;;
  *) echo "ERROR: first arg must be a mode (run|prepare|start|destroy), got '$MODE'" >&2; exit 2 ;;
esac
# bench.sh mode -> run-distributed.sh subcommand. `run` = the one-shot ALL mode, which
# run-distributed.sh selects when given NO subcommand — so RD_MODE is empty for it.
case "$MODE" in
  run)     RD_MODE="" ;;
  prepare) RD_MODE="prepare" ;;
  start)   RD_MODE="run" ;;       # rd `run` = arm a PREPARED fleet + poll + pull + teardown
  destroy) RD_MODE="destroy" ;;
esac

SUITE_OPT="${SUITE:-}"
SEQ=0
MATRIX=""
ARMS=""
BENCHES=""
COMBOS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --suite)   SUITE_OPT="${2:?--suite needs a value}"; shift 2 ;;
    --seq)     SEQ=1; shift ;;
    --matrix)  MATRIX="${2:?--matrix needs a value}"; shift 2 ;;
    --arms)    ARMS="${2:?--arms needs a value}"; shift 2 ;;
    --benches) BENCHES="${2:?--benches needs a value}"; shift 2 ;;
    *:*)       COMBOS+=("$1"); shift ;;                       # explicit arm:benchmark
    *) echo "ERROR: unknown arg '$1' (want arm:benchmark, or --arms/--benches)" >&2; exit 2 ;;
  esac
done

# Cartesian product of --arms x --benches, appended to any explicit combos.
if [ -n "$ARMS" ] && [ -n "$BENCHES" ]; then
  IFS=',' read -r -a _arms <<<"$ARMS"
  IFS=',' read -r -a _benches <<<"$BENCHES"
  for _a in "${_arms[@]}"; do for _b in "${_benches[@]}"; do
    COMBOS+=("${_a}:${_b}")
  done; done
fi
[ "${#COMBOS[@]}" -gt 0 ] || { echo "ERROR: no combos — give arm:benchmark args or --arms + --benches" >&2; exit 2; }

# Shared matrix id -> per-combo LABEL prefix. run-distributed folds -<benchmark> onto the
# LABEL for non-verified benchmarks, so LABEL=<matrix>-<arm> yields distinct fleets:
#   econ:verified -> <matrix>-econ   econ:pro -> <matrix>-econ-pro   claude:terminal -> <matrix>-claude-terminal
[ -n "$MATRIX" ] || MATRIX="mtx-$(date +%s)"

echo "==> matrix '$MATRIX': mode=$MODE suite=${SUITE_OPT:-<default full>} combos=${#COMBOS[@]} $([ "$SEQ" = 1 ] && echo sequential || echo parallel)"
for c in "${COMBOS[@]}"; do echo "    - $c"; done

LOGDIR="${LOGDIR:-$HERE/out/bench-$MATRIX}"
mkdir -p "$LOGDIR"
# Authoritative record of what this matrix launched: one TAB row per combo with
# the RESOLVED label + app (read back from run-distributed.sh's own PLAN output,
# so the label-fold / app-scoping rules stay single-sourced there — download-all.sh
# reads this to re-pull every fleet's bundle without re-deriving those rules).
MANIFEST="$LOGDIR/manifest.tsv"
: >"$MANIFEST"
printf '# matrix\t%s\tmode\t%s\tsuite\t%s\n' "$MATRIX" "$MODE" "${SUITE_OPT:-full}" >>"$MANIFEST"
printf '# arm\tbenchmark\tlabel\tapp\n' >>"$MANIFEST"
USER_PLAN_ONLY="${PLAN_ONLY:-}"     # capture the caller's intent before the per-combo probe shadows it
pids=()
rc_overall=0

launch() {  # $1 = arm:benchmark
  local combo="$1" arm="${1%%:*}" bench="${1##*:}"
  # Normalize legacy arm aliases to the canonical scheme (SAME mapping as
  # run-distributed.sh) so this manifest's app/label match what the launcher
  # actually creates: claude -> claude-open, claude-real -> claude-native.
  case "$arm" in claude) arm="claude-open" ;; claude-real) arm="claude-native" ;; esac
  local raw_label="${MATRIX}-${arm}"
  local log="$LOGDIR/${arm}-${bench}.log"
  # Probe run-distributed.sh in PLAN_ONLY (no fly API calls) for the AUTHORITATIVE
  # resolved APP + folded LABEL. run-distributed.sh owns those rules; we read them
  # back rather than re-deriving (verified keeps LABEL as-is; non-verified folds
  # -<bench> on; claude arm uses the -claude app). Falls back to the obvious value.
  local plan app label
  plan="$(ARM="$arm" BENCHMARK="$bench" LABEL="$raw_label" SUITE="$SUITE_OPT" \
          PLAN_ONLY=1 "$HERE/run-distributed.sh" $RD_MODE 2>&1 || true)"
  app="$(printf '%s\n' "$plan"   | sed -n 's/^[[:space:]]*APP=\(.*\)$/\1/p' | head -1)"
  label="$(printf '%s\n' "$plan" | sed -n 's/^[[:space:]]*LABEL=\([^ ]*\).*$/\1/p' | head -1)"
  # Fallback (plan parse failed) mirrors run-distributed.sh's DEFAULT_APP: one
  # app per (arm x benchmark), '_' -> '-' in the benchmark key, then
  # 'verified' -> 'verif' (fly's abuse filter blocks names containing "verified").
  if [ -z "$app" ]; then
    app="${bench//_/-}"; app="swebench-dist-${arm}-${app//verified/verif}"
  fi
  [ -n "$label" ] || label="$raw_label"
  printf '%s\t%s\t%s\t%s\n' "$arm" "$bench" "$label" "$app" >>"$MANIFEST"
  echo "==> [$combo] ARM=$arm BENCHMARK=$bench -> LABEL=$label APP=$app -> run-distributed.sh ${RD_MODE:-<all/one-shot>}  (log: $log)"
  if [ "$USER_PLAN_ONLY" = 1 ]; then
    printf '%s\n' "$plan" >"$log"     # plan-only: the probe IS the run — don't invoke twice
    return 0
  fi
  ARM="$arm" BENCHMARK="$bench" LABEL="$raw_label" SUITE="$SUITE_OPT" \
    "$HERE/run-distributed.sh" $RD_MODE >"$log" 2>&1
}

for combo in "${COMBOS[@]}"; do
  if [ "$SEQ" = 1 ]; then
    launch "$combo" || rc_overall=1
  else
    launch "$combo" &
    pids+=($!)
  fi
done

if [ "$SEQ" != 1 ]; then
  for p in "${pids[@]}"; do wait "$p" || rc_overall=1; done
fi

echo "==> matrix '$MATRIX' done (mode=$MODE). Per-combo logs + manifest in $LOGDIR"
echo "    manifest: $MANIFEST"
[ "$MODE" = run ] && echo "    pull every fleet's results+traces:  ./download-all.sh --matrix $MATRIX"
[ "$rc_overall" = 0 ] || echo "==> WARNING: one or more combos exited non-zero — inspect the logs above" >&2
exit "$rc_overall"
