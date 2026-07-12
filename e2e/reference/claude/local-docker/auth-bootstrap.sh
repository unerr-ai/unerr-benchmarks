#!/usr/bin/env bash
# Mint a long-lived Claude subscription token for headless benchmark runs —
# NO API key. Run this ONCE on your laptop. It uses `claude setup-token`, which
# performs an OAuth login against your claude.ai Pro/Max account and prints a
# long-lived token (CLAUDE_CODE_OAUTH_TOKEN). Billing goes to your subscription.
#
# Why a token (not just your interactive login)? The benchmark passes the token
# into Docker containers via `docker run -e`. The macOS Keychain (where an
# interactive `claude` login stores creds) cannot be mounted into a container,
# so the env-var token is the portable path. ToS permits this for ordinary
# individual use on a personal machine.
#
# Usage:
#   ./auth-bootstrap.sh            # interactive: mints token, writes .env.local
#   source ./auth-bootstrap.sh     # also exports it into your current shell
#
# Then run the benchmark in the same shell (or `set -a; . .env.local; set +a`).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ENV_FILE="$HERE/.env.local"   # gitignored

command -v claude >/dev/null 2>&1 || { echo "claude CLI not found on PATH (install @anthropic-ai/claude-code)"; return 1 2>/dev/null || exit 1; }

if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  echo "CLAUDE_CODE_OAUTH_TOKEN already set in this shell — nothing to do."
  return 0 2>/dev/null || exit 0
fi

echo "==> Running 'claude setup-token' (a browser/OAuth login may open)."
echo "    Log in with the Pro/Max account you want to bill the benchmark to."
TOKEN="$(claude setup-token | tail -n1 | tr -d '[:space:]')"

if [ -z "$TOKEN" ]; then
  echo "No token captured. If setup-token printed a token above, copy it and run:"
  echo "    export CLAUDE_CODE_OAUTH_TOKEN=<token>"
  return 1 2>/dev/null || exit 1
fi

umask 077
printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$TOKEN" > "$ENV_FILE"
echo "==> wrote $ENV_FILE (chmod 600, gitignored)"
echo "==> load it before a run:   set -a; . $ENV_FILE; set +a"

# If sourced, export into the caller's shell too.
export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN"
echo "==> exported CLAUDE_CODE_OAUTH_TOKEN into this shell"
echo
echo "Tokens expire eventually — re-run this script to re-mint when a run 401s."
