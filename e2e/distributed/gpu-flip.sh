#!/usr/bin/env bash
# gpu-flip.sh — flip the econ LiteLLM gateway's per-tier upstreams to DEDICATED
# Fireworks deployments you raised manually, or revert them to serverless.
#
# Model: you raise a dedicated GPU/deployment on Fireworks per tier and pass its
# deployment id here; this sets the matching <TIER>_DEPLOYMENT_PATH secret on the
# econ-litellm fly app. That app's econ-entrypoint.sh rewrites the tier's config.yaml
# `model:` line to the dedicated `#deployments/...` form at boot. Revert = unset it,
# and the gateway serves serverless again. Runnable at your convenience, any subset of
# tiers, independently of any benchmark run.
#
# Tier -> Fireworks base-model slug mirrors ECON_TIER_BINDING (econ model-registry.ts):
#   conductor=minimax-m3   oracle=glm-5p2   reasoner=deepseek-v4-pro   executor=gpt-oss-120b
#
# Tier -> gateway model_name (used by --verify's tool-call probes):
#   conductor=minimax/minimax-m3   oracle=z-ai/glm-5.2
#   reasoner=deepseek/deepseek-v4-pro   executor=openai/gpt-oss-120b
#
# Usage:
#   ./gpu-flip.sh --conductor <dep-id> [--oracle <id>] [--reasoner <id>] [--executor <id>]
#   ./gpu-flip.sh --revert [--conductor] [--oracle] [--reasoner] [--executor]  # revert named tiers
#   ./gpu-flip.sh --serverless        # revert ALL tiers to serverless
#   ./gpu-flip.sh --status            # show which tier secrets are currently set
#   ./gpu-flip.sh --verify            # probe all 4 tiers (chat/completions + responses) for a
#                                      # real tool call through the gateway; PASS/FAIL table,
#                                      # STALE-FLIP flags a dead dedicated deployment; exit 0
#                                      # iff all 8 probes pass
#   Prefix any command with --dry-run to print the flyctl call without running it.
#   NOTE: put --revert BEFORE the tier flags you want reverted.
#
# Env:
#   GATEWAY_APP   fly app for the gateway   (default econ-litellm)
#   GATEWAY_URL   gateway base URL for --verify probes  (default https://econ-litellm.fly.dev)
#   FW_ACCOUNT    Fireworks account slug    (default accounts/vamsee-k-ra566yo1je2)
#   LITELLM_API_KEY / LITELLM_MASTER_KEY   gateway bearer key for --verify (else sourced from
#                                          infra/litellm/.env.local, never printed)
set -euo pipefail

GATEWAY_APP="${GATEWAY_APP:-econ-litellm}"
GATEWAY_URL="${GATEWAY_URL:-https://econ-litellm.fly.dev}"
FW_ACCOUNT="${FW_ACCOUNT:-accounts/vamsee-k-ra566yo1je2}"
DRY_RUN=0
MODE=""

# tier -> fireworks base-model slug (bash-3.2 portable: a case, not an assoc array).
slug_for() {
  case "$1" in
    conductor) echo "minimax-m3" ;;
    oracle)    echo "glm-5p2" ;;
    reasoner)  echo "deepseek-v4-pro" ;;
    executor)  echo "gpt-oss-120b" ;;
    *) return 1 ;;
  esac
}
secret_name() { printf '%s_DEPLOYMENT_PATH' "$(printf '%s' "$1" | tr 'a-z' 'A-Z')"; }
dep_path() {  # $1=slug  $2=deployment-id
  printf 'fireworks_ai/accounts/fireworks/models/%s#%s/deployments/%s' "$1" "$FW_ACCOUNT" "$2"
}
# tier -> gateway model_name (the LiteLLM route name, not the raw Fireworks slug
# above) — used only by --verify's tool-call probes.
verify_model_for() {
  case "$1" in
    conductor) echo "minimax/minimax-m3" ;;
    oracle)    echo "z-ai/glm-5.2" ;;
    reasoner)  echo "deepseek/deepseek-v4-pro" ;;
    executor)  echo "openai/gpt-oss-120b" ;;
    *) return 1 ;;
  esac
}
# classify_probe <body> <chat|responses> — PASS if a real tool call came back;
# else FAIL(param-seam) (model rejects these params), FAIL(STALE-FLIP) (a dead
# dedicated deployment — the gateway 404s), or FAIL(other: ...) (first 200 chars).
classify_probe() {
  local body="$1" kind="$2" has_tool
  if [ "$kind" = chat ]; then
    has_tool="$(printf '%s' "$body" | jq -e '((.choices[0].message.tool_calls // []) | length) > 0' 2>/dev/null || true)"
  else
    has_tool="$(printf '%s' "$body" | jq -e '([.output[]? | select(.type=="function_call")] | length) > 0' 2>/dev/null || true)"
  fi
  if [ "$has_tool" = "true" ]; then
    echo "PASS"
  elif printf '%s' "$body" | grep -q 'UnsupportedParamsError'; then
    echo "FAIL(param-seam)"
  elif printf '%s' "$body" | grep -qE 'NOT_FOUND|Model not found|NotFoundError'; then
    echo "FAIL(STALE-FLIP)"
  else
    echo "FAIL(other: $(printf '%s' "$body" | cut -c1-200))"
  fi
}
# cmd_verify — probe all 4 tiers x {chat/completions, responses} through the
# gateway for a real tool call; table + exit 0 iff all 8 probes PASS. Read-only
# (no --dry-run gate — nothing here mutates gateway state).
cmd_verify() {
  command -v curl >/dev/null 2>&1 || { echo "ERROR: curl not found on PATH" >&2; exit 3; }
  command -v jq   >/dev/null 2>&1 || { echo "ERROR: jq not found on PATH" >&2; exit 3; }

  # key: env first (never printed), else infra/litellm/.env.local relative to THIS script.
  local key=""
  if [ -n "${LITELLM_API_KEY:-}" ]; then
    key="$LITELLM_API_KEY"
  elif [ -n "${LITELLM_MASTER_KEY:-}" ]; then
    key="$LITELLM_MASTER_KEY"
  else
    local script_dir env_file
    script_dir="$(cd "$(dirname "$0")" && pwd)"
    env_file="$script_dir/../../infra/litellm/.env.local"
    if [ -f "$env_file" ]; then
      # `|| true`: a no-match grep exits 1 and would kill the script inside the
      # assignment under `set -euo pipefail` BEFORE the MASTER_KEY fallback runs.
      key="$(grep -E '^LITELLM_API_KEY=' "$env_file" | head -1 | sed 's/^LITELLM_API_KEY=//; s/^"//; s/"$//' || true)"
      [ -n "$key" ] || key="$(grep -E '^LITELLM_MASTER_KEY=' "$env_file" | head -1 | sed 's/^LITELLM_MASTER_KEY=//; s/^"//; s/"$//' || true)"
    fi
  fi
  [ -n "$key" ] || { echo "ERROR: no gateway key — set LITELLM_API_KEY or LITELLM_MASTER_KEY, or add one to infra/litellm/.env.local" >&2; exit 3; }

  local secrets_out
  secrets_out="$(flyctl secrets list --app "$GATEWAY_APP" 2>/dev/null || true)"

  echo "==> --verify: probing 4 tiers x {chat, responses} on $GATEWAY_URL (app $GATEWAY_APP)"
  printf '%-11s | %-7s | %-24s | %-24s\n' "tier" "flipped" "chat" "responses"

  local overall=0 t model flipped chat_body resp_body chat_v resp_v
  for t in conductor oracle reasoner executor; do
    model="$(verify_model_for "$t")"
    if printf '%s\n' "$secrets_out" | grep -q "$(secret_name "$t")"; then flipped="yes"; else flipped="no"; fi

    chat_body="$(curl -s --max-time 120 -X POST "$GATEWAY_URL/v1/chat/completions" \
      -H "Authorization: Bearer $key" -H "Content-Type: application/json" \
      -d "$(jq -n --arg m "$model" '{model:$m, messages:[{role:"user",content:"Call the echo tool with text=hi"}], tools:[{type:"function",function:{name:"echo",parameters:{type:"object",properties:{text:{type:"string"}},required:["text"]}}}], tool_choice:"required", max_tokens:400}')" \
      2>/dev/null || true)"
    chat_v="$(classify_probe "$chat_body" chat)"

    resp_body="$(curl -s --max-time 120 -X POST "$GATEWAY_URL/v1/responses" \
      -H "Authorization: Bearer $key" -H "Content-Type: application/json" \
      -d "$(jq -n --arg m "$model" '{model:$m, input:"Call the echo tool with text=hi", tools:[{type:"function",name:"echo",parameters:{type:"object",properties:{text:{type:"string"}},required:["text"]}}], tool_choice:"required", max_output_tokens:400}')" \
      2>/dev/null || true)"
    resp_v="$(classify_probe "$resp_body" responses)"

    printf '%-11s | %-7s | %-24s | %-24s\n' "$t" "$flipped" "$chat_v" "$resp_v"

    [ "$chat_v" = "PASS" ] || overall=1
    [ "$resp_v" = "PASS" ] || overall=1
    if [ "$chat_v" = "FAIL(STALE-FLIP)" ] || [ "$resp_v" = "FAIL(STALE-FLIP)" ]; then
      echo "./gpu-flip.sh --revert --$t   # or re-raise the GPU and re-flip"
    fi
  done

  exit "$overall"
}
usage() { sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

SET_PAIRS=()      # NAME=value for `fly secrets set`
REVERT_TIERS=()   # tiers to revert

[ $# -eq 0 ] && usage 1
while [ $# -gt 0 ]; do
  case "$1" in
    --conductor|--oracle|--reasoner|--executor)
      tier="${1#--}"
      if [ "$MODE" = revert ]; then
        REVERT_TIERS+=("$tier"); shift
      else
        MODE="flip"
        id="${2:-}"; [ -n "$id" ] || { echo "ERROR: $1 needs a deployment id" >&2; exit 2; }
        slug="$(slug_for "$tier")"
        SET_PAIRS+=("$(secret_name "$tier")=$(dep_path "$slug" "$id")")
        shift 2
      fi ;;
    --revert)     MODE="revert"; shift ;;
    --serverless) MODE="revert"; REVERT_TIERS=(conductor oracle reasoner executor); shift ;;
    --status)     MODE="status"; shift ;;
    --verify)     MODE="verify"; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --app)        GATEWAY_APP="${2:?--app needs a value}"; shift 2 ;;
    -h|--help)    usage 0 ;;
    *) echo "ERROR: unknown arg '$1'" >&2; usage 2 ;;
  esac
done

[ "$DRY_RUN" = 1 ] || command -v flyctl >/dev/null 2>&1 || { echo "ERROR: flyctl not found on PATH" >&2; exit 3; }
run() { echo "+ $*" >&2; [ "$DRY_RUN" = 1 ] || "$@"; }

case "$MODE" in
  flip)
    [ "${#SET_PAIRS[@]}" -gt 0 ] || { echo "ERROR: no tier/deployment-id given" >&2; exit 2; }
    echo "==> flipping ${#SET_PAIRS[@]} tier(s) on $GATEWAY_APP to dedicated deployments"
    for p in "${SET_PAIRS[@]}"; do echo "    ${p%%=*} -> ${p#*=}"; done
    run flyctl secrets set "${SET_PAIRS[@]}" --app "$GATEWAY_APP"
    ;;
  revert)
    [ "${#REVERT_TIERS[@]}" -gt 0 ] || REVERT_TIERS=(conductor oracle reasoner executor)
    UNSET_NAMES=()
    for t in "${REVERT_TIERS[@]}"; do
      slug_for "$t" >/dev/null || { echo "ERROR: unknown tier '$t'" >&2; exit 2; }
      UNSET_NAMES+=("$(secret_name "$t")")
    done
    echo "==> reverting ${#UNSET_NAMES[@]} tier(s) on $GATEWAY_APP to serverless"
    for n in "${UNSET_NAMES[@]}"; do echo "    unset $n"; done
    run flyctl secrets unset "${UNSET_NAMES[@]}" --app "$GATEWAY_APP"
    ;;
  status)
    echo "==> tier secrets on $GATEWAY_APP (present = flipped to dedicated; absent = serverless):"
    run flyctl secrets list --app "$GATEWAY_APP"
    ;;
  verify)
    cmd_verify
    ;;
  *) usage 2 ;;
esac
