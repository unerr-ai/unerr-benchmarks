#!/usr/bin/env bash
# score.sh — Full scoring pipeline for the Claude local-docker A/B benchmark
#
# Runs three steps in sequence:
#   1. SWE-bench grader for each arm (on / off)
#   2. score.mjs — joins meta JSONL + grader verdicts → arm-on.jsonl / arm-off.jsonl
#   3. tsx e2e/common/scoring/run.ts — SWE-Effi A/B report → e2e/common/results/ab-report.md
#
# NOTE ON LOCALIZATION F1:
#   This backend (local-docker full-resolve) does NOT produce file-localization
#   predictions. Localization F1 is the fly-remote loc-runner metric and is
#   out of scope here. Metrics produced: resolve rate + SWE-Effi (resolve vs
#   tokens / usd).
#
# Usage:
#   RESULTS_DIR=results/<label> DATASET=princeton-nlp/SWE-bench_Verified ./score.sh
#
# Or pass args directly:
#   ./score.sh <results-dir> [dataset] [split]
#
# Required env / args:
#   RESULTS_DIR (or $1) — directory containing preds_on.json, preds_off.json,
#                          meta_on.jsonl, meta_off.jsonl produced by the Claude runner.
#   DATASET     (or $2) — HuggingFace dataset id (default: princeton-nlp/SWE-bench_Verified)
#   SPLIT       (or $3) — dataset split (default: test)
#
# Prerequisites:
#   - swebench installed in the active Python env  (pip install swebench)
#   - Docker running (grader uses it for instance evaluation)
#   - tsx installed globally or via npx  (for the shared SWE-Effi scorer)
#   - Node ≥ 18  (for score.mjs)
#
# Output:
#   <RESULTS_DIR>/arm-on.jsonl   — trajectory JSONL for the "on" (treatment) arm
#   <RESULTS_DIR>/arm-off.jsonl  — trajectory JSONL for the "off" (baseline) arm
#   e2e/common/results/ab-report.md — SWE-Effi A/B report (markdown)
#
# Grader output files:
#   The SWE-bench harness writes <model_name_or_path>.<run_id>.json in the
#   current working directory. This script captures those paths and passes them
#   to score.mjs automatically.
#   - On arm: claude-on.claude_on.json
#   - Off arm: claude-off.claude_off.json

set -euo pipefail

# ---------------------------------------------------------------------------
# Args / env
# ---------------------------------------------------------------------------

RESULTS_DIR="${1:-${RESULTS_DIR:-}}"
DATASET="${2:-${DATASET:-princeton-nlp/SWE-bench_Verified}}"
SPLIT="${3:-${SPLIT:-test}}"

if [[ -z "$RESULTS_DIR" ]]; then
  echo "usage: RESULTS_DIR=results/<label> ./score.sh" >&2
  echo "   or: ./score.sh <results-dir> [dataset] [split]" >&2
  exit 2
fi

RESULTS_DIR="$(cd "$RESULTS_DIR" && pwd)"   # make absolute

# Script location — used to resolve sibling score.mjs and common/scoring
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
COMMON_SCORING="$REPO_ROOT/e2e/common/scoring/run.ts"

echo "[score.sh] results-dir : $RESULTS_DIR"
echo "[score.sh] dataset     : $DATASET"
echo "[score.sh] split       : $SPLIT"
echo "[score.sh] repo-root   : $REPO_ROOT"

# ---------------------------------------------------------------------------
# Guard: required input files
# ---------------------------------------------------------------------------

for f in preds_on.json preds_off.json meta_on.jsonl meta_off.jsonl; do
  if [[ ! -f "$RESULTS_DIR/$f" ]]; then
    echo "ERROR: $RESULTS_DIR/$f not found — run the Claude benchmark first" >&2
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# Step 1 — SWE-bench grader for each arm
#
# The harness writes <model_name_or_path>.<run_id>.json in $PWD.
# preds_on.json contains model_name_or_path="claude-on"  → claude-on.claude_on.json
# preds_off.json contains model_name_or_path="claude-off" → claude-off.claude_off.json
#
# We run the grader from RESULTS_DIR so the output lands there (alongside preds).
# ---------------------------------------------------------------------------

echo ""
echo "=== Step 1: SWE-bench grader (on arm) ==="
(
  cd "$RESULTS_DIR"
  python -m swebench.harness.run_evaluation \
    --dataset_name "$DATASET" \
    --split "$SPLIT" \
    --predictions_path preds_on.json \
    --run_id claude_on \
    --max_workers 4 \
    --cache_level env
)
GRADER_ON="$RESULTS_DIR/claude-on.claude_on.json"

echo ""
echo "=== Step 1: SWE-bench grader (off arm) ==="
(
  cd "$RESULTS_DIR"
  python -m swebench.harness.run_evaluation \
    --dataset_name "$DATASET" \
    --split "$SPLIT" \
    --predictions_path preds_off.json \
    --run_id claude_off \
    --max_workers 4 \
    --cache_level env
)
GRADER_OFF="$RESULTS_DIR/claude-off.claude_off.json"

# Verify grader output files exist
for f in "$GRADER_ON" "$GRADER_OFF"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: grader output not found at $f" >&2
    echo "  The SWE-bench harness may have placed it elsewhere." >&2
    echo "  Check the current directory and RESULTS_DIR for *.json files." >&2
    echo "  Then run score.mjs manually:" >&2
    echo "    node $SCRIPT_DIR/score.mjs $RESULTS_DIR <grader-on.json> <grader-off.json>" >&2
    exit 1
  fi
done

echo "[score.sh] grader-on  : $GRADER_ON"
echo "[score.sh] grader-off : $GRADER_OFF"

# ---------------------------------------------------------------------------
# Step 2 — Build arm JSONL (adapter: meta + grader verdicts → Trajectory shape)
# ---------------------------------------------------------------------------

echo ""
echo "=== Step 2: build arm-on.jsonl / arm-off.jsonl ==="
node "$SCRIPT_DIR/score.mjs" \
  "$RESULTS_DIR" \
  "$GRADER_ON" \
  "$GRADER_OFF" \
  "$RESULTS_DIR"

ARM_ON="$RESULTS_DIR/arm-on.jsonl"
ARM_OFF="$RESULTS_DIR/arm-off.jsonl"

for f in "$ARM_ON" "$ARM_OFF"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: score.mjs did not produce $f" >&2
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# Step 3 — SWE-Effi A/B report (shared scorer)
#
# Baseline = off arm (claude without unerr MCP tools)
# Treatment = on arm (claude with unerr MCP tools)
# Output → e2e/common/results/ab-report.md
# ---------------------------------------------------------------------------

echo ""
echo "=== Step 3: SWE-Effi A/B report ==="
if ! command -v tsx &>/dev/null; then
  echo "[score.sh] tsx not in PATH — trying npx tsx" >&2
  TSX_CMD="npx tsx"
else
  TSX_CMD="tsx"
fi

$TSX_CMD "$COMMON_SCORING" "$ARM_OFF" "$ARM_ON"

echo ""
echo "=== Done ==="
echo "A/B report : $REPO_ROOT/e2e/common/results/ab-report.md"
echo "arm-off    : $ARM_OFF   (baseline: claude without unerr)"
echo "arm-on     : $ARM_ON    (treatment: claude with unerr)"
