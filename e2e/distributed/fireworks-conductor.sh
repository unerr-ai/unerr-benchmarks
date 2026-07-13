#!/usr/bin/env bash
# Ephemeral Fireworks DEDICATED-GPU deployment for the econ conductor tier.
#
# Why: the conductor (minimax/minimax-m3, a 428B MoE) is served from Fireworks
# SERVERLESS by default, whose shared TPM limits throttle us (429/503) above ~2
# parallel resolves. A dedicated on-demand deployment has "no hard rate limits —
# only your deployment's capacity", so a single one lets the whole fleet run in
# parallel. It is billed per-GPU-SECOND ($0 when deleted), so we bring it UP just
# before a flagged benchmark run and DELETE it at teardown (see run-distributed.sh
# DEDICATED_CONDUCTOR=1). This script is that lifecycle: up / wait-ready / down /
# status / print-path.
#
# Verified live 2026-07-13 against account accounts/vamsee-k-ra566yo1je2 (autorail):
# minimax-m3 requires a MIN of 8 accelerators on B200/B300 (or 16 on H100/H200).
# Default here = 8x B300 288GB ($12/GPU-hr = $96/hr, FP4) — Fireworks' RECOMMENDED
# default shape for minimax-m3, so it has ready GPU capacity (8x B200 at $80/hr is
# valid + cheaper but sat in the B200 scheduling queue on 2026-07-13). A pinned
# deploymentId gives a STABLE model path so the gateway config can reference it.
#
# Standalone usage (manual test):
#   ./fireworks-conductor.sh up                 # create + wait until READY
#   ./fireworks-conductor.sh status
#   ./fireworks-conductor.sh print-path         # the dedicated model path (for the gateway secret)
#   ./fireworks-conductor.sh down               # delete (stops billing)
#
# The API key is resolved WITHOUT ever printing it: $FIREWORKS_API_KEY, else
# e2e/econ/.env.local, else pulled from the econ-litellm fly secret.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ECON_ENV="${ECON_ENV:-$HERE/../econ/.env.local}"

# ── tunables (env-overridable; defaults = the verified cheapest valid shape) ──
FW_API="${FW_API:-https://api.fireworks.ai/v1}"
FW_ACCOUNT="${FW_ACCOUNT:-accounts/vamsee-k-ra566yo1je2}"
FW_BASE_MODEL="${FW_BASE_MODEL:-accounts/fireworks/models/minimax-m3}"
FW_DEPLOYMENT_ID="${FW_DEPLOYMENT_ID:-bench-conductor}"
FW_ACCEL_TYPE="${FW_ACCEL_TYPE:-NVIDIA_B300_288GB}"   # 8x B300 = Fireworks default shape, $96/hr, FP4
FW_ACCEL_COUNT="${FW_ACCEL_COUNT:-8}"
FW_PRECISION="${FW_PRECISION:-FP4}"                   # FP4 = B300 default (matches the READY stqphvpa spec); "" lets auto-tune pick
FW_MIN_REPLICAS="${FW_MIN_REPLICAS:-0}"              # MUST be 0 — the create fails code=INTERNAL with min>0. Warmth is NOT from min>0:
                                                     # it comes from traffic (scaleToZeroWindow=3600s keeps 1 replica up 1h after last request)
FW_MAX_REPLICAS="${FW_MAX_REPLICAS:-1}"               # autoscales 0→1 on demand; raise for more concurrency
FW_MULTIREGION="${FW_MULTIREGION:-GLOBAL}"            # placement.multiRegion — GLOBAL = broadest B300 capacity (matches working spec)
FW_READY_TIMEOUT="${FW_READY_TIMEOUT:-1800}"          # 30m — 428B on 8 GPUs is a slow cold start
FW_GATEWAY_APP="${FW_GATEWAY_APP:-econ-litellm}"      # only used for the key-of-last-resort pull

# The gateway model path that routes to this deployment (LiteLLM `#deployment` form).
# Serverless (default, committed in config.yaml) vs the dedicated form built from the id.
FW_SERVERLESS_PATH="fireworks_ai/${FW_BASE_MODEL}"
FW_DEDICATED_PATH="fireworks_ai/${FW_BASE_MODEL}#${FW_ACCOUNT}/deployments/${FW_DEPLOYMENT_ID}"

log() { printf '[fw-conductor] %s\n' "$*" >&2; }

# ── resolve the Fireworks API key (never printed) ─────────────────────────────
_fw_key() {
  if [ -n "${FIREWORKS_API_KEY:-}" ]; then printf '%s' "$FIREWORKS_API_KEY"; return 0; fi
  if [ -f "$ECON_ENV" ]; then
    local k; k="$(grep -E '^FIREWORKS_API_KEY=' "$ECON_ENV" | head -1 | sed 's/^FIREWORKS_API_KEY=//; s/^["'"'"']//; s/["'"'"']$//')"
    [ -n "$k" ] && { printf '%s' "$k"; return 0; }
  fi
  # last resort: read it straight off the running gateway machine (needs flyctl auth)
  local k; k="$(flyctl ssh console -a "$FW_GATEWAY_APP" -C 'printenv FIREWORKS_API_KEY' 2>/dev/null | tr -d '\r\n' || true)"
  [ -n "$k" ] && { printf '%s' "$k"; return 0; }
  log "FATAL: FIREWORKS_API_KEY not found (env / $ECON_ENV / $FW_GATEWAY_APP fly secret)"
  return 1
}

# curl the control-plane; args: METHOD PATH [json-body]. Prints response body.
_fw_api() {
  local method="$1" path="$2" body="${3:-}" key
  key="$(_fw_key)" || return 1
  if [ -n "$body" ]; then
    curl -sS -X "$method" -H "Authorization: Bearer $key" -H "Content-Type: application/json" \
      "$FW_API/$path" -d "$body"
  else
    curl -sS -X "$method" -H "Authorization: Bearer $key" "$FW_API/$path"
  fi
}

# read a top-level JSON string field without a jq dependency guarantee (jq if present).
_json_field() { # _json_field <field>  (reads stdin)
  if command -v jq >/dev/null 2>&1; then jq -r ".${1} // empty" 2>/dev/null; else
    "${PYTHON:-python3}" -c 'import sys,json;
try: d=json.load(sys.stdin)
except Exception: d={}
v=d.get(sys.argv[1]);
print("" if v is None else v)' "$1" 2>/dev/null; fi
}

_deployment_state() { # prints the deployment state, or "" if it does not exist
  _fw_api GET "$FW_ACCOUNT/deployments/$FW_DEPLOYMENT_ID" | _json_field state
}

cmd_up() {
  local st; st="$(_deployment_state || true)"
  if [ -n "$st" ] && [ "$st" != "DELETING" ] && [ "$st" != "DELETED" ]; then
    log "deployment $FW_DEPLOYMENT_ID already exists (state=$st) — reusing"
  else
    log "creating dedicated deployment $FW_DEPLOYMENT_ID: ${FW_ACCEL_COUNT}x ${FW_ACCEL_TYPE}${FW_PRECISION:+ ($FW_PRECISION)}, replicas ${FW_MIN_REPLICAS}-${FW_MAX_REPLICAS}"
    local body resp name prec_field=""
    [ -n "$FW_PRECISION" ] && prec_field="$(printf ',"precision":"%s"' "$FW_PRECISION")"
    # This body mirrors the READY stqphvpa deployment field-for-field. Two fields are
    # LOAD-BEARING and were the reason the earlier raw creates died with code=INTERNAL:
    #   autoTune:{}                 → lets Fireworks merge the model's throughput shape
    #                                 (accounts/fireworks/deploymentShapes/minimax-m3-throughput).
    #                                 OMITTING it (with min>0) fails server-side.
    #   minReplicaCount:0           → min>0 also fails INTERNAL; the deployment autoscales 0→1.
    # placement.multiRegion=GLOBAL matches the working spec (broadest B300 capacity).
    body="$(printf '{"baseModel":"%s","acceleratorType":"%s","acceleratorCount":%s,"minReplicaCount":%s,"maxReplicaCount":%s,"autoTune":{},"placement":{"multiRegion":"%s"}%s,"displayName":"econ bench conductor (ephemeral)"}' \
      "$FW_BASE_MODEL" "$FW_ACCEL_TYPE" "$FW_ACCEL_COUNT" "$FW_MIN_REPLICAS" "$FW_MAX_REPLICAS" "$FW_MULTIREGION" "$prec_field")"
    resp="$(_fw_api POST "$FW_ACCOUNT/deployments?deploymentId=$FW_DEPLOYMENT_ID" "$body")"
    name="$(printf '%s' "$resp" | _json_field name)"
    if [ -z "$name" ]; then
      log "FATAL: create failed: $(printf '%s' "$resp" | _json_field message)"
      log "  full response: $(printf '%s' "$resp" | head -c 400)"
      return 1
    fi
    log "created $name"
  fi
  cmd_wait_ready
}

cmd_wait_ready() {
  log "waiting for $FW_DEPLOYMENT_ID to reach READY (timeout ${FW_READY_TIMEOUT}s)"
  local waited=0 st
  while [ "$waited" -lt "$FW_READY_TIMEOUT" ]; do
    st="$(_deployment_state || true)"
    case "$st" in
      READY|DEPLOYED) log "  READY after ${waited}s"; return 0 ;;
      FAILED|UNHEALTHY|DELETING|DELETED|"") [ -n "$st" ] && { log "FATAL: deployment entered state=$st"; return 1; } ;;
    esac
    log "  state=$st (${waited}s)"
    sleep 20; waited=$((waited + 20))
  done
  log "FATAL: not READY within ${FW_READY_TIMEOUT}s (last state=$st)"
  return 1
}

cmd_down() {
  local st; st="$(_deployment_state || true)"
  if [ -z "$st" ]; then log "no deployment $FW_DEPLOYMENT_ID — nothing to delete"; return 0; fi
  log "deleting deployment $FW_DEPLOYMENT_ID (state=$st) — stops billing"
  _fw_api DELETE "$FW_ACCOUNT/deployments/$FW_DEPLOYMENT_ID" >/dev/null 2>&1 || true
  log "delete requested"
}

cmd_status()          { local st; st="$(_deployment_state || true)"; printf '%s\n' "${st:-ABSENT}"; }
cmd_print_path()      { printf '%s\n' "$FW_DEDICATED_PATH"; }
cmd_print_serverless(){ printf '%s\n' "$FW_SERVERLESS_PATH"; }

case "${1:-}" in
  up)                    cmd_up ;;
  wait-ready)            cmd_wait_ready ;;
  down)                  cmd_down ;;
  status)                cmd_status ;;
  print-path)            cmd_print_path ;;
  print-serverless-path) cmd_print_serverless ;;
  *) echo "usage: $0 {up|wait-ready|down|status|print-path|print-serverless-path}" >&2; exit 2 ;;
esac
