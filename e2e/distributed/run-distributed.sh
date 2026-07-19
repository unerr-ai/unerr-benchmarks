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
#   BENCHMARK=pro SUITE=smoke MACHINES=3 ARM=econ LABEL=pro-smoke ./run-distributed.sh   # BENCHMARK selects the task set (default verified); SUITE selects SIZE (full/smoke/mini/explicit)
#   PLAN_ONLY=1 BENCHMARK=pro SUITE=smoke ARM=econ ./run-distributed.sh                  # print the resolved plan and exit — no fly calls, no LABEL/MACHINES required
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
# BENCHMARK (verified|lite|pro|terminal|live_verified) is ORTHOGONAL to ARM: ARM
# picks the runner (econ/claude), BENCHMARK picks the task set + grading —
# tools/benchmarks.py is the single source of truth (do not duplicate its
# descriptors here). Default verified keeps every pre-BENCHMARK invocation
# byte-identical.
BENCHMARK="${BENCHMARK:-verified}"
case "$BENCHMARK" in
  verified|lite|pro|terminal|live_verified) ;;
  *) echo "BENCHMARK must be one of: verified|lite|pro|terminal|live_verified (got '$BENCHMARK')"; exit 1 ;;
esac
# App is per-(ARM × BENCHMARK): every combo gets its OWN fly app so image builds,
# fleets, monitoring and teardown are fully isolated and multiple benchmarks can
# build+run in parallel without contending on one app's remote builder (two
# concurrent `flyctl deploy --build-only` to the SAME app stall its Depot builder).
# Fly app names allow only [a-z0-9-], so the benchmark key's '_' -> '-'
# (live_verified -> live-verified). The image itself is benchmark-AGNOSTIC (one
# Dockerfile.dist serves every benchmark; the worker dispatches on the BENCHMARK
# env), so a run can still REUSE one baked image across sibling apps via IMAGE=.
# KEEP this derivation byte-identical to tools/fleet-common.sh::fc_default_app.
# APP= override always wins.
BENCH_APP_SLUG="${BENCHMARK//_/-}"          # fly app names: [a-z0-9-] only
# fly's abuse filter REJECTS app names containing "verified" ("a common phishing
# target" — observed live on create), so the slug shortens it: verified -> verif,
# live-verified -> live-verif. KEEP in sync with fc_default_app + bench.sh fallback.
BENCH_APP_SLUG="${BENCH_APP_SLUG//verified/verif}"
DEFAULT_APP="swebench-dist-${ARM}-${BENCH_APP_SLUG}"
APP="${APP:-$DEFAULT_APP}"                  # per-(arm×benchmark) app; runs further scoped by fleet=<LABEL> metadata
RAW_LABEL="${LABEL:-}"                      # the LABEL as the caller typed it — echoed back in follow-up commands (prepare/run/arm), so a second invocation folds identically
LABEL="$RAW_LABEL"                          # REQUIRED, unique per run — doubles as RUN_ID + fleet metadata
# Fold BENCHMARK into LABEL so an (arm × benchmark × runid) triple always gets
# its own fleet: ARM already gets its own app (above), but BENCHMARK does not —
# two benchmarks sharing a LABEL under the same app/ARM would otherwise collide
# on the SAME fleet=<LABEL> metadata (machines, coordinator volume, OUTDIR,
# teardown scope). BENCHMARK=verified (the default) leaves LABEL untouched —
# byte-identical to pre-BENCHMARK behavior. Re-supply the SAME BENCHMARK on any
# follow-up prepare/run/arm/destroy invocation for a non-default benchmark run.
if [ -n "$LABEL" ] && [ "$BENCHMARK" != "verified" ]; then
  LABEL="${LABEL}-${BENCHMARK}"
fi
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
CPU_KIND="${CPU_KIND:-performance}"         # 'performance' (default since 2026-07-19, dedicated cores) for every arm×benchmark combo; 'shared' remains the explicit cheap override
ROOTFS_GB="${ROOTFS_GB:-50}"                 # ephemeral-rootfs size in GB (see NOTE at worker create). 50 = fleet
                                              # floor, now the GLOBAL default for every benchmark (was live_verified-
                                              # only — workers pull 1-4GB per-task eval images from the private
                                              # mirror and ENOSPC below it across the whole SWE-bench family, not
                                              # just live_verified). Set ROOTFS_GB=0 to opt out entirely (fly
                                              # default 8GB rootfs, no --rootfs-size flag — old-flyctl fallback).
# fly hard-caps config.rootfs.size_gb at 50. 0 = explicit opt-out (treated as unset,
# no flag). Anything else is clamped/floored to the valid [50] value instead of
# failing every worker create.
if [ "$ROOTFS_GB" = "0" ]; then
  ROOTFS_GB=""
elif [ "$ROOTFS_GB" -gt 50 ] 2>/dev/null; then
  echo "==> ROOTFS_GB=$ROOTFS_GB exceeds fly's 50 GB max — clamping to 50" >&2
  ROOTFS_GB=50
elif [ "$ROOTFS_GB" -lt 50 ] 2>/dev/null; then
  echo "==> ROOTFS_GB=$ROOTFS_GB is below the 50GB fleet floor — raising to 50" >&2
  ROOTFS_GB=50
fi

# coordinator sizing (bumped 2026-07-19: performance CPU + more headroom for the
# archive/grade fan-in across every arm×benchmark combo)
COORD_SIZE="${COORD_SIZE:-performance-1x}"
COORD_MEM="${COORD_MEM:-2048}"
# fly volume names allow only lowercase alphanumeric + underscores, ≤30 chars —
# LABEL may contain hyphens/uppercase, so sanitize it for the volume name only
# (metadata fleet=<LABEL> keeps the raw label).
_VOL_SLUG="$(printf 'dist_coord_%s' "$LABEL" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9_' '_' | cut -c1-30)"
COORD_VOL="${COORD_VOL:-$_VOL_SLUG}"            # the fleet's ONE small volume
COORD_VOL_GB="${COORD_VOL_GB:-50}"

# task set + grading
SUITE="${SUITE:-}"
TASKS="${TASKS:-}"
TASKS_FILE="${TASKS_FILE:-}"
# SPLIT default is benchmark-aware: only live_verified's frozen "verified split"
# (tools/benchmarks.py _LIVE_VERIFIED) differs from the `test` default every
# other benchmark uses. An explicit SPLIT= always wins.
if [ -z "${SPLIT:-}" ]; then
  case "$BENCHMARK" in
    live_verified) SPLIT="verified" ;;
    terminal)      SPLIT="2.1" ;;
    *)             SPLIT="test" ;;
  esac
fi
# BENCHMARK selects WHICH benchmark; SUITE selects its SIZE. Map (BENCHMARK,
# SUITE) -> the suite.py --suite selector (tools/benchmarks.py owns the actual
# id resolution — this is only the selector string). TASKS/TASKS_FILE keep
# their existing precedence over SUITE in suite.py itself, unaffected here.
case "$(printf '%s' "${SUITE:-}" | tr 'A-Z' 'a-z')" in
  ''|full|all) SUITE_SELECTOR="$BENCHMARK" ;;          # full set for the benchmark
  smoke)       SUITE_SELECTOR="${BENCHMARK}-mini" ;;   # 5-id smoke set
  mini)        SUITE_SELECTOR="mini" ;;                # LEGACY Verified Mini-10 — byte-for-byte, never remapped
  *)           SUITE_SELECTOR="$SUITE" ;;              # explicit passthrough (e.g. pro-mini, terminal)
esac
# Grade against the dataset the benchmark uses unless DATASET is pinned. Legacy
# SUITE=lite (pre-BENCHMARK invocations, BENCHMARK left at its verified default)
# is checked FIRST so that path stays byte-identical; BENCHMARK is the fallback
# for everything else (including BENCHMARK=lite with no SUITE=lite literal).
if [ -z "${DATASET:-}" ]; then
  case "$(printf '%s' "${SUITE:-}" | tr 'A-Z' 'a-z')" in
    lite) DATASET="princeton-nlp/SWE-bench_Lite" ;;
    *)
      case "$BENCHMARK" in
        lite)          DATASET="princeton-nlp/SWE-bench_Lite" ;;
        pro)           DATASET="ScaleAI/SWE-bench_Pro" ;;
        terminal)      DATASET="terminal-bench-2-1" ;;  # Harbor terminal-bench-2-1 (2.1); label only — harness_terminal ignores it, ids come from the vendored task dir
        live_verified) DATASET="SWE-bench-Live/SWE-bench-Live" ;;
        *)             DATASET="princeton-nlp/SWE-bench_Verified" ;;
      esac
      ;;
  esac
fi

HOLD="${HOLD:-3600}"                        # coordinator holds open this long after bundle for the pull
MAXWAIT="${MAXWAIT:-864000}"                 # absolute coordinator drain backstop (10 days) — propagated to the coordinator; NOT a normal limiter (the harness imposes no per-task wall-clock or stall kill — the agent owns its own watchdog — so a legitimate full 500-task run may run for days; the coordinator waits PROGRESS-aware, giving up early only when wedged — see NO_PROGRESS_GIVEUP)
NO_PROGRESS_GIVEUP="${NO_PROGRESS_GIVEUP:-7200}"  # coordinator gives up EARLY only if the fleet is WEDGED: nothing leased AND no completion for this long (workers gone, tasks stranded). Passed to the coordinator.
MAX_FAILURE_RERUN="${MAX_FAILURE_RERUN:-1}"  # coordinator's failure-rerun-on-drain budget per instance; read by coordinator-entrypoint.sh
GRADE_WORKERS="${GRADE_WORKERS:-6}"         # swebench harness --max_workers on the worker
HEARTBEAT_TIMEOUT="${HEARTBEAT_TIMEOUT:-300}"          # coordinator reaps a lease after this much heartbeat silence (10 beats @30s)
KEEP="${KEEP:-0}"                           # 1 = keep the coordinator volume on teardown
PY_HOST="${PYTHON:-python3}"

# Tigris end-of-run archive (OPT-IN, default off): opt-in end-of-run archive of
# results/traces to Tigris. ONE shared bucket serves every per-(arm×benchmark)
# app — the AWS_* creds for it are auto-staged onto each combo's app at prepare
# (see the "ensuring app" block) from .env.tigris; never forwarded here, only
# the ARCHIVE flag + bucket NAME (the bucket name is not a secret). When the
# operator doesn't pass TIGRIS_BUCKET, default it from the same .env.tigris so
# all runs land in the ONE provisioned bucket (no accidental per-run buckets).
ARCHIVE_TIGRIS="${ARCHIVE_TIGRIS:-0}"
TIGRIS_BUCKET="${TIGRIS_BUCKET:-}"
if [ "$ARCHIVE_TIGRIS" = "1" ] && [ -z "$TIGRIS_BUCKET" ] && [ -f "$HERE/.env.tigris" ]; then
  TIGRIS_BUCKET="$(sed -n 's/^TIGRIS_BUCKET=//p' "$HERE/.env.tigris" | head -1)"
  [ -n "$TIGRIS_BUCKET" ] && echo "==> TIGRIS_BUCKET defaulted from .env.tigris: $TIGRIS_BUCKET"
fi
TIGRIS_PREFIX="${TIGRIS_PREFIX:-runs}"
RUN_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"  # captured once, run-scoped — passed to the coordinator for archive key naming

# ── PLAN_ONLY / --print-plan: print the resolved config and exit 0 BEFORE any
# fly auth or API call — proves the BENCHMARK×SUITE selector map, the LABEL
# fold, and the worker/coordinator env additions without touching fly. ────────
if [ "${PLAN_ONLY:-0}" = "1" ] || [ "${1:-}" = "--print-plan" ]; then
  echo "==> PLAN_ONLY: resolved config (no fly API calls made)"
  echo "    ARM=$ARM"
  echo "    BENCHMARK=$BENCHMARK"
  echo "    SUITE=${SUITE:-<unset>} -> suite selector=$SUITE_SELECTOR"
  echo "    APP=$APP"
  echo "    LABEL=${LABEL:-<unset>} (raw=${RAW_LABEL:-<unset>})"
  echo "    DATASET=$DATASET SPLIT=$SPLIT"
  echo "    worker env additions: BENCHMARK=$BENCHMARK"
  echo "    coordinator env additions: ARM=$ARM BENCHMARK=$BENCHMARK MAX_FAILURE_RERUN=$MAX_FAILURE_RERUN"
  exit 0
fi

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
  # ── gpu-flip staleness check (best-effort, NEVER fails teardown) ────────────
  # A dedicated tier flip still set after this fleet is gone likely means the
  # underlying GPU deployment is gone too (or was never torn down) — every probe
  # on that tier then 404s fleet-wide for the NEXT run that hits this gateway.
  local gf_secrets
  gf_secrets="$(flyctl secrets list -a "$GATEWAY_APP" 2>/dev/null || true)"
  if printf '%s\n' "$gf_secrets" | grep -qE '(CONDUCTOR|ORACLE|REASONER|EXECUTOR)_DEPLOYMENT_PATH'; then
    log "  ================================================================"
    log "  WARNING: gateway $GATEWAY_APP still has dedicated tier flip(s) set after this teardown"
    log "    the underlying GPU deployment may already be gone (torn down or never flipped back) —"
    log "    a stale flip 404s that tier FLEET-WIDE for the next run against this gateway"
    log "    run: ./gpu-flip.sh --verify   (then ./gpu-flip.sh --revert --<tier> if dead)"
    log "  ================================================================"
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

# ── gpu-flip staleness gate (before any machine is created) ───────────────────
# A prior DEDICATED_CONDUCTOR run (or a manual gpu-flip.sh) can leave a tier
# flipped to a dedicated deployment that's since been deleted — every probe on
# that tier then 404s fleet-wide the moment a worker claims a task. Refuse to
# build a fleet against a gateway in that state. Best-effort: an `flyctl
# secrets list` failure only warns (never break a plain serverless run) — the
# ONLY hard exit here is an explicit STALE-FLIP verdict from gpu-flip.sh --verify.
GF_SECRETS="$(flyctl secrets list -a "$GATEWAY_APP" 2>/dev/null || true)"
if [ -z "$GF_SECRETS" ]; then
  echo "==> gpu-flip preflight: could not list secrets on $GATEWAY_APP (or none set) — skipping"
elif printf '%s\n' "$GF_SECRETS" | grep -qE '(CONDUCTOR|ORACLE|REASONER|EXECUTOR)_DEPLOYMENT_PATH'; then
  echo "==> gpu-flip preflight: dedicated tier flip(s) present on $GATEWAY_APP — verifying with gpu-flip.sh --verify"
  if ! "$HERE/gpu-flip.sh" --verify; then
    echo "ERROR: gateway has a STALE dedicated flip — run ./gpu-flip.sh --verify (and --revert if dead) before preparing a fleet" >&2
    exit 1
  fi
  echo "    gpu-flip preflight: verified OK"
fi

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
    _INFRA_ENV="$HERE/../../infra/litellm/.env.local"
    if [ -f "$_INFRA_ENV" ]; then
      LITELLM_MASTER_KEY="$(grep -E '^LITELLM_MASTER_KEY=' "$_INFRA_ENV" | head -1 | sed 's/^LITELLM_MASTER_KEY=//; s/^["'"'"']//; s/["'"'"']$//')"
    fi
  fi
  [ -n "${LITELLM_MASTER_KEY:-}" ] || { echo "set LITELLM_MASTER_KEY (or add it to infra/litellm/.env.local) — claude arm needs it to mint per-instance keys"; exit 1; }
  export LITELLM_MASTER_KEY
  echo "==> LITELLM_MASTER_KEY: set (len ${#LITELLM_MASTER_KEY})"
fi

# ── auth: Claude Code OAuth token (claude-real arm only) — REAL Anthropic models,
# stock Claude Code: no base-URL/model-alias env overrides, no LiteLLM anywhere.
# Prefer env, else repo-root .env.local. Value never printed.
if [ "$ARM" = "claude-real" ]; then
  if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    _ROOT_ENV="$HERE/../../.env.local"
    if [ -f "$_ROOT_ENV" ]; then
      CLAUDE_CODE_OAUTH_TOKEN="$(grep -E '^CLAUDE_CODE_OAUTH_TOKEN=' "$_ROOT_ENV" | head -1 | sed 's/^CLAUDE_CODE_OAUTH_TOKEN=//; s/^["'"'"']//; s/["'"'"']$//')"
    fi
  fi
  [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || { echo "set CLAUDE_CODE_OAUTH_TOKEN (or add it to .env.local at repo root) — claude-real arm needs it"; exit 1; }
  export CLAUDE_CODE_OAUTH_TOKEN
  echo "==> CLAUDE_CODE_OAUTH_TOKEN: set (len ${#CLAUDE_CODE_OAUTH_TOKEN})"
fi

# EXA web-search key — the econ agent now ships Exa web search DEFAULT-ON across all
# tiers/personas (econ-coding-agent: "default-on Exa web search across all tiers +
# personas"), so the ECON arm enables Exa BY DEFAULT for EVERY benchmark; the claude
# arm stays opt-in (Tavily below). Set WEBSEARCH=0 to force a clean, baseline-
# comparable (no-web) econ run — web-on is a distinct result class. Key is sourced
# from the econ repo's .env.local (canonical, where EXA_API_KEY is maintained), then
# the arm pipeline dir, then any exported EXA_API_KEY.
EXA_DEFAULT=0; [ "$ARM" = "econ" ] && EXA_DEFAULT=1
if [ "${WEBSEARCH:-$EXA_DEFAULT}" = "1" ]; then
  if [ -z "${EXA_API_KEY:-}" ]; then
    for _envf in "$ECON_REPO/.env.local" "$ECON_DIR/.env.local"; do
      [ -f "$_envf" ] || continue
      EXA_API_KEY="$(grep -E '^EXA_API_KEY=' "$_envf" | head -1 | sed 's/^EXA_API_KEY=//; s/^["'"'"']//; s/["'"'"']$//')"
      [ -n "$EXA_API_KEY" ] && break
    done
  fi
  export EXA_API_KEY="${EXA_API_KEY:-}"
else
  export EXA_API_KEY=""   # WEBSEARCH=0 → force web search off (baseline-comparable)
fi
[ -n "$EXA_API_KEY" ] \
  && echo "==> EXA_API_KEY: set (len ${#EXA_API_KEY}) — Exa web search ON (econ default${WEBSEARCH:+ · WEBSEARCH=$WEBSEARCH}; web-on result class)" \
  || echo "==> EXA_API_KEY: unset — Exa web search OFF (baseline-comparable)"

# TAVILY web-search key — the claude arm's search path (Tavily hosted MCP, wired
# by run-instance.sh into the instance's .mcp.json). STRICT opt-in (only WEBSEARCH=1
# enables it; an ambient TAVILY_API_KEY is IGNORED otherwise) — UNLIKE the econ arm's
# default-on Exa above. Web-on runs are a separate result class — label them accordingly.
if [ "${WEBSEARCH:-0}" = "1" ]; then
  if [ -z "${TAVILY_API_KEY:-}" ] && [ -f "$ECON_DIR/.env.local" ]; then
    TAVILY_API_KEY="$(grep -E '^TAVILY_API_KEY=' "$ECON_DIR/.env.local" | head -1 | sed 's/^TAVILY_API_KEY=//; s/^["'"'"']//; s/["'"'"']$//')"
  fi
  export TAVILY_API_KEY="${TAVILY_API_KEY:-}"
else
  export TAVILY_API_KEY=""   # ignore any ambient key unless WEBSEARCH=1
fi
[ -n "$TAVILY_API_KEY" ] \
  && echo "==> TAVILY_API_KEY: set (len ${#TAVILY_API_KEY}) — claude-arm web search ENABLED (NOT baseline-comparable)" \
  || echo "==> TAVILY_API_KEY: unset — claude-arm web search disabled"

# ── serialize the LOCAL vendor+bake across CONCURRENT launchers ───────────────
# Per-(arm×benchmark) apps make the REMOTE bake contention-free, but every
# launcher still writes the SAME local vendor/ dir and uploads the SAME e2e/
# build context — two triggers fired at the same moment would tear each other's
# context mid-upload. This mkdir-lock covers vendor start → image ref resolved;
# a second simultaneous trigger waits here (with a log line), then bakes on its
# own app's builder. Triggers minutes apart never see it. A crashed holder's
# lock is stolen as soon as its pid is gone (no trap needed — the failure exit
# paths leave a dead pid behind, which the next trigger detects).
BUILD_LOCK="$HERE/out/.build-lock"
acquire_build_lock() {
  local waited=0 holder
  while ! mkdir "$BUILD_LOCK" 2>/dev/null; do
    holder="$(cat "$BUILD_LOCK/pid" 2>/dev/null || true)"
    if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
      echo "==> build lock: holder pid $holder gone — stealing stale lock"
      rm -rf "$BUILD_LOCK"; continue
    fi
    if [ $((waited % 60)) -eq 0 ]; then
      echo "==> build lock held by pid ${holder:-<starting>} (another trigger is vendoring/baking the shared local context) — waiting (${waited}s)"
    fi
    sleep 10; waited=$((waited + 10))
  done
  echo "$$" >"$BUILD_LOCK/pid"
}
release_build_lock() { rm -rf "$BUILD_LOCK"; }
acquire_build_lock

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
elif [ "$ARM" = "claude" ] || [ "$ARM" = "claude-real" ]; then
  log "arm=$ARM: no vendor step"
fi

# ── resolve the task set → INSTANCE_IDS (comma-separated) ─────────────────────
echo "==> resolving task set (benchmark=$BENCHMARK suite=${SUITE:-full} selector=$SUITE_SELECTOR tasks=${TASKS:+set} file=${TASKS_FILE:-none})"
INSTANCE_IDS="$("$PY_HOST" "$HERE/tools/suite.py" \
  --suite "$SUITE_SELECTOR" ${TASKS:+--tasks "$TASKS"} ${TASKS_FILE:+--file "$TASKS_FILE"} \
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

# ── stage Tigris AWS_* app secrets (per-(arm×benchmark) apps are created on
# demand, so a fresh app lacks them; the coordinator's end-of-run archive reads
# AWS_* ONLY as fly APP SECRETS — they are never forwarded as -e machine env).
# Values come from .env.tigris (written once by provision-tigris.sh). Idempotent
# (skips when the app already carries AWS_ACCESS_KEY_ID); --stage applies on the
# next machine launch (these apps have no Fly Launch release to trigger). Values
# are never echoed — lengths only. Non-fatal: a warn here means the archive step
# would write 0 objects (backfill later from the local bundle).
if [ "$ARCHIVE_TIGRIS" = "1" ]; then
  TIGRIS_ENVFILE="$HERE/.env.tigris"
  if flyctl secrets list -a "$APP" 2>/dev/null | awk '{print $1}' | grep -qx 'AWS_ACCESS_KEY_ID'; then
    echo "==> tigris AWS_* already on app $APP — leaving as-is"
  elif [ -f "$TIGRIS_ENVFILE" ]; then
    TG_AK="$(sed -n 's/^AWS_ACCESS_KEY_ID=//p' "$TIGRIS_ENVFILE" | head -1)"
    TG_SK="$(sed -n 's/^AWS_SECRET_ACCESS_KEY=//p' "$TIGRIS_ENVFILE" | head -1)"
    TG_EP="$(sed -n 's/^AWS_ENDPOINT_URL_S3=//p' "$TIGRIS_ENVFILE" | head -1)"
    if [ -n "$TG_AK" ] && [ -n "$TG_SK" ]; then
      echo "==> staging tigris AWS_* onto app $APP (key lens ${#TG_AK}/${#TG_SK}; values not echoed)"
      flyctl secrets set --stage -a "$APP" \
        AWS_ACCESS_KEY_ID="$TG_AK" AWS_SECRET_ACCESS_KEY="$TG_SK" \
        ${TG_EP:+AWS_ENDPOINT_URL_S3="$TG_EP"} >/dev/null 2>&1 \
        || echo "    WARN: secrets set failed — archive may write 0 objects (stage manually per provision-tigris.sh)"
    else
      echo "==> WARN: $TIGRIS_ENVFILE lacks AWS keys — fresh app '$APP' cannot archive (run provision-tigris.sh)"
    fi
    unset TG_AK TG_SK TG_EP
  else
    echo "==> WARN: ARCHIVE_TIGRIS=1 but no $TIGRIS_ENVFILE — fresh app '$APP' lacks AWS_*; run provision-tigris.sh first"
  fi
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
  # Persist the freshly-baked ref per arm so a SIBLING benchmark trigger can skip
  # its own bake entirely (the image is benchmark-agnostic; registry access is
  # org-scoped): IMAGE=$(cat out/.last-image-<arm>) SKIP_ECON_BUILD=1 ...
  echo "$IMG" > "$HERE/out/.last-image-$ARM" 2>/dev/null || true
fi
release_build_lock   # local vendor/ + context no longer read — parallel triggers may bake now

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
    # Slow placement: fly CREATED the machine (it has a "Machine ID:" in the log)
    # but didn't confirm 'started' inside flyctl's own start-wait window, so the
    # command exits non-zero even though the machine reaches 'started' seconds
    # later. Common when iad is capacity-tight or another fleet is churning
    # placement in parallel — treating it as fatal is exactly what stops
    # independent fleets from coming up side by side. So poll the created machine
    # before giving up; downstream scrapes the machine id from THIS same log, so a
    # slow-but-started machine just works. Only recreate if it truly never starts.
    if grep -qi 'failed to reach desired start state' "$logf"; then
      local mid; mid="$(grep -oE 'Machine ID: [0-9a-f]+' "$logf" | head -1 | awk '{print $3}')"
      if [ -n "$mid" ]; then
        log "  machine $mid created but slow to start (try $tries/$max) — polling up to ${SLOW_START_WAIT:-240}s (iad placement lag / parallel-fleet contention)"
        local waited=0 st
        while [ "$waited" -lt "${SLOW_START_WAIT:-240}" ]; do
          st="$(flyctl machine status "$mid" -a "$APP" 2>/dev/null | grep -ioE 'state *= *[a-z]+' | head -1)"
          if printf '%s' "$st" | grep -qi started; then
            log "  machine $mid reached 'started' after slow placement — proceeding"
            return 0
          fi
          sleep 10; waited=$((waited + 10))
        done
        log "  machine $mid never started in ${SLOW_START_WAIT:-240}s — destroying orphan + retrying (same image)"
        flyctl machine destroy "$mid" -a "$APP" --force >/dev/null 2>&1 || true
        sleep $((tries * 5)); continue
      fi
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
  -e RUN_ID="$LABEL" -e TASKS="$INSTANCE_IDS" -e ARM="$ARM" -e BENCHMARK="$BENCHMARK" \
  -e DATASET="$DATASET" -e SPLIT="$SPLIT" -e HOLD="$HOLD" \
  -e HEARTBEAT_TIMEOUT="$HEARTBEAT_TIMEOUT" -e COORD_ARMED="$COORD_ARMED_VAL" \
  -e MAXWAIT="$MAXWAIT" -e NO_PROGRESS_GIVEUP="$NO_PROGRESS_GIVEUP" \
  -e MAX_FAILURE_RERUN="$MAX_FAILURE_RERUN" \
  -e ARCHIVE_TIGRIS="$ARCHIVE_TIGRIS" -e TIGRIS_BUCKET="$TIGRIS_BUCKET" \
  -e TIGRIS_PREFIX="$TIGRIS_PREFIX" -e RUN_STARTED_AT="$RUN_STARTED_AT" \
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
# `--rootfs-size`, set ROOTFS_GB=0 to opt out (fly default rootfs) and, if the
# env-image unpack is IOPS-starved on the smoke, flip a worker to a volume instead.
echo "==> creating $MACHINES worker(s) (${MEM}MB/${CPUS}cpu/${CPU_KIND}, no volume, paced <=1/s)"
ROOTFS_FLAG=(); [ -n "$ROOTFS_GB" ] && ROOTFS_FLAG=(--rootfs-size "$ROOTFS_GB")
# claude-only worker env — EMPTY for every other arm (econ unaffected).
EXTRA_ENV=()
if [ "$ARM" = "claude" ]; then
  EXTRA_ENV+=(-e CLAUDE_OPEN_MODELS=1 -e LITELLM_MASTER_KEY="$LITELLM_MASTER_KEY" -e ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://econ-litellm.fly.dev}")
elif [ "$ARM" = "claude-real" ]; then
  # stock Claude Code on REAL Anthropic models: OAuth token only — no
  # base-URL/model-alias envs, no LiteLLM. CLAUDE_REAL=1 turns on the same
  # harness staging (agents/hooks/ON prompt) the open-models arm gets.
  # TOOLBOX_TAG pins the SHARED claude toolbox (same local-docker dir,
  # identical image) — without it worker-entrypoint derives
  # unerr-claude-real-toolbox and rebuilds an identical toolbox per arm.
  EXTRA_ENV+=(-e CLAUDE_REAL=1 -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" -e CLAUDE_MODEL="${CLAUDE_MODEL:-sonnet}" -e TOOLBOX_TAG=unerr-claude-toolbox)
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
      -e COORDINATOR_URL="$COORD_URL" -e ARM="$ARM" -e BENCHMARK="$BENCHMARK" -e RUN_ID="$LABEL" \
      -e DATASET="$DATASET" -e SPLIT="$SPLIT" \
      -e GRADE_WORKERS="$GRADE_WORKERS" \
      -e MAX_ARTIFACT_TEXT_BYTES="${MAX_ARTIFACT_TEXT_BYTES:-5000000}" -e MAX_ARTIFACT_DB_BYTES="${MAX_ARTIFACT_DB_BYTES:-8000000}" \
      -e LITELLM_API_KEY="$LITELLM_API_KEY" -e EXA_API_KEY="$EXA_API_KEY" \
      -e TAVILY_API_KEY="$TAVILY_API_KEY" \
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
      LABEL=$RAW_LABEL ARM=$ARM BENCHMARK=$BENCHMARK DEDICATED_CONDUCTOR=${DEDICATED_CONDUCTOR} ./run-distributed.sh run
    Or just flip the gate without babysitting the pull:
      LABEL=$RAW_LABEL BENCHMARK=$BENCHMARK ./run-distributed.sh arm
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
  # Stream-independent terminal check: the coordinator writes /data/BUNDLE_READY
  # AFTER the Tigris archive (race-safe, same moment as the stdout beacon). Poll it
  # over the reliable `ssh curl` channel so a dropped `flyctl logs` stream can't
  # strand a finished run past MAXWAIT (observed live: coordinator done+archived,
  # launcher stuck polling done=None for hours). Synthesize the beacon into
  # coord.log so the downstream bundle-pull + teardown gate fires unchanged.
  if flyctl ssh console -a "$APP" --machine "$COORD_MID" \
       -C 'cat /data/BUNDLE_READY 2>/dev/null' 2>/dev/null \
       | grep -q '"bundle_ready"'; then
    echo '{"ev":"bundle_ready","via":"sentinel"}' >> "$OUTDIR/coord.log"
    echo "==> bundle_ready (durable sentinel; log stream had dropped the beacon) after ${WAITED}s"; break
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
c = d.get("counts", {}) or {}
total = d.get("total")
res = d.get("resolved")
# /status has no top-level "done" — the completed tally lives in counts.done.
print(f"    progress: done={c.get('done',0)}/{total} resolved={res} "
      f"pending={c.get('pending',0)} leased={c.get('leased',0)} "
      f"dead={c.get('dead',0)} failed={c.get('failed',0)}")
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
