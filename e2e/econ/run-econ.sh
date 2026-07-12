#!/usr/bin/env bash
# e2e/econ/run-econ.sh — single-arm benchmark runner: econ (OpenCode fork) on SWE-bench.
#
# SINGLE ARM, not A/B: unerr is compiled directly INTO econ — it lives in
# packages/code-intelligence and is registered unconditionally in
# packages/opencode/src/tool/registry.ts. There is no on/off flip, no
# `unerr install`, no unerrd daemon, no offline-Pro entitlement, and no MCP
# wiring step (unlike the codex/claude runners, which attach unerr as an
# external MCP server per arm). This script just runs econ once per instance
# and measures resolve rate + cost — econ itself IS the (only) arm.
#
# Prereqs:
#   - econ built (or runnable via bun) at ../../econ-coding-agent
#   - swebench Python harness: pip install datasets swebench
#   - LITELLM_API_KEY set — econ routes its conductor/oracle/executor model
#     tiers via the self-hosted LiteLLM gateway per its own opencode.json
#     config. No login step, no --model flag: the model is chosen by econ's
#     config, not by this script.
#
# Usage:
#   bash run-econ.sh [--instances N] [--slice START:END] [--dataset HF_ID]
#                     [--repo-dir PATH] [--label NAME] [--timeout SECONDS]
#                     [--preflight]
#
# stdout = progress log. Results land in ./results/.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# econ-coding-agent is a SIBLING of this benchmark repo (…/IdeaProjects/econ-coding-agent),
# i.e. three levels up from e2e/econ. Override with ECON_REPO if it lives elsewhere.
ECON_REPO="${ECON_REPO:-$SCRIPT_DIR/../../../econ-coding-agent}"

# ── Defaults ──────────────────────────────────────────────────────────────────
INSTANCES="${INSTANCES:-1}"
SLICE="${SLICE:-0:50}"
DATASET="${DATASET:-princeton-nlp/SWE-bench_Verified}"
REPO_DIR="${REPO_DIR:-/testbed}"
LABEL="${LABEL:-econ}"
ECON_TIMEOUT="${ECON_TIMEOUT:-1800}"
PREFLIGHT_ONLY="${PREFLIGHT_ONLY:-0}"
RESULTS_DIR="$SCRIPT_DIR/results"
ARTIFACTS_DIR="$RESULTS_DIR/artifacts"

# ECON_BIN: override wholesale via env. May be multi-word (e.g. a `bun run …`
# invocation) — it's split on whitespace in resolve_econ below, so keep any
# override flag-free (pass flags on the econ invocation line instead).
ECON_BIN="${ECON_BIN:-}"

# ── .env.local fallback for LITELLM_API_KEY ───────────────────────────────────
# Preflight checks LITELLM_API_KEY presence; this just gives it a fallback
# source when the caller hasn't exported it into the shell already.
if [ -z "${LITELLM_API_KEY:-}" ] && [ -f "$SCRIPT_DIR/.env.local" ]; then
  LITELLM_API_KEY="$(grep -E '^LITELLM_API_KEY=' "$SCRIPT_DIR/.env.local" | head -1 | sed 's/^LITELLM_API_KEY=//; s/^["'"'"']//; s/["'"'"']$//')"
  export LITELLM_API_KEY
fi

# ── Arg parsing ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --instances)  INSTANCES="$2";    shift 2 ;;
    --slice)      SLICE="$2";        shift 2 ;;
    --dataset)    DATASET="$2";      shift 2 ;;
    --repo-dir)   REPO_DIR="$2";     shift 2 ;;
    --label)      LABEL="$2";        shift 2 ;;
    --timeout)    ECON_TIMEOUT="$2"; shift 2 ;;
    --preflight)  PREFLIGHT_ONLY=1;  shift   ;;
    *) echo "[run-econ] unknown arg: $1" >&2; exit 1 ;;
  esac
done

log() { printf '[run-econ] %s\n' "$*" >&2; }

# ── Resolve the econ binary ───────────────────────────────────────────────────
# Order: (1) $ECON_BIN if the caller set it; (2) a built `opencode` binary
# under the sibling repo's dist/; (3) plain `opencode` on PATH; (4) the dev
# runner (`bun run … src/index.ts`) as a last resort. Result lands in the
# ECON_CMD array so a multi-word command (the dev runner) is invoked intact.
resolve_econ() {
  if [ -n "$ECON_BIN" ]; then
    read -r -a ECON_CMD <<< "$ECON_BIN"
    return
  fi
  local _os _arch built
  _os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  case "$(uname -m)" in
    arm64|aarch64) _arch="arm64" ;;
    x86_64|amd64)  _arch="x64" ;;
    *)             _arch="$(uname -m)" ;;
  esac
  built="$(find "$ECON_REPO/packages/opencode/dist" -type f -name opencode -path "*${_os}-${_arch}*" 2>/dev/null | head -1)"
  if [ -n "$built" ]; then
    ECON_CMD=("$built")
    return
  fi
  if command -v opencode >/dev/null 2>&1; then
    ECON_CMD=("opencode")
    return
  fi
  ECON_CMD=(bun run --cwd "$ECON_REPO/packages/opencode" --conditions=browser src/index.ts)
}

# ── Preflight (zero token spend) ──────────────────────────────────────────────
preflight() {
  log "=== preflight (zero cost — no econ invocation) ==="

  log "--- 1. econ binary resolves ---"
  resolve_econ
  log "  resolved: ${ECON_CMD[*]}"
  if [ "${ECON_CMD[0]}" = "bun" ]; then
    if [ -f "$ECON_REPO/packages/opencode/src/index.ts" ]; then
      log "  [PASS] econ (dev runner): entrypoint found"
    else
      log "  [FAIL] econ (dev runner): entrypoint missing at $ECON_REPO/packages/opencode/src/index.ts"
    fi
  else
    if "${ECON_CMD[@]}" --version >/tmp/econ-ver.out 2>&1; then
      log "  [PASS] econ: $(head -1 /tmp/econ-ver.out)"
    else
      log "  [FAIL] econ --version failed (ECON_CMD=${ECON_CMD[*]})"
      sed 's/^/  /' /tmp/econ-ver.out >&2
    fi
  fi

  log "--- 2. LITELLM_API_KEY set ---"
  if [ -n "${LITELLM_API_KEY:-}" ]; then
    log "  [PASS] LITELLM_API_KEY is set"
  else
    log "  [FAIL] LITELLM_API_KEY is not set — econ routes all model tiers via it"
  fi

  log "--- 3. dataset accessible ---"
  if python3 -c "from datasets import load_dataset; load_dataset('$DATASET', split='test')" \
       >/tmp/ds-check.log 2>&1; then
    log "  [PASS] dataset loadable: $DATASET"
  else
    log "  [FAIL] dataset load failed: pip install datasets?"
    sed 's/^/  /' /tmp/ds-check.log >&2
  fi

  log "--- 4. econ-telemetry.py present ---"
  if [ -f "$SCRIPT_DIR/econ-telemetry.py" ]; then
    log "  [PASS] $SCRIPT_DIR/econ-telemetry.py"
  else
    log "  [WARN] $SCRIPT_DIR/econ-telemetry.py missing — meta.jsonl will carry telemetry:{}"
  fi

  log ""
  log "Preflight done. If all [PASS], run:"
  log "  bash run-econ.sh --instances 1"
}

# ── Main run ──────────────────────────────────────────────────────────────────
run() {
  mkdir -p "$RESULTS_DIR" "$ARTIFACTS_DIR"
  resolve_econ
  log "econ: ${ECON_CMD[*]}"
  local preds_file="$RESULTS_DIR/preds.jsonl"
  local meta_file="$RESULTS_DIR/meta.jsonl"
  : > "$preds_file"
  : > "$meta_file"

  log "Loading dataset: $DATASET (slice=$SLICE, n=$INSTANCES)"
  local instances_json
  instances_json=$(python3 - <<PYEOF
import json, sys
from datasets import load_dataset
ds = load_dataset("$DATASET", split="test")
start, end = map(int, "$SLICE".split(":"))
rows = [ds[i] for i in range(start, min(end, len(ds), start + int("$INSTANCES")))]
print(json.dumps(rows))
PYEOF
  )

  local total
  total=$(echo "$instances_json" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
  log "Instances to run: $total"

  local idx=0
  echo "$instances_json" | python3 -c "
import json, sys
rows = json.load(sys.stdin)
for r in rows:
    print(json.dumps(r))
" | while IFS= read -r instance_json; do
    idx=$((idx + 1))
    local iid
    iid=$(echo "$instance_json" | python3 -c "import json,sys; print(json.load(sys.stdin)['instance_id'])")
    local problem_statement
    problem_statement=$(echo "$instance_json" | python3 -c "import json,sys; print(json.load(sys.stdin)['problem_statement'])")

    log "[$idx/$total] $iid"

    local inst_dir="$ARTIFACTS_DIR/$iid"
    mkdir -p "$inst_dir"
    local problem_file
    problem_file="$(mktemp "/tmp/econ-problem-${iid}.XXXXXX")"
    printf '%s\n' "$problem_statement" > "$problem_file"

    cd "$REPO_DIR" || { log "  no repo at $REPO_DIR — skipping"; rm -f "$problem_file"; continue; }
    git config --global --add safe.directory "$REPO_DIR" >/dev/null 2>&1 || true
    git checkout -- . >/dev/null 2>&1 || true
    git clean -fdq >/dev/null 2>&1 || true

    local prompt
    prompt="$(cat "$problem_file")"
    local db="$inst_dir/opencode.db"
    rm -f "$db"
    export OPENCODE_DB="$db"
    local t0 t1 wall_s rc
    t0=$(date +%s)
    timeout "$ECON_TIMEOUT" "${ECON_CMD[@]}" run \
      --format json \
      --dir "$REPO_DIR" \
      --dangerously-skip-permissions \
      "$prompt" \
      > "$inst_dir/econ-events.jsonl" \
      2> "$inst_dir/econ-err.txt"
    rc=$?
    t1=$(date +%s)
    wall_s=$((t1 - t0))
    log "  econ exit=$rc wall=${wall_s}s"

    # ── Capture prediction: git diff vs base_commit ────────────────────────────
    # Exclude econ's own footprint defensively, in case it drops config files
    # into the repo (it shouldn't — unerr is compiled in, not installed, so
    # there's no daemon socket / entitlement / MCP config to strip here).
    local diff_excludes=( ':(exclude).opencode' ':(exclude)opencode.json' ':(exclude).unerr' ':(exclude)repro_issue.*' )
    git add -A >/dev/null 2>&1 || true
    local patch_file="$inst_dir/patch.diff"
    git diff --cached -- . "${diff_excludes[@]}" > "$patch_file"
    local patch_bytes
    patch_bytes=$(wc -c < "$patch_file" | tr -d ' ')

    python3 - "$iid" "$LABEL" "$patch_file" <<'PYEOF' >> "$preds_file"
import json, sys
iid, label, patch_file = sys.argv[1], sys.argv[2], sys.argv[3]
with open(patch_file, "r", errors="replace") as f:
    patch = f.read()
print(json.dumps({
    "instance_id": iid,
    "model_name_or_path": label,
    "model_patch": patch,
}))
PYEOF

    # ── Telemetry: parse econ's --format json event stream ─────────────────────
    # econ-telemetry.py prints ONE JSON object to stdout:
    #   turns, in_tokens, cached_in, out_tokens, reasoning_tokens, usd,
    #   tool_calls, graph_tool_calls, tools
    local telemetry_file="$inst_dir/telemetry.json"
    if [ -f "$SCRIPT_DIR/econ-telemetry.py" ]; then
      python3 "$SCRIPT_DIR/econ-telemetry.py" "$inst_dir/econ-events.jsonl" \
        > "$telemetry_file" 2>>"$inst_dir/econ-err.txt" \
        || { log "  WARN: econ-telemetry.py failed for $iid"; printf '{}' > "$telemetry_file"; }
    else
      printf '{}' > "$telemetry_file"
    fi

    # ── Per-tier cost from econ's session SQLite (captures the executor tier,
    # which the --format json stream cannot see; see econ-tier-cost.py) ────────
    local sid
    sid="$(python3 -c "
import json,sys
for l in open('$inst_dir/econ-events.jsonl'):
    l=l.strip()
    if not l: continue
    try: s=json.loads(l).get('sessionID')
    except: continue
    if s: print(s); break
" 2>/dev/null)"
    local tiercost_file="$inst_dir/tier-cost.json"
    if [ -f "$SCRIPT_DIR/econ-tier-cost.py" ] && [ -f "$db" ]; then
      python3 "$SCRIPT_DIR/econ-tier-cost.py" --db "$db" ${sid:+--session "$sid"} > "$tiercost_file" 2>>"$inst_dir/econ-err.txt" || printf '{}' > "$tiercost_file"
    else
      printf '{}' > "$tiercost_file"
    fi

    python3 - "$iid" "$LABEL" "$wall_s" "$rc" "$patch_bytes" "$inst_dir" "$telemetry_file" "$tiercost_file" "$sid" <<'PYEOF' >> "$meta_file"
import json, sys
iid, label, wall_s, rc, patch_bytes, artifacts_dir, telemetry_file, tiercost_file, sid = sys.argv[1:10]
try:
    with open(telemetry_file) as f:
        telemetry = json.load(f)
except Exception:
    telemetry = {}
try:
    with open(tiercost_file) as f:
        tier_cost_db = json.load(f)
except Exception:
    tier_cost_db = {}
try:
    with open(artifacts_dir + "/econ-err.txt", "r", errors="replace") as f:
        stderr_tail = f.read()[-2000:]
except Exception:
    stderr_tail = ""
print(json.dumps({
    "instance_id": iid,
    "label": label,
    "wall_s": int(wall_s),
    "rc": int(rc),
    "patch_bytes": int(patch_bytes),
    "telemetry": telemetry,
    "artifacts_dir": artifacts_dir,
    "stderr_tail": stderr_tail,
    "session_id": sid or None,
    "tier_cost_db": tier_cost_db,
}))
PYEOF

    rm -f "$problem_file"
    log "  done: $iid (patch_bytes=$patch_bytes)"
  done

  log "Predictions written to: $preds_file"
  log "Meta written to: $meta_file"
  log ""
  log "Next — grade with the SWE-bench harness:"
  log "  python -m swebench.harness.run_evaluation \\"
  log "    --dataset_name $DATASET --split test \\"
  log "    --predictions_path $preds_file \\"
  log "    --run_id $LABEL --max_workers 4"
  log ""
  log "Then build the report:"
  log "  python3 $SCRIPT_DIR/report.py --meta $meta_file --grade-report <run_evaluation report json> --label $LABEL"
}

# ── Main ──────────────────────────────────────────────────────────────────────
if [ "$PREFLIGHT_ONLY" = "1" ]; then
  preflight
  exit 0
fi

run
log "=== run complete ==="
