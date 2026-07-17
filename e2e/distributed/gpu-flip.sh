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
# Usage:
#   ./gpu-flip.sh --conductor <dep-id> [--oracle <id>] [--reasoner <id>] [--executor <id>]
#   ./gpu-flip.sh --revert [--conductor] [--oracle] [--reasoner] [--executor]  # revert named tiers
#   ./gpu-flip.sh --serverless        # revert ALL tiers to serverless
#   ./gpu-flip.sh --status            # show which tier secrets are currently set
#   Prefix any command with --dry-run to print the flyctl call without running it.
#   NOTE: put --revert BEFORE the tier flags you want reverted.
#
# Env:
#   GATEWAY_APP   fly app for the gateway   (default econ-litellm)
#   FW_ACCOUNT    Fireworks account slug    (default accounts/vamsee-k-ra566yo1je2)
set -euo pipefail

GATEWAY_APP="${GATEWAY_APP:-econ-litellm}"
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
usage() { sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

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
  *) usage 2 ;;
esac
