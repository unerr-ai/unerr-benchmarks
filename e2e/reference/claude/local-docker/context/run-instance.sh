#!/usr/bin/env bash
# In-container driver. Runs INSIDE a derived instance image (env + toolbox).
# Produces the SWE-bench prediction: a unified diff of Claude Code's edits, to
# stdout. Parallel to e2e/reference/codex/local-docker/context/run-instance.sh — same
# offline-Pro + patch-diff machinery, but drives `claude -p` instead of `codex`.
#
# Env in:
#   CLAUDE_CODE_OAUTH_TOKEN  required — subscription auth (Pro/Max). Minted on the
#                            host once via `claude setup-token`; passed via
#                            `docker run -e`. NO API key, no in-container login.
#   UNERR_MODE               on | off   (arm B = unerr attached, arm A = bare Claude)
#   REPO_DIR                 repo root in the image (SWE-bench default: /testbed)
#   ART_DIR                  optional mounted dir for artifact exfiltration
#
# No per-task wall-clock cap (Claude CLI has no --max-turns) — the agent runs
# to completion; it owns its own watchdog now (mirrors the econ arm).
# Args:
#   $1               path to a file holding the problem_statement
#
# stdout = the patch (nothing else). All logs go to stderr.
#
# MODEL PINNED, otherwise DEFAULT CONFIG: we pass --model (CLAUDE_MODEL, default
# opus) so the run uses the user's real default model rather than the container's
# bare baseline (sonnet-4-6, since no ~/.claude/settings.json is present). We pass
# NO --effort or other tuning, and the SAME model on both arms, so the A/B delta
# stays purely "unerr on vs off", never a model choice.

set -uo pipefail
export PATH=/opt/toolbox/node/bin:/opt/toolbox/bin:$PATH
TOOLBOX=/opt/toolbox
. "$TOOLBOX/lib.sh"

REPO_DIR="${REPO_DIR:-/testbed}"
MODE="${UNERR_MODE:-on}"
# Model is PINNED (default: opus) and identical for BOTH arms. The container has
# no ~/.claude/settings.json, so without this Claude Code falls back to its
# built-in baseline (sonnet-4-6) — NOT the user's real default. Pinning opus here
# makes the run reflect the user's actual default config; the A/B stays clean
# because on/off use the SAME model. Override with CLAUDE_MODEL=sonnet etc.
CLAUDE_MODEL="${CLAUDE_MODEL:-opus}"
# Open-models mode: run-benchmark.py sets CLAUDE_OPEN_MODELS=1 (plus
# ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN/ANTHROPIC_DEFAULT_*_MODEL and
# CLAUDE_MODEL=sonnet) to route Claude Code through an OpenAI-compatible
# gateway instead of the Anthropic API. Claude Code reads those ANTHROPIC_*
# vars itself — this flag only gates OUR extra behavior below (shipped agent
# files + extra system-prompt text), so it's a no-op when unset/0.
OPEN_MODELS="${CLAUDE_OPEN_MODELS:-0}"
PROBLEM_FILE="${1:?usage: run-instance.sh <problem_statement_file>}"

# Hardening for reproducible headless runs: no mid-run auto-update, no
# nonessential traffic. (Auth + model calls still go through normally.)
export DISABLE_AUTOUPDATER=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
# SWE-bench instance containers run as ROOT, and Claude Code refuses
# --dangerously-skip-permissions under root unless IS_SANDBOX=1. The container
# IS the sandbox here, so this is the intended bypass (without it claude -p exits
# 1 immediately → empty patch).
export IS_SANDBOX=1

log() { printf '[run-instance] %s\n' "$*" >&2; }

cd "$REPO_DIR" || { log "no repo at $REPO_DIR"; exit 2; }
git config --global --add safe.directory "$REPO_DIR" >/dev/null 2>&1 || true
# Clean any leftover state so the diff reflects only this run's edits.
git checkout -- . >/dev/null 2>&1 || true
git clean -fdq >/dev/null 2>&1 || true

# Claude reads CLAUDE_CODE_OAUTH_TOKEN for subscription auth. Do NOT use --bare:
# that mode forces ANTHROPIC_API_KEY and never reads the OAuth token/keychain.
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  log "WARNING: no CLAUDE_CODE_OAUTH_TOKEN/ANTHROPIC_API_KEY — model calls will fail"
fi

MCP_ARGS=()
if [ "$MODE" = "on" ]; then
  # 0. Verify the unerr CLI we built+packed is actually on PATH and runnable in
  #    THIS (target) repo before we lean on it. The toolbox image installs the
  #    freshly-built unerr tgz (build-toolbox.sh: `pnpm run build && pnpm pack`),
  #    so a missing/broken binary here means the build or install regressed — fail
  #    loud rather than silently degrade the ON arm into a bare run.
  if command -v unerr >/dev/null 2>&1; then
    log "unerr binary: $(command -v unerr) v$(unerr --version 2>/dev/null | head -n1)"
  else
    log "FATAL: unerr binary not on PATH — toolbox build/install regressed; ON arm cannot proceed"
    exit 3
  fi

  # 1. Offline Pro entitlement (no login). Export BEFORE starting the daemon.
  unerr_offline_pro
  log "entitlement: ${UNERR_ENTITLEMENT_KID:-<none>} (offline pro)"

  # 1.5. BUILD THE ON-DISK GRAPH before the daemon starts. `unerr recon` only
  #      READS the graph — it errors "no indexed graph yet" on a cold repo and
  #      silently no-ops. The real builder is `unerr index --force --json`:
  #      --force so a "fresh" snapshot doesn't skip the build; --json to assert
  #      success. Must run BEFORE the daemon to avoid an in-memory/on-disk desync
  #      (daemon would otherwise build lazily on the first MCP call, starving the
  #      unerrd heartbeat → bridge declares daemon dead → "-32000 Connection closed"
  #      → 2400s no-runs).
  log "graph index: building on-disk graph before daemon (cold django ~100-366s)"
  if timeout 600 unerr index --force --json >/tmp/unerr-index.log 2>&1; then
    log "graph index: ok ($(grep -oE '"entityCount"[: ]*[0-9]+' /tmp/unerr-index.log | head -1))"
  else
    log "graph index: FAILED/timeout (see /tmp/unerr-index.log) — agent may stall on first MCP call"
  fi

  # 2. Start unerrd AFTER the env is exported so the daemon honors Pro (it
  #    decides the repo cap in its own process). Poll the socket up to 120s:
  #    cold-indexing a large repo (django ~2.8k files) under DinD can outlast
  #    `unerr pm start`'s internal wait even though the daemon keeps coming up.
  unerr_start_daemon >/tmp/unerrd-start.log 2>&1 || true
  for _ in $(seq 1 120); do unerr_daemon_up && break; sleep 1; done
  if unerr_daemon_up; then
    log "unerrd: up"
  else
    log "unerrd: start FAILED (see /tmp/unerrd-start.log)"; sed 's/^/[unerrd] /' /tmp/unerrd-start.log >&2
  fi

  # 3. Wire unerr into Claude Code for THIS repo: .mcp.json + .claude/settings.json
  #    (hooks) + CLAUDE.md. `claude-code` is the agent id (matches --coding-agent).
  if unerr install claude-code >/tmp/unerr-install.log 2>&1; then
    log "unerr install claude-code: ok"
  else
    log "unerr install claude-code FAILED (see /tmp/unerr-install.log)"; sed 's/^/[install] /' /tmp/unerr-install.log >&2
  fi

  # Load ONLY the unerr MCP server, explicitly, so headless never hits the
  # project-MCP trust prompt. Hooks in .claude/settings.json still auto-load.
  if [ -f "$REPO_DIR/.mcp.json" ]; then
    MCP_ARGS=( --mcp-config "$REPO_DIR/.mcp.json" --strict-mcp-config )
  else
    log "WARNING: .mcp.json absent after install — unerr MCP may not load"
  fi

  # 3.05 WEB SEARCH (optional, key-gated): merge Tavily's HOSTED MCP server into
  #      the same .mcp.json --strict-mcp-config loads. Remote HTTP transport —
  #      no npm install, no extra process; the key rides in the URL. Wired on
  #      BOTH arms (see the OFF branch) so the A/B delta stays purely unerr.
  #      NB: runs with web search are a SEPARATE result class — the agent can
  #      find the actual upstream fix, so never compare them 1:1 against no-web
  #      baselines (label them web-on; not leaderboard-submittable).
  if [ -n "${TAVILY_API_KEY:-}" ] && [ -f "$REPO_DIR/.mcp.json" ]; then
    if node -e '
      const fs = require("fs");
      const p = process.argv[1];
      const cfg = JSON.parse(fs.readFileSync(p, "utf8"));
      (cfg.mcpServers ??= {}).tavily = {
        type: "http",
        url: "https://mcp.tavily.com/mcp/?tavilyApiKey=" + process.env.TAVILY_API_KEY,
      };
      fs.writeFileSync(p, JSON.stringify(cfg, null, 2));
    ' "$REPO_DIR/.mcp.json" 2>/tmp/tavily-merge.err; then
      log "web-search: tavily hosted MCP merged into .mcp.json"
    else
      log "web-search: tavily merge FAILED (non-fatal, see /tmp/tavily-merge.err)"
    fi
  fi

  # 3.1 OPEN-MODELS: overwrite the just-installed agent files with our shipped,
  #     customized versions (Task-delegation policy tuned for the open-models
  #     arm). Shipped next to this script in the toolbox image under agents/.
  #     No-op on the real-Claude path (OPEN_MODELS unset/0).
  if [ "$OPEN_MODELS" = "1" ]; then
    SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
    AGENTS_SRC="$SELF_DIR/agents"
    if [ -d "$AGENTS_SRC" ]; then
      mkdir -p "$REPO_DIR/.claude/agents"
      n=0
      for f in "$AGENTS_SRC"/unerr-*.md; do
        [ -f "$f" ] || continue
        cp -f "$f" "$REPO_DIR/.claude/agents/"
        n=$((n + 1))
      done
      log "open-models: copied $n shipped agent file(s) into $REPO_DIR/.claude/agents/"
    else
      log "open-models: WARNING — agents source dir missing ($AGENTS_SRC), keeping installed agents"
    fi
  fi

  # 3.15 OPEN-MODELS: install the mechanical finish-gate + edit-deny hooks
  #      (PreToolUse deny + PostToolUse recorder + Stop-time gate; see
  #      cc-harness-hooks.py, shipped in $TOOLBOX the same way as
  #      mcp-healthcheck.mjs). Written to .claude/settings.local.json,
  #      NEVER .claude/settings.json: unerr owns settings.json (it wrote its
  #      own hooks there during `unerr install claude-code` above) and never
  #      touches settings.local.json, and Claude Code UNIONS the hook arrays
  #      from both files — so this file only ADDS our three hooks, it can
  #      never clobber or conflict with unerr's. Hook STATE lives under
  #      /tmp/cc-harness/ (see cc-harness-hooks.py), never inside REPO_DIR, so
  #      it can never leak into the graded model_patch diff — the diff step
  #      near the end of this script excludes .claude/ too, but /tmp is the
  #      real reason it's safe. All three hooks are FAIL-OPEN by construction
  #      (cc-harness-hooks.py: any internal exception -> exit 0), so a bug
  #      here degrades to a no-op gate/deny, never a broken run.
  if [ "$OPEN_MODELS" = "1" ]; then
    # $TOOLBOX is NOT set inside the instance container: Dockerfile.instance COPYs
    # the toolbox to /opt/toolbox but does not carry the toolbox image's ENV, and no
    # `-e TOOLBOX` is passed at `docker run`. Default it to the fixed COPY target.
    # Resolve an ABSOLUTE python too — SWE-bench conda envs may expose only `python`
    # (not `python3`); the hooks run as Claude Code's children and inherit THIS shell's
    # PATH, so a write-time absolute resolution is what makes the gate actually fire
    # (a bare `python3` that doesn't resolve would exit non-zero → gate silently no-op).
    : "${TOOLBOX:=/opt/toolbox}"
    PYBIN="$(command -v python3 || command -v python || echo python3)"
    mkdir -p "$REPO_DIR/.claude"
    cat > "$REPO_DIR/.claude/settings.local.json" <<EOF
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|mcp__unerr__file_edit",
        "hooks": [
          { "type": "command", "command": "$PYBIN $TOOLBOX/cc-harness-hooks.py deny" }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash|Task|Edit|Write|MultiEdit|mcp__unerr__file_edit",
        "hooks": [
          { "type": "command", "command": "$PYBIN $TOOLBOX/cc-harness-hooks.py record" }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "$PYBIN $TOOLBOX/cc-harness-hooks.py gate" }
        ]
      }
    ]
  }
}
EOF
    log "open-models: wrote $REPO_DIR/.claude/settings.local.json (mechanical finish-gate + edit-deny hooks)"
  fi

  # 3.5 HEALTH GATE: confirm unerrd + the process manager actually registered THIS
  #     repo and report its state, via `unerr pm status` (run from the target repo
  #     cwd so it resolves to /testbed, not the unerr-cli repo). This is the
  #     ground-truth check that the daemon is supervising the repo we're about to
  #     query — distinct from the socket poll above (socket up ≠ repo registered).
  #     Logged + exfiltrated for post-hoc root-cause; non-fatal (warm-up below is
  #     the functional gate), but a repo absent here predicts an MCP stall.
  unerr pm status >/tmp/unerr-pm-status.log 2>&1 || true
  sed 's/^/[pm status] /' /tmp/unerr-pm-status.log >&2
  if grep -qiE "(running|ready|indexed|up)" /tmp/unerr-pm-status.log 2>/dev/null; then
    log "pm status: daemon supervising repo(s) — healthy"
  else
    log "pm status: WARNING — no healthy repo state reported (see /tmp/unerr-pm-status.log)"
  fi

  # 4. PRIME THE COMPOSITE CACHE now that the graph exists on disk (built in
  #    step 1.5 above). This single non-fatal recon warms the in-memory composite
  #    cache so the agent's first search_code call is fast. Non-fatal: if it fails
  #    the graph is already on disk, so the agent can still make progress (it will
  #    just pay a small first-call penalty to load the snapshot into memory).
  WARMQ="$(head -n1 "$PROBLEM_FILE" | cut -c1-120)"
  timeout 120 unerr recon "${WARMQ:-symbol}" >/tmp/unerr-warm.log 2>&1 \
    && log "recon: composite cache primed" \
    || log "recon: prime skipped (non-fatal; graph already built on disk)"
else
  # OFF arm: guarantee a clean baseline — zero MCP servers regardless of any
  # stray repo .mcp.json (plus tavily when TAVILY_API_KEY is set, mirroring the
  # ON arm so web search never becomes the confound in the A/B). Claude still
  # has its native tools (Read/Edit/Bash/…) — the "disciplined bare agent".
  if [ -n "${TAVILY_API_KEY:-}" ]; then
    printf '{"mcpServers":{"tavily":{"type":"http","url":"https://mcp.tavily.com/mcp/?tavilyApiKey=%s"}}}\n' \
      "$TAVILY_API_KEY" > /tmp/empty-mcp.json
  else
    echo '{"mcpServers":{}}' > /tmp/empty-mcp.json
  fi
  MCP_ARGS=( --mcp-config /tmp/empty-mcp.json --strict-mcp-config )
fi

PROMPT="$(cat "$PROBLEM_FILE")"

# BASE autonomy directive — BOTH arms. Headless `claude -p` has no human, so
# without this it can answer an ambiguous SWE-bench statement with a clarifying
# question or a plan and end the turn with no edits → empty patch. This is harness
# necessity (not unerr policy), so the OFF baseline gets it too: a fair, non-stalling
# bare agent. Kept generic — no mention of unerr, tools, web search, or subagents.
AUTONOMY_PROMPT="You are operating fully autonomously in an automated benchmark, with no human available to answer questions. Resolve the task by editing the repository's source files directly. Never ask questions, present options, seek confirmation, or enter plan mode — pick the most reasonable interpretation, implement it, and then stop."

# ON-ONLY unerr operator policy, appended on top of the base: shortest path,
# web-search fallback, parallel unerr subagents, and ignore test files unless
# mandatory (the last directly counters the ON-arm "wrote extra regression tests →
# more turns" behavior we root-caused). These are unerr-workflow directives, kept
# out of the OFF baseline so the A/B delta stays purely "unerr on vs off".
if [ "$MODE" = "on" ]; then
  AUTONOMY_PROMPT="$AUTONOMY_PROMPT Take the shortest correct path to a working fix. If you are unsure how to fix something, use web search to find the answer. Delegate independent sub-tasks to unerr subagents so they run in parallel. Do not modify test files unless the fix is impossible without it."
  # Key-gated: make the web-search directive above ACTIONABLE. On open-models the
  # native WebSearch tool hard-400s through the gateway (fireworks rejects the
  # server-side web_search param) — point the model at the tavily MCP tools instead.
  if [ -n "${TAVILY_API_KEY:-}" ]; then
    AUTONOMY_PROMPT="$AUTONOMY_PROMPT Web search runs through the tavily MCP tools (tavily_search to find pages, tavily_extract to pull a page's content); a single targeted search of the issue's key error message or symptom is often the fastest route to the root cause. MCP servers connect asynchronously — if the tavily tools are not in your tool list yet, call WaitForMcpServers once, then search. The built-in WebSearch tool is unavailable in this environment — never call it."
  fi
  # OPEN-MODELS ONLY: orchestration + escalation contract (delegate to subagents;
  # escalate the hard tail). The prior in-prompt WORK PROTOCOL (reproduce-first /
  # typed-assert / leave-tests-red) was REMOVED 2026-07-14: appended on top
  # of Claude Code's OWN agentic harness it "enforced the harness twice" and drove the
  # Mini-10 regressions — repro-false-confidence (11848), a Rule-4 license to ship
  # PASS_TO_PASS regressions (11885/12039), and a 131-turn/$8 thrash. Claude Code's
  # native loop already reproduces + verifies; keep only the orchestration here.
  #
  # ADDED 2026-07-15 — three prompt-level priors distilled from the econ
  # path-to-85 / harness-variance forensics, filtered to items that are (i) UNIVERSAL
  # across SWE-Verified, not Django-specific, and (ii) NON-CONFLICTING with the native
  # loop (they are CODING priors — how to write the fix — never verification machinery):
  #  - TRACK: minimax under-weights the TaskCreate/TaskUpdate guidance that lives only
  #    in the installed CLAUDE.md; restate it in the appended prompt where it lands harder.
  #  - FIX DISCIPLINE / fix-at-definition: the "root-most layer" maintainer prior the
  #    strict cycle proved matters (11964 patched the symptom layer and failed).
  #  - FIX DISCIPLINE / native-type: the systematic "web-format-default stringification"
  #    class (11790; arXiv:2512.00215, model-agnostic) — POSITIVELY framed on purpose
  #    (a negative "never stringify" backfires, Pink-Elephant). We can't add econ's
  #    check-time typed-fidelity detector without re-creating the double-harness, so the
  #    positively-framed preamble line is the only lever available in the native loop.
  # DELIBERATELY EXCLUDED (conflict with the native harness): the oracle-inversion /
  # "leave the bug-encoding test red" carve-out (== the removed Rule-4 that regressed
  # 11885/12039; SWE-bench discards model test edits anyway) and all repro-first /
  # finish-gate / M4-pin machinery (the native loop already owns verification).
  # ESCALATION triggers rewritten from prose to COUNTABLE (Part V item 1 is the
  # strongest-evidenced fix: 8/8 forensic reds ran 0 escalations; prose advisories
  # never steered the cheap conductor — a countable trigger it can self-evaluate might).
  # UPDATE 2026-07-15 (same day): priors3 shows the countable triggers above still
  # under-fire in prose form — 0/3 forensic runs escalated (11848 bailed on an
  # unverified "fix"; 11885 finished with 2 PASS_TO_PASS tests left red). Finish-gates
  # are now MECHANICAL for this arm: a Stop hook (cc-harness-hooks.py, wired via
  # settings.local.json in step 3.15) blocks a no-edit/regressed/unverified finish or
  # an unescalated trigger (caps Z1/R1/V2/E1, overall 3, fail-open) — superseding the
  # "finish-gate machinery" exclusion above for OPEN-MODELS only, since a hook gate
  # is not prompt prose and can't double-harness (it fires once, at Stop, never mid-turn).
  if [ "$OPEN_MODELS" = "1" ]; then
    AUTONOMY_PROMPT="$AUTONOMY_PROMPT

TRACK — before your first edit, if the task takes 2+ steps call TaskCreate to write the plan down (one task per slice) and TaskUpdate each to completed as it lands; treat the tracker as your working memory across a long run, not bookkeeping, and clear it when the fix is done.

FIX DISCIPLINE (applies to every edit you make):
- Fix at the definition. Change the entity whose behavior the issue calls wrong at the site where it is DEFINED; a fix that coerces or special-cases at a downstream site where the value merely flows through is almost always the wrong layer.
- Keep values in their native type. Emit each value in the type its source uses — a value that starts typed (an int, a field length, an enum member) carries that type through to where it is stored; do not collapse it to the rendered or stringified form you usually see it printed as.
- Test files are read-only in this benchmark — the harness mechanically denies test edits; never attempt them, and never count a test edit as part of a fix.

DELEGATION — use your agents when they pay, not by reflex:
- unerr-junior (fast, cheap): parallel recon across many files, running test suites or repro scripts (it reports exact output), web lookups. Do a single quick lookup yourself.
- unerr-worker (executor): scoped multi-file mechanical changes; run independent slices in parallel. Do a small single-file edit yourself.

ESCALATION — the moment a problem proves hard, STOP soloing (continuing to grind alone is how hard instances are lost). Escalate on ANY of these countable triggers: (a) after 2 distinct fix attempts the issue's symptom is still present when you re-check; (b) you have edited the same file 3 or more times without reaching a working fix; (c) you have 2+ candidate defect sites and the evidence does not decide between them; (d) your fix turned a previously-passing test red and one rework did not recover it.
Escalate by spawning unerr-opus AND unerr-fable IN PARALLEL (one message, two Task calls). Give each the SAME evidence brief — the issue text, what you observed, what you tried, and ALL candidate sites — but NOT your preferred hypothesis, so their reads stay independent. Instruct them to investigate and return a one-line root cause plus an exact minimal patch proposal WITHOUT editing files. Reconcile: if they agree, implement it; if they disagree, prefer the verdict that explains ALL observed evidence, then the one that fixes a definition site over one that compensates at a flow site. Exception — if a concrete fix already exists but has failed twice, run them in SEQUENCE instead: unerr-opus implements directly, then unerr-fable reviews the diff against the issue. At most one escalation round per problem; after reconciling, implement and finish. Triggers (b) and (d) are machine-checked at stop: if they have fired and you try to finish without having escalated, the stop gate blocks you and returns you to work.
FINISH CONTRACT — machine-checked when you try to stop (an unmet gate returns you to work with instructions):
- After your final edit, re-run your reproduction of the issue AND the narrowest existing test module covering each edited file; a finish without a green post-edit verification run is blocked.
- A test that passed before your change and fails after it is a regression caused by your fix — rework it until green; finishing while it is red is blocked.
- If the stop gate blocks you, do exactly what its message names, then finish. Do not fight the gate; it releases after its condition is met."
  fi
fi
SYSPROMPT_ARGS=( --append-system-prompt "$AUTONOMY_PROMPT" )

log "claude -p starting (mode=$MODE, repo=$REPO_DIR, model=$CLAUDE_MODEL)"
# The container is the sandbox, so bypass permission checks for full autonomy.
# --output-format stream-json (requires --verbose) gives a machine-readable event
# stream for token/turn/tool telemetry, mirroring codex --json. There is no turn
# cap (Claude CLI has no --max-turns) and no per-task wall-clock cap — the agent
# runs to completion; it owns its own watchdog now (mirrors the econ arm).
# SYSPROMPT_ARGS injects the autonomy directive: BOTH arms get the base (anti-stall);
# ON also gets the unerr operator policy appended (built above).

# ── DEBUG-ONLY MCP heartbeat (DEBUG_MCP_PROBE=1) ─────────────────────────────
# While claude -p runs, probe the unerr MCP path every PROBE_INTERVAL seconds via
# a REAL `unerr --mcp` roundtrip (mcp-healthcheck.mjs: init -> tools/list ->
# tools/call file_read). If the daemon dies mid-run the probe flips PASS->FAIL/
# TIMEOUT and timestamps it, pinning a "-32000 Connection closed" stall to the
# exact second. OFF by default — only for hardening. ON arm only (OFF has no MCP).
DEBUG_MCP_PROBE="${DEBUG_MCP_PROBE:-0}"
PROBE_INTERVAL="${PROBE_INTERVAL:-25}"
PROBE_PID=""
mcp_probe_once() { # $1 = phase label (pre|during|post)
  local label="$1" sock t0 t1 lat raw rc verdict note iso elapsed
  sock="down"; [ -S "$HOME/.unerr/unerrd.sock" ] && sock="up"
  t0=$(date +%s%3N)
  raw="$(timeout 30 node "$TOOLBOX/mcp-healthcheck.mjs" "$REPO_DIR" unerr "$PROBE_FILE" 20000 2>&1)"; rc=$?
  t1=$(date +%s%3N); lat=$((t1 - t0))
  if [ "$rc" -eq 124 ]; then verdict="TIMEOUT"; note="probe>30s daemon/MCP hung"
  elif printf '%s' "$raw" | grep -q "ALL PASS"; then verdict="PASS"; note="ok"
  else verdict="FAIL"; note="$(printf '%s' "$raw" | grep -iE 'FAIL|refusal|never returned|-32[0-9]{3}' | head -1 | tr '\t' ' ' | cut -c1-90)"; fi
  iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; elapsed=$(( $(date +%s) - PROBE_START ))
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$iso" "$(date +%s)" "$elapsed" "$sock" "$lat" "$verdict" "$label:$note" >> /tmp/mcp-probe.tsv
  [ "$verdict" != "PASS" ] && log "MCP PROBE @${elapsed}s [$label]: $verdict ($note)"
  return 0
}
mcp_probe_loop() { while :; do mcp_probe_once during; sleep "$PROBE_INTERVAL"; done; }
if [ "$MODE" = "on" ] && [ "$DEBUG_MCP_PROBE" = "1" ]; then
  PROBE_FILE="$(git -C "$REPO_DIR" ls-files '*__init__.py' 2>/dev/null | head -1)"
  [ -z "$PROBE_FILE" ] && PROBE_FILE="$(git -C "$REPO_DIR" ls-files '*.py' '*.js' '*.ts' 2>/dev/null | head -1)"
  PROBE_START=$(date +%s)
  printf 'iso_utc\tepoch\telapsed_s\tsocket\tlatency_ms\tverdict\tnote\n' > /tmp/mcp-probe.tsv
  log "MCP heartbeat ENABLED: every ${PROBE_INTERVAL}s, probe_file=$PROBE_FILE -> /tmp/mcp-probe.tsv"
  mcp_probe_once pre
  mcp_probe_loop & PROBE_PID=$!
fi

hb_loop() { while :; do sleep 240; log "HB events_bytes=$(wc -c < /tmp/claude-events.jsonl 2>/dev/null | tr -d ' ' || echo 0)"; done; }
hb_loop & HB_PID=$!

# Open-models: native WebSearch is a dead tool (server-side web_search param →
# gateway 400 on fireworks); remove it so the model can't burn turns on it.
# Real-Claude path keeps it (works there). WebFetch stays on both (client-side).
WEB_ARGS=()
[ "$OPEN_MODELS" = "1" ] && WEB_ARGS=( --disallowedTools "WebSearch" )

claude -p "$PROMPT" \
  --model "$CLAUDE_MODEL" \
  "${SYSPROMPT_ARGS[@]}" \
  --output-format stream-json --verbose \
  --dangerously-skip-permissions \
  "${MCP_ARGS[@]}" \
  "${WEB_ARGS[@]}" \
  > /tmp/claude-events.jsonl 2>/tmp/claude.err
CLAUDE_RC=$?
kill "$HB_PID" 2>/dev/null; wait "$HB_PID" 2>/dev/null || true
if [ -n "$PROBE_PID" ]; then
  kill "$PROBE_PID" 2>/dev/null; wait "$PROBE_PID" 2>/dev/null || true
  mcp_probe_once post   # is unerr STILL alive after the run completed?
  first_fail=$(awk -F'\t' 'NR>1 && $6!="PASS"{print "@"$3"s "$6" ("$7")"; exit}' /tmp/mcp-probe.tsv 2>/dev/null)
  [ -n "$first_fail" ] && log "MCP heartbeat FIRST FAILURE: $first_fail" || log "MCP heartbeat: unerr healthy across the ENTIRE run (pre+during+post all PASS)"
fi
log "claude -p exit=$CLAUDE_RC"
[ "$CLAUDE_RC" -ne 0 ] && sed 's/^/[claude.err] /' /tmp/claude.err >&2

# --- harness summary (-> stderr; survives into meta.jsonl stderr_tail) -------
# The distributed bundle drops per-instance artifacts (n_artifacts:0), so the
# deny/gate evidence in state.jsonl would die with the container. Summarize it
# to stderr, which worker-loop keeps as stderr_tail — the ONE per-instance
# harness signal that reaches the bundle. (Open-models arm only — the hooks
# that write this state are installed only when OPEN_MODELS=1.)
if [ -f /tmp/cc-harness/state.jsonl ]; then
  HS="$("${PYBIN:-python3}" - <<'PYEOF' 2>/dev/null
import json, collections
c = collections.Counter(); denies = collections.Counter(); blocks = collections.Counter()
try:
    for l in open("/tmp/cc-harness/state.jsonl"):
        l = l.strip()
        if not l: continue
        try: d = json.loads(l)
        except Exception: continue
        ev = d.get("ev", "?"); c[ev] += 1
        if ev == "deny":  denies[d.get("rule", "?")] += 1
        if ev == "block": blocks[d.get("gate", "?")] += 1
    print(json.dumps({"events": dict(c), "denies": dict(denies), "blocks": dict(blocks)}, sort_keys=True))
except Exception:
    print("{}")
PYEOF
)"
  log "HARNESS_SUMMARY ${HS:-{}}"
fi

# --- telemetry (-> stderr only; stdout stays the patch) ----------------------
# Parse the claude stream-json so the host can verify, per instance: did unerr
# fire (mcp_tool_calls>0), how many turns/tool-calls, tokens, and $ for the run.
# The final {"type":"result"} object carries total_cost_usd, num_turns and usage;
# per-message tool_use blocks give the tool-call counts (mcp = name "mcp__*").
node -e '
const fs=require("fs");
let inn=0,cap=0,ccreate=0,out=0,turns=0,usd=0,model="";
const tools={};
try{ for(const line of fs.readFileSync("/tmp/claude-events.jsonl","utf8").split("\n")){
  if(!line.trim())continue; let ev; try{ev=JSON.parse(line)}catch{continue}
  // tool calls: assistant messages carry tool_use content blocks
  if(ev.type==="assistant"&&ev.message){
    if(ev.message.model)model=ev.message.model;
    for(const b of (ev.message.content||[])){
      if(b&&b.type==="tool_use"){const n=b.name||"tool";tools[n]=(tools[n]||0)+1;}
    }
  }
  // final result: authoritative usage + cost + turns
  if(ev.type==="result"){
    turns=ev.num_turns||turns;
    if(typeof ev.total_cost_usd==="number")usd=ev.total_cost_usd;
    const u=ev.usage||{};
    inn+=u.input_tokens||0; cap+=u.cache_read_input_tokens||0;
    ccreate+=u.cache_creation_input_tokens||0; out+=u.output_tokens||0;
    if(ev.modelUsage){const k=Object.keys(ev.modelUsage);if(k.length)model=model||k[0];}
  }
}}catch(e){}
const tot=Object.values(tools).reduce((a,b)=>a+b,0);
const mcp=Object.entries(tools).filter(([n])=>n.startsWith("mcp__")).reduce((a,[,c])=>a+c,0);
// usd is Claudes own total_cost_usd (API-equivalent cost; reported even on a
// subscription run). report-runs can recompute from raw tokens if desired.
process.stderr.write("UNERR_TELEMETRY "+JSON.stringify({mode:process.env.UNERR_MODE||"on",model,turns,in_tokens:inn,cached_in:cap,cache_creation:ccreate,out_tokens:out,usd:Number(usd.toFixed(4)),tool_calls:tot,mcp_tool_calls:mcp,tools})+"\n");
' || true   # no 2>/dev/null here — it would swallow the UNERR_TELEMETRY line itself

# --- artifact exfiltration (only when the mounted volume is available) -------
# Everything here is for POST-HOC ROOT-CAUSE: when the unerr arm degrades (warm-up
# fails, MCP "Connection closed", empty patch) the answer lives in these driver +
# daemon logs. Capture them ALL — not just the model transcript — or a failure is
# uninvestigable after the --rm container is gone.
if [ -n "${ART_DIR:-}" ]; then
  [ -f /tmp/claude-events.jsonl ] && cp /tmp/claude-events.jsonl "$ART_DIR/"
  [ -f /tmp/claude.err ]          && cp /tmp/claude.err          "$ART_DIR/claude-stderr.txt"
  # Driver-side logs from the unerr bring-up (warm-up, daemon start, install).
  # These hold the WHY behind a degraded ON arm (e.g. recon timeouts, install errs).
  for L in unerr-warm unerr-index unerrd-start unerr-install unerr-pm-status codex-login; do
    [ -f "/tmp/$L.log" ] && cp "/tmp/$L.log" "$ART_DIR/$L.log"
  done
  # DEBUG_MCP_PROBE heartbeat timeline (the per-second unerr-health log).
  [ -f /tmp/mcp-probe.tsv ] && cp /tmp/mcp-probe.tsv "$ART_DIR/mcp-probe.tsv"
  # Capture .unerr/** *.jsonl AND *.log from both REPO_DIR and HOME, preserving
  # tree. The *.log glob is what pulls ~/.unerr/logs/unerrd.log — the daemon log
  # that records index pressure + the MCP connection drop (was: .jsonl only, so
  # the single most useful file for diagnosing "Connection closed" was lost).
  for ROOT in "$REPO_DIR" "$HOME"; do
    [ -d "$ROOT/.unerr" ] || continue
    ( cd "$ROOT" && find .unerr -type f \( -name '*.jsonl' -o -name '*.log' \) -print0 | \
      while IFS= read -r -d "" f; do
        mkdir -p "$ART_DIR/unerr/$(dirname "$f")"
        cp "$f" "$ART_DIR/unerr/$f"
      done ) || true
  done
fi

# Prediction = working-tree diff vs base_commit, EXCLUDING the unerr/claude
# install footprint. `unerr install claude-code` writes .mcp.json, .claude/ and
# CLAUDE.md and edits .gitignore; unerrd writes .unerr/. None of these are the
# model's fix — left in, they pollute model_patch and break grading.
# repro_issue.* is the open-models repro-script convention (protocol says /tmp,
# this is the safety net if the model writes it in the repo anyway).
INSTALL_ARTIFACTS=( ':(exclude).unerr' ':(exclude).claude' ':(exclude).mcp.json' ':(exclude)CLAUDE.md' ':(exclude).gitignore' ':(exclude)repro_issue.*' )
git add -A >/dev/null 2>&1 || true
git reset -q -- .unerr .claude .mcp.json CLAUDE.md .gitignore 'repro_issue.*' >/dev/null 2>&1 || true
git diff --cached -- . "${INSTALL_ARTIFACTS[@]}"
