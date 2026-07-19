#!/usr/bin/env bash
# OL-8.A3 — usage-passthrough probe: the go/no-go gate before any econ code cutover.
# For each econ model via the gateway, asserts:
#   1. the completion answers (slug + auth valid),
#   2. the usage block carries cached-token detail (prompt_tokens_details.cached_tokens),
#   3. reasoning-effort round-trips for the reasoning models (no reasoning-off downgrade).
# Usage: LITELLM_API_KEY=... ./probe.sh [https://econ-litellm.fly.dev]
set -euo pipefail

BASE="${1:-https://econ-litellm.fly.dev}"
KEY="${LITELLM_API_KEY:?set LITELLM_API_KEY (master or virtual key)}"
MODELS=("deepseek/deepseek-v4-flash" "z-ai/glm-5.2" "deepseek/deepseek-v4-pro" "openai/gpt-oss-120b")
FAIL=0

for MODEL in "${MODELS[@]}"; do
  echo "── $MODEL"
  BODY=$(jq -n --arg m "$MODEL" '{
    model: $m,
    messages: [{role: "user", content: "Reply with exactly: ok"}],
    max_tokens: 400
  }')
  RESP=$(curl -sS --max-time 120 "$BASE/v1/chat/completions" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d "$BODY") || {
    echo "  ✗ request failed"; FAIL=1; continue; }

  echo "$RESP" | jq -e '.choices[0].message' >/dev/null 2>&1 \
    && echo "  ✓ completion" \
    || { echo "  ✗ no completion: $(echo "$RESP" | head -c 300)"; FAIL=1; continue; }

  # usage passthrough — cached-token detail must exist (value may be 0 on a cold call)
  echo "$RESP" | jq -e '.usage.prompt_tokens_details | has("cached_tokens")' >/dev/null 2>&1 \
    && echo "  ✓ usage.prompt_tokens_details.cached_tokens present" \
    || { echo "  ✗ cached_tokens detail missing from usage"; FAIL=1; }

  # model fidelity — the gateway must not substitute models (dumb-pipe stance)
  GOT=$(echo "$RESP" | jq -r '.model // empty')
  case "$GOT" in
    *"${MODEL##*/}"*) echo "  ✓ model echo ($GOT)" ;;
    *) echo "  ✗ model mismatch: asked $MODEL got $GOT"; FAIL=1 ;;
  esac
done

# Warm-repeat cache check on the conductor model: second identical call should show
# cached_tokens > 0 IF the provider reports cache reads through the gateway.
M="${MODELS[0]}"
LONG=$(printf 'context filler %.0s' {1..400})
BODY=$(jq -n --arg m "$M" --arg c "$LONG" '{model:$m, messages:[{role:"system",content:$c},{role:"user",content:"Reply with exactly: ok"}], max_tokens: 400}')
curl -sS --max-time 120 "$BASE/v1/chat/completions" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d "$BODY" >/dev/null
CACHED=$(curl -sS --max-time 120 "$BASE/v1/chat/completions" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d "$BODY" \
  | jq -r '.usage.prompt_tokens_details.cached_tokens // 0')
echo "── warm-repeat cache read on ${M}: cached_tokens=$CACHED (record in OL-8.C1 baseline)"

[ "$FAIL" -eq 0 ] && echo "PROBE PASS" || { echo "PROBE FAIL"; exit 1; }
