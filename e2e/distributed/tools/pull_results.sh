#!/usr/bin/env bash
# pull_results.sh — standalone re-pull of a LIVE fleet's bundle by LABEL, for
# KEEP=1 debug runs (inspect a bundle before/without tearing the fleet down).
#
# Reuses the EXACT fleet-lookup + sftp-get + extract sequence
# run-distributed.sh runs inline before its own teardown (see its "pull the
# one merged bundle off the coordinator volume" section): same fleet metadata
# (role=coordinator, fleet=<LABEL>) lookup via `flyctl machines list --json`,
# same `flyctl ssh sftp get /data/bundle.tgz`, same tar extraction into
# out/dist-<LABEL>/bundle/. This script does NOT tear anything down — the
# fleet (and its $/hr billing) keeps running after it returns; teardown is
# run-distributed.sh's job (DESTROY_ONLY=1 LABEL=<LABEL> ./run-distributed.sh).
#
# Usage:
#   tools/pull_results.sh <LABEL> [APP]
#
# APP defaults to swebench-agent-dist (the econ/default fleet app, matching
# run-distributed.sh's DEFAULT_APP). Pass swebench-agent-dist-claude (arg 2)
# for the claude arm, or any other non-default app.
set -euo pipefail

LABEL="${1:?usage: pull_results.sh <LABEL> [APP]}"
APP="${2:-${APP:-swebench-agent-dist}}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"     # e2e/distributed
OUTDIR="$HERE/out/dist-$LABEL"
PY_HOST="${PYTHON:-python3}"

log() { printf '[pull_results] %s\n' "$*" >&2; }

# ── auth: fly token — prefer env, else the saved token (verbatim from run-distributed.sh) ──
if [ -z "${FLY_API_TOKEN:-}" ]; then
  FLY_API_TOKEN="$(node -e "const fs=require('fs');const y=fs.readFileSync(process.env.HOME+'/.fly/config.yml','utf8');const m=y.match(/access_token:\s*(\S+)/);process.stdout.write(m?m[1]:'')" 2>/dev/null || true)"
fi
export FLY_API_TOKEN
[ -n "$FLY_API_TOKEN" ] || { log "no fly token (run: flyctl auth login)"; exit 1; }

# ── find the coordinator machine (metadata role=coordinator, fleet=<LABEL>) ──
# same fleet_ids() lookup as run-distributed.sh, scoped to role=coordinator.
COORD_MID="$(flyctl machines list -a "$APP" --json 2>/dev/null | "$PY_HOST" -c '
import sys, json
label = sys.argv[1]
try:
    ms = json.load(sys.stdin)
except Exception:
    ms = []
for m in ms:
    md = (m.get("config") or {}).get("metadata") or {}
    if md.get("fleet") == label and md.get("role") == "coordinator":
        print(m.get("id"))
' "$LABEL" | head -1)"

if [ -z "$COORD_MID" ]; then
  log "no coordinator machine for fleet=$LABEL on app $APP — fleet already torn down (or never existed on this app)."
  if [ -d "$OUTDIR/bundle" ]; then
    log "bundle is already local: $OUTDIR/bundle/ — nothing to pull."
    exit 0
  fi
  log "no local bundle at $OUTDIR/bundle/ either — nothing to pull. (Different APP? pass it as \$2.)"
  exit 1
fi
log "coordinator: $COORD_MID (fleet=$LABEL, app=$APP)"

mkdir -p "$OUTDIR"
log "pulling /data/bundle.tgz via sftp"
# `fly ssh sftp get` REFUSES to overwrite — clear a stale bundle first (verbatim from run-distributed.sh).
rm -f "$OUTDIR/bundle.tgz"
flyctl ssh sftp get /data/bundle.tgz "$OUTDIR/bundle.tgz" -a "$APP" --machine "$COORD_MID" 2>&1 | tail -3 || \
  { log "sftp pull failed — is the coordinator still up and holding (HOLD)?"; exit 1; }

[ -f "$OUTDIR/bundle.tgz" ] || { log "sftp did not produce $OUTDIR/bundle.tgz"; exit 1; }
rm -rf "$OUTDIR/bundle"
mkdir -p "$OUTDIR/bundle"
tar xzf "$OUTDIR/bundle.tgz" -C "$OUTDIR/bundle"
log "extracted -> $OUTDIR/bundle/"
