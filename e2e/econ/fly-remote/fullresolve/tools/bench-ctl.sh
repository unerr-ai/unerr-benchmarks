#!/usr/bin/env bash
# bench-ctl — one control surface for the econ fly full-resolve runs.
# Replaces the ad-hoc monitor/pull/parse scripts we kept re-writing each session.
#
# Subcommands:
#   machines                 list machines for the app
#   status  [LABEL]          in-VM progress: resolve stage, rows done, bundle state
#   watch   [LABEL]          poll until a FRESH bundle lands, pull+extract to out/<LABEL>-bundle/
#   pull    [LABEL]          one-shot: pull /data/bundle.tgz now → out/<LABEL>-bundle/
#   audit   [BUNDLE_DIR]     cost/token/tier/resolution table (default: newest out/*-bundle)
#   trace   BUNDLE_DIR IID   per-instance root-cause (tool seq, test runs, edited-tests)
#   trace   BUNDLE_DIR --all terse root-cause line per instance
#   destroy                  destroy ALL machines for the app (frees the volume)
#
# Distributed-fleet subcommands (separate app; see e2e/distributed/PLAN.md):
#   distributed-status  LABEL       fleet machines (role+state) + coordinator /status (done/total/resolved, per-instance)
#   distributed-pull    LABEL       sftp-get /data/bundle.tgz from the coordinator -> e2e/distributed/out/dist-<LABEL>/
#   distributed-destroy LABEL [-y]  destroy ALL fleet machines (any role) + the coordinator volume (safety-net teardown)
#
# Env: APP (default unerr-bench-econ-fullresolve), MID (pin a machine; else auto-discover),
#      REF (epoch floor for watch freshness; default = now).
#      DIST_APP (default unerr-bench-dist, the distributed-fleet app), FORCE=1 (skip the
#      distributed-destroy confirmation prompt, same as passing -y).
set -uo pipefail
FR="$(cd "$(dirname "$0")/.." && pwd)"      # the fullresolve dir
OUT="$FR/out"; TOOLS="$FR/tools"
APP="${APP:-unerr-bench-econ-fullresolve}"
mkdir -p "$OUT"

if [ -z "${FLY_API_TOKEN:-}" ]; then
  FLY_API_TOKEN="$(node -e "const fs=require('fs');const y=fs.readFileSync(process.env.HOME+'/.fly/config.yml','utf8');const m=y.match(/access_token:\s*(\S+)/);process.stdout.write(m?m[1]:'')")"
fi
export FLY_API_TOKEN

# ── distributed fleet (separate app + out-dir from the single-machine commands above) ──
DIST_APP="${DIST_APP:-unerr-bench-dist}"
DIST_HERE="$(cd "$FR/../../../distributed" 2>/dev/null && pwd || true)"   # e2e/distributed

dist_fleet_ids() {                          # dist_fleet_ids <label> [role] -> "id role state" lines
  flyctl machines list -a "$DIST_APP" --json 2>/dev/null | python3 -c '
import sys, json
label = sys.argv[1]
role  = sys.argv[2] if len(sys.argv) > 2 else ""
try:
    ms = json.load(sys.stdin)
except Exception:
    ms = []
for m in ms:
    md = (m.get("config") or {}).get("metadata") or {}
    if md.get("fleet") != label:
        continue
    if role and md.get("role") != role:
        continue
    print(m.get("id"), md.get("role"), m.get("state"))
' "$1" "${2:-}"
}

discover_mid() {                            # newest started/created machine
  [ -n "${MID:-}" ] && { echo "$MID"; return; }
  flyctl machines list -a "$APP" 2>/dev/null | awk '/started|created/{print $1; exit}'
}
ssh_vm() {                                  # ssh_vm <machine> <remote bash cmd>
  flyctl ssh console -a "$APP" --machine "$1" -C "bash -lc '$2'" 2>/dev/null \
    | grep -vE 'Connecting|Waiting|already|Connected'
}
newest_bundle() { ls -td "$OUT"/*-bundle 2>/dev/null | head -1; }

cmd="${1:-help}"; shift || true
case "$cmd" in

machines)
  flyctl machines list -a "$APP" ;;

status)
  LABEL="${1:-econ}"; MID="$(discover_mid)"
  [ -n "$MID" ] || { echo "no running machine for $APP"; exit 1; }
  echo "machine=$MID app=$APP label=$LABEL"
  ssh_vm "$MID" "echo STAGE:; grep -oE \"\\[[0-9]+/[0-9]+\\] (START|DONE) django__django-[0-9]+\" /data/logs/resolve-$LABEL.log 2>/dev/null | tail -6; echo; echo DONE_ROWS:; wc -l < /data/results/$LABEL/meta.jsonl 2>/dev/null; echo BUNDLE:; ls -la /data/bundle.tgz 2>&1 | head -1; echo GRADE:; grep -oE 'grade .*resolved=[0-9]+/[0-9]+' /data/logs/report-$LABEL.log /data/logs/*.log 2>/dev/null | tail -1" ;;

watch)
  LABEL="${1:-econ}"; MID="$(discover_mid)"; REF="${REF:-$(date +%s)}"
  [ -n "$MID" ] || { echo "no running machine for $APP"; exit 1; }
  DEST="$OUT/$LABEL-bundle"
  echo "watching machine=$MID label=$LABEL (fresh-guard REF=$REF) → $DEST"
  for i in $(seq 1 200); do
    R="$(ssh_vm "$MID" "b=/data/bundle.tgz; if [ -f \$b ] && [ \$(stat -c %Y \$b) -gt $REF ]; then echo BUNDLE_FRESH; fi; grep -oE \"\\[[0-9]+/[0-9]+\\] DONE\" /data/logs/resolve-$LABEL.log 2>/dev/null | tail -1")"
    echo "[$(date -u +%H:%M:%S)] poll#$i: $(echo $R | tr '\n' ' ')"
    if echo "$R" | grep -q BUNDLE_FRESH; then
      rm -f "$OUT/$LABEL-bundle.tgz"
      flyctl ssh sftp get /data/bundle.tgz "$OUT/$LABEL-bundle.tgz" -a "$APP" --machine "$MID" 2>&1 | tail -1
      rm -rf "$DEST"; mkdir -p "$DEST"; tar xzf "$OUT/$LABEL-bundle.tgz" -C "$DEST" \
        && echo "extracted → $DEST ($(du -h "$OUT/$LABEL-bundle.tgz"|cut -f1))"
      echo "--- audit ---"; python3 "$TOOLS/bench-audit.py" --bundle "$DEST" --label "$LABEL"
      exit 0
    fi
    sleep 120
  done
  echo "watch timed out"; exit 1 ;;

pull)
  LABEL="${1:-econ}"; MID="$(discover_mid)"; DEST="$OUT/$LABEL-bundle"
  [ -n "$MID" ] || { echo "no running machine for $APP"; exit 1; }
  rm -f "$OUT/$LABEL-bundle.tgz"
  flyctl ssh sftp get /data/bundle.tgz "$OUT/$LABEL-bundle.tgz" -a "$APP" --machine "$MID" 2>&1 | tail -2
  [ -f "$OUT/$LABEL-bundle.tgz" ] || { echo "pull failed (no bundle yet?)"; exit 1; }
  rm -rf "$DEST"; mkdir -p "$DEST"; tar xzf "$OUT/$LABEL-bundle.tgz" -C "$DEST" && echo "extracted → $DEST" ;;

audit)
  B="${1:-$(newest_bundle)}"; [ -n "$B" ] || { echo "no bundle dir (pass one, or pull first)"; exit 1; }
  python3 "$TOOLS/bench-audit.py" --bundle "$B" "${@:2}" ;;

trace)
  B="${1:?usage: bench-ctl trace BUNDLE_DIR (IID|--all)}"; shift
  python3 "$TOOLS/bench-trace.py" --bundle "$B" "$@" ;;

destroy)
  for m in $(flyctl machines list -a "$APP" 2>/dev/null | awk 'NR>1 && /[0-9a-f]{10,}/{print $1}'); do
    echo "destroying $m"; flyctl machine destroy "$m" -a "$APP" --force 2>&1 | tail -1
  done ;;

distributed-status)
  LABEL="${1:?usage: bench-ctl distributed-status LABEL}"
  echo "fleet=$LABEL app=$DIST_APP"
  echo "-- machines --"
  IDS_OUT="$(dist_fleet_ids "$LABEL")"
  if [ -z "$IDS_OUT" ]; then
    echo "  no machines with fleet=$LABEL (not launched, or already torn down)"
  else
    printf '%s\n' "$IDS_OUT" | while read -r mid role state; do
      printf '  %-18s role=%-11s state=%s\n' "$mid" "$role" "$state"
    done
  fi
  COORD_MID="$(printf '%s\n' "$IDS_OUT" | awk '$2=="coordinator"{print $1; exit}')"
  if [ -z "$COORD_MID" ]; then
    echo "-- coordinator: not up yet (no role=coordinator machine) --"
    exit 0
  fi
  echo "-- coordinator $COORD_MID /status --"
  S="$(flyctl ssh console -a "$DIST_APP" --machine "$COORD_MID" -C "curl -s localhost:8080/status" 2>/dev/null \
    | grep -vE 'Connecting|Waiting|Connected|already')"
  if [ -z "$S" ]; then
    echo "  coordinator not responding yet (still booting / server not up)"
    exit 0
  fi
  printf '%s' "$S" | python3 -c '
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    print("  raw:", raw[:300]); raise SystemExit
c = d.get("counts", {})
print(f"  run_id={d.get(\"run_id\")} total={d.get(\"total\")} resolved={d.get(\"resolved\")}")
print(f"  pending={c.get(\"pending\",0)} leased={c.get(\"leased\",0)} done={c.get(\"done\",0)} dead={c.get(\"dead\",0)}")
for r in d.get("instances", []):
    print(f"    {r[\"instance_id\"]:<32} {r[\"status\"]:<8} resolved={r[\"resolved\"]} attempts={r[\"attempt_count\"]} worker={r[\"worker_id\"]}")
' 2>/dev/null || echo "  raw: $S" ;;

distributed-pull)
  LABEL="${1:?usage: bench-ctl distributed-pull LABEL}"
  [ -n "$DIST_HERE" ] || { echo "cannot locate e2e/distributed (expected at $FR/../../../distributed)"; exit 1; }
  COORD_MID="$(dist_fleet_ids "$LABEL" coordinator | awk '{print $1; exit}')"
  [ -n "$COORD_MID" ] || { echo "no coordinator machine for fleet=$LABEL"; exit 1; }
  DEST_OUT="$DIST_HERE/out/dist-$LABEL"; mkdir -p "$DEST_OUT"
  echo "pulling /data/bundle.tgz from coordinator $COORD_MID -> $DEST_OUT"
  rm -f "$DEST_OUT/bundle.tgz"
  flyctl ssh sftp get /data/bundle.tgz "$DEST_OUT/bundle.tgz" -a "$DIST_APP" --machine "$COORD_MID" 2>&1 | tail -2
  [ -f "$DEST_OUT/bundle.tgz" ] || { echo "pull failed (no bundle yet?)"; exit 1; }
  rm -rf "$DEST_OUT/bundle"; mkdir -p "$DEST_OUT/bundle"
  tar xzf "$DEST_OUT/bundle.tgz" -C "$DEST_OUT/bundle" && echo "extracted -> $DEST_OUT/bundle" ;;

distributed-destroy)
  LABEL="${1:?usage: bench-ctl distributed-destroy LABEL [-y]}"; shift || true
  FORCE="${FORCE:-0}"; [ "${1:-}" = "-y" ] && FORCE=1
  if [ "$FORCE" != "1" ]; then
    printf 'destroy ALL machines + coordinator volume for fleet=%s on app=%s? [y/N] ' "$LABEL" "$DIST_APP"
    read -r ans; case "$ans" in y|Y|yes) ;; *) echo "aborted"; exit 1 ;; esac
  fi
  echo "tearing down fleet '$LABEL' on app $DIST_APP"
  IDS="$(dist_fleet_ids "$LABEL" | awk '{print $1}')"
  if [ -z "$IDS" ]; then
    echo "  no machines with fleet=$LABEL"
  else
    for m in $IDS; do
      echo "  destroy machine $m"; flyctl machine destroy "$m" -a "$DIST_APP" --force 2>&1 | tail -1
    done
  fi
  VOL="dist_coord_${LABEL}"
  VIDS="$(flyctl volumes list -a "$DIST_APP" --json 2>/dev/null | python3 -c '
import sys, json
name = sys.argv[1]
try:
    vs = json.load(sys.stdin)
except Exception:
    vs = []
for v in vs:
    if v.get("name") == name:
        print(v.get("id"))
' "$VOL")"
  if [ -z "$VIDS" ]; then
    echo "  no volume named $VOL (already gone, or KEEP=1 was used at launch)"
  else
    for v in $VIDS; do
      echo "  destroy volume $v ($VOL)"; flyctl volume destroy "$v" -a "$DIST_APP" --yes 2>&1 | tail -1
    done
  fi ;;

help|*)
  sed -n '2,23p' "$0" ;;
esac
