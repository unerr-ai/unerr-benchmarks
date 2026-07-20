#!/usr/bin/env bash
# debug-workers.sh — look INSIDE a fleet's workers: what is each one actually
# doing right now? For every worker machine it prints (a) the coordinator's view
# — which instance it currently holds (leased), attempts, resolved — and (b) the
# tail of that machine's fly logs, highlighting the boot/work state lines
# (dockerd up, toolbox build, /claim, resolving, resolved, dead, ERROR). Use it
# when a run looks stuck, a worker went quiet, or a patch came back empty.
#
# Fleet (pick one; default = the run you most recently launched):
#   (no args)            newest out/bench-*/manifest.tsv  (every fleet's workers)
#   --matrix <id>        that matrix's fleets
#   <LABEL> [APP]        one fleet (APP inferred: arm from a -claude or -claude-real
#                        fold in the label, benchmark from $BENCHMARK else the
#                        label's -pro/-terminal/-live_verified/-lite suffix, else verified)
#
# Options:
#   --lines N        log lines to tail per worker (default 60)
#   --grep <re>      only show log lines matching this regex (extra to the state highlight)
#   --follow         stream logs live for the fleet's workers (flyctl logs, no --no-tail); Ctrl-C to stop
#   --instance <id>  only the worker currently holding this instance_id (from /status)
#
# Examples:
#   ./debug-workers.sh                            # newest matrix, all workers, last 60 lines each
#   ./debug-workers.sh smk-e-econ --lines 120     # one fleet, deeper tail
#   ./debug-workers.sh smk-e-econ --follow        # live-stream both workers
#   ./debug-workers.sh smk-e-econ --instance django__django-11999
#   BENCHMARK=pro ./debug-workers.sh <LABEL>      # non-verified fleet whose label lacks the suffix
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=tools/fleet-common.sh
source "$HERE/tools/fleet-common.sh"

MATRIX=""; MANIFEST=""; LINES=60; GREP=""; FOLLOW=0; ONLY_INSTANCE=""
POS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --matrix)   MATRIX="${2:?--matrix needs a value}"; shift 2 ;;
    --manifest) MANIFEST="${2:?--manifest needs a value}"; shift 2 ;;
    --lines)    LINES="${2:?--lines needs a value}"; shift 2 ;;
    --grep)     GREP="${2:?--grep needs a value}"; shift 2 ;;
    --follow)   FOLLOW=1; shift ;;
    --instance) ONLY_INSTANCE="${2:?--instance needs a value}"; shift 2 ;;
    -h|--help)  sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)         echo "ERROR: unknown flag '$1'" >&2; exit 2 ;;
    *)          POS+=("$1"); shift ;;
  esac
done

fc_fly_token || exit 1

# ── benchmark key for an APP-less <LABEL>: $BENCHMARK env wins (for a non-verified
# fleet whose label carries no suffix); else the label's own -<benchmark> suffix,
# also matching a grade-rerun tail (<base>-<benchmark>-graderr-<time>); else verified. ──
_bench_from_label() {  # <label>
  local lbl="$1"
  if [ -n "${BENCHMARK:-}" ]; then echo "$BENCHMARK"; return; fi
  case "$lbl" in
    *-pro|*-pro-graderr-*)                     echo pro ;;
    *-terminal|*-terminal-graderr-*)           echo terminal ;;
    *-live_verified|*-live_verified-graderr-*) echo live_verified ;;
    *-lite|*-lite-graderr-*)                   echo lite ;;
    *)                                         echo verified ;;
  esac
}

# Resolve the fleet set -> labels[]/apps[]/combos[].
labels=(); apps=(); combos=()
[ -n "$MATRIX" ] && [ -z "$MANIFEST" ] && MANIFEST="$HERE/out/bench-$MATRIX/manifest.tsv"
if [ "${#POS[@]}" -ge 1 ]; then
  lbl="${POS[0]}"; app="${POS[1]:-}"
  if [ -z "$app" ]; then
    bench="$(_bench_from_label "$lbl")"
    # claude-real MUST be matched before the bare *claude* fallback (its label
    # also contains "claude") or it mislabels as claude's own app.
    case "$lbl" in
      *claude-real*) app="$(fc_default_app claude-real "$bench")" ;;
      *claude*)      app="$(fc_default_app claude "$bench")" ;;
      *)             app="$(fc_default_app econ "$bench")" ;;
    esac
  fi
  labels+=("$lbl"); apps+=("$app"); combos+=("$lbl")
else
  [ -n "$MANIFEST" ] || MANIFEST="$(fc_newest_manifest)"
  [ -n "$MANIFEST" ] && [ -f "$MANIFEST" ] || {
    echo "ERROR: no fleet given and no manifest found — pass <LABEL> [APP] or --matrix <id>." >&2; exit 1; }
  echo "==> fleets from $MANIFEST" >&2
  while IFS=$'\t' read -r a b l ap; do
    labels+=("$l"); apps+=("$ap"); combos+=("${a}:${b}")
  done < <(fc_read_manifest "$MANIFEST")
fi
[ "${#labels[@]}" -gt 0 ] || { echo "ERROR: no fleets to debug" >&2; exit 1; }

# "worker_id<TAB>instance_id<TAB>status<TAB>attempt<TAB>resolved" for leased rows,
# so we can label each worker with the instance it currently holds. Empty on any error.
leased_map() {  # <app> <coord_mid>
  local app="$1" coord="$2" js
  js="$(fc_status "$app" "$coord")"
  [ -n "$js" ] || return 0
  printf '%s\n' "$js" | "$PY_HOST" -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for it in (d.get("instances") or []):
    if it.get("status") == "leased" and it.get("worker_id"):
        print("\t".join([str(it["worker_id"]), str(it.get("instance_id","?")),
                          str(it.get("status","?")), str(it.get("attempt_count",0)),
                          "R" if it.get("resolved") else "."]))
'
}

# Recent state of one worker: what the coordinator says it holds + a highlighted log tail.
debug_worker() {  # <app> <wid> <leased_line-or-empty>
  local app="$1" wid="$2" leased="$3"
  echo
  echo "──────── worker $wid  (app=$app) ────────"
  if [ -n "$leased" ]; then
    IFS=$'\t' read -r _w iid st att res <<<"$leased"
    printf '  holding: %s  [%s]  attempt=%s resolved=%s   (per coordinator /status)\n' "$iid" "$st" "$att" "$res"
  else
    printf '  holding: (nothing leased right now — between tasks, warming, or drained)\n'
  fi
  echo "  ---- last $LINES log lines (state lines flagged with »») ----"
  # --no-tail = the buffered logs then exit. Flag the lines that reveal boot/work state.
  flyctl logs -a "$app" --machine "$wid" --no-tail 2>/dev/null \
    | { [ -n "$GREP" ] && grep -E "$GREP" || cat; } \
    | tail -n "$LINES" \
    | sed -E 's#^.*(dockerd_up|docker daemon|toolbox|building|claim|claimed|resolving|resolved|patch|report|dead|drain|no work|ERROR|Error|Traceback|FATAL|OOM|Killed).*#»» &#' \
    || echo "  (no logs — machine may be brand new, stopped, or on a different app)"
}

# ── streaming mode: hand off to flyctl logs (multi-machine live tail) ──
if [ "$FOLLOW" = 1 ]; then
  # follow only makes sense for a single fleet; use the first resolved one.
  app="${apps[0]}"; label="${labels[0]}"
  echo "==> following logs for workers of fleet '$label' (app=$app) — Ctrl-C to stop" >&2
  wids="$(fc_machines "$app" "$label" worker)"; rc=$?
  [ "$rc" -eq 3 ] && { echo "ERROR: fly API unreachable — worker state UNKNOWN (not proof the fleet is gone): $(fc_last_error)" >&2; exit 1; }
  [ -n "$wids" ] || { echo "ERROR: no worker machines for fleet=$label on $app" >&2; exit 1; }
  # flyctl logs streams a whole app; filter to the fleet's workers when multiple.
  set --
  for w in $wids; do set -- "$@" --machine "$w"; done
  # flyctl accepts a single --machine; loop-stream each in the background for a combined view.
  pids=()
  for w in $wids; do
    ( flyctl logs -a "$app" --machine "$w" 2>&1 | sed "s/^/[$w] /" ) &
    pids+=($!)
  done
  trap 'kill "${pids[@]}" 2>/dev/null || true' INT TERM EXIT
  wait
  exit 0
fi

# ── snapshot mode: per fleet, per worker ──
i=0
while [ "$i" -lt "${#labels[@]}" ]; do
  combo="${combos[$i]}"; label="${labels[$i]}"; app="${apps[$i]}"
  echo
  echo "════════ ${combo}   label=$label  app=$app ════════"
  coord="$(fc_coord "$app" "$label")"
  wids="$(fc_machines "$app" "$label" worker)"; rc=$?
  if [ "$rc" -eq 3 ]; then
    # could not ASK the API — do not claim the fleet is gone (see fleet-common.sh)
    echo "  UNKNOWN — fly API unreachable ($(fc_last_error))"
    i=$(( i + 1 )); continue
  fi
  if [ -z "$wids" ]; then
    echo "  no worker machines (fleet torn down, or never created on $app)"
    i=$(( i + 1 )); continue
  fi
  # Map worker_id -> leased line once per fleet (one /status call).
  lmap=""
  [ -n "$coord" ] && lmap="$(leased_map "$app" "$coord" || true)"
  for w in $wids; do
    if [ -n "$ONLY_INSTANCE" ]; then
      # skip workers not holding the requested instance
      hold="$(printf '%s\n' "$lmap" | awk -F'\t' -v w="$w" -v iid="$ONLY_INSTANCE" '$1==w && $2==iid')"
      [ -n "$hold" ] || continue
    fi
    line="$(printf '%s\n' "$lmap" | awk -F'\t' -v w="$w" '$1==w {print; exit}')"
    debug_worker "$app" "$w" "$line"
  done
  i=$(( i + 1 ))
done
