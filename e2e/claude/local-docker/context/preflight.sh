#!/usr/bin/env bash
# Preflight: prove unerr actually runs and works INSIDE the instance image —
# BEFORE spending any tokens. No CLAUDE_CODE_OAUTH_TOKEN, no `claude -p`, $0.
#
# Parallel to e2e/codex/local-docker/context/preflight.sh, but checks the Claude
# Code install seam (.mcp.json + .claude/settings.json + CLAUDE.md) instead of
# codex's (.codex/config.toml + AGENTS.md). The MCP-path check (section 6) is
# identical — it drives a raw `unerr --mcp` session, which is agent-agnostic.
#
# Verifies, in order: toolbox binaries, native modules load, offline Pro
# entitlement, unerrd running, `unerr install claude-code`, and the MCP path
# (initialize -> tools/list -> file_read). Exits non-zero if any check fails.
#
#   docker run --rm -e REPO_DIR=/testbed unerr-claude-run:<id> /opt/toolbox/preflight.sh

set -uo pipefail
export PATH=/opt/toolbox/node/bin:/opt/toolbox/bin:$PATH
TOOLBOX=/opt/toolbox
REPO_DIR="${REPO_DIR:-/testbed}"
. "$TOOLBOX/lib.sh"

fails=0
ck() { # ck "<name>" <cmd...>
  local name="$1"; shift
  if "$@" >/tmp/ck.out 2>&1; then
    printf '[PASS] %s\n' "$name"
  else
    printf '[FAIL] %s\n' "$name"; sed 's/^/   /' /tmp/ck.out
    fails=$((fails + 1))
  fi
}

echo "=== e2e preflight — repo=$REPO_DIR (no token, zero cost) ==="

echo "--- 1. toolbox binaries ---"
ck "node present"   node --version
ck "claude present" claude --version
ck "unerr present"  unerr --version

echo "--- 2. unerr doctor (native cozo/sqlite modules load in this image) ---"
unerr doctor </dev/null >/tmp/doctor.out 2>&1 || true
sed -n '1,40p' /tmp/doctor.out | sed 's/^/   /'

echo "--- 3. offline Pro entitlement (no login, no cloud) ---"
unerr_offline_pro
ck "entitlement env exported" test -n "${UNERR_ENTITLEMENT_KID:-}"
node "$TOOLBOX/dev-entitlement.mjs" status </dev/null >/tmp/ent.out 2>&1 || true
sed 's/^/   /' /tmp/ent.out
# Hard-assert the FULL Pro tier, not just "an entitlement exists": plan=pro AND
# repos unlimited (max_active_repos:-1 in the signed cache). If either is wrong
# the cache resolved to free and every Pro feature is off — fail loudly here.
ck "effective tier is Pro"        grep -qi "plan=pro" /tmp/ent.out
ck "repo limit is unlimited (-1)" grep -qE '"max_active_repos"[: ]*-1' "$HOME/.unerr/entitlements.json"

echo "--- 4. unerrd running (started AFTER entitlement, so it honors Pro) ---"
unerr_start_daemon </dev/null 2>&1 | sed 's/^/   /' || true
ck "unerrd socket up" unerr_daemon_up

echo "--- 5. unerr install claude-code (per-repo MCP + hooks + CLAUDE.md) ---"
cd "$REPO_DIR" || { echo "[FAIL] no repo at $REPO_DIR"; exit 1; }
ck "install claude-code runs"     unerr install claude-code
ck ".mcp.json written"            test -f "$REPO_DIR/.mcp.json"
ck "mcp config references unerr"   grep -qi unerr "$REPO_DIR/.mcp.json"
ck ".claude/settings.json written" test -f "$REPO_DIR/.claude/settings.json"
ck "CLAUDE.md written"            test -f "$REPO_DIR/CLAUDE.md"

echo "--- 6. MCP tools work (initialize -> tools/list -> file_read) ---"
PROBE_FILE="$(cd "$REPO_DIR" && git ls-files 2>/dev/null | grep -E '\.(py|js|ts|tsx|md|txt|rb|go|java|c|h|cpp)$' | head -1)"
echo "   probe file: ${PROBE_FILE:-<none found>}"
if node "$TOOLBOX/mcp-healthcheck.mjs" "$REPO_DIR" "$(command -v unerr)" "$PROBE_FILE" 45000; then
  :
else
  fails=$((fails + 1))
fi

echo "=== preflight summary: $([ "$fails" -eq 0 ] && echo 'ALL PASS ✓' || echo "$fails CHECK(S) FAILED ✗") ==="
exit "$fails"
