#!/usr/bin/env bash
# Fly machine entrypoint: wire codex (±unerr), mint offline-Pro entitlement,
# start unerrd, then run the localization A/B. Results -> stdout (JSONL) + volume.
set -uo pipefail
export PATH=/opt/toolbox/bin:$PATH
export HOME=/work
export TOOLBOX=/opt/toolbox
. /opt/toolbox/lib.sh
log() { printf '[entrypoint] %s\n' "$*" >&2; }

: "${OPENAI_API_KEY:?OPENAI_API_KEY required}"
mkdir -p /work/results

# --- offline Pro entitlement + daemon (for the unerr arm) ---
unerr_offline_pro
log "entitlement: ${UNERR_ENTITLEMENT_KID:-<none>} (offline pro)"
unerr_start_daemon >/tmp/unerrd.log 2>&1 && unerr_daemon_up && log "unerrd: up" || log "unerrd: start issue (see /tmp/unerrd.log)"

# --- codex BASELINE home (no mcp) ---
export CODEX_HOME_BASE=/work/codex-base
mkdir -p "$CODEX_HOME_BASE"
cat > "$CODEX_HOME_BASE/config.toml" <<TOML
model = "gpt-5.4-mini"
model_reasoning_effort = "medium"
TOML
printf '%s' "$OPENAI_API_KEY" | CODEX_HOME="$CODEX_HOME_BASE" codex login --with-api-key >/dev/null 2>&1 && log "codex base login ok" || log "codex base login FAILED"

# --- codex UNERR home (unerr mcp + hooks) ---
export CODEX_HOME_UNERR=/work/codex-unerr
mkdir -p "$CODEX_HOME_UNERR"
cat > "$CODEX_HOME_UNERR/config.toml" <<TOML
model = "gpt-5.4-mini"
model_reasoning_effort = "medium"

[mcp_servers.unerr]
command = "/opt/toolbox/bin/unerr"
args = ["--mcp", "--coding-agent=codex"]
TOML
printf '%s' "$OPENAI_API_KEY" | CODEX_HOME="$CODEX_HOME_UNERR" codex login --with-api-key >/dev/null 2>&1 && log "codex unerr login ok" || log "codex unerr login FAILED"

log "starting localization A/B (SELECT=${SELECT:-default} LIMIT=${LIMIT:-all} ARMS=${ARMS:-baseline,unerr})"
node /app/loc-runner.mjs
RC=$?
log "runner exit=$RC — results at /work/results/loc-results.jsonl"
exit $RC
