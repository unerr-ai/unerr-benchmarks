#!/usr/bin/env bash
# In-container driver. Runs INSIDE a derived instance image (env + toolbox).
# Produces the SWE-bench prediction: a unified diff of Codex's edits, to stdout.
#
# Env in:
#   OPENAI_API_KEY   required — Codex auth (passed via `docker run -e`)
#   UNERR_MODE       on | off   (arm B = unerr attached, arm A = bare Codex)
#   REPO_DIR         repo root in the image (SWE-bench default: /testbed)
# Args:
#   $1               path to a file holding the problem_statement
#
# stdout = the patch (nothing else). All logs go to stderr.

set -uo pipefail
export PATH=/opt/toolbox/node/bin:/opt/toolbox/bin:$PATH
TOOLBOX=/opt/toolbox
. "$TOOLBOX/lib.sh"

REPO_DIR="${REPO_DIR:-/testbed}"
MODE="${UNERR_MODE:-on}"
CODEX_MODEL="${CODEX_MODEL:-gpt-5.4-mini}"
PROBLEM_FILE="${1:?usage: run-instance.sh <problem_statement_file>}"

log() { printf '[run-instance] %s\n' "$*" >&2; }

cd "$REPO_DIR" || { log "no repo at $REPO_DIR"; exit 2; }
git config --global --add safe.directory "$REPO_DIR" >/dev/null 2>&1 || true
# Clean any leftover state so the diff reflects only this run's edits.
git checkout -- . >/dev/null 2>&1 || true
git clean -fdq >/dev/null 2>&1 || true

if [ "$MODE" = "on" ]; then
  # 1. Offline Pro entitlement (no login). Export BEFORE starting the daemon.
  unerr_offline_pro
  log "entitlement: ${UNERR_ENTITLEMENT_KID:-<none>} (offline pro)"

  # 2. Start unerrd AFTER the env is exported so the daemon honors Pro (it
  #    decides the repo cap in its own process). Stays up for the whole run.
  unerr_start_daemon >/tmp/unerrd-start.log 2>&1 || true
  # `unerr pm start` can return/time-out before the socket settles: cold-indexing
  # a large repo (django ~2.8k files) under DinD outlasts its internal wait, yet
  # the daemon keeps coming up. Poll the socket up to 120s before calling it dead,
  # so the unerr arm isn't degraded by a startup race (was: one-shot check → false).
  for _ in $(seq 1 120); do unerr_daemon_up && break; sleep 1; done
  if unerr_daemon_up; then
    log "unerrd: up"
  else
    log "unerrd: start FAILED (see /tmp/unerrd-start.log)"; sed 's/^/[unerrd] /' /tmp/unerrd-start.log >&2
  fi

  # 3. Wire unerr into Codex for THIS repo: .codex/config.toml + AGENTS.md + hooks.
  if unerr install codex >/tmp/unerr-install.log 2>&1; then
    log "unerr install codex: ok"
  else
    log "unerr install codex FAILED (see /tmp/unerr-install.log)"; sed 's/^/[install] /' /tmp/unerr-install.log >&2
  fi
fi

# Codex 0.142+ ignores OPENAI_API_KEY for the websocket Responses transport — it
# only honors ~/.codex/auth.json. Materialize it via `codex login` (auth_mode=apikey)
# or every model call 401s on wss://api.openai.com/v1/responses.
if [ -n "${OPENAI_API_KEY:-}" ]; then
  if codex login --with-api-key <<<"$OPENAI_API_KEY" >/tmp/codex-login.log 2>&1; then
    log "codex login: ok (apikey)"
  else
    log "codex login FAILED (see /tmp/codex-login.log)"; sed 's/^/[login] /' /tmp/codex-login.log >&2
  fi
else
  log "codex login: skipped (no OPENAI_API_KEY)"
fi

log "codex exec starting (mode=$MODE, model=$CODEX_MODEL, repo=$REPO_DIR)"
# The container is the sandbox, so bypass approvals/sandbox for full autonomy.
# --json: machine-readable events (token/turn telemetry). Prompt via stdin.
codex exec \
  --json \
  --model "$CODEX_MODEL" \
  --dangerously-bypass-approvals-and-sandbox \
  --output-last-message /tmp/codex-last.txt \
  - < "$PROBLEM_FILE" > /tmp/codex-events.jsonl 2>/tmp/codex.err
CODEX_RC=$?
log "codex exec exit=$CODEX_RC"
[ "$CODEX_RC" -ne 0 ] && sed 's/^/[codex.err] /' /tmp/codex.err >&2

# --- telemetry (-> stderr only; stdout stays the patch) ----------------------
# Parse the codex --json stream so the host can verify, per instance: did unerr
# fire (mcp_tool_calls>0), how many turns/tool-calls, tokens, and $ for the run.
node -e '
const fs=require("fs");
let inn=0,cap=0,out=0,turns=0; const tools={};
const ACT=new Set(["command_execution","mcp_tool_call","function_call","local_shell_call"]);
try{ for(const line of fs.readFileSync("/tmp/codex-events.jsonl","utf8").split("\n")){
  if(!line.trim())continue; let ev; try{ev=JSON.parse(line)}catch{continue}
  if(ev.type==="turn.completed"&&ev.usage){inn+=ev.usage.input_tokens||0;cap+=ev.usage.cached_input_tokens||0;out+=ev.usage.output_tokens||0;turns++}
  if(ev.type==="item.completed"&&ev.item&&ACT.has(ev.item.type)){const t=ev.item.type;tools[t]=(tools[t]||0)+1}
}}catch(e){}
const mcp=tools["mcp_tool_call"]||0;
const tot=Object.values(tools).reduce((a,b)=>a+b,0);
// Authoritative $ is recomputed host-side from raw tokens by report-runs.py PRICING; this in-container usd is a mini-priced approximation only.
const P={in:0.25,cached_in:0.025,out:2.00}; // gpt-5.4-mini $/M tokens (mirrors fly runner)
const usd=(Math.max(0,inn-cap)*P.in+cap*P.cached_in+out*P.out)/1e6;
process.stderr.write("UNERR_TELEMETRY "+JSON.stringify({mode:process.env.UNERR_MODE||"on",model:process.env.CODEX_MODEL||"",turns,in_tokens:inn,cached_in:cap,out_tokens:out,usd:Number(usd.toFixed(4)),tool_calls:tot,mcp_tool_calls:mcp,tools})+"\n");
' || true   # no 2>/dev/null here — it would swallow the UNERR_TELEMETRY line itself

# --- artifact exfiltration (only when the mounted volume is available) -------
if [ -n "${ART_DIR:-}" ]; then
  [ -f /tmp/codex-events.jsonl ] && cp /tmp/codex-events.jsonl "$ART_DIR/"
  [ -f /tmp/codex-last.txt ]     && cp /tmp/codex-last.txt     "$ART_DIR/"
  # Capture .unerr/**/*.jsonl from both REPO_DIR and HOME, preserving tree.
  for ROOT in "$REPO_DIR" "$HOME"; do
    [ -d "$ROOT/.unerr" ] || continue
    ( cd "$ROOT" && find .unerr -type f -name '*.jsonl' -print0 | \
      while IFS= read -r -d "" f; do
        mkdir -p "$ART_DIR/unerr/$(dirname "$f")"
        cp "$f" "$ART_DIR/unerr/$f"
      done ) || true
  done
fi

# Prediction = working-tree diff vs base_commit, EXCLUDING the unerr/codex install
# footprint. `unerr install codex` writes .codex/, AGENTS.md and edits .gitignore;
# unerrd writes .unerr/. None of these are the model's fix — left in, they pollute
# model_patch and break grading (and inflate patch_bytes even with zero real edits).
INSTALL_ARTIFACTS=( ':(exclude).unerr' ':(exclude).codex' ':(exclude)AGENTS.md' ':(exclude).gitignore' )
git add -A >/dev/null 2>&1 || true
git reset -q -- .unerr .codex AGENTS.md .gitignore >/dev/null 2>&1 || true
git diff --cached -- . "${INSTALL_ARTIFACTS[@]}"
