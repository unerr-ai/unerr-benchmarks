#!/usr/bin/env bash
# In-container driver. Runs INSIDE a derived instance image (official SWE-bench
# env + the grafted econ toolbox). Produces the SWE-bench prediction: a unified
# diff of econ's edits, to stdout.
#
# SINGLE-ARM: unerr is compiled INTO econ (packages/code-intelligence), so there
# is NO unerr install, NO unerrd daemon, NO offline-Pro entitlement, NO MCP
# wiring, and NO on/off flip. econ itself IS the (only) arm. Contrast the
# codex/claude drivers, which attach unerr as an external MCP server per arm.
#
# Env in:
#   LITELLM_API_KEY      required — econ routes its model tiers via the
#                         self-hosted LiteLLM gateway
#   ECON_TIMEOUT         per-instance seconds (default 1800)
#   REPO_DIR             repo root in the image (SWE-bench default: /testbed)
# Args:
#   $1                   path to a file holding the problem_statement
#
# stdout = the patch (nothing else). All logs go to stderr.

set -uo pipefail
TOOLBOX=/opt/toolbox
OUT=/work-out          # host artifact dir mounted by run-benchmark.py

REPO_DIR="${REPO_DIR:-/testbed}"
ECON_TIMEOUT="${ECON_TIMEOUT:-1800}"
PROBLEM_FILE="${1:?usage: run-instance.sh <problem_statement_file>}"

log() { printf '[run-instance] %s\n' "$*" >&2; }

mkdir -p "$OUT"

cd "$REPO_DIR" || { log "no repo at $REPO_DIR"; exit 2; }
git config --global --add safe.directory "$REPO_DIR" >/dev/null 2>&1 || true
# Clean any leftover state so the diff reflects only this run's edits (the image
# is already at base_commit).
git checkout -- . >/dev/null 2>&1 || true
git clean -fdq >/dev/null 2>&1 || true

# ── Graft econ config so tiered routing activates ────────────────────────────
# econ picks up opencode.json + .opencode/ from the repo root (its --dir). Copy
# them off the toolbox into the repo so the conductor/oracle/executor tiers and
# plugins load. These are stripped from the prediction diff below.
cp "$TOOLBOX/opencode.json" "$REPO_DIR/opencode.json" 2>/dev/null || true
if [ -d "$TOOLBOX/.opencode" ]; then
  rm -rf "$REPO_DIR/.opencode"
  cp -R "$TOOLBOX/.opencode" "$REPO_DIR/.opencode" 2>/dev/null || true
fi

# ── econ session DB on the mounted artifact dir, so it survives the container ─
export OPENCODE_DB="$OUT/opencode.db"
rm -f "$OPENCODE_DB"

PROBLEM="$(cat "$PROBLEM_FILE")"

log "econ run starting (repo=$REPO_DIR, timeout=${ECON_TIMEOUT}s)"
# Container is the sandbox → --dangerously-skip-permissions for full autonomy.
# --format json: machine-readable event stream (token/turn/tier telemetry).
# NO --model: routing is opencode.json-driven. All output to the mounted dir.
timeout "$ECON_TIMEOUT" "$TOOLBOX/opencode" run \
  --format json \
  --dir "$REPO_DIR" \
  --dangerously-skip-permissions \
  "$PROBLEM" \
  > "$OUT/events.jsonl" 2> "$OUT/err.txt"
ECON_RC=$?
log "econ exit=$ECON_RC"
[ "$ECON_RC" -ne 0 ] && sed 's/^/[econ.err] /' "$OUT/err.txt" | tail -30 >&2

# ── Capture the top-level sessionID (report.py / econ-tier-cost.py want it) ───
SID="$(python3 -c "
import json,sys
for l in open('$OUT/events.jsonl'):
    l=l.strip()
    if not l: continue
    try: s=json.loads(l).get('sessionID')
    except: continue
    if s: print(s); break
" 2>/dev/null)"
printf '%s' "${SID:-}" > "$OUT/session_id.txt"
log "sessionID=${SID:-<none>}"

# ── Prediction = working-tree diff vs base_commit, EXCLUDING econ's footprint ─
# The grafted opencode.json / .opencode / opencode.db (and any AGENTS.md econ
# writes) are NOT the model's fix — left in, they pollute model_patch and break
# grading. Strip them from the diff.
git add -A >/dev/null 2>&1 || true
git -C "$REPO_DIR" diff --cached -- . \
  ':(exclude)opencode.json' ':(exclude).opencode' \
  ':(exclude)opencode.db' ':(exclude)AGENTS.md'
