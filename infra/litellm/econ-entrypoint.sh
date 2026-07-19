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
# NOTE: a dedicated deployment's param support cannot be resolved by LiteLLM, so with
# drop_params:false it REJECTS tool_choice/reasoning_effort (UnsupportedParamsError) and
# that tier's calls fail. TWO layers guard this: (1) the flipped tiers' config entries
# carry allowed_openai_params:["tool_choice","reasoning_effort"] (already on minimax-m3;
# added to glm-5p2 + deepseek-v4-pro) — but LiteLLM ONLY honors that on /chat/completions,
# NOT on the /responses endpoint (opencode's default surface: it 400s every call, which is
# what broke the terminal-bench econ arm — 0 tokens, 0 resolved). So (2) flip_tier ALSO
# injects a per-tier `drop_params: true` below, which DOES take effect on /responses (it
# drops the unsupported params instead of 400ing). allowed_openai_params still wins on
# /chat/completions, so econ's reasoning_effort pins ride through untouched there. Harmless
# on serverless (never flipped); global litellm_settings.drop_params stays false so
# non-flipped tiers are unaffected.
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
    # Flip the model line to the dedicated path AND inject a sibling `drop_params: true`
    # into the same litellm_params block (\1 = leading whitespace, matching indent) so the
    # dedicated deployment's calls survive the /responses endpoint (see header NOTE). GNU
    # sed (the Debian base image) expands \n in the replacement to a newline.
    sed -i -E "s|^([[:space:]]*)model:[[:space:]]*fireworks_ai/accounts/fireworks/models/${_slug}[[:space:]]*\$|\\1model: ${_path}\\n\\1drop_params: true|" "$CONFIG"
    echo "[econ-entrypoint] ${_tier} flipped to DEDICATED (+drop_params:true): ${_path}"
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
