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
#   CLAUDE_REAL               1 = claude-real arm: stock real-Anthropic Claude Code,
#                             but staged with the same unerr harness (shipped
#                             agents + finish-gate hooks + ON prompt) as
#                             open-models — set by run-benchmark.py alongside the
#                             CLAUDE_CODE_OAUTH_TOKEN/ANTHROPIC_API_KEY auth above;
#                             mutually exclusive with CLAUDE_OPEN_MODELS.
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
# claude-real: single-arm harness run against STOCK real-Anthropic Claude Code
# (no gateway, no model-alias env) — set by run-benchmark.py alongside subscription
# auth when CLAUDE_REAL=1. Mutually exclusive with OPEN_MODELS (enforced on the
# host side); harmless to check both here.
CLAUDE_REAL="${CLAUDE_REAL:-0}"
# HARNESS_ON gates the unerr-harness STAGING shared by both single-arm run modes
# (shipped agent files, finish-gate hooks, the ON operator system-prompt block).
# OPEN_MODELS alone still gates gateway-SPECIFIC behavior (e.g. the WebSearch
# removal below) since real-Claude keeps native WebSearch working.
HARNESS_ON=0
if [ "$OPEN_MODELS" = "1" ] || [ "$CLAUDE_REAL" = "1" ]; then
  HARNESS_ON=1
fi
# PROFILE selects which hook behavior + prompt text the harness uses when
# HARNESS_ON=1: "swe" (default) is this benchmark's existing test-file-deny +
# test-based finish contract, byte-identical to before this var existed;
# "generic" (via HARNESS_PROFILE=generic, or the shorthand HARNESS_HOOKS=generic)
# swaps in a benchmark-agnostic checkable-outcome contract (see step 3.15 and the
# ON prompt block below). run-benchmark.py does not forward either var into this
# container today, so PROFILE stays "swe" on the SWE-bench flow unless a caller
# explicitly wires one through.
PROFILE="${HARNESS_PROFILE:-swe}"
if [ "${HARNESS_HOOKS:-}" = "generic" ]; then
  PROFILE=generic
fi
# ESCALATION_PANEL selects the ON-prompt escalation shape, orthogonal to PROFILE
# and applied in both: unset/"0"/anything but "1" -> PANEL=0, the default two-rung
# LADDER (unerr-opus alone, then unerr-fable only if still unresolved — cheapest
# when both tiers are one model family at different effort, e.g. claude-gpt/
# claude-native); "1" -> PANEL=1, the original judge-panel (unerr-opus AND
# unerr-fable spawned together) reserved for genuinely decorrelated tiers
# (claude-open). See the ON prompt block below for the two text variants.
PANEL=0
[ "${ESCALATION_PANEL:-}" = "1" ] && PANEL=1
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

  # 3.1 HARNESS: overwrite the just-installed agent files with our shipped,
  #     customized versions (Task-delegation policy tuned for this harness).
  #     Shipped next to this script in the toolbox image under agents/.
  #     Fires for open-models AND claude-real (HARNESS_ON); no-op on the plain
  #     real-Claude A/B path (both flags unset/0).
  if [ "$HARNESS_ON" = "1" ]; then
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
      log "harness: copied $n shipped agent file(s) into $REPO_DIR/.claude/agents/"
    else
      log "harness: WARNING — agents source dir missing ($AGENTS_SRC), keeping installed agents"
    fi
  fi

  # 3.15 HARNESS (open-models OR claude-real): install the mechanical finish-gate
  #      + edit-deny hooks (PreToolUse deny + PostToolUse recorder + Stop-time gate; see
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
  #      here degrades to a no-op gate/deny, never a broken run. Each hook
  #      command is prefixed `env HARNESS_PROFILE=$PROFILE HARNESS_HOOKS=1
  #      ESCALATION_PANEL=$PANEL` so cc-harness-hooks.py sees PROFILE and the
  #      escalation-panel flag deterministically regardless of the shell that
  #      spawns it (mirrors the terminal agent's own hook wiring).
  if [ "$HARNESS_ON" = "1" ]; then
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
        "matcher": "Bash|Edit|Write|MultiEdit|mcp__unerr__file_edit",
        "hooks": [
          { "type": "command", "command": "env HARNESS_PROFILE=$PROFILE HARNESS_HOOKS=1 ESCALATION_PANEL=$PANEL $PYBIN $TOOLBOX/cc-harness-hooks.py deny" }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash|Task|Edit|Write|MultiEdit|mcp__unerr__file_edit",
        "hooks": [
          { "type": "command", "command": "env HARNESS_PROFILE=$PROFILE HARNESS_HOOKS=1 ESCALATION_PANEL=$PANEL $PYBIN $TOOLBOX/cc-harness-hooks.py record" }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "env HARNESS_PROFILE=$PROFILE HARNESS_HOOKS=1 ESCALATION_PANEL=$PANEL $PYBIN $TOOLBOX/cc-harness-hooks.py gate" }
        ]
      }
    ]
  }
}
EOF
    log "harness: wrote $REPO_DIR/.claude/settings.local.json (mechanical finish-gate + edit-deny hooks)"

    # 3.16 FAIL-LOUD VALIDATION (mirrors harbor_agents.py's
    #      _hooks_settings_command on the terminal flow, root-caused
    #      2026-07-20: a settings write that silently mislands, or a gate
    #      script that isn't actually staged, leaves every hook inert with
    #      zero trace — the exact failure class HARNESS_UNIVERSAL.md §7
    #      "fix 5" exists for). Existence-check the settings file AND the
    #      cc-harness-hooks.py gate script the settings JSON points at, then
    #      JSON-parse the settings file — any miss is an unmistakable FATAL +
    #      non-zero exit instead of a quiet no-op gate.
    [ -f "$REPO_DIR/.claude/settings.local.json" ] || {
      log "FATAL: $REPO_DIR/.claude/settings.local.json missing after harness hooks install"
      exit 1
    }
    [ -f "$TOOLBOX/cc-harness-hooks.py" ] || {
      log "FATAL: $TOOLBOX/cc-harness-hooks.py missing — settings.local.json points at a gate script that isn't staged"
      exit 1
    }
    "$PYBIN" -m json.tool "$REPO_DIR/.claude/settings.local.json" >/dev/null 2>&1 || {
      log "FATAL: $REPO_DIR/.claude/settings.local.json is not valid JSON"
      exit 1
    }
    log "harness: validated settings.local.json + cc-harness-hooks.py (existence + JSON)"
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
  # HARNESS ONLY (open-models or claude-real): orchestration + escalation contract
  # (delegate to subagents; escalate the hard tail). The prior in-prompt WORK PROTOCOL (reproduce-first /
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
  # "finish-gate machinery" exclusion above for this HARNESS (open-models or
  # claude-real) only, since a hook gate is not prompt prose and can't
  # double-harness (it fires once, at Stop, never mid-turn).
  if [ "$HARNESS_ON" = "1" ]; then
    # Universal profile fragments (single form, no PROFILE branching):
    # discover the project's own check while onboarding, reproduce the
    # failure first, then verify against the command marked `# unerr:verify`.
    TEST_FILES_BULLET="
- Fix real source, not the checks. A grader runs its own copy of the tests/checks, so editing a test or the verification itself to make it pass usually only fakes progress — fix the code the check exercises. Change a test only when the task itself is to change tests."
    ESCALATION_TRIGGER_D="your change turned your verification red and one rework did not recover it"
    FINISH_CONTRACT="FINISH CONTRACT — machine-checked when you try to stop (an unmet gate returns you to work with instructions): every task has a checkable outcome. Before your first change, decide the command that proves success for THIS task — prefer the project's own test/build/run check you found while onboarding; otherwise a script you write, curl the endpoint, or diff output against expected. Run it BEFORE you edit to confirm it fails the way the task describes (a reproduced failure is your grounded before/after), appending the marker comment \`# unerr:verify\` — the harness tracks marked commands only. After your final change, re-run the marked check and confirm it exits 0. The stop gate blocks finishing when no marked verification has succeeded since your last change; a marked command that once passed and now fails is a regression — fix it before finishing. Mark only the check you would stake the task on, never exploratory commands. A verify command that merely reads back a value you wrote yourself proves the write happened, not that the value is correct — when the expected value isn't taken directly from the task statement, prove it by recomputing the answer independently, never by comparing your own output to itself."
    # ESCALATION_PANEL fragment (see PANEL above, orthogonal to PROFILE): PANEL=1
    # keeps the original judge-panel text byte-identical (both tiers spawned
    # together, one escalation round — worth it only when the tiers are
    # genuinely decorrelated models, e.g. claude-open); PANEL=0 (default) swaps
    # in a two-rung ladder (unerr-opus alone first, unerr-fable only if still
    # unresolved) so a same-family-different-effort arm (claude-gpt,
    # claude-native) doesn't pay for a correlated second opinion by default.
    if [ "$PANEL" = "1" ]; then
      ESCALATION_SPAWN="Escalate by spawning unerr-opus AND unerr-fable IN PARALLEL (one message, two Task calls). Give each the SAME evidence brief — the task text, what you observed, what you tried, and ALL candidate approaches — but NOT your preferred hypothesis, so their reads stay independent. Instruct them to investigate and return a one-line root cause plus an exact minimal proposal WITHOUT editing files. Reconcile: if they agree, implement it; if they disagree, prefer the verdict that explains ALL observed evidence, then the one that fixes a definition site over one that compensates at a flow site. Exception — if a concrete fix already exists but has failed twice, run them in SEQUENCE instead: unerr-opus implements directly, then unerr-fable reviews the diff against the task. At most one escalation round per task; after reconciling, implement and finish."
    else
      ESCALATION_SPAWN="Escalate by spawning unerr-opus — ONE Task call. Give it the evidence brief — the task text, what you observed, what you tried, and ALL candidate approaches — but NOT your preferred hypothesis, so its read stays independent. Instruct it to investigate and return a one-line root cause plus an exact minimal proposal WITHOUT editing files. Implement that proposal, then re-run your verification. If the problem is STILL not resolved after that, escalate a SECOND time — spawn unerr-fable, and include unerr-opus's proposal and exactly why it failed; prefer the verdict that explains ALL observed evidence, and a fix at the definition site over one that compensates at a flow site. Exception — if a concrete fix already exists but has failed twice, have unerr-opus implement directly and then unerr-fable review the diff against the task. At most two escalation rounds per task; after the second, implement and finish."
    fi
    AUTONOMY_PROMPT="$AUTONOMY_PROMPT

TRACK — before your first edit, if the task takes 2+ steps call TaskCreate to write the plan down (one task per slice) and TaskUpdate each to completed as it lands; treat the tracker as your working memory across a long run, not bookkeeping, and clear it when the task is done.

SHAPE — classify the task before ONBOARD, into one of three shapes (this decides what onboarding and verification mean for THIS task): REPAIR — something exists and is broken (a repo, a failing test) — keeps the steps below as written: onboard, reproduce the failure first, fix, re-verify. PRODUCE — create an artifact to an exact spec (write a file, render an image, emit a report), no project to onboard and nothing failing at t=0 — reproduce-first is replaced by spec extraction: read the task statement and write down the exact output path, filename, format, field names, value constraints, and tolerances; those become what you verify against. OPERATE — make a system actually work (boot it, serve it, make it reachable) — probe the current state first, then verify by EXERCISING the running thing (curl the endpoint, ssh in, connect the client), never by inspecting config. Two rules apply regardless of shape: any non-text input (image, video, audio, binary) must be processed programmatically (PIL / cv2 / numpy / ffmpeg / objdump — install the tool if it is absent); looking at it may inform a hypothesis but is never the basis of an answer. And the exact output path, filename, and format are part of correctness, not presentation — re-read the task statement for them before finishing and confirm the artifact exists exactly where specified.

ONBOARD — before your first edit, learn how THIS project builds, tests, and runs itself: read its CI workflows (.github/workflows, .gitlab-ci.yml — the richest source, they list the exact commands maintainers run), its config/manifests (Makefile, package.json, pyproject.toml, Cargo.toml, go.mod, pom.xml, CMakeLists.txt), lockfiles, and README. If a runtime or tool the task needs is missing, install it yourself (uv/pip/npm/apt/apk) — never assume the environment is complete. Note the build / test / run / lint commands you find; you will verify against them.

FIX DISCIPLINE (applies to every edit you make):
- Fix at the definition. Change the entity whose behavior is wrong at the site where it is DEFINED; a fix that coerces or special-cases at a downstream site where the value merely flows through is almost always the wrong layer.
- Keep values in their native type. Emit each value in the type its source uses — a value that starts typed (an int, a field length, an enum member) carries that type through to where it is stored; do not collapse it to the rendered or stringified form you usually see it printed as.$TEST_FILES_BULLET

DELEGATION — use your agents when they pay, not by reflex:
- unerr-junior (fast, cheap): parallel recon across many files, running test suites or repro scripts (it reports exact output), web lookups. Do a single quick lookup yourself.
- unerr-worker (executor): scoped multi-file mechanical changes; run independent slices in parallel. Do a small single-file edit yourself.

ESCALATION — the moment a problem proves hard, STOP soloing (continuing to grind alone is how hard tasks are lost). Escalate on ANY of these countable triggers: (a) after 2 distinct attempts the problem's symptom is still present when you re-check; (b) you have edited the same file 3 or more times without reaching a working fix; (c) you have 2+ candidate approaches and the evidence does not decide between them; (d) $ESCALATION_TRIGGER_D.
$ESCALATION_SPAWN Triggers (b) and (d) are machine-checked at stop: if they have fired and you try to finish without having escalated, the stop gate blocks you and returns you to work.

$FINISH_CONTRACT"
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
# harness signal that reaches the bundle. (HARNESS_ON arms only — the hooks
# that write this state are installed only when open-models or claude-real.)
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
let inn=0,cap=0,ccreate=0,out=0,turns=0,usd=0,costSeen=false,model="";
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
    if(typeof ev.total_cost_usd==="number"){usd=ev.total_cost_usd;costSeen=true;}
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
// cost_reported distinguishes "no total_cost_usd in this CLI output" (usd stays
// the 0 fallback above) from a genuine $0 — the claude-real cost-capture path in
// run-benchmark.py reads this so it never mistakes an absent cost for a real one.
process.stderr.write("UNERR_TELEMETRY "+JSON.stringify({mode:process.env.UNERR_MODE||"on",model,turns,in_tokens:inn,cached_in:cap,cache_creation:ccreate,out_tokens:out,usd:Number(usd.toFixed(4)),cost_reported:costSeen,tool_calls:tot,mcp_tool_calls:mcp,tools})+"\n");
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
