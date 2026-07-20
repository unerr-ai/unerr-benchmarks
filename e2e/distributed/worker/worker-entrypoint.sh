#!/usr/bin/env bash
# PID1 for a distributed SWE-bench WORKER machine (Slice C).
#
# Runs INSIDE a fly worker machine (shared-cpu-8x / 16 GB, NO volume, restart:no).
# Boots Docker-in-Docker with its data-root on the EPHEMERAL rootfs (workers have no
# volume — PLAN.md decision 2), builds the arm toolbox image once via the shared boot
# lib, then execs worker-loop.py which claim→resolve→grade→reports each instance and
# exits 0 when the coordinator's queue drains (the --restart no machine then stops,
# ending compute billing).
#
# The dockerd + toolbox-build logic lives ONCE in e2e/distributed/lib/boot.sh (Slice A)
# and is sourced here (the single-machine econ entrypoint sources the same lib).
#
# Env in (set by the launcher via `flyctl machine run -e`):
#   COORDINATOR_URL       required — http://<coord_id>.vm.<app>.internal:8080 (6PN)
#   RUN_ID                required — shared run/label id (== swebench --run_id)
#   WORKER_ID             optional — worker-loop.py defaults it to $FLY_MACHINE_ID
#   ARM                   optional — econ | claude-<mix> | claude-native | codex (default econ); picks runner + toolbox.
#                         claude-<mix> (gateway ensemble via LiteLLM, e.g. claude-gpt/claude-open) and
#                         claude-native (real Anthropic, CLAUDE_CODE_OAUTH_TOKEN, no LiteLLM) share
#                         claude's ARM_DIR/context below. Legacy claude/claude-real are aliased to
#                         claude-open/claude-native by the launcher before this runs.
#   DATASET / SPLIT       optional — HF dataset + split for resolve + grade
#   PER_INSTANCE_TIMEOUT  optional — per-instance seconds (default 2700)
#   GRADE_WORKERS         optional — swebench --max_workers (default 6)
#   LITELLM_API_KEY       required for econ — passed straight through to run-benchmark.py
#   EXA_API_KEY           optional — econ websearch key, passed through if set
#   BOOT_LIB              optional — override the boot-lib path (default /work/lib/boot.sh)
#   DOCKER_ROOT / LOGDIR  optional — dockerd data-root / log dir (ephemeral-rootfs defaults)
#
# ── Slice E (Dockerfile.dist) MUST COPY into the worker image, at these exact paths:
#     e2e/distributed/lib/boot.sh            → /work/lib/boot.sh
#     e2e/distributed/worker/worker-loop.py  → /work/distributed/worker/worker-loop.py
#     e2e/<arm>/local-docker/ (econ context) → /work/local-docker/
#         (run-benchmark.py, Dockerfile.toolbox, Dockerfile.instance, context/)
#   Do NOT edit run.sh/Dockerfile here — this entrypoint only assumes those paths.
set -uo pipefail

log()  { printf '[dist-worker] %s\n' "$*" >&2; }
emit() { printf '{"ev":%s}\n' "$1"; }   # one-line JSON beacons the host/coordinator scrape

ARM="${ARM:-econ}"

# Worker rootfs is ephemeral (NO volume) — dockerd data-root + logs live on rootfs,
# never /data (PLAN.md decision 2). The launcher enlarges rootfs (--rootfs-size 50,
# fly's max). boot.sh's ensure_overlay2_backing() then backs DOCKER_ROOT with a
# loopback ext4 image, because overlay2 can't stack on the overlayfs rootfs.
DOCKER_ROOT="${DOCKER_ROOT:-/var/lib/docker-dind}"
LOGDIR="${LOGDIR:-/var/log/dist-worker}"
mkdir -p "$DOCKER_ROOT" "$LOGDIR"

# ── Shared boot lib (Slice A contract) ───────────────────────────────────────
BOOT_LIB="${BOOT_LIB:-/work/lib/boot.sh}"
if [ ! -f "$BOOT_LIB" ]; then
  log "FATAL: boot lib not found at $BOOT_LIB (Slice E must COPY lib/boot.sh → /work/lib/)"
  emit '"fatal","stage":"boot-lib-missing"'
  exit 10
fi
# shellcheck source=/dev/null
source "$BOOT_LIB"

# ── 1. Docker-in-Docker (data-root on the ephemeral rootfs) ──────────────────
log "booting dockerd (data-root=$DOCKER_ROOT)"
boot_dockerd "$DOCKER_ROOT" "$LOGDIR"
emit '"dockerd_up"'

# ── 2. Build the arm toolbox image once (grafted into every instance) ────────
# Arm-parameterized tag. Econ's toolbox does NOT install unerr, so its tag drops
# the prefix: `econ-toolbox`, matching Dockerfile.instance's `COPY --from`. Every
# claude-<mix>/claude-native arm installs unerr and builds an IDENTICAL toolbox
# (the model mix is runtime env, not baked), so they ALL share one tag —
# unerr-claude-toolbox — with no per-mix rebuild. Codex keeps unerr-<arm>-toolbox.
if [ "$ARM" = "econ" ]; then
  TOOLBOX_TAG="${TOOLBOX_TAG:-econ-toolbox}"
else
  case "$ARM" in
    claude|claude-*) TOOLBOX_TAG="${TOOLBOX_TAG:-unerr-claude-toolbox}" ;;
    *)               TOOLBOX_TAG="${TOOLBOX_TAG:-unerr-${ARM}-toolbox}" ;;
  esac
fi
log "building toolbox $TOOLBOX_TAG (arm=$ARM)"
ARM_DIR="/work/local-docker"
# Every claude-<mix> gateway arm AND claude-native reuse the SAME claude
# context/runner (identical image) — only the auth/model env differs, wired at the
# launcher level (LiteLLM + tier aliases for gateway; CLAUDE_CODE_OAUTH_TOKEN for
# native), not here.
case "$ARM" in claude|claude-*) ARM_DIR="/work/claude/local-docker" ;; esac
build_toolbox "$ARM_DIR/Dockerfile.toolbox" "$ARM_DIR/context" "$TOOLBOX_TAG" "$LOGDIR"
emit "\"toolbox_built\",\"tag\":\"$TOOLBOX_TAG\""

# ── 3. Worker loop: claim → resolve → grade → report, until the queue drains ─
# worker-loop.py exits 0 on drain; exec so it is PID1 and receives signals directly.
log "starting worker-loop (coordinator=${COORDINATOR_URL:-UNSET} run_id=${RUN_ID:-UNSET} arm=$ARM)"
emit '"worker_loop_start"'
exec python3 /work/distributed/worker/worker-loop.py
