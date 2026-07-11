#!/usr/bin/env bash
# Fly machine entrypoint: run the Claude preflight (zero cost, no token), then
# HOLD the machine open (sleep infinity) so the built image + installed
# claude/unerr + seeded repo stay reusable for further setup testing.
#
# Re-run the preflight anytime without rebuilding:
#   flyctl ssh console -a unerr-bench-claude -C /opt/toolbox/preflight.sh
set -uo pipefail
export PATH=/opt/toolbox/bin:$PATH
export TOOLBOX=/opt/toolbox
export HOME=/work
export REPO_DIR="${REPO_DIR:-/testbed}"
mkdir -p /work
log() { printf '[entrypoint] %s\n' "$*" >&2; }

log "=== Claude fly preflight (zero cost, no token) — repo=$REPO_DIR ==="
/opt/toolbox/preflight.sh
RC=$?
log "preflight exit=$RC"
echo "PREFLIGHT_EXIT=$RC"
echo "$RC" > /work/last-preflight-rc 2>/dev/null || true

# Keep the machine ALIVE so nothing is lost — the user wants to reuse this setup.
log "preflight done — holding machine open (sleep infinity). Stop with: flyctl machine stop"
exec sleep infinity
