#!/usr/bin/env python3
"""Custom Harbor agent — Claude Code CLI staged with the FULL unerr harness
(same install + shipped sub-agents + ON operator prompt the claude/claude-real
SWE-bench arms use) instead of Harbor's bare `claude-code` agent, for the
`terminal` benchmark's claude arms.

Loaded by harness_terminal.py via `--agent-import-path
harbor_agents:ClaudeUnerrAgent` (the module is resolved off PYTHONPATH, which
harness_terminal.py points at this file's own directory) for BOTH claude arms
UNLESS TERMINAL_STOCK_AGENT=1 (harness_terminal.py's bare-baseline control,
still Harbor's own unmodified claude-code agent). econ (opencode) is untouched
by this module.

── Pinned against harbor==0.20.0 (verified live 2026-07-19: `pip install
   harbor` into a scratch venv, then read the installed source — NOT taken on
   faith from any prior recon). Findings that shaped this class:
     - `BaseInstalledAgent.setup()` (harbor.agents.installed.base) is a
       concrete template method — it calls the ABSTRACT `install(environment)`
       and wraps it in error handling. Subclasses override `install()`, not
       `setup()`.
     - `ClaudeCode` (harbor.agents.installed.claude_code) already declares
       `append_system_prompt` as a CLI_FLAGS descriptor (`--append-system-prompt`,
       resolved once in `__init__`), and its `run()` reads gateway/auth env
       (ANTHROPIC_BASE_URL/API_KEY/CLAUDE_CODE_OAUTH_TOKEN) straight off
       os.environ / extra_env, builds `claude --print --output-format=
       stream-json ...` from `build_cli_flags()`, pipes the instruction over
       stdin, and tees to /logs/agent/claude-code.txt. Subclassing it and
       injecting our prompt into that ONE kwarg in `__init__` reuses run()
       COMPLETELY UNCHANGED — no auth/model/output-teeing code is duplicated
       here, and the class stays arm-agnostic (it never hard-codes a provider or
       gateway URL). It makes two run-influencing overrides:
       (1) `build_cli_flags()` (below) swaps Harbor's default
       `--permission-mode=bypassPermissions` for the nuclear
       `--dangerously-skip-permissions`, because bypassPermissions is silently
       downgraded to interactive prompt-mode when `claude -p` runs as root (as
       terminal-bench containers do) and then denies every Write/Edit/Bash —
       see that method's own docstring for the full root-cause; and
       (2) `ENV_VARS` (below) forwards the per-tier model aliases
       (ANTHROPIC_DEFAULT_{SONNET,OPUS,HAIKU,FABLE}_MODEL) + the main-loop
       ANTHROPIC_MODEL into the container, because run()'s hardcoded env dict
       drops them otherwise and the main loop then defaults to the missing
       claude-opus-4-8 — see that block's own comment for the full root-cause.
     - `ClaudeCode._build_register_mcp_servers_command()` writes
       `self.mcp_servers` (a `list[MCPServerConfig]`, normally populated from
       task config) to a USER-scoped `$CLAUDE_CONFIG_DIR/.claude.json` —
       "loads without a trust dialog" per its own docstring, unlike a
       project-scoped `.mcp.json`. Appending an `unerr --mcp` stdio entry to
       `self.mcp_servers` in `__init__` gets it wired by the SAME parent
       run(), with zero extra CLI flags or run() override (mirrors
       mcp-healthcheck.mjs's own `spawn("unerr", ["--mcp"], ...)` call shape).
     - Node-dependent installed agents (opencode.py, gemini_cli.py,
       qwen_code.py, pi.py, acp.py) all install Node via
       `harbor.agents.installed.node_install.nvm_node_install_snippet()` (an
       nvm install — official Node builds need glibc, matching this repo's
       Debian-based toolbox). Reused verbatim here for the SAME reason: the
       unerr CLI is an npm package with native deps (better-sqlite3/cozo-node).

── unerr-in-an-arbitrary-container install path (install() below): the
   vendored `unerr-ai-unerr-*.tgz` + `dev-entitlement.mjs` already baked into
   the dist image at /work/claude/local-docker/context (Dockerfile.dist's
   `COPY reference/claude/local-docker /work/claude/local-docker` — the SAME
   context/ Dockerfile.toolbox installs the claude-arm toolbox image from) are
   uploaded into the task container, Node is nvm-installed, `npm install -g`
   stages the unerr binary, `dev-entitlement.mjs mint pro` mints an offline-Pro
   entitlement (parsed from its stdout — each exec() below is its own process,
   so there is no shared shell to `eval` into, unlike run-instance.sh), then
   PYBIN is resolved (_resolve_pybin: prefer an already-present system
   python3/python — cheap, no upload; only when the task image has neither,
   upload + extract the vendored, self-contained python-build-standalone
   CPython into {UNERR_REMOTE_DIR}/py/ — no apt/apk/yum, no network from
   inside the task container), then `unerr index --force` + `unerr pm start`
   + `unerr install claude-code` wire the graph + daemon +
   .mcp.json/.claude/settings.json/CLAUDE.md, and finally the shipped
   `.claude/agents/unerr-*.md` sub-agent defs are copied over whatever
   `unerr install` just wrote — same order, same artifacts, run-instance.sh's
   ON path. We deliberately do NOT fabricate a git repo in the task workdir
   (no `apt-get install git`, no `git init`/`commit`) — that would mutate the
   benchmark's own environment with a repo the task never shipped, so `unerr
   index` (which hard-requires one) DEGRADES BY DESIGN on a bare-fixture
   terminal-bench task with no `.git` at all; it runs through _lenient_exec
   (log-and-continue) like every other best-effort step below. Getting
   `unerr` onto PATH and resolving PYBIN are the two hard gates (both raise
   on failure — the "unerr binary not on PATH" fatal check in run-instance.sh,
   and a missing interpreter meaning a task without gates, which would
   produce invalid benchmark data); the graph index / daemon start / `unerr
   install claude-code` steps past those gates stay BEST-EFFORT, exactly as
   lenient there too (a cold index or slow daemon boot degrades the run, it
   does not abort it).

── cc-harness-hooks.py determination (read live: e2e/reference/claude/
   local-docker/context/cc-harness-hooks.py): a single UNIVERSAL mode now —
   discover the project's own build/test/run check, reproduce-first, and
   verify against whatever command the agent itself marked with
   `# unerr:verify`; there is no fixed repo/tests/layout assumption and no
   "mechanically denied" test-edit guarantee. Rule T (the test-edit nudge)
   is a soft one-time reminder, not a hard deny; the old broad-test rule C
   is removed entirely. Gated OFF by default here via HARNESS_HOOKS (unset/0
   for terminal); any other value opts the SAME universal hooks in (see
   __init__'s hooks resolution) — there is no profile axis anymore. The
   appended ON prompt (_build_autonomy_prompt below) mirrors this: the
   FIX-DISCIPLINE "fix real source, not the checks" bullet is always
   present, and the FINISH CONTRACT paragraph (the machine-checked stop
   gate, describing the hooks' mechanical behavior) is only appended when
   hooks are on; the TRACK/ONBOARD/FIX-DISCIPLINE(root-cause+native-type)/
   DELEGATION guidance is generic prose and always included regardless of
   hooks.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import override

from harbor.agents.installed.base import EnvVar
from harbor.agents.installed.claude_code import ClaudeCode
from harbor.agents.installed.node_install import nvm_node_install_snippet
from harbor.environments.base import BaseEnvironment
from harbor.models.task.config import MCPServerConfig

# Loads before nvm's own install completes each exec() call (a fresh process
# every time — nvm's shell function state never survives across calls), so
# every command that needs node/npm/unerr on PATH re-sources it. `|| true`
# covers the (never-taken here, but harmless) case nvm was never installed.
_NVM_LOAD = '. "$HOME/.nvm/nvm.sh" 2>/dev/null || true; '

_BASE_AUTONOMY_PROMPT = (
    "You are operating fully autonomously in an automated benchmark, with no "
    "human available to answer questions. Resolve the task directly. Never "
    "ask questions, present options, seek confirmation, or enter plan mode — "
    "pick the most reasonable interpretation, implement it, and then stop."
)

_ON_OPERATOR_POLICY = (
    " Take the shortest correct path to a working solution. If you are "
    "unsure how to proceed, use web search to find the answer. Delegate "
    "independent sub-tasks to unerr subagents so they run in parallel. Do "
    "not modify test files unless the fix is impossible without it."
)


def _build_autonomy_prompt(hooks_on: bool, escalation_panel: bool = False) -> str:
    """The ON operator policy appended via --append-system-prompt — same base
    autonomy directive + shortest-path/delegation policy + TRACK/SHAPE/
    ONBOARD/FIX-DISCIPLINE/DELEGATION/ESCALATION/FINISH-CONTRACT prompt
    run-instance.sh's HARNESS_ON block uses (kept verbatim; not re-authored),
    collapsed to a SINGLE universal profile: classify the task's SHAPE
    (REPAIR/PRODUCE/OPERATE, §2 Layer 0 of HARNESS_UNIVERSAL.md) before
    onboarding, since what replaces reproduce-first and what the done-signal
    must exercise both depend on it; discover the project's own build/test/
    run check while onboarding (REPAIR); fix real source rather than the
    check; and verify against the agent-marked `# unerr:verify` command
    (including a line against restating your own written output as its own
    proof — the chess-best-move false-green). The FINISH CONTRACT paragraph
    (the machine-checked stop gate) is appended ONLY when `hooks_on`; the
    FIX-DISCIPLINE "fix real source, not the checks" bullet and the SHAPE
    paragraph are always present regardless of hooks. `escalation_panel`
    picks the ESCALATION paragraph's shape: False (default) is a mechanical
    two-rung LADDER (unerr-opus alone first, unerr-fable only if still
    unresolved) — the default because on the gateway arms both agents map to
    the same model family at different effort, so a parallel panel is a
    doubled bill for a correlated second opinion; True is the original
    PARALLEL panel (unerr-opus AND unerr-fable together), opt-in via
    ESCALATION_PANEL=1 for arms (e.g. claude-open) where the two tiers are
    genuinely different models."""
    test_files_bullet = (
        "\n- Fix real source, not the checks. A grader runs its own copy of "
        "the tests/checks, so editing a test or the verification itself to "
        "make it pass usually only fakes progress — fix the code the check "
        "exercises. Change a test only when the task itself is to change "
        "tests."
    )
    escalation_gate_note = (
        " Triggers (b) and (d) are machine-checked at stop: if they have "
        "fired and you try to finish without having escalated, the stop "
        "gate blocks you and returns you to work."
        if hooks_on else ""
    )
    # Trigger (d) is otherwise-identical shared ESCALATION prose (below) —
    # the universal profile's hooks track whatever command the agent itself
    # marked with `# unerr:verify` (there is no fixed repo-test convention to
    # name check failure against).
    trigger_d = (
        "your change turned your verification red and one rework did not "
        "recover it"
    )
    finish_contract = (
        (
            "\n\nFINISH CONTRACT — machine-checked when you try to stop (an "
            "unmet gate returns you to work with instructions): every task "
            "has a checkable outcome. Before your first change, decide the "
            "command that proves success for THIS task — prefer the "
            "project's own test/build/run check you found while onboarding; "
            "otherwise a script you write, curl the endpoint, or diff "
            "output against expected. Run it BEFORE you edit to confirm it "
            "fails the way the task describes (a reproduced failure is your "
            "grounded before/after), appending the marker comment "
            "`# unerr:verify` — the harness tracks marked commands only. "
            "After your final change, re-run the marked check and confirm "
            "it exits 0. The stop gate blocks finishing when no marked "
            "verification has succeeded since your last change; a marked "
            "command that once passed and now fails is a regression — fix "
            "it before finishing. Mark only the check you would stake the "
            "task on, never exploratory commands. A verify command that "
            "merely reads back a value you wrote yourself proves the write "
            "happened, not that the value is correct — when the expected "
            "value isn't taken directly from the task statement, prove it "
            "by recomputing the answer independently, never by comparing "
            "your own output to itself."
        ) if hooks_on else ""
    )
    # ESCALATION_PANEL frozen contract: True is the ORIGINAL parallel-panel
    # paragraph, byte-identical — never re-word it. False (default) is the
    # mechanical two-rung ladder — see this function's own docstring for why.
    escalation_action = (
        "Escalate by spawning unerr-opus AND unerr-fable IN PARALLEL (one "
        "message, two Task calls). Give each the SAME evidence brief — the "
        "task text, what you observed, what you tried, and ALL candidate "
        "approaches — but NOT your preferred hypothesis, so their reads "
        "stay independent. Instruct them to investigate and return a "
        "one-line root cause plus an exact minimal proposal WITHOUT editing "
        "files. Reconcile: if they agree, implement it; if they disagree, "
        "prefer the verdict that explains ALL observed evidence, then the "
        "one that fixes a definition site over one that compensates at a "
        "flow site. Exception — if a concrete fix already exists but has "
        "failed twice, run them in SEQUENCE instead: unerr-opus implements "
        "directly, then unerr-fable reviews the diff against the task. At "
        "most one escalation round per task; after reconciling, implement "
        "and finish."
    ) if escalation_panel else (
        "Escalate by spawning unerr-opus — ONE Task call. Give it the "
        "evidence brief — the task text, what you observed, what you tried, "
        "and ALL candidate approaches — but NOT your preferred hypothesis, "
        "so its read stays independent. Instruct it to investigate and "
        "return a one-line root cause plus an exact minimal proposal "
        "WITHOUT editing files. Implement that proposal, then re-run your "
        "verification. If the problem is STILL not resolved after that, "
        "escalate a SECOND time — spawn unerr-fable, and include "
        "unerr-opus's proposal and exactly why it failed; prefer the "
        "verdict that explains ALL observed evidence, and a fix at the "
        "definition site over one that compensates at a flow site. "
        "Exception — if a concrete fix already exists but has failed "
        "twice, have unerr-opus implement directly and then unerr-fable "
        "review the diff against the task. At most two escalation rounds "
        "per task; after the second, implement and finish."
    )

    return (
        _BASE_AUTONOMY_PROMPT + _ON_OPERATOR_POLICY +
        "\n\n"
        "TRACK — before your first edit, if the task takes 2+ steps call "
        "TaskCreate to write the plan down (one task per slice) and "
        "TaskUpdate each to completed as it lands; treat the tracker as your "
        "working memory across a long run, not bookkeeping, and clear it "
        "when the task is done.\n\n"
        "SHAPE — classify the task before ONBOARD, into one of three "
        "shapes (this decides what onboarding and verification mean for "
        "THIS task): REPAIR — something exists and is broken (a repo, a "
        "failing test) — keeps the steps below as written: onboard, "
        "reproduce the failure first, fix, re-verify. PRODUCE — create an "
        "artifact to an exact spec (write a file, render an image, emit a "
        "report), no project to onboard and nothing failing at t=0 — "
        "reproduce-first is replaced by spec extraction: read the task "
        "statement and write down the exact output path, filename, format, "
        "field names, value constraints, and tolerances; those become what "
        "you verify against. OPERATE — make a system actually work (boot "
        "it, serve it, make it reachable) — probe the current state first, "
        "then verify by EXERCISING the running thing (curl the endpoint, "
        "ssh in, connect the client), never by inspecting config. Two "
        "rules apply regardless of shape: any non-text input (image, "
        "video, audio, binary) must be processed programmatically (PIL / "
        "cv2 / numpy / ffmpeg / objdump — install the tool if it is "
        "absent); looking at it may inform a hypothesis but is never the "
        "basis of an answer. And the exact output path, filename, and "
        "format are part of correctness, not presentation — re-read the "
        "task statement for them before finishing and confirm the "
        "artifact exists exactly where specified.\n\n"
        "ONBOARD — before your first edit, learn how THIS project builds, "
        "tests, and runs itself: read its CI workflows (.github/workflows, "
        ".gitlab-ci.yml — the richest source, they list the exact commands "
        "maintainers run), its config/manifests (Makefile, package.json, "
        "pyproject.toml, Cargo.toml, go.mod, pom.xml, CMakeLists.txt), "
        "lockfiles, and README. If a runtime or tool the task needs is "
        "missing, install it yourself (uv/pip/npm/apt/apk) — never assume "
        "the environment is complete. Note the build / test / run / lint "
        "commands you find; you will verify against them.\n\n"
        "FIX DISCIPLINE (applies to every edit you make):\n"
        "- Fix at the definition. Change the entity whose behavior is wrong "
        "at the site where it is DEFINED; a fix that coerces or "
        "special-cases at a downstream site where the value merely flows "
        "through is almost always the wrong layer.\n"
        "- Keep values in their native type. Emit each value in the type "
        "its source uses — a value that starts typed (an int, a field "
        "length, an enum member) carries that type through to where it is "
        "stored; do not collapse it to the rendered or stringified form you "
        "usually see it printed as."
        + test_files_bullet +
        "\n\n"
        "DELEGATION — use your agents when they pay, not by reflex:\n"
        "- unerr-junior (fast, cheap): parallel recon across many files, "
        "running test suites or repro scripts (it reports exact output), "
        "web lookups. Do a single quick lookup yourself.\n"
        "- unerr-worker (executor): scoped multi-file mechanical changes; "
        "run independent slices in parallel. Do a small single-file edit "
        "yourself.\n\n"
        "ESCALATION — the moment a problem proves hard, STOP soloing "
        "(continuing to grind alone is how hard tasks are lost). Escalate "
        "on ANY of these countable triggers: (a) after 2 distinct attempts "
        "the problem's symptom is still present when you re-check; (b) you "
        "have edited the same file 3 or more times without reaching a "
        "working fix; (c) you have 2+ candidate approaches and the evidence "
        "does not decide between them; (d) " + trigger_d + ".\n"
        + escalation_action + escalation_gate_note
        + finish_contract
    )


def _context_dir() -> Path:
    """Locate the shipped harness artifacts dir (vendored unerr tgz,
    dev-entitlement.mjs, agents/unerr-*.md, cc-harness-hooks.py) — the SAME
    context/ Dockerfile.toolbox builds the claude-arm toolbox image from.
    CLAUDE_LOCALDOCKER_DIR overrides for a dev/laptop layout with a
    non-standard checkout location; the dist-image path is Dockerfile.dist's
    own COPY target (`COPY reference/claude/local-docker
    /work/claude/local-docker`); the repo-relative fallback resolves from
    THIS file's own location (e2e/distributed/tools/harbor_agents.py) for an
    in-place source-checkout run."""
    override = os.environ.get("CLAUDE_LOCALDOCKER_DIR")
    candidates = [
        (Path(override) / "context") if override else None,
        Path("/work/claude/local-docker/context"),
        Path(__file__).resolve().parent.parent.parent
        / "reference" / "claude" / "local-docker" / "context",
    ]
    for cand in candidates:
        if cand is not None and cand.is_dir():
            return cand
    # Fall through to the dist-image path even when absent — install()'s own
    # tgz/entitlement-file presence checks turn a genuine miss into a clear
    # RuntimeError rather than a confusing missing-attribute error here.
    return candidates[1]


def _find_unerr_tgz(context_dir: Path) -> Path | None:
    """The vendored unerr npm tarball (glob, not a pinned filename — Dockerfile
    .toolbox's own COPY uses the same `unerr-ai-unerr-*.tgz` glob so a
    version bump never needs a code change here)."""
    matches = sorted(context_dir.glob("unerr-ai-unerr-*.tgz"))
    return matches[0] if matches else None


def _find_python_standalone_tarball(context_dir: Path) -> Path | None:
    """The vendored python-build-standalone CPython tarball (glob, not a
    pinned filename — build-toolbox.sh's own fetch step names it by release
    tag + python version, so a version bump never needs a code change here).
    Only staged/used when the task image has no system python3/python at
    all — see ClaudeUnerrAgent._resolve_pybin."""
    matches = sorted(context_dir.glob(
        "cpython-*-x86_64-unknown-linux-gnu-install_only.tar.gz"))
    return matches[0] if matches else None


def _hooks_settings_command(remote_dir: str, hooks_on: bool,
                             hooks_value: str, escalation_panel: bool,
                             pybin: str) -> str:
    """Shell command writing .claude/settings.local.json + the PreToolUse
    auto-approve helper script (`allow-all.sh`).

    The AUTO-APPROVE PreToolUse hook is written UNCONDITIONALLY. Root cause
    (2026-07-20, build-pmars): `--dangerously-skip-permissions` bypasses the
    permission resolver for the MAIN Claude session ONLY — it does NOT propagate
    to Task SUB-AGENTS under `claude -p` as root, so a sub-agent's Bash / Read /
    WebSearch / WebFetch all come back "Permission to use <tool> has been denied"
    (main-agent Bash = 0 denials, sub-agent Bash = every call denied — identical
    on claude-code 2.1.212 and 2.1.215, so version-independent). The ON prompt
    delegates the real work to sub-agents, so this silently zeroed every task.
    A PreToolUse hook — unlike the flag, settings `defaultMode`, or sub-agent
    frontmatter `permissionMode`, none of which propagate — IS inherited by
    sub-agents from settings and fires before the permission resolver on every
    tool call, so emitting the documented {permissionDecision:"allow"}
    (code.claude.com/docs/en/hooks) grants the sub-agent's tools. VERIFIED
    locally: sub-agent denials 11->0, build-pmars reward 0/1 -> 1/1.

    Whenever hooks are on (HARNESS_HOOKS is any non-off value) the SAME three
    mechanical gate hooks — PreToolUse deny, PostToolUse record, and the Stop
    gate (byte-identical wiring to run-instance.sh step 3.15) — are added to
    the SAME hook arrays; there is a single universal profile now, so
    HARNESS_HOOKS on/off is the only axis forwarded to cc-harness-hooks.py.
    Claude Code evaluates a `deny` before an `allow`, so a denied edit stays
    blocked even though the allow-hook matches "*". Never
    .claude/settings.json — unerr owns that (written by `unerr install
    claude-code`); Claude Code UNIONS hook arrays across both files, so this
    only ADDS hooks, never clobbers unerr's own.

    Each gate-hook command is prefixed with an inline `env
    HARNESS_HOOKS=<hooks_value> ESCALATION_PANEL=<0|1>` — a Claude Code hook
    is spawned by the CLI as its OWN subprocess, not inherited from this
    install() step's Python process, so cc-harness-hooks.py cannot rely on
    session-env propagation to know whether hooks are on (or the escalation
    shape) to apply; the inline prefix pins it deterministically on every
    invocation. The PreToolUse allow-all hook above is hooks-agnostic and
    stays unprefixed.

    `pybin` is the interpreter path install() already resolved via
    _resolve_pybin (an already-present system python3/python, or the shipped
    python-build-standalone interpreter when the task image had neither). It
    is baked in as a LITERAL path — both in the hook "command" strings below
    and in the json.tool validator — rather than re-resolved with `command -v`
    at hook-invocation time, since a Claude Code hook subprocess isn't
    guaranteed to inherit this session's env or PATH."""
    # matcher "*" => fire on EVERY tool call; allow-all.sh prints the decision.
    pretooluse = (
        '      { "matcher": "*",\n'
        '        "hooks": [ { "type": "command", "command": "bash '
        + remote_dir + '/allow-all.sh" } ] }'
    )
    posttooluse = ""
    stop = ""
    if hooks_on:
        # Pinned inline so cc-harness-hooks.py's OWN subprocess sees hooks
        # are on (and the escalation shape) regardless of session-env
        # propagation (see this function's own docstring).
        escalation_panel_value = "1" if escalation_panel else "0"
        hook_env_prefix = (
            f"env HARNESS_HOOKS={hooks_value} "
            f"ESCALATION_PANEL={escalation_panel_value} "
        )
        pretooluse += (
            ",\n"
            '      { "matcher": "Bash|Edit|Write|MultiEdit|mcp__unerr__file_edit",\n'
            '        "hooks": [ { "type": "command", "command": "'
            + hook_env_prefix + pybin + ' '
            + remote_dir + '/cc-harness-hooks.py deny" } ] }'
        )
        posttooluse = (
            ',\n'
            '    "PostToolUse": [\n'
            '      { "matcher": "Bash|Task|Edit|Write|MultiEdit|mcp__unerr__file_edit",\n'
            '        "hooks": [ { "type": "command", "command": "'
            + hook_env_prefix + pybin + ' '
            + remote_dir + '/cc-harness-hooks.py record" } ] }\n'
            "    ]"
        )
        stop = (
            ',\n'
            '    "Stop": [\n'
            '      { "hooks": [ { "type": "command", "command": "'
            + hook_env_prefix + pybin + ' '
            + remote_dir + '/cc-harness-hooks.py gate" } ] }\n'
            "    ]"
        )
    settings_json = (
        "{\n"
        '  "hooks": {\n'
        '    "PreToolUse": [\n'
        + pretooluse + "\n"
        "    ]"
        + posttooluse + stop + "\n"
        "  }\n"
        "}\n"
    )
    # Quoted heredoc keeps the JSON literal; settings.local.json's own heredoc
    # stays unquoted for parity with the rest of this command, though nothing
    # in settings_json needs shell expansion any more — pybin is baked in as
    # a Python-side literal above, not a shell variable.
    approve_sh = (
        "#!/bin/bash\n"
        "cat <<'JSON'\n"
        '{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
        '"permissionDecision":"allow",'
        '"permissionDecisionReason":"unerr benchmark sandbox auto-approve"}}\n'
        "JSON\n"
        "exit 0\n"
    )
    # Root-caused 2026-07-20 (gpttb-terminal live smoke): a prior version
    # wrote ONLY the project-relative .claude/settings.local.json and ran
    # this whole command through _lenient_exec — a write that silently
    # failed (or landed somewhere Claude Code never reads) left every hook
    # inert with zero trace: `find / -name settings.local.json` in a live
    # task container came back empty, and PreToolUse/PostToolUse/Stop never
    # fired. Two hardenings close that hole. (1) The settings file is
    # written to BOTH the project-relative path Claude Code reads for a
    # project-scoped launch AND $HOME/.claude/ (a user-scope fallback —
    # cheap insurance against any CWD surprise). (2) Every artifact this
    # command writes or depends on is existence-checked, and both JSON
    # copies are parse-checked, in-command — exiting non-zero with an
    # unmistakable `FATAL: <path>` line on any miss. install() now runs
    # this command via exec_as_agent (raises on non-zero) instead of
    # _lenient_exec, so a FATAL here aborts the task run instead of quietly
    # degrading it — a task without gates produces invalid benchmark data,
    # which is worse than a loud failure.
    verify_targets = [f"{remote_dir}/allow-all.sh"]
    if hooks_on:
        verify_targets.append(f"{remote_dir}/cc-harness-hooks.py")
    verify_targets += [".claude/settings.local.json", "$HOME/.claude/settings.local.json"]
    exist_checks = "; ".join(
        f'[ -f {path} ] || {{ echo "FATAL: {path} missing after harness hooks '
        f'install" >&2; exit 1; }}'
        for path in verify_targets
    )
    json_checks = "; ".join(
        f'{shlex.quote(pybin)} -m json.tool {path} > /dev/null || '
        f'{{ echo "FATAL: {path} is not valid JSON" >&2; exit 1; }}'
        for path in (".claude/settings.local.json", "$HOME/.claude/settings.local.json")
    )
    return (
        "cat > " + remote_dir + "/allow-all.sh <<'SH'\n" + approve_sh + "SH\n"
        "chmod +x " + remote_dir + "/allow-all.sh; "
        "mkdir -p .claude $HOME/.claude && cat > .claude/settings.local.json <<EOF\n"
        + settings_json + "EOF\n"
        "cp .claude/settings.local.json $HOME/.claude/settings.local.json; "
        + exist_checks + "; " + json_checks
    )


class ClaudeUnerrAgent(ClaudeCode):
    """Claude Code CLI staged with the full unerr harness (install + shipped
    sub-agents + ON operator policy) instead of Harbor's bare claude-code
    agent — for BOTH claude terminal arms (gateway-routed `claude` and
    real-Anthropic `claude-real`; the arm's own auth/model env, set by
    harness_terminal.py, decides which provider it talks to). This class adds
    no provider/gateway-specific logic — it stages the harness, lets
    ClaudeCode's own run() (unchanged) drive the CLI, and makes two
    run-influencing overrides: build_cli_flags() forces
    `--dangerously-skip-permissions` (the arm-agnostic permission bypass proven
    by run-instance.sh; see that method), and ENV_VARS forwards the per-tier
    GPT model aliases (+ the main-loop ANTHROPIC_MODEL) into the container so the
    gateway ensemble actually routes (see the ENV_VARS block below for the
    root cause).
    @sem domain=benchmark-harness role=agent-adapter
    """

    UNERR_REMOTE_DIR = "/tmp/unerr-harness"

    # PER-TIER MODEL FORWARDING (root-caused 2026-07-19 on the GPT-5.6 gateway
    # smoke). Harbor's ClaudeCode.run() builds the CONTAINER env from a hardcoded
    # key list + the alias-flatten + `env.update(self._resolved_env_vars)` — it
    # does NOT forward arbitrary host/extra_env vars. So the four
    # ANTHROPIC_DEFAULT_{SONNET,OPUS,HAIKU,FABLE}_MODEL aliases that
    # harness_terminal._arm_agent_config puts in the agent's env NEVER reached
    # the CLI, and with no concrete --model the main loop fell back to Claude
    # Code's built-in default model id (claude-opus-4-8) — which the GPT gateway
    # does not publish -> "API Error: 400 ... Invalid model name passed in
    # model=claude-opus-4-8" on turn 1, every task 0-resolved. Declaring the
    # aliases as ENV_VARS routes them through `_resolved_env_vars`, which run()
    # merges into the container env LAST (after the alias-flatten, ~line 1477 of
    # claude_code.py), so:
    #   * ANTHROPIC_MODEL (the main-loop model) is pinned to the SONNET alias
    #     value (== `conductor`, the one tier harness_terminal always sets) — the
    #     main loop runs on the conductor model instead of the missing
    #     claude-opus-4-8; and
    #   * OPUS/HAIKU/FABLE keep their distinct per-tier models, so in-agent
    #     escalation and sub-agents each resolve to their intended GPT tier
    #     instead of collapsing to one.
    # env_fallback reads the agent's extra_env then os.environ (harness_terminal
    # merges extra_env into the harbor subprocess env), and a descriptor whose
    # fallback var is UNSET resolves to None and is dropped — so "absent stays
    # absent": the real-Anthropic claude-real arm (concrete --model, no
    # ANTHROPIC_DEFAULT_* set) is unaffected. Empty --model on the unerr path is
    # still REQUIRED: a concrete --model makes run() fire the flatten, which also
    # sets CLAUDE_CODE_SUBAGENT_MODEL (NOT re-overridden here) and would pin
    # every sub-agent to one tier.
    ENV_VARS = [
        *ClaudeCode.ENV_VARS,
        EnvVar("unerr_main_model", env="ANTHROPIC_MODEL", type="str",
               env_fallback="ANTHROPIC_DEFAULT_SONNET_MODEL"),
        EnvVar("unerr_sonnet_model", env="ANTHROPIC_DEFAULT_SONNET_MODEL",
               type="str", env_fallback="ANTHROPIC_DEFAULT_SONNET_MODEL"),
        EnvVar("unerr_opus_model", env="ANTHROPIC_DEFAULT_OPUS_MODEL",
               type="str", env_fallback="ANTHROPIC_DEFAULT_OPUS_MODEL"),
        EnvVar("unerr_haiku_model", env="ANTHROPIC_DEFAULT_HAIKU_MODEL",
               type="str", env_fallback="ANTHROPIC_DEFAULT_HAIKU_MODEL"),
        EnvVar("unerr_fable_model", env="ANTHROPIC_DEFAULT_FABLE_MODEL",
               type="str", env_fallback="ANTHROPIC_DEFAULT_FABLE_MODEL"),
    ]

    @staticmethod
    @override
    def name() -> str:
        # Distinct from AgentName.CLAUDE_CODE ("claude-code") so job/trial
        # labeling never conflates a harnessed run with Harbor's bare agent.
        return "claude-code-unerr"

    @override
    def build_cli_flags(self) -> str:
        """Force the PROVEN permission bypass (run-instance.sh's exact flow):
        drop Harbor's default `--permission-mode=bypassPermissions` and use the
        nuclear `--dangerously-skip-permissions` instead.

        WHY (root-caused 2026-07-19 on the GPT-5.6 gateway smoke): Claude Code
        SILENTLY DOWNGRADES `bypassPermissions` mode to the interactive
        prompt-mode when `claude -p` runs as ROOT (terminal-bench task
        containers run the agent as root). In non-interactive `-p` mode that
        prompt can't be answered, so every Write/Edit/Bash then comes back
        "requested permissions to write to <path>, but you haven't granted it
        yet" and the CLI exits non-zero -> Harbor's NonZeroAgentExitCodeError,
        0 tasks resolved. `--dangerously-skip-permissions` is instead HONORED
        under root because the inherited ClaudeCode.run() already exports
        IS_SANDBOX=1 ("Allow bypassPermissions mode when running as root inside
        containers"). run-instance.sh — the proven SWE-bench unerr+Claude flow
        that DOES land real edits in root containers — uses exactly this flag +
        IS_SANDBOX and passes NO --permission-mode; we mirror it byte-for-byte
        so no residual mode flag can re-trigger the downgrade. Applies to BOTH
        claude arms (run-instance.sh's skip flag is unconditional across
        open-models and claude-real)."""
        # Remove --permission-mode at the SOURCE (the base renderer skips a
        # None-valued flag) rather than string-stripping the rendered flags.
        self._resolved_flags["permission_mode"] = None
        # SHELL-SAFE --append-system-prompt (root-caused 2026-07-20 on the
        # gateway terminal smoke): harbor==0.20.0's build_cli_flags renders every
        # str flag as bare `f"{cli} {value}"` with NO quoting, and the whole
        # flag string is spliced into a `bash -c` script. Our autonomy/escalation/
        # finish-contract prompt carries `(...)`, backticks and newlines, so
        # unquoted it made bash mis-parse — `syntax error near unexpected token
        # '('` — dropping all but the first bare word of the prompt AND exiting
        # non-zero (NonZeroAgentExitCodeError; trial-FATAL under --max-retries 0,
        # i.e. econ/stock). Pop the flag so the parent omits it (renderer skips a
        # key that resolves to None), then re-add it shlex.quote'd as a single
        # shell token. Restore the raw value afterward for any later reader.
        append_prompt = self._resolved_flags.pop("append_system_prompt", None)
        flags = super().build_cli_flags()
        if append_prompt:
            self._resolved_flags["append_system_prompt"] = append_prompt
            flags = f"{flags} --append-system-prompt {shlex.quote(append_prompt)}".strip()
        # Bypass flag stays a trailing token — it can never touch an earlier value.
        return f"{flags} --dangerously-skip-permissions".strip()

    def __init__(self, logs_dir, *args, **kwargs):
        # HARNESS_HOOKS: unset/"0" -> off; any other value -> on. There is no
        # profile axis anymore — the harness has a single universal profile,
        # so HARNESS_HOOKS is purely on/off. self._hooks_env_value is the raw
        # string, re-forwarded verbatim to the hook processes themselves (see
        # _hooks_settings_command) since a Claude Code hook subprocess isn't
        # guaranteed to inherit this session's env.
        self._hooks_env_value = os.environ.get("HARNESS_HOOKS", "")
        self._hooks_on = self._hooks_env_value not in ("", "0")
        # Spliced UNQUOTED into the `env VAR=<value>` prefix of each hook
        # command inside the settings.local.json JSON (see
        # _hooks_settings_command's hook_env_prefix). It's an operator-set env
        # token, so in normal use it's a tiny safe vocabulary — but a typo'd
        # value carrying a space/quote/paren would corrupt the JSON (invalid
        # settings -> hooks silently don't load) AND break the hook's shell.
        # Fail LOUD on anything that isn't a bare token rather than degrade
        # silently. Empty is fine (HARNESS_HOOKS default).
        if self._hooks_env_value and not re.fullmatch(
                r"[A-Za-z0-9_.-]+", self._hooks_env_value):
            raise ValueError(
                f"HARNESS_HOOKS={self._hooks_env_value!r} is not a bare "
                r"token ([A-Za-z0-9_.-]+): it would corrupt the hook "
                "settings JSON and shell. Set it to a simple value.")
        # ESCALATION_PANEL toggles ladder-vs-panel escalation shape. Default
        # (unset/not "1") is the ladder; only "1" opts back into the parallel
        # panel (see _build_autonomy_prompt's docstring for why the default
        # flipped).
        self._escalation_panel = os.environ.get("ESCALATION_PANEL", "") == "1"
        prompt = _build_autonomy_prompt(self._hooks_on, self._escalation_panel)
        existing = kwargs.get("append_system_prompt")
        kwargs["append_system_prompt"] = (
            f"{existing}\n\n{prompt}" if existing else prompt
        )
        super().__init__(logs_dir, *args, **kwargs)
        # Register unerr as a Harbor-native MCP server so the PARENT run()'s
        # own _build_register_mcp_servers_command() wires it into the
        # user-scoped $CLAUDE_CONFIG_DIR/.claude.json (no trust dialog) — see
        # module docstring. Appended (not replacing) any servers the task's
        # own config already supplied via BaseAgent.__init__'s mcp_servers kwarg.
        self.mcp_servers = [
            *self.mcp_servers,
            MCPServerConfig(
                name="unerr", transport="stdio", command="unerr", args=["--mcp"],
            ),
        ]

    async def _lenient_exec(self, environment: BaseEnvironment, command: str,
                             env: dict[str, str] | None = None,
                             user: str | int | None = None):
        """Best-effort step — logs and continues past a failure instead of
        raising, mirroring run-instance.sh's own log-only/`|| true` treatment
        of everything past the hard unerr-binary-on-PATH gate in install().
        `user` lets a root-only step (e.g. an apt-get install) stay lenient
        too, same as the default-agent-user steps around it."""
        result = await environment.exec(command=command, env=env, user=user)
        if result.return_code != 0:
            self.logger.warning(
                "unerr harness setup step failed (non-fatal): %s\nstdout: "
                "%s\nstderr: %s", command,
                self._truncate_output(result.stdout),
                self._truncate_output(result.stderr))
        return result

    async def _resolve_pybin(self, environment: BaseEnvironment,
                              context_dir: Path) -> str:
        """The ONE place that decides which python3 interpreter
        cc-harness-hooks.py (at runtime) and the settings-JSON validator
        (_hooks_settings_command) use. Prefers an already-present system
        python3/python in the task image — cheap, no upload. Only when
        neither exists does it upload + extract the vendored, self-contained
        python-build-standalone CPython into `{UNERR_REMOTE_DIR}/py/` — no
        apt/apk/yum, no network from inside the task container, no writes
        outside UNERR_REMOTE_DIR. ~30% of terminal-bench base images ship no
        python3 at all, so this can't be skipped, but provisioning one via a
        package manager mutates the task's own environment — the exact thing
        this install() step stopped doing for git — hence the shipped
        interpreter instead. Raises loudly (mirroring the unerr-tgz missing
        check above) when neither a system interpreter nor the vendored
        tarball is available: a task with no working PYBIN produces invalid
        gate data, worse than a loud failure.
        @sem domain=benchmark-harness role=interpreter-provisioning
        """
        probe = await environment.exec(
            command="command -v python3 || command -v python")
        system_pybin = (probe.stdout or "").strip() if probe.return_code == 0 else ""
        if system_pybin:
            return system_pybin

        tarball = _find_python_standalone_tarball(context_dir)
        if tarball is None:
            raise RuntimeError(
                "no system python3/python in the task image AND no vendored "
                f"python-build-standalone tarball found under {context_dir} "
                "(expected cpython-*-x86_64-unknown-linux-gnu-install_only."
                "tar.gz) — run e2e/reference/claude/local-docker/"
                "build-toolbox.sh (or set CLAUDE_LOCALDOCKER_DIR to a context "
                "dir that has one) to vendor it, or rerun with "
                "TERMINAL_STOCK_AGENT=1 for the bare-baseline control instead "
                "of a broken ON arm"
            )

        remote_py_dir = f"{self.UNERR_REMOTE_DIR}/py"
        pybin = f"{remote_py_dir}/python/bin/python3"
        await self.exec_as_agent(environment, command=f"mkdir -p {remote_py_dir}")
        await environment.upload_file(
            source_path=tarball, target_path=f"{remote_py_dir}/{tarball.name}")
        await self.exec_as_agent(
            environment,
            command=(
                f"tar -xzf {remote_py_dir}/{tarball.name} -C {remote_py_dir} && "
                f"[ -x {pybin} ] || {{ echo \"FATAL: {pybin} missing after "
                f"extracting {tarball.name}\" >&2; exit 1; }}"
            ),
        )
        return pybin

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        # 1. Claude Code CLI — Harbor's own installer, byte-for-byte unmodified.
        await super().install(environment)

        context_dir = _context_dir()
        tgz = _find_unerr_tgz(context_dir)
        entitlement_src = context_dir / "dev-entitlement.mjs"
        if tgz is None or not entitlement_src.is_file():
            raise RuntimeError(
                f"unerr harness artifacts not found under {context_dir} "
                "(expected unerr-ai-unerr-*.tgz + dev-entitlement.mjs) — set "
                "CLAUDE_LOCALDOCKER_DIR to the vendored context dir, or rerun "
                "with TERMINAL_STOCK_AGENT=1 for the bare-baseline control "
                "instead of a broken ON arm"
            )

        remote = self.UNERR_REMOTE_DIR
        await self.exec_as_agent(environment, command=f"mkdir -p {remote}/agents")
        await environment.upload_file(source_path=tgz, target_path=f"{remote}/{tgz.name}")
        await environment.upload_file(
            source_path=entitlement_src, target_path=f"{remote}/dev-entitlement.mjs")

        agents_src = context_dir / "agents"
        if agents_src.is_dir():
            await environment.upload_dir(source_dir=agents_src, target_dir=f"{remote}/agents")

        if self._hooks_on:
            hooks_src = context_dir / "cc-harness-hooks.py"
            if not hooks_src.is_file():
                raise RuntimeError(
                    f"HARNESS_HOOKS={self._hooks_env_value!r} but "
                    f"cc-harness-hooks.py is missing under {context_dir}")
            await environment.upload_file(
                source_path=hooks_src, target_path=f"{remote}/cc-harness-hooks.py")

        # 2. Node runtime (unerr's CLI is an npm package with native deps —
        #    better-sqlite3/cozo-node) via the SAME nvm snippet harbor's own
        #    node-dependent agents use (see module docstring). 3. `npm
        #    install -g` the vendored tarball. THIS is the hard gate — mirrors
        #    run-instance.sh's "unerr binary not on PATH -> exit 3": fail
        #    loudly here rather than silently degrade the ON arm into a bare
        #    run downstream.
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                f"{nvm_node_install_snippet()} && "
                f"npm install -g {remote}/{tgz.name} && "
                "unerr --version"
            ),
        )

        # Offline-Pro entitlement (mirrors run-instance.sh's unerr_offline_pro):
        # minted locally, no cloud round-trip. Parsed here rather than
        # shell-eval'd — each exec() is its own process with no shared shell
        # state to `eval` into, unlike run-instance.sh's single long script.
        mint = await self.exec_as_agent(
            environment,
            command=_NVM_LOAD + f"node {remote}/dev-entitlement.mjs mint pro --fresh-hours 720",
        )
        entitlement_env: dict[str, str] = {}
        for line in (mint.stdout or "").splitlines():
            m = re.match(r"^export (UNERR_ENTITLEMENT_\w+)=(.*)$", line.strip())
            if m:
                entitlement_env[m.group(1)] = m.group(2)
        if "UNERR_ENTITLEMENT_KID" not in entitlement_env:
            raise RuntimeError(
                "unerr offline-Pro entitlement mint produced no KID/PUBKEY "
                f"(stdout tail: {(mint.stdout or '')[-300:]!r}) — cannot start unerrd"
            )
        entitlement_env["UNERR_TOKEN"] = os.environ.get(
            "UNERR_TOKEN", "unerr_sk_e2e_offline_benchmark")

        # `unerr index` hard-requires a git repo (`git rev-parse
        # --is-inside-work-tree`) — true by construction for a SWE-bench
        # workdir (always a git checkout) but NOT for an arbitrary
        # terminal-bench task workdir, which is often a bare fixture with no
        # `.git` at all. We deliberately do NOT fabricate one here (no
        # `apt-get install git`, no `git init`/`add`/`commit`) — that would
        # mutate the benchmark's own environment with a repo the task never
        # shipped. `unerr index` therefore DEGRADES BY DESIGN on a bare-
        # fixture task: the call below runs through _lenient_exec
        # (log-and-continue), same as every other step past the PATH gate,
        # so losing the graph index there does not abort the run.

        # PYBIN — the interpreter cc-harness-hooks.py (at runtime) and the
        # settings-JSON validator below shell out through. Resolved once, in
        # ONE place (_resolve_pybin): prefer an already-present system
        # python3/python; only when the task image has neither, upload +
        # extract the vendored, self-contained python-build-standalone
        # interpreter (no apt/apk/yum, no network from inside the task
        # container) — heterogeneous terminal-bench base images sometimes
        # lack python3 entirely (root-caused: ~30% of terminal tasks used to
        # fail hook install this way). Unlike the git bootstrap above, a
        # missing PYBIN is NOT best-effort — it raises (see _resolve_pybin).
        pybin = await self._resolve_pybin(environment, context_dir)

        # Best-effort past the PATH gate above — graph index, daemon start,
        # and `unerr install claude-code` are ALL log-and-continue in
        # run-instance.sh too (only the binary-on-PATH check is fatal there).
        await self._lenient_exec(
            environment, _NVM_LOAD + "unerr index --force --json", env=entitlement_env)
        await self._lenient_exec(
            environment, _NVM_LOAD + "unerr pm start", env=entitlement_env)
        await self._lenient_exec(
            environment, _NVM_LOAD + "unerr install claude-code", env=entitlement_env)

        # Overwrite the just-installed agent files with the shipped,
        # task-delegation-tuned versions (run-instance.sh step 3.1).
        if agents_src.is_dir():
            await self._lenient_exec(
                environment,
                "mkdir -p .claude/agents && "
                f"cp -f {remote}/agents/unerr-*.md .claude/agents/ 2>/dev/null || true",
            )

        # Settings.local.json: the PreToolUse auto-approve hook that lets Task
        # SUB-AGENTS run tools under -p/root is written ALWAYS (--dangerously-
        # skip-permissions reaches the main session only — see
        # _hooks_settings_command). The mechanical deny + record + Stop gate
        # ride in the SAME file whenever hooks are on (HARNESS_HOOKS is any
        # non-off value); hooks_value/escalation_panel are re-forwarded
        # inline on each gate-hook command (see _hooks_settings_command's own
        # docstring). STRICT (exec_as_agent, not _lenient_exec, root-caused
        # 2026-07-20): a silently-failed or silently-mislanded write here
        # leaves every gate inert with zero trace — a task without gates
        # produces invalid benchmark data, which is worse than a loud
        # failure, so this step now raises and aborts the run instead of
        # log-and-continue (the command's own in-line FATAL assertions give
        # the raised error an unmistakable message — see
        # _hooks_settings_command).
        await self.exec_as_agent(
            environment,
            command=_hooks_settings_command(
                remote, self._hooks_on,
                self._hooks_env_value, self._escalation_panel, pybin))
