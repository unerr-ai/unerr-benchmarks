#!/usr/bin/env bash
# On-fly orchestrator for the SINGLE-ARM econ full-resolve SWE-bench run.
#
# Runs INSIDE the fly machine (a real x86_64 VM). Boots its own Docker daemon
# (Docker-in-Docker) with the data-root on the attached volume, builds the econ
# toolbox image once, then:
#   1. run-benchmark.py  → preds.json + meta.jsonl (+ per-instance artifacts)
#   2. swebench harness  → grade preds.json → resolve report
#   3. report.py         → cost/token/per-tier summary from meta.jsonl + grade
# All durable output lands on /data (the fly volume) for the host to pull.
#
# SINGLE-ARM: econ runs ONCE per instance — no MODES loop, no on/off, no unerr
# install/daemon/MCP (unerr is compiled into econ).
#
# Env in (set by `flyctl machine run -e`):
#   LITELLM_API_KEY      required — econ routes its model tiers via the
#                         self-hosted LiteLLM gateway
#   EXA_API_KEY          optional — econ's websearch key; inherited straight into
#                         run-benchmark.py's env → passed to each instance container.
#                         Unset = web search disabled (baseline-comparable default;
#                         SWE-bench fixes are public, so web search = answer-lookup risk)
#   INSTANCES            cap to the first N Mini ids (default 1 = smoke)
#   HOLD                 seconds to hold open after finishing, for the SFTP pull
#   DATASET / SPLIT      HF dataset (default: SWE-bench Verified, Mini filtered)
set -uo pipefail

PY=/work/.venv/bin/python
DATA=/data
DOCKER_ROOT="$DATA/docker"
OUT="$DATA/results"
REPORTS="$DATA/reports"
LOGDIR="$DATA/logs"
mkdir -p "$DOCKER_ROOT" "$OUT" "$REPORTS" "$LOGDIR"

INSTANCES="${INSTANCES:-1}"
DATASET="${DATASET:-princeton-nlp/SWE-bench_Verified}"
SPLIT="${SPLIT:-test}"
LABEL="${LABEL:-econ}"
PARALLEL="${PARALLEL:-1}"                 # instances resolved concurrently (I/O-bound: model calls are remote)
# Grade with at least as many workers as we resolved, floor 4.
GRADE_WORKERS="${GRADE_WORKERS:-$(( PARALLEL > 4 ? PARALLEL : 4 ))}"

log() { printf '[fly-econ-fullresolve] %s\n' "$*" >&2; }
emit() { printf '{"ev":%s}\n' "$1"; }   # one-line JSON beacons the host scrapes

# BOOT_LIB: dockerd-boot + toolbox-build now lives in the shared
# e2e/distributed/lib/boot.sh (one copy, sourced by both this single-machine
# entrypoint and the distributed worker-entrypoint.sh). The fly image build
# CONTEXT for this entrypoint is e2e/econ (see run.sh) — e2e/distributed is
# NOT inside that context, so boot.sh cannot be sourced via a path relative
# to this file at runtime. The image-assembly step (Slice E) must COPY
# e2e/distributed/lib/boot.sh -> /work/lib/boot.sh into the toolbox image.
# Locally (running this script straight from a repo checkout, not inside the
# fly container) we fall back to the real repo-relative path.
BOOT_LIB="${BOOT_LIB:-/work/lib/boot.sh}"
[ -f "$BOOT_LIB" ] || BOOT_LIB="$(dirname "${BASH_SOURCE[0]}")/../../../distributed/lib/boot.sh"
# shellcheck source=../../../distributed/lib/boot.sh
source "$BOOT_LIB"

# ── 1. Boot Docker-in-Docker ────────────────────────────────────────────────
# Fly VMs are full Firecracker microVMs (real kernel), so dockerd runs natively
# in-VM — no privileged flag needed. Pin data-root to the volume; the rootfs is
# small and the SWE-bench env images are tens of GB.
boot_dockerd "$DOCKER_ROOT" "$LOGDIR"

# ── 2. Build the econ toolbox image once (grafted into every instance) ───────
build_toolbox /work/local-docker/Dockerfile.toolbox /work/local-docker/context \
    unerr-econ-toolbox "$LOGDIR"

# ── 2b. Parallel-safe stall watchdog ─────────────────────────────────────────
# econ has NO client-side request timeout, so a hung upstream model call freezes
# an instance until its ~30-min docker timeout — wasting a worker slot (and, under
# PARALLEL>1, one slot the whole time). This watchdog kills opencode ONLY in the
# stalled instance's OWN container (matched by its instance image tag), never the
# healthy siblings, so run-instance times out fast and the pool advances. A single
# all-containers kill (the old approach) is unsafe once instances run concurrently.
STALL_SECS="${STALL_SECS:-480}"           # kill an instance idle > this (no events written)
watchdog() {
  # the resolver starts AFTER the toolbox build, so wait for it to appear first
  for _ in $(seq 1 240); do pgrep -f run-benchmark.py >/dev/null 2>&1 && break; sleep 5; done
  log "watchdog: per-container, stall threshold ${STALL_SECS}s"
  while pgrep -f run-benchmark.py >/dev/null 2>&1; do
    for d in "$OUT"/*/artifacts/*/; do
      f="$d/events.jsonl"; [ -f "$f" ] || continue
      age=$(( $(date +%s) - $(stat -c %Y "$f") ))
      [ "$age" -gt "$STALL_SECS" ] || continue
      iid="$(basename "$d")"
      # run-benchmark.py tags each instance image unerr-econ-run:<iid __→_1776_, lowercased>
      img="unerr-econ-run:$(printf '%s' "$iid" | sed 's/__/_1776_/' | tr 'A-Z' 'a-z')"
      c="$(docker ps -q --filter ancestor="$img" | head -1)"
      if [ -n "$c" ]; then
        log "watchdog: $iid stale ${age}s -> killing opencode in ITS container ($c) only"
        docker exec "$c" pkill -KILL -f opencode 2>/dev/null || true
        sleep 20
      fi
    done
    sleep 60
  done
  log "watchdog: resolver exited — stopping"
}
watchdog >>"$LOGDIR/watchdog.log" 2>&1 &
WATCHDOG_PID=$!
log "watchdog launched (pid $WATCHDOG_PID)"

# ── 3. Resolve (single arm) ─────────────────────────────────────────────────
# /data persists across machines, so a prior run's results/meta linger under the
# same label — run-benchmark APPENDS to meta.jsonl and grading would re-grade
# stale instances. Start fresh unless RESUME=1.
if [ "${RESUME:-0}" != "1" ]; then
  log "fresh run: clearing prior $OUT and $REPORTS + stale bundle (set RESUME=1 to keep + resume)"
  rm -rf "${OUT:?}/"* "${REPORTS:?}/"* 2>/dev/null || true
  # A prior run's bundle.tgz lingers on the volume (results/reports are cleared, but
  # this wasn't) → a host-side "bundle exists" poll would false-trigger on the stale
  # one before this run finishes. Remove it so bundle.tgz only appears when WE write it.
  rm -f "$DATA/bundle.tgz" 2>/dev/null || true
else
  log "RESUME=1: keeping prior results/reports on the volume"
fi

# IDS (comma-separated instance_ids) targets specific instances for a re-run and
# overrides the first-N INSTANCES cap; otherwise cap to the first N Mini ids.
INST_ARG=()
if [ -n "${IDS:-}" ]; then
  INST_ARG=(--ids "$IDS")
elif [ "$INSTANCES" != "0" ]; then
  INST_ARG=(--instances "$INSTANCES")
fi

log "=== resolve label=$LABEL instances=$INSTANCES parallel=$PARALLEL ==="
emit "\"resolve_start\",\"instances\":\"$INSTANCES\",\"parallel\":$PARALLEL"
if DOCKER_DEFAULT_PLATFORM= "$PY" /work/local-docker/run-benchmark.py \
      --dataset "$DATASET" --split "$SPLIT" \
      --out "$OUT" --label "$LABEL" --parallel "$PARALLEL" "${INST_ARG[@]}" \
      >"$LOGDIR/resolve-$LABEL.log" 2>&1; then
  log "resolve: done"
else
  log "resolve: nonzero exit — tail:"; tail -30 "$LOGDIR/resolve-$LABEL.log" >&2
fi
emit '"resolve_done"'

# ── 4. Grade with the swebench harness (in a scratch CWD) ────────────────────
RUNDIR="$OUT/$LABEL"
PREDS="$RUNDIR/preds.json"
GRADE_REPORT=""
if [ -f "$PREDS" ]; then
  GDIR="$LOGDIR/grade-$LABEL"; mkdir -p "$GDIR"
  log "grading $LABEL"
  ( cd "$GDIR" && "$PY" -m swebench.harness.run_evaluation \
      --dataset_name "$DATASET" --split "$SPLIT" \
      --predictions_path "$PREDS" --run_id "$LABEL" \
      --max_workers "$GRADE_WORKERS" --cache_level env ) \
      >"$LOGDIR/grade-$LABEL.log" 2>&1 || log "grade $LABEL nonzero exit"
  # swebench writes <model_name_or_path>.<run_id>.json in CWD → collect it.
  find "$GDIR" -maxdepth 1 -name "*.${LABEL}.json" -exec cp {} "$REPORTS/" \; 2>/dev/null
  GRADE_REPORT="$(find "$REPORTS" -maxdepth 1 -name "*.${LABEL}.json" | head -1)"
  RESOLVED=$(jq -r '.resolved_instances // 0' "$GRADE_REPORT" 2>/dev/null | head -1)
  TOTAL=$(jq -r '.submitted_instances // .total_instances // 0' "$GRADE_REPORT" 2>/dev/null | head -1)
  log "grade $LABEL: resolved=${RESOLVED:-?}/${TOTAL:-?}"
  emit "\"grade_done\",\"run_id\":\"$LABEL\",\"resolved\":${RESOLVED:-0},\"total\":${TOTAL:-0}"
else
  log "no $PREDS — skipping grade"
  emit '"grade_done","run_id":"'"$LABEL"'","resolved":0,"total":0'
fi

# ── 5. Cost/tier report (report.py joins meta.jsonl + the grade report) ──────
log "aggregating cost report"
GR_ARG=()
[ -n "$GRADE_REPORT" ] && [ -f "$GRADE_REPORT" ] && GR_ARG=(--grade-report "$GRADE_REPORT")
if ! "$PY" /work/report.py --meta "$RUNDIR/meta.jsonl" "${GR_ARG[@]}" \
      --label "$LABEL" --out "$RUNDIR" >"$LOGDIR/report-$LABEL.log" 2>&1; then
  log "report.py nonzero exit — tail:"; tail -20 "$LOGDIR/report-$LABEL.log" >&2
fi

# Echo the headline into the log stream so the host has the answer even if the
# SFTP pull is flaky.
emit '"resolve_summary"'
[ -n "$GRADE_REPORT" ] && jq -c '{resolved:.resolved_instances, total:(.submitted_instances // .total_instances)}' "$GRADE_REPORT" 2>/dev/null
echo "----- COST REPORT (begin) -----"; cat "$RUNDIR/cost-report.md" 2>/dev/null; echo "----- COST REPORT (end) -----"

# ── 6. Bundle the durable output and HOLD so the host can SFTP it ────────────
# A `--restart no` machine stops the instant PID1 exits, and a stopped machine
# can't be SSH'd — so build the bundle, signal readiness, then sleep. The host
# pulls /data/bundle.tgz and then destroys us (which ends the sleep).
log "bundling /data → bundle.tgz"
tar czf "$DATA/bundle.tgz" -C "$DATA" results reports logs 2>/dev/null || true
BSZ=$(du -h "$DATA/bundle.tgz" 2>/dev/null | cut -f1)

# ── 7. Docker cleanup — reclaim the volume (data-root lives on /data) ────────
if [ "${CLEANUP:-1}" = "1" ]; then
  log "docker disk usage BEFORE cleanup:"; docker system df >&2 2>/dev/null || true
  VOLFLAG=""; [ "${CLEANUP_VOLUMES:-0}" = "1" ] && VOLFLAG="--volumes"
  log "pruning: docker system prune -af $VOLFLAG + builder cache"
  docker system prune -af $VOLFLAG >"$LOGDIR/cleanup.log" 2>&1 || true
  docker builder prune -af        >>"$LOGDIR/cleanup.log" 2>&1 || true
  RECLAIMED=$(grep -iE 'Total reclaimed space' "$LOGDIR/cleanup.log" | paste -sd'; ' -)
  log "cleanup: ${RECLAIMED:-done}"
  emit "\"cleanup_done\",\"detail\":\"$(echo "${RECLAIMED:-}" | tr -d '\"')\""
else
  log "CLEANUP=0 — leaving docker images/cache on the volume (faster re-runs)"
fi

log "ALL DONE — bundle.tgz=${BSZ:-?} ready on $DATA; holding ${HOLD:-1800}s for host pull"
emit "\"bundle_ready\",\"path\":\"/data/bundle.tgz\",\"size\":\"${BSZ:-?}\""
sync
sleep "${HOLD:-1800}"
exit 0
