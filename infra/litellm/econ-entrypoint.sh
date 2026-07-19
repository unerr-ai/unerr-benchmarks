#!/usr/bin/env sh
# econ gateway entrypoint wrapper — OPTIONAL per-tier flip to dedicated Fireworks
# deployments, chained in front of the base litellm image entrypoint.
#
# Default (no *_DEPLOYMENT_PATH secret set): a no-op — config.yaml is served
# verbatim (every tier on Fireworks SERVERLESS), so nothing about the committed
# dumb-pipe config or the OL-8.B2 drift test changes. The flip is done at RUNTIME,
# in the container's copy of config.yaml only, never in the repo file.
#
# When a <TIER>_DEPLOYMENT_PATH secret is set (by gpu-flip.sh via `fly secrets set`
# on the econ-litellm app), rewrite ONLY that tier's litellm_params.model line
# (serverless base model -> the dedicated `#deployments/...` form) before launching.
# Tiers map to Fireworks base-model slugs exactly as ECON_TIER_BINDING does in econ's
# model-registry.ts (the drift-tested source of truth):
#     conductor -> minimax-m3        oracle   -> glm-5p2
#     reasoner  -> deepseek-v4-pro   executor -> gpt-oss-120b
# gpu-flip.sh sets the secret when the caller's dedicated GPU is up and unsets it at
# teardown, so the gateway auto-reverts to serverless. CONDUCTOR_DEPLOYMENT_PATH keeps
# its original single-tier behaviour (back-compat with DEDICATED_CONDUCTOR runs).
#
# NOTE: LiteLLM resolves a fireworks model's supported params from the model STRING,
# so a dedicated `#deployments/...` path used to lose tool_choice/reasoning_effort →
# UnsupportedParamsError 400s with drop_params:false (2026-07-13), and the interim
# per-tier `drop_params: true` injection that stopped the /responses 400s silently
# STRIPPED those params there instead — tool-call behaviour differed between
# serverless and dedicated. FIXED at the IMAGE level since 2026-07-19:
# patches/fireworks_supported_params.py (appended by the Dockerfile) resolves
# capabilities from the base-model half of the string, so dedicated == serverless on
# BOTH /chat/completions and /responses and this wrapper injects NOTHING — the flip
# is a pure model-line swap. allowed_openai_params stays in config.yaml as a
# harmless extra belt on /chat/completions.
#
# We CHAIN the base image entrypoint (/app/docker/prod_entrypoint.sh) rather than exec
# litellm directly so its startup work (prisma / spend-logs DB setup) still runs.
set -eu

CONFIG="${ECON_CONFIG_PATH:-/app/config.yaml}"

# Flip one tier's model line to its dedicated deployment path, if that path is set.
# $1=tier label (logs)  $2=fireworks base-model slug  $3=deployment path.
# Anchored end-of-line match so ONLY the serverless base-model line is touched, never
# the model_name slug or another tier. `|` sed delimiter because the dedicated path
# contains `#` and `/` (but never `|`).
flip_tier() {
  _tier="$1"; _slug="$2"; _path="$3"
  [ -n "$_path" ] || return 0
  _re="^[[:space:]]*model:[[:space:]]*fireworks_ai/accounts/fireworks/models/${_slug}[[:space:]]*\$"
  if grep -qE "$_re" "$CONFIG"; then
    # Pure model-line swap — no drop_params injection; the image-level param patch
    # (see header NOTE) makes the dedicated path resolve the serverless capability set.
    sed -i -E "s|^([[:space:]]*)model:[[:space:]]*fireworks_ai/accounts/fireworks/models/${_slug}[[:space:]]*\$|\\1model: ${_path}|" "$CONFIG"
    echo "[econ-entrypoint] ${_tier} flipped to DEDICATED: ${_path}"
  else
    echo "[econ-entrypoint] WARN: ${_tier}_DEPLOYMENT_PATH set but the serverless ${_tier} line (${_slug}) was not found in $CONFIG — serving as-is" >&2
  fi
}

# tier:slug:path — path LAST (it contains no ':' , only '#' and '/'). POSIX sh, no arrays.
FLIPPED=0
for _spec in \
  "conductor:minimax-m3:${CONDUCTOR_DEPLOYMENT_PATH:-}" \
  "oracle:glm-5p2:${ORACLE_DEPLOYMENT_PATH:-}" \
  "reasoner:deepseek-v4-pro:${REASONER_DEPLOYMENT_PATH:-}" \
  "executor:gpt-oss-120b:${EXECUTOR_DEPLOYMENT_PATH:-}" \
; do
  _t=$(printf '%s' "$_spec" | cut -d: -f1)
  _s=$(printf '%s' "$_spec" | cut -d: -f2)
  _p=$(printf '%s' "$_spec" | cut -d: -f3-)
  [ -n "$_p" ] || continue
  flip_tier "$_t" "$_s" "$_p"
  FLIPPED=1
done
[ "$FLIPPED" = 0 ] && echo "[econ-entrypoint] all tiers on serverless (no *_DEPLOYMENT_PATH secrets set)"

# Hand off to the base image's REAL server entrypoint. In the berriai/litellm image
# `docker/prod_entrypoint.sh` is the ENTRYPOINT (`exec litellm "$@"`, which runs the
# prisma migration internally on boot then serves); `docker/entrypoint.sh` is a
# migration-ONLY helper that exits without starting the server — do NOT chain to it.
if [ -x /app/docker/prod_entrypoint.sh ]; then
  exec /app/docker/prod_entrypoint.sh "$@"
else
  exec litellm "$@"
fi
