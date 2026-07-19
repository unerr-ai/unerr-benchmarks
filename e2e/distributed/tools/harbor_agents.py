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
       COMPLETELY UNCHANGED — no auth/model/permission-bypass/output-teeing
       code is duplicated here, and the class stays arm-agnostic (it never
       reads ANTHROPIC_*/gateway env itself).
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
   so there is no shared shell to `eval` into, unlike run-instance.sh), then a
   git repo is bootstrapped (git installed as root if missing + `git init` +
   baseline commit as the agent user, since an arbitrary terminal-bench
   workdir — unlike a SWE-bench checkout — often starts with no `.git` at
   all and `unerr index` hard-requires one), then `unerr index --force` +
   `unerr pm start` + `unerr install claude-code` wire the graph + daemon +
   .mcp.json/.claude/settings.json/CLAUDE.md, and finally the shipped
   `.claude/agents/unerr-*.md` sub-agent defs are copied over whatever
   `unerr install` just wrote — same order, same artifacts, run-instance.sh's
   ON path. Only getting `unerr` onto PATH is a hard gate (raises, matching
   the "unerr binary not on PATH" fatal check in run-instance.sh); the git
   bootstrap / graph index / daemon start / `unerr install claude-code` steps
   are BEST-EFFORT past that gate, exactly as lenient there too (a cold index
   or slow daemon boot degrades the run, it does not abort it).

── cc-harness-hooks.py determination (read live: e2e/reference/claude/
   local-docker/context/cc-harness-hooks.py): its deny/gate rules are
   SWE-bench-shaped, not terminal-bench-general — rule T hardcodes `tests/`/
   `testing/` path segments, `is_broad_test`/`rule_b` assume a django/sympy-
   style `runtests.py|pytest|manage.py test|bin/test` repo-test convention,
   and the FINISH CONTRACT prose it backs asserts a "mechanically denied" test
   -edit guarantee. An arbitrary terminal-bench task has no fixed repo/tests/
   layout (often no repo at all), so wiring these hooks unconditionally would
   silently deny legitimate edits or assert a guarantee that doesn't hold.
   Gated OFF by default here via HARNESS_HOOKS (unset/0 for terminal);
   HARNESS_HOOKS=1 opts back in verbatim (same file, same
   .claude/settings.local.json shape run-instance.sh writes) for a terminal
   task that IS a SWE-bench-style repo checkout. The appended ON prompt
   (_build_autonomy_prompt below) mirrors this: the FIX-DISCIPLINE "test files
   are read-only" bullet and the whole FINISH CONTRACT paragraph — both of
   which describe the hooks' mechanical behavior — are only appended when
   HARNESS_HOOKS=1; the TRACK/FIX-DISCIPLINE(root-cause+native-type)/
   DELEGATION/ESCALATION guidance is generic prose and always included.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import override

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


def _build_autonomy_prompt(hooks_on: bool) -> str:
    """The ON operator policy appended via --append-system-prompt — same base
    autonomy directive + shortest-path/delegation policy + TRACK/FIX-
    DISCIPLINE/DELEGATION/ESCALATION contract run-instance.sh's HARNESS_ON
    block uses for the claude SWE-bench arms (kept verbatim; not re-authored).
    The two hooks-describing pieces (the read-only-tests bullet and the whole
    FINISH CONTRACT paragraph) are appended ONLY when `hooks_on` — see the
    module docstring's cc-harness-hooks.py determination."""
    test_files_bullet = (
        "\n- Test files are read-only in this benchmark — the harness "
        "mechanically denies test edits; never attempt them, and never "
        "count a test edit as part of a fix."
        if hooks_on else ""
    )
    escalation_gate_note = (
        " Triggers (b) and (d) are machine-checked at stop: if they have "
        "fired and you try to finish without having escalated, the stop "
        "gate blocks you and returns you to work."
        if hooks_on else ""
    )
    finish_contract = (
        "\n\nFINISH CONTRACT — machine-checked when you try to stop (an "
        "unmet gate returns you to work with instructions):\n"
        "- After your final edit, re-run your reproduction of the issue AND "
        "the narrowest existing verification covering each edited file; a "
        "finish without a green post-edit verification run is blocked.\n"
        "- A check that passed before your change and fails after it is a "
        "regression caused by your fix — rework it until green; finishing "
        "while it is red is blocked.\n"
        "- If the stop gate blocks you, do exactly what its message names, "
        "then finish. Do not fight the gate; it releases after its "
        "condition is met."
        if hooks_on else ""
    )

    return (
        _BASE_AUTONOMY_PROMPT + _ON_OPERATOR_POLICY +
        "\n\n"
        "TRACK — before your first edit, if the task takes 2+ steps call "
        "TaskCreate to write the plan down (one task per slice) and "
        "TaskUpdate each to completed as it lands; treat the tracker as your "
        "working memory across a long run, not bookkeeping, and clear it "
        "when the task is done.\n\n"
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
        "does not decide between them; (d) your change turned a previously-"
        "passing check red and one rework did not recover it.\n"
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
        "and finish." + escalation_gate_note
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


def _hooks_settings_command(remote_dir: str) -> str:
    """Shell command writing .claude/settings.local.json — byte-identical
    shape to run-instance.sh's own HARNESS_ON block (step 3.15): PreToolUse
    deny + PostToolUse record + Stop gate, all pointed at the uploaded
    cc-harness-hooks.py. Only invoked when HARNESS_HOOKS=1 (see module
    docstring's cc-harness-hooks.py determination). Never .claude/settings.json
    — unerr owns that file (written by `unerr install claude-code`); Claude
    Code UNIONS PreToolUse/PostToolUse/Stop hook arrays from both files, so
    this only ADDS hooks, never clobbers unerr's own."""
    settings_json = (
        "{\n"
        '  "hooks": {\n'
        '    "PreToolUse": [\n'
        '      { "matcher": "Edit|Write|MultiEdit|mcp__unerr__file_edit",\n'
        '        "hooks": [ { "type": "command", "command": "$PYBIN '
        + remote_dir + '/cc-harness-hooks.py deny" } ] }\n'
        "    ],\n"
        '    "PostToolUse": [\n'
        '      { "matcher": "Bash|Task|Edit|Write|MultiEdit|mcp__unerr__file_edit",\n'
        '        "hooks": [ { "type": "command", "command": "$PYBIN '
        + remote_dir + '/cc-harness-hooks.py record" } ] }\n'
        "    ],\n"
        '    "Stop": [\n'
        '      { "hooks": [ { "type": "command", "command": "$PYBIN '
        + remote_dir + '/cc-harness-hooks.py gate" } ] }\n'
        "    ]\n"
        "  }\n"
        "}\n"
    )
    return (
        'PYBIN="$(command -v python3 || command -v python || echo python3)"; '
        "mkdir -p .claude && cat > .claude/settings.local.json <<EOF\n"
        + settings_json + "EOF"
    )


class ClaudeUnerrAgent(ClaudeCode):
    """Claude Code CLI staged with the full unerr harness (install + shipped
    sub-agents + ON operator policy) instead of Harbor's bare claude-code
    agent — for BOTH claude terminal arms (gateway-routed `claude` and
    real-Anthropic `claude-real`; the arm's own auth/model env, set by
    harness_terminal.py, decides which provider it talks to). This class adds
    no provider/gateway-specific logic — it only stages the harness and lets
    ClaudeCode's own run() (unchanged) drive the CLI.
    @sem domain=benchmark-harness role=agent-adapter
    """

    UNERR_REMOTE_DIR = "/tmp/unerr-harness"

    @staticmethod
    @override
    def name() -> str:
        # Distinct from AgentName.CLAUDE_CODE ("claude-code") so job/trial
        # labeling never conflates a harnessed run with Harbor's bare agent.
        return "claude-code-unerr"

    def __init__(self, logs_dir, *args, **kwargs):
        self._hooks_on = os.environ.get("HARNESS_HOOKS") == "1"
        prompt = _build_autonomy_prompt(self._hooks_on)
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
                    f"HARNESS_HOOKS=1 but cc-harness-hooks.py is missing under {context_dir}")
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
        # `.git` at all. Bootstrap one so the index step below can actually
        # succeed instead of failing "not inside a git repository": install
        # git as root if the base image doesn't already have it (ClaudeCode's
        # own root-install step above only pulls in curl/procps, never git),
        # then init + a single baseline commit as the agent user, with the
        # identity inlined via `-c` so the commit can never prompt/fail on
        # missing user.email/user.name. Both steps are best-effort/
        # log-and-continue like every other step past the PATH gate.
        await self._lenient_exec(
            environment,
            "command -v git >/dev/null 2>&1 || "
            "(apt-get update -qq && apt-get install -y -qq git)",
            user="root",
        )
        await self._lenient_exec(
            environment,
            "git rev-parse --is-inside-work-tree >/dev/null 2>&1 || "
            "(git init -q && git add -A && "
            "git -c user.email=agent@unerr-bench.local "
            "-c user.name='unerr bench agent' "
            "commit -q -m baseline --allow-empty)",
        )

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

        # Mechanical finish-gate hooks — gated behind HARNESS_HOOKS (see
        # module docstring's cc-harness-hooks.py determination).
        if self._hooks_on:
            await self._lenient_exec(environment, _hooks_settings_command(remote))
