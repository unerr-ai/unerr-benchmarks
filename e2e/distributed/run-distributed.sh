#!/usr/bin/env bash
# Distributed SWE-bench runner — HOST LAUNCHER.
#
# Sibling of e2e/econ/fly-remote/fullresolve/run.sh. Where run.sh drives ONE
# DinD machine serially, this fans the work across a work-stealing FLEET: one
# small COORDINATOR machine (SQLite queue) + N worker machines (16GB/8cpu, no
# volume) that claim → resolve → grade → report one instance at a time and
# self-stop when the queue drains. The host builds the image, seeds the queue,
# paces out the workers, polls the coordinator, pulls one merged bundle, and
# tears the fleet down by metadata.
#
# It REUSES run.sh's helpers verbatim where possible: fly-token read, app/volume
# ensure, the econ-binary vendor step, and the `flyctl deploy --build-only
# --remote-only --push` remote build (+ IMAGE= reuse + MANIFEST_UNKNOWN retry).
# resolve/grade themselves are never reimplemented — the worker shells out to the
# SAME e2e/<arm>/local-docker/run-benchmark.py + swebench harness the image bakes.
#
# Usage:
#   MACHINES=2 ARM=econ LABEL=dist-smoke TASKS="django__django-11880,django__django-11951,django__django-11790" ./run-distributed.sh
#   MACHINES=5 ARM=econ LABEL=mini SUITE=mini ./run-distributed.sh
#   MACHINES=25 ARM=econ LABEL=full-verified ./run-distributed.sh          # SUITE defaults to full Verified
#   MACHINES=25 ARM=econ LABEL=full-ded DEDICATED_CONDUCTOR=1 ./run-distributed.sh  # ephemeral dedicated-GPU conductor (see README §2); ~$80/hr while up
#   DESTROY_ONLY=1 LABEL=dist-smoke ./run-distributed.sh                   # just tear a fleet down
#
#   # prepare/run split — warm the fleet (build the ~10-min toolbox) BEFORE raising
#   # the GPU, so the $80/hr conductor never idles during warmup:
#   MACHINES=5 ARM=claude LABEL=mini SUITE=mini ./run-distributed.sh prepare  # build + warm + HOLD at the gate (no work, no GPU)
#   LABEL=mini ARM=claude DEDICATED_CONDUCTOR=1 ./run-distributed.sh run      # raise GPU → arm → poll → bundle → teardown
#   LABEL=mini ./run-distributed.sh arm                                       # just flip the gate (fleet claims; poll/bundle later with `run`)
#
# Prereqs: flyctl logged in (token auto-read from ~/.fly/config.yml); econ built
#          locally (see RUNBOOK §1); LITELLM_API_KEY exported or in e2e/econ/.env.local;
#          python3 on PATH (+ `datasets` only if SUITE=full/verified/lite).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"                               # e2e/distributed
E2E_DIR="$(cd "$HERE/.." && pwd)"                                   # e2e — the docker build context
ECON_DIR="$E2E_DIR/econ"                                            # arm pipeline + .env.local
ECON_REPO="${ECON_REPO:-$HERE/../../../econ-coding-agent}"         # sibling of this benchmark repo
cd "$HERE"

# ── config ────────────────────────────────────────────────────────────────────
ORG="${FLY_ORG:-your-fly-org}"               # fly org; override via FLY_ORG
REGION="${REGION:-iad}"
ARM="${ARM:-econ}"
# App default is arm-aware (ARM must be known first, above): econ keeps the
# original fixed app name byte-for-byte; claude gets its own app so the two
# arms' fleets never collide.
DEFAULT_APP="swebench-agent-dist"
[ "$ARM" = "claude" ] && DEFAULT_APP="swebench-agent-dist-claude"
APP="${APP:-$DEFAULT_APP}"                  # FIXED app; each run scoped by fleet=<LABEL> metadata
LABEL="${LABEL:-}"                          # REQUIRED, unique per run — doubles as RUN_ID + fleet metadata
MACHINES="${MACHINES:-}"                    # REQUIRED (unless DESTROY_ONLY) — worker count N
DESTROY_ONLY="${DESTROY_ONLY:-0}"           # 1 = only tear down the fleet named by LABEL, then exit
IMAGE="${IMAGE:-}"                          # reuse a prior built image ref (skip the remote build)
CAMPAIGN="${CAMPAIGN:-}"                    # pins one image across all tranches of a multi-run campaign

# Dedicated conductor (OPT-IN, default off): when 1, bring up an ephemeral Fireworks
# dedicated-GPU deployment for the conductor tier for THIS run and flip the shared
# econ-litellm gateway to it (via a fly secret the gateway's econ-entrypoint.sh reads),
# then tear both down at teardown. Escapes serverless rate-limiting (429/503 above ~2
# parallel). Costs ~$80/hr while up (8x B200) — billed only during the run. One-time:
# deploy the gateway with infra/litellm/econ-entrypoint.sh before the first flagged run.
DEDICATED_CONDUCTOR="${DEDICATED_CONDUCTOR:-0}"
GATEWAY_APP="${GATEWAY_APP:-econ-litellm}"
GATEWAY_HEALTH_URL="${GATEWAY_HEALTH_URL:-https://$GATEWAY_APP.fly.dev/health/liveliness}"
CONDUCTOR_SECRET="${CONDUCTOR_SECRET:-CONDUCTOR_DEPLOYMENT_PATH}"

# worker sizing (shared-cpu-8x / 16GB — SWE-bench env images + DinD need the room)
MEM="${MEM:-16384}"
CPUS="${CPUS:-8}"
CPU_KIND="${CPU_KIND:-shared}"              # 'shared' (default) or 'performance' — dedicated cores; A/B knob for the CPU-starvation → stuck-escalation hypothesis
ROOTFS_GB="${ROOTFS_GB:-}"                  # OPTIONAL ephemeral-rootfs size (see NOTE at worker create)
# fly hard-caps config.rootfs.size_gb at 50 (0/unset = image default). Clamp so a
# too-large value degrades to the max instead of failing every worker create.
if [ -n "$ROOTFS_GB" ] && [ "$ROOTFS_GB" -gt 50 ] 2>/dev/null; then
  echo "==> ROOTFS_GB=$ROOTFS_GB exceeds fly's 50 GB max — clamping to 50" >&2
  ROOTFS_GB=50
fi

# coordinator sizing (tiny — it only runs SQLite + aiohttp)
COORD_SIZE="${COORD_SIZE:-shared-cpu-1x}"
COORD_MEM="${COORD_MEM:-1024}"
# fly volume names allow only lowercase alphanumeric + underscores, ≤30 chars —
# LABEL may contain hyphens/uppercase, so sanitize it for the volume name only
# (metadata fleet=<LABEL> keeps the raw label).
_VOL_SLUG="$(printf 'dist_coord_%s' "$LABEL" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9_' '_' | cut -c1-30)"
COORD_VOL="${COORD_VOL:-$_VOL_SLUG}"            # the fleet's ONE small volume
COORD_VOL_GB="${COORD_VOL_GB:-10}"

# task set + grading
SUITE="${SUITE:-}"
TASKS="${TASKS:-}"
TASKS_FILE="${TASKS_FILE:-}"
SPLIT="${SPLIT:-test}"
# Grade against the dataset the suite came from unless DATASET is pinned.
if [ -z "${DATASET:-}" ]; then
  case "$(printf '%s' "${SUITE:-}" | tr 'A-Z' 'a-z')" in
    lite) DATASET="princeton-nlp/SWE-bench_Lite" ;;
    *)    DATASET="princeton-nlp/SWE-bench_Verified" ;;
  esac
fi

HOLD="${HOLD:-3600}"                        # coordinator holds open this long after bundle for the pull
MAXWAIT="${MAXWAIT:-172800}"                # host poll ceiling (48h — covers long/large runs; the fleet self-stops on drain regardless of this)
PER_INSTANCE_TIMEOUT="${PER_INSTANCE_TIMEOUT:-10800}"  # per-task resolve ceiling (3h normal; stall watchdog handles hangs — STALL_KILL_S)
GRADE_WORKERS="${GRADE_WORKERS:-6}"         # swebench harness --max_workers on the worker
STALL_KILL_S="${STALL_KILL_S:-2700}"        # seconds of zero log growth before a resolve is killed for restart
HEARTBEAT_TIMEOUT="${HEARTBEAT_TIMEOUT:-300}"          # coordinator reaps a lease after this much heartbeat silence (10 beats @30s)
KEEP="${KEEP:-0}"                           # 1 = keep the coordinator volume on teardown
PY_HOST="${PYTHON:-python3}"

log()  { printf '[dist] %s\n' "$*" >&2; }

# ── auth: fly token — prefer env, else the saved token (verbatim from run.sh) ──
if [ -z "${FLY_API_TOKEN:-}" ]; then
  FLY_API_TOKEN="$(node -e "const fs=require('fs');const y=fs.readFileSync(process.env.HOME+'/.fly/config.yml','utf8');const m=y.match(/access_token:\s*(\S+)/);process.stdout.write(m?m[1]:'')")"
fi
export FLY_API_TOKEN
[ -n "$FLY_API_TOKEN" ] || { echo "no fly token (run: flyctl auth login)"; exit 1; }

# ── fleet helpers (metadata-scoped) ───────────────────────────────────────────
# Machine ids for this fleet (optionally filtered by role) via the JSON list —
# metadata is not shown in the table view. python3 (already a prereq) avoids a
# host jq dependency.
fleet_ids() {                               # fleet_ids [role]
  flyctl machines list -a "$APP" --json 2>/dev/null | "$PY_HOST" -c '
import sys, json
role  = sys.argv[1]
label = sys.argv[2]
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
    print(m.get("id"))
' "${1:-}" "$LABEL"
}

destroy_fleet() {
  log "tearing down fleet '$LABEL' (metadata fleet=$LABEL) on app $APP"
  local ids; ids="$(fleet_ids || true)"
  if [ -z "$ids" ]; then
    log "  no machines with fleet=$LABEL"
  else
    for m in $ids; do
      log "  destroy machine $m"
      flyctl machine destroy "$m" -a "$APP" --force 2>&1 | tail -1 || true
    done
  fi
  if [ "$KEEP" = "1" ]; then
    log "  KEEP=1 — leaving coordinator volume $COORD_VOL in place"
  else
    local vids
    vids="$(flyctl volumes list -a "$APP" --json 2>/dev/null | "$PY_HOST" -c '
import sys, json
name = sys.argv[1]
try:
    vs = json.load(sys.stdin)
except Exception:
    vs = []
for v in vs:
    if v.get("name") == name:
        print(v.get("id"))
' "$COORD_VOL")"
    for v in $vids; do
      log "  destroy volume $v ($COORD_VOL)"
      flyctl volume destroy "$v" -a "$APP" --yes 2>&1 | tail -1 || true
    done
  fi
}

# ── dedicated-conductor lifecycle (only acts when DEDICATED_CONDUCTOR=1) ──────
# cleanup_dedicated is idempotent + trap-driven so a mid-run abort can never orphan
# the ~$80/hr Fireworks deployment: it unsets the gateway secret (reverting the
# conductor to serverless) and deletes the deployment (stops billing).
FW="$HERE/fireworks-conductor.sh"
CLEANED=0
cleanup_dedicated() {
  [ "$DEDICATED_CONDUCTOR" = "1" ] || return 0
  [ "$CLEANED" = "1" ] && return 0
  CLEANED=1
  log "dedicated conductor: reverting gateway secret + deleting deployment (idempotent)"
  flyctl secrets unset "$CONDUCTOR_SECRET" -a "$GATEWAY_APP" 2>&1 | tail -1 || true
  bash "$FW" down || true
}
# best-effort routing proof: does the gateway now advertise the dedicated path? A
# NO means the gateway image lacks the flip wrapper → you'd pay for an unused GPU.
verify_gateway_flip() {                     # verify_gateway_flip <dedicated-model-path>
  local dep_id info p
  dep_id="${1##*/deployments/}"
  for p in /v1/model/info /model/info; do
    info="$(curl -s --max-time 10 -H "Authorization: Bearer $LITELLM_API_KEY" "https://$GATEWAY_APP.fly.dev$p" 2>/dev/null || true)"
    printf '%s' "$info" | grep -q "deployments/$dep_id" && return 0
  done
  return 1
}

# ── dedicated-conductor bring-up (extracted so both all-in-one and `run` mode
#    raise the GPU + flip the gateway from one place). ──────────────────────────
bring_up_dedicated() {
  echo "==> DEDICATED_CONDUCTOR=1 — bringing up ephemeral Fireworks conductor deployment (~\$80/hr while up)"
  trap 'cleanup_dedicated; exit 130' INT
  trap 'cleanup_dedicated; exit 143' TERM
  trap 'cleanup_dedicated' EXIT
  bash "$FW" up || { echo "dedicated conductor: deployment did not come up — aborting"; destroy_fleet; exit 1; }
  DPATH="$(bash "$FW" print-path)"
  echo "==> flipping gateway $GATEWAY_APP conductor -> dedicated (secret $CONDUCTOR_SECRET); gateway restarts"
  flyctl secrets set "$CONDUCTOR_SECRET=$DPATH" -a "$GATEWAY_APP" >/dev/null \
    || { echo "failed to set gateway secret — aborting"; destroy_fleet; exit 1; }
  echo "==> waiting for gateway health after restart ($GATEWAY_HEALTH_URL)"
  gw_ok=0; code=""
  for _ in $(seq 1 40); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$GATEWAY_HEALTH_URL" 2>/dev/null || true)"
    [ "$code" = "200" ] && { gw_ok=1; echo "    gateway healthy"; break; }
    sleep 6
  done
  [ "$gw_ok" = "1" ] || echo "    WARN: gateway health not confirmed (last code=${code:-none}) — proceeding"
  if verify_gateway_flip "$DPATH"; then
    echo "    verified: gateway conductor now routes to the dedicated deployment"
  else
    echo "    WARN: could not confirm conductor routing to the dedicated deployment —"
    echo "          is the gateway built with infra/litellm/econ-entrypoint.sh? deploy it once, else you pay for an unused GPU"
  fi
}

# ── /status probe (curl INSIDE the coordinator via ssh — host is off 6PN); used
#    by both the prepare readiness-wait and the main drain poll. ────────────────
poll_status() {
  flyctl ssh console -a "$APP" --machine "$COORD_MID" -C "curl -s localhost:8080/status" 2>/dev/null \
    | grep -vE 'Connecting|Waiting|Connected|already'
}

# ── mode dispatch: prepare (warm+hold) / run (arm a prepared fleet + poll) /
#    arm (just flip the gate) / destroy | default = all-in-one (unchanged). ──────
MODE="all"
case "${1:-}" in
  prepare|run|arm) MODE="$1"; shift ;;
  destroy)         DESTROY_ONLY=1; shift ;;
esac
# COORD_ARMED=0 only in prepare → server holds /claim at {wait} so workers warm
# and idle; every other mode boots armed (byte-identical all-in-one behaviour).
COORD_ARMED_VAL=1; [ "$MODE" = "prepare" ] && COORD_ARMED_VAL=0

# ── DESTROY_ONLY: nuke the fleet named by LABEL and exit ──────────────────────
if [ "$DESTROY_ONLY" = "1" ]; then
  [ -n "$LABEL" ] || { echo "DESTROY_ONLY=1 needs LABEL=<fleet>"; exit 1; }
  destroy_fleet
  log "destroy-only done."
  exit 0
fi

# ── required args ─────────────────────────────────────────────────────────────
[ -n "$LABEL" ]    || { echo "set LABEL=<unique-run-id> (names the fleet metadata + coordinator RUN_ID)"; exit 1; }
# run/arm attach to an already-created fleet, so MACHINES is only required when
# building one (prepare / all-in-one).
if [ "$MODE" != "run" ] && [ "$MODE" != "arm" ]; then
  [ -n "$MACHINES" ] || { echo "set MACHINES=<N> (number of worker machines)"; exit 1; }
  case "$MACHINES" in (*[!0-9]*|'') echo "MACHINES must be a positive integer"; exit 1;; esac
  [ "$MACHINES" -ge 1 ] || { echo "MACHINES must be >= 1"; exit 1; }
fi

OUTDIR="$HERE/out/dist-$LABEL"
mkdir -p "$OUTDIR"

# ── mode branch: run/arm ATTACH to an already-prepared fleet; prepare/all BUILD it
if [ "$MODE" = "run" ] || [ "$MODE" = "arm" ]; then
  COORD_MID="$(fleet_ids coordinator | head -1)"
  [ -n "$COORD_MID" ] || { echo "no coordinator found for fleet '$LABEL' — run 'prepare' first"; exit 1; }
  COORD_URL="http://${COORD_MID}.vm.${APP}.internal:8080"
  echo "==> attaching to prepared fleet '$LABEL' — coordinator $COORD_MID  ($COORD_URL)"
  # Raise the GPU now (run only) — kept out of `prepare` so it never idles during warmup.
  if [ "$MODE" = "run" ] && [ "$DEDICATED_CONDUCTOR" = "1" ]; then bring_up_dedicated; fi
  echo "==> arming fleet (POST /arm on the coordinator via ssh)"
  flyctl ssh console -a "$APP" --machine "$COORD_MID" -C "curl -s -X POST localhost:8080/arm" 2>/dev/null \
    | grep -vE 'Connecting|Waiting|Connected|already' || true
  if [ "$MODE" = "arm" ]; then
    echo "==> armed. Fleet '$LABEL' is now claiming; poll/bundle later with: LABEL=$LABEL ./run-distributed.sh run"
    exit 0
  fi
  echo "==> armed — proceeding to poll/bundle/teardown"
else
# ═══════════ prepare / all-in-one: build the image + create the fleet ═════════

# ── campaign image pin (locks one image across all tranches of a multi-run
# campaign) — skipped entirely when CAMPAIGN is unset (DESTROY_ONLY already
# exited above). The first tranche writes the lock once IMG is resolved
# (below, after the build/reuse step); every later tranche in the same
# campaign must resolve to the identical image — a stray IMAGE or a fresh
# build must not sneak into a pinned campaign, so a mismatch here is fatal.
CAMPAIGN_LOCK="$HERE/out/campaign-$CAMPAIGN.json"
if [ -n "$CAMPAIGN" ] && [ -f "$CAMPAIGN_LOCK" ]; then
  LOCKED_IMAGE="$("$PY_HOST" -c '
import json, sys
with open(sys.argv[1]) as f:
    print(json.load(f).get("image", ""))
' "$CAMPAIGN_LOCK")"
  [ -n "$LOCKED_IMAGE" ] || { echo "campaign lock $CAMPAIGN_LOCK has no image field — remove it and re-run"; exit 1; }
  if [ -z "$IMAGE" ]; then
    IMAGE="$LOCKED_IMAGE"
    echo "==> campaign '$CAMPAIGN': pinned image $IMAGE (from lock)"
  elif [ "$IMAGE" = "$LOCKED_IMAGE" ]; then
    echo "==> campaign '$CAMPAIGN': IMAGE matches lock ($IMAGE)"
  else
    echo "==> campaign '$CAMPAIGN': IMAGE=$IMAGE conflicts with locked image $LOCKED_IMAGE — refusing to run" >&2
    echo "    a stray IMAGE or a fresh build must not sneak into a pinned campaign" >&2
    exit 1
  fi
fi

# ── auth: LiteLLM gateway key (never printed) — prefer env, else e2e/econ/.env.local (from run.sh) ──
if [ -z "${LITELLM_API_KEY:-}" ] && [ -f "$ECON_DIR/.env.local" ]; then
  LITELLM_API_KEY="$(grep -E '^LITELLM_API_KEY=' "$ECON_DIR/.env.local" | head -1 | sed 's/^LITELLM_API_KEY=//; s/^["'"'"']//; s/["'"'"']$//')"
fi
[ -n "${LITELLM_API_KEY:-}" ] || { echo "set LITELLM_API_KEY (or add it to e2e/econ/.env.local)"; exit 1; }
export LITELLM_API_KEY
echo "==> LITELLM_API_KEY: set (len ${#LITELLM_API_KEY})"

# ── auth: LiteLLM MASTER key (claude arm only) — the econ LITELLM_API_KEY above
# may be a non-master/placeholder; claude mints a per-instance virtual key on
# each worker and needs a mint-capable master key to do it. Prefer env, else
# the sibling econ-coding-agent infra .env.local (never econ's .env.local).
if [ "$ARM" = "claude" ]; then
  if [ -z "${LITELLM_MASTER_KEY:-}" ]; then
    _INFRA_ENV="$HERE/../../../econ-coding-agent/infra/litellm/.env.local"
    if [ -f "$_INFRA_ENV" ]; then
      LITELLM_MASTER_KEY="$(grep -E '^LITELLM_MASTER_KEY=' "$_INFRA_ENV" | head -1 | sed 's/^LITELLM_MASTER_KEY=//; s/^["'"'"']//; s/["'"'"']$//')"
    fi
  fi
  [ -n "${LITELLM_MASTER_KEY:-}" ] || { echo "set LITELLM_MASTER_KEY (or add it to ../econ-coding-agent/infra/litellm/.env.local) — claude arm needs it to mint per-instance keys"; exit 1; }
  export LITELLM_MASTER_KEY
  echo "==> LITELLM_MASTER_KEY: set (len ${#LITELLM_MASTER_KEY})"
fi

# EXA web-search key — web search OFF by default for a clean, baseline-comparable
# run (SWE-bench fixes are public → web search = answer-lookup). STRICTLY opt-in:
# only WEBSEARCH=1 enables it. An ambient EXA_API_KEY in the shell env is IGNORED
# (matches the single-machine runbook's `env -u EXA_API_KEY WEBSEARCH=0` invariant).
if [ "${WEBSEARCH:-0}" = "1" ]; then
  if [ -z "${EXA_API_KEY:-}" ] && [ -f "$ECON_DIR/.env.local" ]; then
    EXA_API_KEY="$(grep -E '^EXA_API_KEY=' "$ECON_DIR/.env.local" | head -1 | sed 's/^EXA_API_KEY=//; s/^["'"'"']//; s/["'"'"']$//')"
  fi
  export EXA_API_KEY="${EXA_API_KEY:-}"
else
  export EXA_API_KEY=""   # ignore any ambient key unless WEBSEARCH=1
fi
[ -n "$EXA_API_KEY" ] \
  && echo "==> EXA_API_KEY: set (len ${#EXA_API_KEY}) — web search ENABLED (NOT baseline-comparable)" \
  || echo "==> EXA_API_KEY: unset — web search disabled (clean, baseline-comparable run)"

# ── vendor the LOCAL econ build into the TOOLBOX build context (from run.sh) ───
# The fleet image bakes the arm toolbox, so the vendored glibc binary must be in
# econ/local-docker/context/vendor/ before the remote build (as run.sh does).
if [ "$ARM" = "econ" ]; then
  VENDOR="$ECON_DIR/local-docker/context/vendor"
  echo "==> vendoring local econ build from $ECON_REPO -> $VENDOR"
  mkdir -p "$VENDOR/dot-opencode"
  # Rebuild the binary from LIVE source every run. .opencode/opencode.json/
  # code-intelligence below are copied live each run (they self-heal); the binary
  # is the ONE econ input that is copied-not-rebuilt, so a stale dist/ would
  # silently ship an old engine to the fleet (the fleet-vs-local drift footgun).
  # SKIP_ECON_BUILD=1 reuses the existing dist/ (iterating on the runner, or a
  # binary already built this session).
  if [ "${SKIP_ECON_BUILD:-0}" = "1" ]; then
    echo "    build: SKIP_ECON_BUILD=1 — reusing existing dist/ (may be stale)"
  else
    echo "    build: rebuilding econ from live $ECON_REPO (HEAD $(git -C "$ECON_REPO" rev-parse --short HEAD 2>/dev/null || echo '?'))"
    ( cd "$ECON_REPO" && bun install && bun run --cwd packages/opencode build ) >"$OUTDIR/econ-build.log" 2>&1 \
      || { echo "    econ build FAILED — see $OUTDIR/econ-build.log (or pass SKIP_ECON_BUILD=1 to reuse dist/)"; tail -20 "$OUTDIR/econ-build.log"; exit 1; }
  fi
  # GLIBC binary (NOT -musl): the SWE-bench instance images are Debian/glibc.
  BIN="$(find "$ECON_REPO/packages/opencode/dist" -type f -name opencode -path '*/opencode-linux-x64-baseline/bin/opencode' 2>/dev/null | head -1)"
  [ -n "$BIN" ] || { echo "no local linux-x64-baseline (glibc) econ build under $ECON_REPO/packages/opencode/dist (run: cd $ECON_REPO && bun install && bun run --cwd packages/opencode build)"; exit 1; }
  cp "$BIN" "$VENDOR/opencode"; chmod +x "$VENDOR/opencode"
  echo "    binary: $BIN -> vendor/opencode ($(du -h "$VENDOR/opencode" | cut -f1))"
  [ -f "$ECON_REPO/opencode.json" ] || { echo "no opencode.json at $ECON_REPO/opencode.json"; exit 1; }
  cp "$ECON_REPO/opencode.json" "$VENDOR/opencode.json"
  echo "    config: $ECON_REPO/opencode.json -> vendor/opencode.json"
  rm -rf "${VENDOR:?}/dot-opencode"; mkdir -p "$VENDOR/dot-opencode"
  if [ -d "$ECON_REPO/.opencode" ]; then
    cp -R "$ECON_REPO/.opencode/." "$VENDOR/dot-opencode/"
    echo "    plugins: $ECON_REPO/.opencode -> vendor/dot-opencode/"
    # Prune econ's GitHub project tools: .opencode/tool/github-*.ts import
    # @opencode-ai/plugin and are useless in an OFFLINE SWE-bench resolve. The
    # .dockerignore fix ships the dep so they COULD load; dropping them here means
    # opencode never even tries — leaner image + one fewer way to crash session
    # startup (a single unloadable tool aborts econ with exit=1, 0 turns).
    rm -f "$VENDOR"/dot-opencode/tool/github-*.ts
    echo "    pruned dot-opencode/tool/github-*.ts (unused offline; belt-and-suspenders w/ .dockerignore fix)"
  else
    echo "    plugins: none at $ECON_REPO/.opencode — vendor/dot-opencode/ left empty"
  fi
  CI_SRC="$ECON_REPO/packages/code-intelligence"
  if [ -d "$CI_SRC/src" ]; then
    CI_DST="$VENDOR/dot-opencode/vendor/code-intelligence"
    rm -rf "$CI_DST"; mkdir -p "$CI_DST"
    cp -R "$CI_SRC/src" "$CI_DST/src"
    cp "$CI_SRC/package.json" "$CI_DST/package.json" 2>/dev/null || true
    echo "    engine: $CI_SRC/{src,package.json} -> vendor/dot-opencode/vendor/code-intelligence/"
  else
    echo "    engine: none at $CI_SRC/src — in-container plugins fall back and stay disabled"
  fi
elif [ "$ARM" = "claude" ]; then
  log "arm=claude: no vendor step"
fi

# ── resolve the task set → INSTANCE_IDS (comma-separated) ─────────────────────
echo "==> resolving task set (suite=${SUITE:-full} tasks=${TASKS:+set} file=${TASKS_FILE:-none})"
INSTANCE_IDS="$("$PY_HOST" "$HERE/tools/suite.py" \
  ${SUITE:+--suite "$SUITE"} ${TASKS:+--tasks "$TASKS"} ${TASKS_FILE:+--file "$TASKS_FILE"} \
  --dataset "$DATASET" --split "$SPLIT")"
[ -n "$INSTANCE_IDS" ] || { echo "suite.py resolved 0 ids"; exit 1; }
NUM_IDS="$(printf '%s' "$INSTANCE_IDS" | tr ',' '\n' | grep -c .)"
echo "    $NUM_IDS instance(s); dataset=$DATASET split=$SPLIT"

# ── ensure app + coordinator volume (from run.sh) ─────────────────────────────
echo "==> ensuring app $APP exists"
if ! flyctl apps create "$APP" --org "$ORG" 2>/tmp/fly-dist-appcreate.err; then
  grep -qi 'already been taken\|already exists' /tmp/fly-dist-appcreate.err \
    && echo "    app $APP already exists — reusing" \
    || { cat /tmp/fly-dist-appcreate.err; exit 1; }
fi

echo "==> ensuring coordinator volume $COORD_VOL (${COORD_VOL_GB}GB, $REGION)"
# fly ALLOWS duplicate volume names, so only create when none exists. Match NAME.
if flyctl volumes list -a "$APP" 2>/dev/null | awk '{print $3}' | grep -qx "$COORD_VOL"; then
  echo "    volume $COORD_VOL already exists — reusing"
else
  flyctl volumes create "$COORD_VOL" -a "$APP" --region "$REGION" --size "$COORD_VOL_GB" --yes
fi

# ── build the fleet image on fly's remote builder (context = e2e) ─────────────
IMG_BUILT_THIS_RUN=0
if [ -n "$IMAGE" ]; then
  # DEFAULT behaviour is to bake a FRESH image from live $ECON_REPO every run
  # (the else branch). Passing IMAGE= REUSES a prebuilt image and BYPASSES that
  # bake — it can ship STALE runner/econ code (this is how the easy-x15 run shipped
  # the pre-fix run-benchmark.py). Reuse is legitimate ONLY for pinning one image
  # across campaign tranches; otherwise unset IMAGE to get the live bake.
  IMG="$IMAGE"
  echo "==> ⚠️  REUSING PREBUILT IMAGE — NOT baking from live source: $IMG"
  echo "    ⚠️  runner/econ code here may be STALE vs $ECON_REPO. Unset IMAGE to bake fresh (default)."
else
  echo "==> baking FRESH fleet image from LIVE source on fly remote builder (context=$E2E_DIR, econ HEAD $(git -C "$ECON_REPO" rev-parse --short HEAD 2>/dev/null || echo '?'))"
  # Explicit --config: with a positional build CONTEXT arg flyctl looks for
  # fly.toml relative to that dir (e2e has none) and otherwise tries to rebuild
  # config from machines — which fails on a fresh, machine-less app.
  flyctl deploy --build-only --remote-only --push -a "$APP" \
    --config "$HERE/fly.toml" \
    --image-label "dist-$(date +%s)" \
    --dockerfile "$HERE/Dockerfile.dist" "$E2E_DIR" 2>&1 | tee "$OUTDIR/build.log"
  IMG="$(grep -oE 'registry\.fly\.io/[^ ]+' "$OUTDIR/build.log" | tail -1)"
  [ -n "$IMG" ] || { echo "could not determine built image ref"; exit 1; }
  IMG_BUILT_THIS_RUN=1
  echo "==> image: $IMG"
fi

# ── econ provenance for the campaign lock / run-info stamp (below) ────────────
# unknown-prebuilt when IMG was reused (IMAGE=/campaign pin) rather than built
# from the local $ECON_REPO checkout this run.
if [ "$IMG_BUILT_THIS_RUN" = "1" ]; then
  ECON_COMMIT="$(git -C "$ECON_REPO" rev-parse HEAD 2>/dev/null || echo unknown)"
  if [ -n "$(git -C "$ECON_REPO" status --porcelain 2>/dev/null)" ]; then ECON_DIRTY=true; else ECON_DIRTY=false; fi
else
  ECON_COMMIT="unknown-prebuilt"
  ECON_DIRTY=false
fi

# ── campaign lock-write (only the tranche that first resolves this image) ─────
if [ -n "$CAMPAIGN" ] && [ ! -f "$CAMPAIGN_LOCK" ]; then
  CAMPAIGN="$CAMPAIGN" LOCK_IMG="$IMG" LOCK_ECON_COMMIT="$ECON_COMMIT" LOCK_ECON_DIRTY="$ECON_DIRTY" \
  LOCK_DATASET="$DATASET" LOCK_SPLIT="$SPLIT" LOCK_LABEL="$LABEL" LOCK_PATH="$CAMPAIGN_LOCK" \
  "$PY_HOST" -c '
import os, json
from datetime import datetime, timezone
env = os.environ
d = {
    "campaign": env["CAMPAIGN"],
    "image": env["LOCK_IMG"],
    "econ_commit": env["LOCK_ECON_COMMIT"],
    "econ_dirty": env["LOCK_ECON_DIRTY"] == "true",
    "dataset": env["LOCK_DATASET"],
    "split": env["LOCK_SPLIT"],
    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "created_by_label": env["LOCK_LABEL"],
}
with open(env["LOCK_PATH"], "w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
'
  echo "==> campaign '$CAMPAIGN': lock written -> $CAMPAIGN_LOCK (image $IMG)"
fi

# ── run-info.json stamp (always — one per run, campaign or not) ───────────────
mkdir -p "$OUTDIR"
CAMPAIGN="$CAMPAIGN" RI_LABEL="$LABEL" RI_IMG="$IMG" \
RI_ECON_COMMIT="$ECON_COMMIT" RI_ECON_DIRTY="$ECON_DIRTY" \
RI_CPU_KIND="$CPU_KIND" RI_MEM="$MEM" RI_CPUS="$CPUS" RI_MACHINES="$MACHINES" \
RI_REGION="$REGION" RI_DATASET="$DATASET" RI_SPLIT="$SPLIT" \
RI_SUITE="${SUITE:-}" RI_TASKS="${TASKS:-}" RI_TASKS_FILE="${TASKS_FILE:-}" \
RI_WEBSEARCH="${WEBSEARCH:-0}" RI_OUT="$OUTDIR/run-info.json" \
"$PY_HOST" -c '
import os, json
from datetime import datetime, timezone

def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return v

def _opt(v):
    return v if v else None

env = os.environ
d = {
    "label": env["RI_LABEL"],
    "campaign": _opt(env.get("CAMPAIGN", "")),
    "image": env["RI_IMG"],
    "econ_commit": env["RI_ECON_COMMIT"],
    "econ_dirty": env["RI_ECON_DIRTY"] == "true",
    "cpu_kind": env["RI_CPU_KIND"],
    "mem": _int(env["RI_MEM"]),
    "cpus": _int(env["RI_CPUS"]),
    "machines": _int(env["RI_MACHINES"]),
    "region": env["RI_REGION"],
    "dataset": env["RI_DATASET"],
    "split": env["RI_SPLIT"],
    "suite": _opt(env.get("RI_SUITE", "")),
    "tasks": _opt(env.get("RI_TASKS", "")),
    "tasks_file": _opt(env.get("RI_TASKS_FILE", "")),
    "websearch": env["RI_WEBSEARCH"] == "1",
    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
with open(env["RI_OUT"], "w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
'
echo "==> run-info: $OUTDIR/run-info.json"

# ── run_machine: `flyctl machine run` with a MANIFEST_UNKNOWN / 429 / capacity retry ──
# The just-pushed image manifest can lag behind the machine-create call
# (MANIFEST_UNKNOWN); the Machines API create is also rate-limited (429) and can
# hit transient capacity errors. Reuse the SAME already-pushed IMG on each retry.
run_machine() {                             # run_machine <logfile> <flyctl machine run args...>
  local logf="$1"; shift
  local tries=0 max="${RUN_RETRIES:-6}"
  while [ "$tries" -lt "$max" ]; do
    tries=$((tries + 1))
    if flyctl machine run "$@" >"$logf" 2>&1; then
      return 0
    fi
    if grep -qiE 'MANIFEST_UNKNOWN|429|rate limit|capacity|please try again' "$logf"; then
      log "  transient machine-create error (try $tries/$max): $(grep -ioE 'MANIFEST_UNKNOWN|429|rate limit|capacity' "$logf" | head -1) — retry in $((tries * 5))s (same image)"
      sleep $((tries * 5)); continue
    fi
    log "  machine run FAILED (non-transient) — tail:"; tail -6 "$logf" >&2
    return 1
  done
  log "  machine run gave up after $tries tries"; return 1
}

# ── create the COORDINATOR machine (1 volume, coordinator entrypoint) ──────────
echo "==> creating coordinator ($COORD_SIZE / ${COORD_MEM}MB, vol $COORD_VOL, $NUM_IDS tasks)"
run_machine "$OUTDIR/coord-run.log" "$IMG" \
  --app "$APP" --region "$REGION" \
  --vm-size "$COORD_SIZE" --vm-memory "$COORD_MEM" \
  --volume "$COORD_VOL:/data" --restart no \
  --entrypoint /work/distributed/coordinator/coordinator-entrypoint.sh \
  --metadata fleet="$LABEL" --metadata role=coordinator \
  -e RUN_ID="$LABEL" -e TASKS="$INSTANCE_IDS" \
  -e DATASET="$DATASET" -e SPLIT="$SPLIT" -e HOLD="$HOLD" \
  -e HEARTBEAT_TIMEOUT="$HEARTBEAT_TIMEOUT" -e COORD_ARMED="$COORD_ARMED_VAL" \
  || { echo "coordinator create failed — see $OUTDIR/coord-run.log"; exit 1; }
cat "$OUTDIR/coord-run.log"
COORD_MID="$(grep -oE 'Machine ID: [0-9a-f]+' "$OUTDIR/coord-run.log" | head -1 | awk '{print $3}')"
[ -n "$COORD_MID" ] || { echo "could not scrape coordinator machine id"; exit 1; }
# 6PN internal address the workers dial (and the host polls via ssh, below).
COORD_URL="http://${COORD_MID}.vm.${APP}.internal:8080"
echo "==> coordinator: $COORD_MID  ($COORD_URL)"

echo "==> waiting for coordinator to reach 'started'"
for _ in $(seq 1 60); do
  ST="$(flyctl machine status "$COORD_MID" -a "$APP" 2>/dev/null | grep -ioE 'state *= *[a-z]+' | head -1)"
  printf '%s' "$ST" | grep -qi started && { echo "    $ST"; break; }
  sleep 5
done

# ── dedicated conductor: bring up the GPU + flip the gateway (all-in-one only).
#    `prepare` skips it so the ~$80/hr GPU never idles during the toolbox warmup;
#    `run` raises it just before arming (see the run/arm branch above). ──────────
if [ "$MODE" = "all" ] && [ "$DEDICATED_CONDUCTOR" = "1" ]; then
  bring_up_dedicated
fi

# ── create N WORKER machines, PACED (burst 3 then sleep) ──────────────────────
# Machines API create is ~1 req/s (burst 3) per app → burst 3, sleep 3; retry
# 429/MANIFEST/capacity in run_machine. Workers take NO volume — DinD data-root
# lives on ephemeral rootfs (PLAN decision 2). NOTE: if flyctl in use lacks
# `--rootfs-size`, leave ROOTFS_GB unset (default rootfs) and, if the env-image
# unpack is IOPS-starved on the smoke, flip a worker to a volume instead.
echo "==> creating $MACHINES worker(s) (${MEM}MB/${CPUS}cpu/${CPU_KIND}, no volume, paced <=1/s)"
ROOTFS_FLAG=(); [ -n "$ROOTFS_GB" ] && ROOTFS_FLAG=(--rootfs-size "$ROOTFS_GB")
# claude-only worker env — EMPTY for every other arm (econ unaffected).
EXTRA_ENV=()
if [ "$ARM" = "claude" ]; then
  EXTRA_ENV+=(-e CLAUDE_OPEN_MODELS=1 -e LITELLM_MASTER_KEY="$LITELLM_MASTER_KEY" -e ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://econ-litellm.fly.dev}")
fi
# Optional pull-through registry mirror for SWE-bench testbed image pulls — ARM-
# AGNOSTIC (unlike EXTRA_ENV above), shared across econ, claude, and future arms
# (see e2e/distributed/lib/boot.sh::boot_dockerd). Passed through only when set in
# the launching shell; unset -> empty array -> no flag emitted, no default set here.
MIRROR_ENV=(); [ -n "${SWEBENCH_REGISTRY_MIRROR:-}" ] && MIRROR_ENV=(-e SWEBENCH_REGISTRY_MIRROR="$SWEBENCH_REGISTRY_MIRROR")
WORKERS_OK=0
for i in $(seq 1 "$MACHINES"); do
  if run_machine "$OUTDIR/worker-$i-run.log" "$IMG" \
      --app "$APP" --region "$REGION" \
      --vm-memory "$MEM" --vm-cpus "$CPUS" --vm-cpu-kind "$CPU_KIND" "${ROOTFS_FLAG[@]+"${ROOTFS_FLAG[@]}"}" \
      --restart no \
      --entrypoint /work/distributed/worker/worker-entrypoint.sh \
      --metadata fleet="$LABEL" --metadata role=worker \
      -e COORDINATOR_URL="$COORD_URL" -e ARM="$ARM" -e RUN_ID="$LABEL" \
      -e DATASET="$DATASET" -e SPLIT="$SPLIT" \
      -e PER_INSTANCE_TIMEOUT="$PER_INSTANCE_TIMEOUT" -e GRADE_WORKERS="$GRADE_WORKERS" \
      -e STALL_KILL_S="${STALL_KILL_S:-2700}" \
      -e MAX_ARTIFACT_TEXT_BYTES="${MAX_ARTIFACT_TEXT_BYTES:-5000000}" -e MAX_ARTIFACT_DB_BYTES="${MAX_ARTIFACT_DB_BYTES:-8000000}" \
      -e LITELLM_API_KEY="$LITELLM_API_KEY" -e EXA_API_KEY="$EXA_API_KEY" \
      "${EXTRA_ENV[@]+"${EXTRA_ENV[@]}"}" "${MIRROR_ENV[@]+"${MIRROR_ENV[@]}"}"; then
    WID="$(grep -oE 'Machine ID: [0-9a-f]+' "$OUTDIR/worker-$i-run.log" | head -1 | awk '{print $3}')"
    WORKERS_OK=$((WORKERS_OK + 1))
    echo "    worker $i/$MACHINES: ${WID:-?}"
  else
    echo "    worker $i/$MACHINES: FAILED (see $OUTDIR/worker-$i-run.log) — continuing"
  fi
  # pacing: burst of 3, then a 3s cool-off; otherwise ~1/s.
  if [ $((i % 3)) -eq 0 ]; then sleep 3; else sleep 1; fi
done
echo "==> $WORKERS_OK/$MACHINES workers created"
[ "$WORKERS_OK" -ge 1 ] || { echo "no workers came up — tearing down"; destroy_fleet; exit 1; }

# ── prepare: fleet is warming (workers building the toolbox). Wait until every
#    worker has polled /claim (its readiness signal), then HOLD — no work, no GPU.
if [ "$MODE" = "prepare" ]; then
  echo "==> prepare: waiting for $MACHINES worker(s) to warm (build toolbox ~10min) & report ready"
  PREP_WAITED=0; PREP_CEIL="${PREPARE_READY_TIMEOUT:-2400}"; NREADY=0
  while [ "$PREP_WAITED" -lt "$PREP_CEIL" ]; do
    # Capture /status first (ssh can blip) then parse — keeps a transient poll
    # failure from producing a multiline count that breaks the integer test.
    SJSON="$(poll_status 2>/dev/null || true)"
    NREADY="$(printf '%s' "$SJSON" | "$PY_HOST" -c 'import json,sys
try: print(len((json.load(sys.stdin).get("workers_seen") or [])))
except Exception: print(0)' 2>/dev/null || echo 0)"
    echo "    warm workers: ${NREADY:-0}/${MACHINES} (t+${PREP_WAITED}s)"
    [ "${NREADY:-0}" -ge "$MACHINES" ] && break
    sleep 30; PREP_WAITED=$((PREP_WAITED + 30))
  done
  if [ "${NREADY:-0}" -ge "$MACHINES" ]; then
    echo "==> PREPARED: ${NREADY}/${MACHINES} workers warm & holding at the armed gate"
  else
    echo "==> WARN: prepare readiness timeout (${PREP_CEIL}s) — ${NREADY:-0}/${MACHINES} warm; the rest join when armed"
  fi
  cat <<EOF
==> Fleet '$LABEL' is PREPARED — warm, holding, no work started, no GPU up.
    Release it (raise your GPU first if using DEDICATED_CONDUCTOR):
      LABEL=$LABEL ARM=$ARM DEDICATED_CONDUCTOR=${DEDICATED_CONDUCTOR} ./run-distributed.sh run
    Or just flip the gate without babysitting the pull:
      LABEL=$LABEL ./run-distributed.sh arm
EOF
  exit 0
fi
fi   # ═══ end prepare/all build section (run/arm attached above) ═══════════════

# ── poll the coordinator until the bundle is ready ────────────────────────────
# The host is NOT on 6PN, so reach the coordinator's HTTP endpoint by running
# curl INSIDE the coordinator via ssh (localhost:8080). We also tail its logs for
# the `"bundle_ready"` beacon (same shape run.sh watches on the single machine).
echo "==> streaming coordinator logs + polling /status (every 30s, up to ${MAXWAIT}s)"
flyctl logs -a "$APP" --machine "$COORD_MID" > "$OUTDIR/coord.log" 2>&1 &
LOGPID=$!
WAITED=0; STOPPED=0
# Transient flyctl/ssh blips inside this loop must not kill the whole run
# under `set -e` (observed live: rc=1 mid-poll orphaned the fleet and skipped
# bundle-pull/teardown). Suspend errexit for the loop body only — the loop's
# real exit conditions (bundle_ready beacon, 2 consecutive empty-status reads
# with a dead machine, or MAXWAIT) are explicit `break`/loop-exhaustion, not
# errexit-driven, so they still fire normally.
set +e
while [ "$WAITED" -lt "$MAXWAIT" ]; do
  if grep -q '"bundle_ready"' "$OUTDIR/coord.log" 2>/dev/null; then
    echo "==> bundle_ready after ${WAITED}s"; break
  fi
  S="$(poll_status || true)"
  if [ -n "$S" ]; then
    printf '%s' "$S" | "$PY_HOST" -c '
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    print("    /status:", raw[:200]); raise SystemExit
done = d.get("done", d.get("completed"))
total = d.get("total")
res = d.get("resolved")
print(f"    progress: done={done} total={total} resolved={res} raw={json.dumps(d)[:160]}")
' 2>/dev/null || echo "    /status: $S"
    STOPPED=0
  else
    # Tolerate transient blips: only conclude the coordinator is gone after TWO
    # consecutive empty status reads AND a non-running machine state.
    ST="$(flyctl machine status "$COORD_MID" -a "$APP" 2>/dev/null | grep -ioE 'state *= *[a-z]+' | head -1)"
    if [ -n "$ST" ] && ! printf '%s' "$ST" | grep -qiE 'started|created|starting'; then
      STOPPED=$((STOPPED + 1))
      [ "$STOPPED" -ge 2 ] && { echo "==> coordinator no longer running ($ST)"; break; }
    fi
  fi
  sleep 30; WAITED=$((WAITED + 30))
done
set -e
[ "$WAITED" -ge "$MAXWAIT" ] && echo "==> hit MAXWAIT ${MAXWAIT}s without bundle_ready"
kill "$LOGPID" 2>/dev/null || true

echo "==> coordinator result beacons:"
grep -oE '\{"ev":.*\}' "$OUTDIR/coord.log" | tee "$OUTDIR/beacons.jsonl" || true

# ── pull the one merged bundle off the coordinator volume (from run.sh) ───────
if grep -q '"bundle_ready"' "$OUTDIR/coord.log" 2>/dev/null; then
  echo "==> pulling /data/bundle.tgz via sftp"
  # `fly ssh sftp get` REFUSES to overwrite — clear a stale bundle first.
  rm -f "$OUTDIR/bundle.tgz"
  flyctl ssh sftp get /data/bundle.tgz "$OUTDIR/bundle.tgz" -a "$APP" --machine "$COORD_MID" 2>&1 | tail -3 || \
    echo "    sftp pull failed — headline numbers are still in $OUTDIR/coord.log / beacons.jsonl"
  [ -f "$OUTDIR/bundle.tgz" ] && { rm -rf "$OUTDIR/bundle"; mkdir -p "$OUTDIR/bundle"; tar xzf "$OUTDIR/bundle.tgz" -C "$OUTDIR/bundle" && echo "    extracted -> $OUTDIR/bundle/"; }
else
  echo "==> no bundle_ready beacon — fleet did not finish; see $OUTDIR/coord.log"
fi

# ── collect failed-instance triage (resolved=0) into a durable, dated archive ──
# Routine on every run: failed-runs/<label>/ (label carries the MMDD-HHMM run
# time) gets each failed instance's model_patch, grade report (which tests
# failed), engine.log, events.jsonl + session db — so a failure stays fully
# triageable long after the ephemeral fleet is destroyed. Best-effort.
if [ -d "$OUTDIR/bundle" ]; then
  python3 "$HERE/tools/collect-failed.py" --bundle "$OUTDIR/bundle" --label "$LABEL" \
    --dest "$HERE/failed-runs/$LABEL" 2>&1 | sed 's/^/    /' \
    || echo "    (failed-triage collection skipped)"
fi

# ── teardown: destroy the whole fleet by metadata; remove the coord volume ────
# Workers self-stopped on drain (restart:no → stopped, compute billing stops);
# the coordinator is destroyed now that the bundle is pulled.
destroy_fleet
echo "==> done. results in $OUTDIR/ (coord.log, beacons.jsonl, bundle/); failure triage in $HERE/failed-runs/$LABEL/"
