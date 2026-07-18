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
#   REPO_DIR             repo root in the image (SWE-bench default: /testbed)
# Args:
#   $1                   path to a file holding the problem_statement
#
# stdout = the patch (nothing else). All logs go to stderr.

set -uo pipefail
TOOLBOX=/opt/toolbox
OUT=/work-out          # host artifact dir mounted by run-benchmark.py

REPO_DIR="${REPO_DIR:-/testbed}"
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

# Start err.txt empty so the [graph-init] marker below (and opencode run's
# appended stderr further down) land together in the file engine.log tails —
# graph-init's log() line alone never reaches the bundle otherwise.
: > "$OUT/err.txt"

# Warm the code-graph up front (mirrors the claude arm's `unerr index`) so the
# first agent turn starts on a ready graph instead of cold-indexing mid-request.
# No --force: warm-starts off the persisted per-repo graph cache (see
# run-benchmark.py's GRAPH_CACHE_ROOT mount) and reconciles changed/added/
# deleted files by content hash instead of re-indexing the whole repo.
if timeout 600 "$TOOLBOX/opencode" init "$REPO_DIR" --json >/tmp/opencode-init.log 2>&1; then
  log "graph init: ok ($(grep -oE '\"entities\":[0-9]+' /tmp/opencode-init.log | head -1) $(grep -oE '\"edges\":[0-9]+' /tmp/opencode-init.log | head -1))"
  printf '[graph-init] %s\n' "$(tr -d "\n" < /tmp/opencode-init.log)" >> "$OUT/err.txt"
else
  log "graph init: FAILED/timeout (see /tmp/opencode-init.log) — agent may cold-index on first tool call"
  printf '[graph-init] FAILED/timeout: %s\n' "$(tr -d "\n" < /tmp/opencode-init.log)" >> "$OUT/err.txt"
fi

PROBLEM="$(cat "$PROBLEM_FILE")"

log "econ run starting (repo=$REPO_DIR)"
# Container is the sandbox → --dangerously-skip-permissions for full autonomy.
# --format json: machine-readable event stream (token/turn/tier telemetry).
# --print-logs: without it the Effect logger only writes opencode.log inside
# the container (packages/core/src/observability/logging.ts loggers()) — the
# orchestration markers (turn_converge_nudge, stuck_escalate, ...) never reach
# stderr/err.txt otherwise. NO --model: routing is opencode.json-driven.
"$TOOLBOX/opencode" run \
  --format json \
  --print-logs \
  --dir "$REPO_DIR" \
  --dangerously-skip-permissions \
  "$PROBLEM" \
  > "$OUT/events.jsonl" 2>> "$OUT/err.txt"
ECON_RC=$?
log "econ exit=$ECON_RC"
[ "$ECON_RC" -ne 0 ] && sed 's/^/[econ.err] /' "$OUT/err.txt" | tail -30 >&2

# ── Full engine log for finish-path forensics, tail-capped ──────────────────
# err.txt (above) is the raw uncapped stderr other tooling already keys on
# (the failure dump just above, worker-loop.py's _read_artifacts). engine.log
# is the artifact this pipeline is built for: same content, capped to the last
# 10MB (tail semantics — the finish markers land at the end of a long run).
ENGINE_LOG_MAX_BYTES=10000000
tail -c "$ENGINE_LOG_MAX_BYTES" "$OUT/err.txt" > "$OUT/engine.log" 2>/dev/null || true

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
  ':(exclude)opencode.db' ':(exclude)AGENTS.md' \
  ':(exclude)repro_issue.*'
