#!/usr/bin/env bash
# On-fly orchestrator for the full-resolve SWE-bench A/B.
#
# Runs INSIDE the fly machine (a real x86_64 VM). Boots its own Docker daemon
# (Docker-in-Docker) with the data-root on the attached volume, builds the
# unerr toolbox image once, then for each model:
#   1. run-benchmark.py  → predictions (preds_<mode>.json) + telemetry meta
#   2. swebench harness  → grade each mode SEQUENTIALLY (shared HF cache cannot
#      take concurrent harnesses in one CWD) → resolve report
#   3. report-runs.py    → cost/token summary from telemetry
# All durable output lands on /data (the fly volume) for the host to pull.
#
# Env in (set by `flyctl machine run -e`):
#   OPENAI_API_KEY   required — Codex auth
#   MODELS           space-separated codex model ids (default: mini + codex)
#   MODES            on | off | both  (default: on — bare numbers come from HAL)
#   INSTANCES        cap per model (default: 0 = all 50 Mini)
#   DATASET / SPLIT  HF dataset (default: SWE-bench Verified, Mini-50 filtered)
set -uo pipefail

PY=/work/.venv/bin/python
DATA=/data
DOCKER_ROOT="$DATA/docker"
OUT="$DATA/results"
REPORTS="$DATA/reports"
LOGDIR="$DATA/logs"
mkdir -p "$DOCKER_ROOT" "$OUT" "$REPORTS" "$LOGDIR"

MODELS="${MODELS:-gpt-5.4-mini gpt-5.3-codex}"
MODES="${MODES:-on}"
INSTANCES="${INSTANCES:-0}"
DATASET="${DATASET:-princeton-nlp/SWE-bench_Verified}"
SPLIT="${SPLIT:-test}"

log() { printf '[fly-fullresolve] %s\n' "$*" >&2; }
emit() { printf '{"ev":%s}\n' "$1"; }   # one-line JSON beacons the host scrapes from logs

label_of() { echo "$1" | tr '/.' '__'; }   # model id → filesystem-safe label

# ── 1. Boot Docker-in-Docker ────────────────────────────────────────────────
# Fly VMs are full Firecracker microVMs (real kernel), so dockerd runs natively
# in-VM — no privileged flag needed. Pin data-root to the volume; the rootfs is
# small and the SWE-bench env images are tens of GB.
log "starting dockerd (data-root=$DOCKER_ROOT)"
dockerd --data-root="$DOCKER_ROOT" --storage-driver=overlay2 \
        >"$LOGDIR/dockerd.log" 2>&1 &
DOCKERD_PID=$!

for i in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then break; fi
  if ! kill -0 "$DOCKERD_PID" 2>/dev/null; then
    log "dockerd died on boot — tail of dockerd.log:"; tail -40 "$LOGDIR/dockerd.log" >&2
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

# ── 2. Build the unerr toolbox image once (grafted into every instance) ──────
log "building unerr-codex-toolbox from /work/context"
if docker build -f /work/Dockerfile.toolbox -t unerr-codex-toolbox /work/context \
     >"$LOGDIR/toolbox-build.log" 2>&1; then
  log "toolbox: built"; emit '"toolbox_built"'
else
  log "toolbox build FAILED — tail:"; tail -40 "$LOGDIR/toolbox-build.log" >&2
  emit '"fatal","stage":"toolbox"'; exit 12
fi

# ── 3. Per-model: resolve → grade → cost ────────────────────────────────────
# The /data volume persists across machines, so prior results/reports linger
# under the same label — run-benchmark APPENDS to meta_*.jsonl and grading would
# re-grade stale instances, corrupting the final numbers. Start fresh unless
# RESUME=1 (the docker image cache on the volume is kept regardless).
if [ "${RESUME:-0}" != "1" ]; then
  log "fresh run: clearing prior $OUT and $REPORTS (set RESUME=1 to keep + resume)"
  rm -rf "${OUT:?}/"* "${REPORTS:?}/"* 2>/dev/null || true
else
  log "RESUME=1: keeping prior results/reports on the volume"
fi

INST_ARG=()
[ "$INSTANCES" != "0" ] && INST_ARG=(--instances "$INSTANCES")

for MODEL in $MODELS; do
  LBL="$(label_of "$MODEL")"
  RUNDIR="$OUT/$LBL"
  log "=== model=$MODEL label=$LBL modes=$MODES ==="
  emit "\"model_start\",\"model\":\"$MODEL\""

  # 3a. Resolve (writes preds_<mode>.json + meta_<mode>.jsonl + artifacts/).
  #     DOCKER_DEFAULT_PLATFORM unset — native amd64, no emulation wanted.
  if DOCKER_DEFAULT_PLATFORM= "$PY" /work/run-benchmark.py \
        --mini --mode "$MODES" --model "$MODEL" \
        --dataset "$DATASET" --split "$SPLIT" \
        --out "$OUT" --label "$LBL" "${INST_ARG[@]}" \
        >"$LOGDIR/resolve-$LBL.log" 2>&1; then
    log "resolve $MODEL: done"
  else
    log "resolve $MODEL: nonzero exit — tail:"; tail -30 "$LOGDIR/resolve-$LBL.log" >&2
  fi
  emit "\"resolve_done\",\"model\":\"$MODEL\""

  # 3b. Grade each mode SEQUENTIALLY in its own scratch CWD (the swebench HF
  #     cache cross-contaminates if two harnesses share a working dir).
  GRADE_MODES=("on" "off"); [ "$MODES" != "both" ] && GRADE_MODES=("$MODES")
  for M in "${GRADE_MODES[@]}"; do
    PREDS="$RUNDIR/preds_${M}.json"
    [ -f "$PREDS" ] || { log "no $PREDS — skip grade"; continue; }
    RID="${LBL}_${M}"
    GDIR="$LOGDIR/grade-$RID"; mkdir -p "$GDIR"
    log "grading $RID"
    ( cd "$GDIR" && "$PY" -m swebench.harness.run_evaluation \
        --dataset_name "$DATASET" --split "$SPLIT" \
        --predictions_path "$PREDS" --run_id "$RID" \
        --max_workers 4 --cache_level env ) \
        >"$LOGDIR/grade-$RID.log" 2>&1 || log "grade $RID nonzero exit"
    # swebench writes <model_name_or_path>.<run_id>.json in CWD — collect them all.
    find "$GDIR" -maxdepth 1 -name "*.${RID}.json" -exec cp {} "$REPORTS/" \; 2>/dev/null
    RESOLVED=$(jq -r '.resolved_instances // 0' "$REPORTS"/*."$RID".json 2>/dev/null | head -1)
    log "grade $RID: resolved=${RESOLVED:-?}"
    emit "\"grade_done\",\"run_id\":\"$RID\",\"resolved\":${RESOLVED:-0}"
  done
done

# ── 4. Cost/token summary across all run dirs ───────────────────────────────
log "aggregating cost report"
( cd "$OUT/.." && cp /work/report-runs.py . && \
  "$PY" report-runs.py results/* >"$DATA/cost-report.txt" 2>&1 ) || log "report-runs nonzero exit"
cp "$OUT"/*/cost-report.* "$DATA/" 2>/dev/null || true

# Echo the headline numbers into the log stream so the host has the answer even
# if the SFTP pull is flaky: per-run-id resolve counts + the whole cost report.
emit '"resolve_summary"'
for R in "$REPORTS"/*.json; do
  [ -f "$R" ] || continue
  jq -c '{report:input_filename, resolved:.resolved_instances, total:(.submitted_instances // .total_instances)}' "$R" 2>/dev/null
done
echo "----- COST REPORT (begin) -----"; cat "$DATA/cost-report.txt" 2>/dev/null; echo "----- COST REPORT (end) -----"

# ── 5. Bundle the durable output and HOLD so the host can SFTP it ────────────
# A `--restart no` machine stops the instant PID1 exits, and a stopped machine
# can't be SSH'd — so build the bundle, signal readiness, then sleep. The host
# pulls /data/bundle.tgz and then destroys us (which ends the sleep).
log "bundling /data → bundle.tgz"
tar czf "$DATA/bundle.tgz" -C "$DATA" results reports logs cost-report.txt 2>/dev/null || \
  tar czf "$DATA/bundle.tgz" -C "$DATA" results reports logs 2>/dev/null || true
BSZ=$(du -h "$DATA/bundle.tgz" 2>/dev/null | cut -f1)

# ── 6. Docker cleanup — reclaim the volume (data-root lives on /data) ────────
# The /data volume persists across runs, so the SWE-bench env images, the
# derived per-instance images, stopped grading containers and build cache would
# pile up tens of GB each run. After grading nothing references them, but they
# are TAGGED, so only `prune -a` reclaims them (plain prune drops dangling only).
# CLEANUP=0 to skip; CLEANUP_VOLUMES=1 to also drop docker volumes.
if [ "${CLEANUP:-1}" = "1" ]; then
  log "docker disk usage BEFORE cleanup:"; docker system df >&2 2>/dev/null || true
  VOLFLAG=""; [ "${CLEANUP_VOLUMES:-0}" = "1" ] && VOLFLAG="--volumes"
  log "pruning: docker system prune -af $VOLFLAG  + builder cache"
  docker system prune -af $VOLFLAG >"$LOGDIR/cleanup.log" 2>&1 || true
  docker builder prune -af       >>"$LOGDIR/cleanup.log" 2>&1 || true
  # cleanup.log has TWO "Total reclaimed space" lines (system prune, then builder
  # prune) — join both so the system-prune total isn't hidden by the builder's 0B.
  RECLAIMED=$(grep -iE 'Total reclaimed space' "$LOGDIR/cleanup.log" | paste -sd'; ' -)
  log "cleanup: ${RECLAIMED:-done}"
  log "docker disk usage AFTER cleanup:"; docker system df >&2 2>/dev/null || true
  log "volume free space:"; df -h /data >&2 2>/dev/null || true
  emit "\"cleanup_done\",\"detail\":\"$(echo "${RECLAIMED:-}" | tr -d '\"')\""
else
  log "CLEANUP=0 — leaving docker images/cache on the volume (faster re-runs)"
fi

log "ALL DONE — bundle.tgz=${BSZ:-?} ready on $DATA; holding ${HOLD:-1800}s for host pull"
emit "\"bundle_ready\",\"path\":\"/data/bundle.tgz\",\"size\":\"${BSZ:-?}\""
sync
sleep "${HOLD:-1800}"
exit 0
