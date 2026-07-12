#!/usr/bin/env bash
# On-fly orchestrator for the Claude full-resolve SWE-bench A/B (Docker-in-Docker).
#
# Runs INSIDE the fly machine (a real x86_64 VM). Boots its own Docker daemon
# (data-root on the attached /data volume), builds the unerr-claude toolbox image
# once, then:
#   1. run-benchmark.py  → predictions (preds_<mode>.json) + telemetry meta
#   2. swebench harness  → grade each mode SEQUENTIALLY → resolve report
#   3. jq                → echo per-instance telemetry beacons (tokens/$/turns/mcp)
# All durable output lands on /data (the fly volume) for the host to pull.
#
# CONFIGURATION: model PINNED via CLAUDE_MODEL (default opus = the user's real
# default), identical on both arms; no other tuning. No model LOOP.
# Auth is the SUBSCRIPTION token CLAUDE_CODE_OAUTH_TOKEN — NOT an API key.
# run-benchmark.py forwards it into every inner instance container.
#
# Env in (set by `flyctl machine run -e`):
#   CLAUDE_CODE_OAUTH_TOKEN  required — subscription token (`claude setup-token`)
#   MODES                    on | off | both  (default: both — the A/B)
#   INSTANCES                cap (default: 1 = smoke; 0 = all 50 Mini)
#   PACE                     seconds between instances (default: 30 when >1 inst)
#   DATASET / SPLIT          HF dataset (default: SWE-bench Verified, Mini-50)
#   LABEL                    output label (default: claude)
#   RESUME=1                 keep prior results on the volume instead of clearing
set -uo pipefail

PY=/work/.venv/bin/python
DATA=/data
DOCKER_ROOT="$DATA/docker"
OUT="$DATA/results"
REPORTS="$DATA/reports"
LOGDIR="$DATA/logs"
mkdir -p "$DOCKER_ROOT" "$OUT" "$REPORTS" "$LOGDIR"

MODES="${MODES:-both}"
INSTANCES="${INSTANCES:-1}"
DATASET="${DATASET:-princeton-nlp/SWE-bench_Verified}"
SPLIT="${SPLIT:-test}"
LABEL="${LABEL:-claude}"
PACE="${PACE:-}"
# Pinned model (default opus = the user's real default; the bare container would
# otherwise fall back to sonnet-4-6). Same model both arms → clean A/B.
CLAUDE_MODEL="${CLAUDE_MODEL:-opus}"

log()  { printf '[fly-claude-fullresolve] %s\n' "$*" >&2; }
emit() { printf '{"ev":%s}\n' "$1"; }   # one-line JSON beacons the host scrapes

: "${CLAUDE_CODE_OAUTH_TOKEN:?CLAUDE_CODE_OAUTH_TOKEN required (subscription token from: claude setup-token)}"

# ── 1. Boot Docker-in-Docker ────────────────────────────────────────────────
log "starting dockerd (data-root=$DOCKER_ROOT)"
dockerd --data-root="$DOCKER_ROOT" --storage-driver=overlay2 \
        >"$LOGDIR/dockerd.log" 2>&1 &
DOCKERD_PID=$!
for _ in $(seq 1 60); do
  docker info >/dev/null 2>&1 && break
  if ! kill -0 "$DOCKERD_PID" 2>/dev/null; then
    log "dockerd died on boot — tail:"; tail -40 "$LOGDIR/dockerd.log" >&2
    emit '"fatal","stage":"dockerd"'; exit 11
  fi
  sleep 2
done
if ! docker info >/dev/null 2>&1; then
  log "dockerd not ready after 120s"; tail -40 "$LOGDIR/dockerd.log" >&2
  emit '"fatal","stage":"dockerd-timeout"'; exit 11
fi
log "dockerd up: $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
emit '"dockerd_up"'

# ── 2. Build the unerr-claude toolbox image once (grafted into every instance) ─
log "building unerr-claude-toolbox from /work/context"
if docker build -f /work/Dockerfile.toolbox -t unerr-claude-toolbox /work/context \
     >"$LOGDIR/toolbox-build.log" 2>&1; then
  log "toolbox: built"; emit '"toolbox_built"'
else
  log "toolbox build FAILED — tail:"; tail -40 "$LOGDIR/toolbox-build.log" >&2
  emit '"fatal","stage":"toolbox"'; exit 12
fi

# ── 3. Fresh-run hygiene (the /data volume persists across machines) ─────────
RUNDIR="$OUT/$LABEL"
if [ "${RESUME:-0}" != "1" ]; then
  log "fresh run: clearing prior $OUT and $REPORTS (set RESUME=1 to keep + resume)"
  rm -rf "${OUT:?}/"* "${REPORTS:?}/"* 2>/dev/null || true
else
  log "RESUME=1: keeping prior results/reports on the volume"
fi
mkdir -p "$RUNDIR"

# ── 4. Resolve (default Claude config; writes preds + meta + artifacts) ──────
INST_ARG=(); [ "$INSTANCES" != "0" ] && INST_ARG=(--instances "$INSTANCES")
PACE_ARG=(); [ -n "$PACE" ] && PACE_ARG=(--pace "$PACE")
log "=== resolve: label=$LABEL modes=$MODES instances=$INSTANCES ==="
emit "\"resolve_start\",\"label\":\"$LABEL\",\"modes\":\"$MODES\""
# DOCKER_DEFAULT_PLATFORM unset — native amd64, no emulation wanted.
if DOCKER_DEFAULT_PLATFORM= "$PY" /work/run-benchmark.py \
      --mini --mode "$MODES" --claude-model "$CLAUDE_MODEL" \
      --dataset "$DATASET" --split "$SPLIT" \
      --out "$OUT" --label "$LABEL" "${INST_ARG[@]}" "${PACE_ARG[@]}" \
      >"$LOGDIR/resolve-$LABEL.log" 2>&1; then
  log "resolve: done"
else
  log "resolve: nonzero exit — tail:"; tail -40 "$LOGDIR/resolve-$LABEL.log" >&2
fi
emit "\"resolve_done\",\"label\":\"$LABEL\""

# ── 5. Grade each mode SEQUENTIALLY in its own scratch CWD ───────────────────
GRADE_MODES=("on" "off"); [ "$MODES" != "both" ] && GRADE_MODES=("$MODES")
for M in "${GRADE_MODES[@]}"; do
  PREDS="$RUNDIR/preds_${M}.json"
  [ -f "$PREDS" ] || { log "no $PREDS — skip grade"; continue; }
  RID="${LABEL}_${M}"
  GDIR="$LOGDIR/grade-$RID"; mkdir -p "$GDIR"
  log "grading $RID"
  ( cd "$GDIR" && "$PY" -m swebench.harness.run_evaluation \
      --dataset_name "$DATASET" --split "$SPLIT" \
      --predictions_path "$PREDS" --run_id "$RID" \
      --max_workers 4 --cache_level env ) \
      >"$LOGDIR/grade-$RID.log" 2>&1 || log "grade $RID nonzero exit"
  find "$GDIR" -maxdepth 1 -name "*.${RID}.json" -exec cp {} "$REPORTS/" \; 2>/dev/null
  RESOLVED=$(jq -r '.resolved_instances // 0' "$REPORTS"/*."$RID".json 2>/dev/null | head -1)
  log "grade $RID: resolved=${RESOLVED:-?}"
  emit "\"grade_done\",\"run_id\":\"$RID\",\"resolved\":${RESOLVED:-0}"
done

# ── 6. Telemetry summary (jq over meta_*.jsonl) ──────────────────────────────
log "telemetry summary"
emit '"telemetry_summary"'
for M in "${GRADE_MODES[@]}"; do
  MF="$RUNDIR/meta_${M}.jsonl"
  [ -f "$MF" ] || continue
  while IFS= read -r ROW; do
    [ -n "$ROW" ] && emit "\"telemetry\",\"row\":$ROW"
  done < <(jq -c '{mode,instance:.instance_id,model,rc,patch_bytes,unerrd_up,install_ok,turns:.telemetry.turns,in:.telemetry.in_tokens,cached:.telemetry.cached_in,out:.telemetry.out_tokens,usd:.telemetry.usd,mcp:.telemetry.mcp_tool_calls}' "$MF" 2>/dev/null)
done

# ── 7. Bundle the durable output and HOLD so the host can SFTP it ────────────
log "bundling /data → bundle.tgz"
tar czf "$DATA/bundle.tgz" -C "$DATA" results reports logs 2>/dev/null || true
BSZ=$(du -h "$DATA/bundle.tgz" 2>/dev/null | cut -f1)

# ── 8. Docker cleanup — reclaim the volume (data-root lives on /data) ────────
if [ "${CLEANUP:-1}" = "1" ]; then
  log "pruning docker images/build cache (CLEANUP=0 to skip)"
  docker system prune -af   >"$LOGDIR/cleanup.log" 2>&1 || true
  docker builder prune -af >>"$LOGDIR/cleanup.log" 2>&1 || true
  RECLAIMED=$(grep -iE 'Total reclaimed space' "$LOGDIR/cleanup.log" | paste -sd'; ' -)
  log "cleanup: ${RECLAIMED:-done}"; df -h /data >&2 2>/dev/null || true
fi

log "ALL DONE — bundle.tgz=${BSZ:-?} on $DATA; holding ${HOLD:-1800}s for host pull"
emit "\"bundle_ready\",\"path\":\"/data/bundle.tgz\",\"size\":\"${BSZ:-?}\""
sync
sleep "${HOLD:-1800}"
exit 0
