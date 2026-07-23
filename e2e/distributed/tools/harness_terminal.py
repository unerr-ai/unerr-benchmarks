#!/usr/bin/env python3
"""Terminal-Bench (Harbor) harness adapter — flow=harness_run for the `terminal`
benchmark descriptor (e2e/distributed/tools/benchmarks.py `_TERMINAL`).

Runs ONE vendored Terminal-Bench task headless through the Harbor framework
(`harbor run` == `harbor job start`, cited below), pointing Harbor's own
first-party agent adapter for the current arm (claude-code | opencode) at OUR
LiteLLM gateway instead of a real provider — the same env vars
e2e/reference/claude/local-docker/run-benchmark.py and e2e/econ already use
for their own model endpoint. Harbor builds + runs the agent INSIDE the
task's own container (its `environment/Dockerfile`, built by the worker's
already-booted in-VM dockerd) and grades with the task's own `tests/test.sh`
(conventionally a pytest run that writes /logs/verifier/reward.txt) — there
is no intermediate git patch, so `patch` is always ''.

Exposes exactly one entry point: `run(worker, iid, scratch, abandon)` — the
contract worker-loop.py's `Worker._run_harness` dispatches to for any
descriptor whose `harness_module` names this module (`mod.run(self, iid,
scratch, abandon)`).

── Harbor API + dataset provenance (verified 2026-07-17 against harbor 0.19.0).
   This module runs ONE local task dir via `harbor run --path <task_dir>` — the
   per-task model our fleet needs (one task per worker). It is version-agnostic:
   it never names a dataset, so switching the vendored task set switches the
   version with no code change here.

   The task set is Terminal-Bench 2.1 = the Harbor REGISTRY dataset
   `terminal-bench/terminal-bench-2-1` (89 tasks). `2.0`/`2.1` are NOT `@version`
   tags (`@2.1` resolves "not found") — they are distinct dataset NAMES:
   `terminal-bench-2` (2.0) vs `terminal-bench-2-1` (2.1), 89 tasks each. 2.1 is a
   registry dataset, NOT a clonable github repo, so Dockerfile.dist vendors it at
   build with `harbor dataset download terminal-bench/terminal-bench-2-1` (shallow
   sparse clones) into /work/terminal-bench/tasks. Distinct from the LEGACY
   `terminal-bench-core@0.1.1` (the old `tb run` CLI dataset, pip `terminal-bench`,
   github.com/laude-institute/terminal-bench) which Harbor superseded.
   benchmarks.py's `dataset`/`split` are informational — `_terminal_task_ids`
   resolves ids only from the vendored subdir names under `ids_source`/TB_TASKS_DIR.

  (a) headless single-task run + pass/fail:
      `harbor run` is registered as `app.command(name="run", ...)(start)` —
      literally `Alias for harbor job start` (src/harbor/cli/main.py:164).
      `start()`'s Dataset panel (src/harbor/cli/jobs.py) exposes `-p/--path
      "Path to a local task or dataset directory"` — pointed at ONE task's
      own directory this runs exactly that task. Pass/fail lives in the
      per-trial `result.json` (TrialResult.verifier_result.rewards; a Terminal
      task's own tests/test.sh writes 1.0/0.0 to reward.txt after `pytest`,
      confirmed below).
  (b) custom agent, OpenAI-compatible endpoint:
      Terminal-Bench's migration README ("Migrating from the Terminal-Bench
      Harness to Harbor") says to subclass BaseInstalledAgent/BaseAgent for a new agent,
      pointing at "how we support Claude Code"
      (src/harbor/agents/installed/claude_code.py). econ (opencode) and a
      TERMINAL_STOCK_AGENT=1 control run still reuse Harbor's OWN first-party
      installed agents unmodified; the claude/claude-real arms instead load a
      custom `harbor_agents.ClaudeUnerrAgent` (this dir's harbor_agents.py,
      via `--agent-import-path`) that subclasses `claude_code.ClaudeCode` to
      stage the full unerr harness (install + shipped sub-agents + ON
      operator prompt) around it — see that module's own docstring. All of
      them still read gateway env vars straight off os.environ:
        - `claude_code.py` `run()`: `env["ANTHROPIC_API_KEY"] =
          self._get_env("ANTHROPIC_API_KEY") or ... "ANTHROPIC_AUTH_TOKEN"`;
          `env["ANTHROPIC_BASE_URL"] = os.environ.get("ANTHROPIC_BASE_URL")`;
          when ANTHROPIC_BASE_URL is set, `env["ANTHROPIC_MODEL"] =
          self.model_name` (the --model value, unmodified) and all the
          per-tier aliases (SONNET/OPUS/HAIKU/SUBAGENT) are set to match —
          the exact 2 env vars + defaulting shape
          e2e/reference/claude/local-docker/run-benchmark.py already uses.
          `ClaudeUnerrAgent` never touches any of this itself (arm-agnostic
          by construction) — it only adds --append-system-prompt content and
          an extra MCP server entry, both read by the SAME unmodified run().
        - `opencode.py` `run()`: for `--model openai/<id>`, forwards
          `OPENAI_API_KEY`/`OPENAI_BASE_URL` straight off os.environ into the
          container env, and `_build_register_config_command()` registers
          `provider.openai.options.baseURL = os.environ["OPENAI_BASE_URL"]`
          in opencode's own config so the "openai" provider actually calls
          that base URL. This is the OpenAI-compatible route the task's
          research question asked about.
        - `src/harbor/models/agent/name.py`: `AgentName.CLAUDE_CODE =
          "claude-code"`, `AgentName.OPENCODE = "opencode"` — the exact
          `--agent` values (bare-baseline / econ paths only).
  (c) per-trial results.json + traces:
      `src/harbor/models/trial/paths.py` `TrialPaths` (docstring + properties):
      `trial_dir/result.json` (`result_path`, holds TrialResult JSON),
      `trial_dir/trial.log`, `trial_dir/agent/` ("Logs written by the agent
      ... saving trajectories"), `trial_dir/verifier/test-stdout.txt` +
      `test-stderr.txt` + `reward.txt`/`reward.json`,
      `trial_dir/artifacts/manifest.json`. `trial_dir` itself is
      `<jobs_dir>/<job_name>/...` (jobs_dir default "jobs",
      src/harbor/models/job/config.py) — this module passes both explicitly
      (`--jobs-dir`, `--job-name`) and globs for `result.json` under
      jobs_dir rather than hardcoding the trial subdir name, since the exact
      trial-dir naming under a job wasn't confirmed live.
  (d) grading is pytest end-state:
      Harbor's `Verifier.verify()` (src/harbor/verifier/verifier.py) just
      execs the task's OWN `tests/test.{sh,ps1,bat}` and parses whatever
      reward file it writes — pytest isn't hardcoded by the framework. But
      every real terminal-bench-2-1 (2.1) task follows the SAME boilerplate
      (confirmed in the actual vendored `tests/test.sh` for both
      {regex-log,chess-best-move} under the baked /work/terminal-bench/tasks/;
      host copy of the same task set: e2e/distributed/out/tb21-tasks/):
      `uv run pytest /tests/test_outputs.py -rA` then
      `echo 1|0 > /logs/verifier/reward.txt` on the pytest exit code — i.e.
      pytest end-state, exactly as the brief described.
"""
from __future__ import annotations

import base64
import glob
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time

# ── litellm_cost import (real LiteLLM spend capture) ────────────────────────
# litellm_cost.py is pure-stdlib (its own docstring: zero third-party
# imports) and lives beside the claude arm's run-benchmark.py; the dist
# image COPYs it to /work/claude/local-docker (Dockerfile.dist) but that dir
# isn't on this module's sys.path (only /work/distributed/tools is), so it's
# added here before the import. CLAUDE_LOCALDOCKER_DIR overrides for a
# dev/laptop layout; the relative fallback covers a source-checkout run
# in-place. Any layout the loop misses just leaves the import unresolved —
# the except below degrades to master-key-only stubs, mirroring
# run-benchmark.py's own ImportError fallback — never a crash.
for _cand in (
    os.environ.get("CLAUDE_LOCALDOCKER_DIR"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "claude", "local-docker"),
    "/work/claude/local-docker",
):
    if _cand and os.path.isdir(_cand) and _cand not in sys.path:
        sys.path.append(_cand)

try:
    from litellm_cost import mint_instance_key, fetch_cost
except ImportError:  # sibling module unavailable — degrade to master-key-only
    def mint_instance_key(base_url, master_key, *, alias, metadata, max_budget=50.0):
        return None

    def fetch_cost(base_url, master_key, vk, *, alias="", settle_timeout=25.0):
        return {"source": "unavailable"}

# ── Harbor binary + task-set location ───────────────────────────────────────
# Mirrors worker-loop.py's VENV_PY convention: harbor lives in the shared
# /work/.venv the Dockerfile.dist pip-install line (reported to the senior,
# not added here — Dockerfile.dist is out of scope for this module) puts it
# in; HARBOR_BIN overrides for a local/dev run where it's just on PATH.
HARBOR_BIN = os.environ.get("HARBOR_BIN", "/work/.venv/bin/harbor")
if not os.path.isfile(HARBOR_BIN):
    HARBOR_BIN = "harbor"  # dev/laptop fallback — assume it's on PATH

# Mirrors benchmarks.py's `_TERMINAL["ids_source"]` default so a run always
# resolves ids AND locates task dirs from the same root; TB_TASKS_DIR
# overrides both there and here together.
DEFAULT_TASKS_DIR = "/work/terminal-bench/tasks"

# Shared LiteLLM gateway (e2e/reference/claude/local-docker/run-benchmark.py's
# open-models default) — one gateway serves the Anthropic-compatible route
# (claude-code) and the OpenAI-compatible route (opencode's "openai"
# provider); a LiteLLM proxy exposes both natively from one deployment. NOT
# verified live for the OpenAI-compatible route specifically — see the
# senior digest.
DEFAULT_GATEWAY_URL = "https://econ-litellm.fly.dev"
# Conductor-tier default — same default run-benchmark.py uses for
# ANTHROPIC_DEFAULT_SONNET_MODEL under open-models.
DEFAULT_CONDUCTOR_MODEL = "minimax/minimax-m3"

# ── Arm family (naming scheme, 2026-07-20): econ | claude-<mix> | claude-native.
# claude-<mix> = Claude Code + unerr via the LiteLLM gateway (the <mix> suffix names
# the model ensemble: claude-gpt, claude-open, …); claude-native = real Anthropic
# (OAuth), no gateway. Behavior keys off these predicates, NEVER a literal arm name,
# so a new mix needs no change here. Legacy claude/claude-real are normalized upstream
# (run-distributed.sh) but are handled here too, for direct/local invocations.
_NATIVE_CLAUDE_ARMS = ("claude-native", "claude-real")


def _is_native_claude(arm: str) -> bool:
    """claude-native (real Anthropic / OAuth, no gateway). Includes legacy claude-real."""
    return arm in _NATIVE_CLAUDE_ARMS


def _is_claude_arm(arm: str) -> bool:
    """Any claude arm — gateway mix or native (incl. the legacy bare 'claude')."""
    return arm == "claude" or arm.startswith("claude-")


def _is_gateway_claude(arm: str) -> bool:
    """A claude-<mix> gateway ensemble (LiteLLM-routed) — every claude arm but native."""
    return _is_claude_arm(arm) and not _is_native_claude(arm)


# The claude-<mix>/claude-native arms drive Harbor's `--agent-import-path` at this
# module.path:ClassName (harbor_agents.py, this same tools/ dir — on
# PYTHONPATH, see run()) instead of Harbor's bare first-party claude-code
# agent, so the FULL unerr harness (install + shipped sub-agents + ON
# operator prompt) runs on terminal-bench too. TERMINAL_STOCK_AGENT=1 reverts
# both claude arms to the bare claude-code agent below (the bare-baseline
# control) — see _arm_agent_config.
UNERR_AGENT_IMPORT_PATH = "harbor_agents:ClaudeUnerrAgent"

# Wall-clock grace beyond the task's own declared timeout (task.toml's
# agent+verifier timeout_sec) or the worker's flat ceiling, covering harbor's
# own CLI/environment-build overhead. (The per-task AGENT budget itself is set
# by _bump_agent_timeout below; the no-output idle watchdog in run() uses
# worker.task_idle_s.)
GRACE_S = 600


def _kill_process_group(proc: "subprocess.Popen") -> None:
    """SIGKILL the whole process group — mirrors worker-loop.py's
    _kill_process_group (start_new_session=True makes proc a group leader)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _litellm_key() -> str | None:
    """LiteLLM gateway key from the host env — the same two names
    e2e/reference/claude/local-docker/run-benchmark.py's _load_litellm_key
    reads (no .env.local fallback here; the distributed worker VM gets its
    env from worker-entrypoint.sh, not a local dotfile)."""
    return os.environ.get("LITELLM_API_KEY") or os.environ.get("LITELLM_MASTER_KEY")


def _task_dir(worker, iid: str) -> str:
    tasks_dir = os.environ.get(
        "TB_TASKS_DIR", (worker.bench.get("ids_source") or DEFAULT_TASKS_DIR))
    return os.path.abspath(os.path.join(tasks_dir, iid))


def _arm_agent_config(worker, vk: str | None = None) -> tuple[str, str, dict]:
    """Map worker.arm -> (harbor --agent name, --model value, extra env vars)
    using Harbor's own first-party installed agents (cited in the module
    docstring), pointed at OUR gateway the same way each arm's own
    local-docker runner wires it:

      claude-<mix> (gateway) -> --agent-import-path harbor_agents:ClaudeUnerrAgent (unless
                TERMINAL_STOCK_AGENT=1, which reverts to Harbor's bare
                first-party --agent claude-code — the control run), same
                ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN either way, plus the
                four ANTHROPIC_DEFAULT_{SONNET,OPUS,HAIKU,FABLE}_MODEL tier
                aliases forwarded from the host env when set (the launcher's
                per-mix model map populates these — claude-gpt, claude-open, …;
                an operator override still wins; absent stays absent here — no
                invented defaults at this layer) so an in-agent escalation to
                opus/haiku/fable resolves through the gateway too. The unerr
                path passes NO concrete --model (returns "") — Harbor would
                otherwise flatten every tier alias to that one model behind a
                custom base URL, collapsing the ensemble; the SONNET alias is
                pinned to `conductor` as the main-loop model instead. The
                stock baseline still gets a concrete --model.
      claude-native -> same import-path swap, CLAUDE_CODE_OAUTH_TOKEN only —
                real Anthropic subscription auth passed through from the
                worker's own env untouched, same as e2e/reference/claude/
                local-docker/run-benchmark.py's non-open-models auth path. No
                ANTHROPIC_BASE_URL, no ANTHROPIC_AUTH_TOKEN/API_KEY, no
                gateway/LiteLLM anything — this arm must never be able to
                route back through our gateway. Model is CLAUDE_MODEL (a
                stock Claude Code alias like "sonnet"/"opus") or "sonnet" if
                unset; raises (before Harbor ever launches) if
                CLAUDE_CODE_OAUTH_TOKEN is missing, so a misconfigured
                claude-native worker fails loudly instead of silently falling
                into the econ branch below.
      econ   -> --agent opencode, OPENAI_BASE_URL/OPENAI_API_KEY, UNTOUCHED by
                the import-path swap above. econ's own toolbox binary is NOT
                invoked here — it isn't installable into an arbitrary
                per-task Harbor container built fresh from that task's own
                Dockerfile, so the econ arm reuses Harbor's opencode agent
                (opencode is econ's own base) against the same gateway + tier
                default instead.

    `vk` is run()'s per-instance LiteLLM virtual key (mint_instance_key
    result, or None on a mint miss/no master key) — when set it's used as
    the agent's gateway auth token INSTEAD of the shared master key, so
    run()'s later fetch_cost can read this instance's spend back in
    isolation from every other instance sharing the gateway. claude-native
    never touches the gateway so `vk` is irrelevant to it (run() also never
    mints one for claude-real — see its real-Anthropic cost-stamp branch).
    """
    litellm_key = _litellm_key()
    token = vk or litellm_key
    gateway = os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_GATEWAY_URL)
    conductor = os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", DEFAULT_CONDUCTOR_MODEL)
    stock_agent = os.environ.get("TERMINAL_STOCK_AGENT") == "1"
    claude_agent = "claude-code" if stock_agent else UNERR_AGENT_IMPORT_PATH

    if _is_gateway_claude(worker.arm):
        env: dict[str, str] = {"ANTHROPIC_BASE_URL": gateway}
        if token:
            env["ANTHROPIC_AUTH_TOKEN"] = token
        # Forward the four Claude Code tier aliases when the host set them, so
        # every tier (main loop + in-agent escalation to opus/haiku/fable)
        # resolves through the gateway. Absent stays absent — no invented
        # defaults — EXCEPT the SONNET alias, pinned to `conductor` (host value
        # or DEFAULT_CONDUCTOR_MODEL) right below: it's the main-loop model and,
        # since the unerr path no longer passes a concrete --model, it's the
        # only thing telling Claude Code what to run.
        for tier_var in (
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_FABLE_MODEL",
        ):
            tier_val = os.environ.get(tier_var)
            if tier_val:
                env[tier_var] = tier_val
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = conductor
        # PER-TIER ENSEMBLE FIX (root-caused 2026-07-19): pass NO concrete
        # --model on the unerr custom-agent path (empty string -> run() omits
        # the flag). Harbor's ClaudeCode.run() FLATTENS all tier aliases to the
        # single --model value whenever ANTHROPIC_BASE_URL is set ("set all
        # model aliases to the same model"), which would collapse the GPT
        # luna/terra/sol/sol-high ensemble to one tier AND pin every sub-agent
        # via CLAUDE_CODE_SUBAGENT_MODEL. With no --model, ANTHROPIC_MODEL is
        # never set pre-flatten so the flatten is skipped. But the aliases set in
        # this env dict do NOT reach the container on their own — run() builds
        # the container env from a hardcoded key list, so the actual forwarding
        # is done by ClaudeUnerrAgent.ENV_VARS (harbor_agents.py), which re-emits
        # these four aliases + pins the main-loop ANTHROPIC_MODEL to the SONNET
        # value; without that the CLI defaulted to claude-opus-4-8 (a model the
        # gateway does not publish) -> 400 on turn 1. The TERMINAL_STOCK_AGENT=1
        # bare baseline keeps a concrete --model (no tiering to preserve, and it
        # is not the ClaudeUnerrAgent so it has no ENV_VARS forwarding).
        model = conductor if stock_agent else ""
        return claude_agent, model, env

    if _is_native_claude(worker.arm):
        oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if not oauth_token:
            raise RuntimeError(
                "claude-real arm requires CLAUDE_CODE_OAUTH_TOKEN in the "
                "worker env (Claude Code CLI's native subscription auth) — "
                "refusing to fall through to gateway/econ auth")
        model = os.environ.get("CLAUDE_MODEL", "sonnet")
        return claude_agent, model, {"CLAUDE_CODE_OAUTH_TOKEN": oauth_token}

    # econ (default arm) — opencode, OpenAI-compatible route through the same
    # gateway. TERMINAL_OPENCODE_MODEL overrides the model id independently
    # of the claude-arm conductor default if the two ever need to diverge.
    model_id = os.environ.get("TERMINAL_OPENCODE_MODEL", conductor)
    model = model_id if model_id.startswith("openai/") else f"openai/{model_id}"
    env = {"OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL", gateway)}
    if token:
        env["OPENAI_API_KEY"] = token
    return "opencode", model, env


# Best-effort task.toml `[agent]`/`[verifier]` `timeout_sec` scrape — a regex,
# not a TOML parser (ZERO third-party imports at module load, matching
# benchmarks.py's design rule; tomllib needs Python 3.11+ and this module
# should run on whatever interpreter VENV_PY's sibling harbor venv ships).
_TOML_TIMEOUT_RE = re.compile(
    r"\[(agent|verifier)\]\s*\n(?:[^\[]*?\btimeout_sec\s*=\s*([0-9.]+))", re.S)


def _task_timeout_hint(task_dir: str) -> int:
    """Best-effort agent+verifier timeout_sec sum straight out of task.toml.
    0 if unreadable/absent; the caller falls back to the worker's flat
    ceiling either way — never fatal. Verified against two tasks under the
    baked /work/terminal-bench/tasks/ (regex-log, chess-best-move; both
    -> 1800)."""
    try:
        with open(os.path.join(task_dir, "task.toml"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return 0
    total = 0
    for _section, secs in _TOML_TIMEOUT_RE.findall(text):
        try:
            total += int(float(secs))
        except ValueError:
            continue
    return total


# Matches the [agent] section's timeout_sec (NOT [verifier]'s) — `[^\[]*?` can't
# cross into the next section header, so only the agent's value is captured.
_AGENT_TIMEOUT_RE = re.compile(r"(\[agent\][^\[]*?\btimeout_sec\s*=\s*)([0-9.]+)", re.S)


def _bump_agent_timeout(task_dir: str, ceiling_s: int) -> bool:
    """Raise task.toml's [agent] timeout_sec up to ceiling_s so Harbor gives the
    agent the full per-task budget instead of the stock terminal-bench 2.1 900s
    (which Harbor enforces INTERNALLY and would otherwise kill a legit long task
    mid-work). Idempotent — only rewrites when the declared value is lower.
    Best-effort: a missing/unreadable/unmatched task.toml returns False and is
    never fatal (the outer wrapper + idle watchdog still bound the run). Returns
    True when the file was changed. Leaves [verifier] timeout_sec (the grading
    budget) untouched."""
    path = os.path.join(task_dir, "task.toml")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    m = _AGENT_TIMEOUT_RE.search(text)
    if not m:
        return False
    try:
        cur = float(m.group(2))
    except ValueError:
        return False
    if cur >= ceiling_s:
        return False
    new_text = text[:m.start(2)] + str(ceiling_s) + text[m.end(2):]
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)
    except OSError:
        return False
    return True


def _find_result_json(jobs_dir: str) -> str | None:
    """The one trial's result.json under jobs_dir (TrialPaths.result_path —
    cited in the module docstring). Globs rather than assuming an exact
    trial_name (unconfirmed live), so a harbor version that renames the
    trial dir doesn't silently break this. Harbor 0.20.0 ALSO writes a
    job-level result.json one directory up; taking the shallow match made
    trial_dir the job dir, so every _collect_traces glob (trajectory.json,
    sessions.cast, err.txt) missed the nested trial dir — prefer the DEEPEST
    match, newest mtime on ties (retries create sibling trial dirs)."""
    matches = glob.glob(os.path.join(jobs_dir, "**", "result.json"), recursive=True)
    if not matches:
        return None
    return max(matches, key=lambda p: (p.count(os.sep), os.path.getmtime(p)))


def _find_trial_dir(jobs_dir: str, result_path: str | None) -> str | None:
    """The trial dir _collect_traces should read from — NOT simply
    os.path.dirname(result_path). Harbor 0.20.0 writes result.json at TWO
    depths: a job-level one at <jobs_dir>/<task>/result.json (always) and a
    trial-level one at <jobs_dir>/<task>/<task>__<hash>/result.json (ONLY
    once the trial COMPLETES). A trial killed mid-run (timeout/SIGKILL)
    never gets the trial-level result.json, so deriving trial_dir from
    result_path silently resolves to the job dir and every nested glob in
    _collect_traces (trial.log, agent/**/trajectory.json,
    agent/**/claude-session.jsonl) misses — even though claude-session.jsonl
    is written INCREMENTALLY specifically so a killed trial is still
    debuggable. Anchor on trial.log instead: Harbor writes it incrementally
    from trial start, so it exists for killed trials too. Same selection
    rule as _find_result_json (deepest match, newest mtime on ties —
    retries create sibling trial dirs). Falls back to
    os.path.dirname(result_path) when no trial.log is found, so behaviour
    is unchanged where it already worked."""
    matches = glob.glob(os.path.join(jobs_dir, "**", "trial.log"), recursive=True)
    if matches:
        deepest = max(matches, key=lambda p: (p.count(os.sep), os.path.getmtime(p)))
        return os.path.dirname(deepest)
    return os.path.dirname(result_path) if result_path else None


def _copy_first(patterns: list[str], dest: str) -> bool:
    for pat in patterns:
        for src in glob.glob(pat, recursive=True):
            if os.path.isfile(src):
                try:
                    shutil.copyfile(src, dest)
                    return True
                except OSError:
                    continue
    return False


def _find_sessions_dir(trial_dir: str) -> str | None:
    """Locate the sessions dir cc-harness-hooks.py's _sync_claude_session /
    _sync_all_claude_sessions write into (container-side
    DEFAULT_SESSIONS_DEST_DIR = /logs/agent/sessions) under trial_dir on the
    host — same **-glob indirection as the claude-session.jsonl copy just
    below (agent/** for the newer Harbor layout, steps/** for older), so this
    stays correct across the same layout variants _find_trial_dir already
    handles. Sorted so a stable candidate wins on the rare case of more than
    one match. None when absent — non-Claude terminal agents never write
    one, and that must stay a no-op, not an error."""
    for pat in (
        os.path.join(trial_dir, "agent", "**", "sessions"),
        os.path.join(trial_dir, "steps", "**", "sessions"),
    ):
        for cand in sorted(glob.glob(pat, recursive=True)):
            if os.path.isdir(cand):
                return cand
    return None


def _collect_traces(trial_dir: str | None, art_dir: str, resolved: bool,
                     harbor_log: str | None = None) -> None:
    """Best-effort trace collection into scratch/<run_id>/artifacts/<iid>/
    using the `terminal` descriptor's filenames (benchmarks.py `_TERMINAL`
    ["traces"]) so Worker._read_artifacts (worker-loop.py) picks them up.
    Harbor's own per-trial layout (TrialPaths, cited in the module
    docstring) has no events.jsonl — that name is this repo's OWN
    convention shared by the econ/claude arms, so it's synthesized here as a
    one-line summary rather than copied. trajectory.json / sessions.cast ARE
    plausible Harbor filenames (agent-written trajectory/asciinema cast) —
    copied when present, skipped otherwise (never fatal; not confirmed
    live for every agent). claude-session.jsonl is Claude Code's OWN
    incrementally-written session transcript, synced into Harbor's
    persisted /logs/agent/sessions/ dir by cc-harness-hooks.py's
    _sync_claude_session (claude-* arms only) — it survives a trial killed
    mid-run, unlike trajectory.json which Harbor only writes on completion.
    `harbor_log` (run()'s captured combined
    stdout+stderr of the `harbor run` subprocess) is copied whole as
    harbor-run.log — the ONLY place a SETUP-phase RuntimeError (raised before
    the agent ever starts, so trial_dir/trial.log don't exist yet) is ever
    captured, so it must land here even when trial_dir is absent.
    claude-sessions.tgz.b64 packages the WHOLE sessions dir cc-harness-
    hooks.py's additive _sync_all_claude_sessions synced (main session PLUS
    every Task sub-agent sidechain — escalation runs in a sub-agent, so this
    is the only place its transcript survives) into one gzip-compressed
    archive, base64-encoded as TEXT so it rides worker-loop.py's existing
    descriptor-driven text pipeline (_read_artifacts/_post_complete)
    unmodified, same as every other traces entry — see benchmarks.py
    `_TERMINAL["traces"]`. coordinator-entrypoint.sh decodes it back to a
    real .tgz at drain (same base64 idiom that pipeline already uses for
    opencode.db). Best-effort, never fatal; skipped when the sessions dir is
    absent (non-Claude terminal agents have none)."""
    os.makedirs(art_dir, exist_ok=True)

    try:
        with open(os.path.join(art_dir, "events.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "harness_result", "resolved": resolved, "ts": time.time(),
            }) + "\n")
    except OSError:
        pass

    if harbor_log:
        _copy_first([harbor_log], os.path.join(art_dir, "harbor-run.log"))

    if not trial_dir or not os.path.isdir(trial_dir):
        return

    _copy_first([os.path.join(trial_dir, "trial.log")],
                os.path.join(art_dir, "err.txt"))
    _copy_first([
        os.path.join(trial_dir, "agent", "**", "trajectory.json"),
        os.path.join(trial_dir, "steps", "**", "trajectory.json"),
    ], os.path.join(art_dir, "trajectory.json"))
    _copy_first([
        os.path.join(trial_dir, "agent", "**", "*.cast"),
        os.path.join(trial_dir, "steps", "**", "*.cast"),
    ], os.path.join(art_dir, "sessions.cast"))
    # claude-* arms only: cc-harness-hooks.py's _sync_claude_session
    # PostToolUse-copies Claude Code's OWN incrementally-written session
    # .jsonl into Harbor's persisted /logs/agent/sessions/ dir (container
    # side), which lands under trial_dir/agent/ on the host — same
    # durability guarantee as claude-code.txt. Unlike trajectory.json
    # (Harbor writes it only when a trial COMPLETES), this file is written
    # incrementally, so it survives a trial killed mid-run. Skipped, never
    # fatal, for non-Claude terminal agents (no such file exists).
    _copy_first([
        os.path.join(trial_dir, "agent", "**", "claude-session.jsonl"),
        os.path.join(trial_dir, "steps", "**", "claude-session.jsonl"),
    ], os.path.join(art_dir, "claude-session.jsonl"))
    # claude-* arms only: package the WHOLE sessions dir (main + every Task
    # sub-agent sidechain _sync_all_claude_sessions synced) into one gzip
    # archive, base64-encoded as text — see docstring above for why base64.
    # Best-effort, never fatal; no-op when the sessions dir is absent.
    sessions_dir = _find_sessions_dir(trial_dir)
    if sessions_dir:
        try:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                tar.add(sessions_dir, arcname="sessions")
            b64_text = base64.b64encode(buf.getvalue()).decode("ascii")
            with open(os.path.join(art_dir, "claude-sessions.tgz.b64"), "w",
                      encoding="utf-8") as f:
                f.write(b64_text)
        except (OSError, tarfile.TarError):
            pass


def _extract_reward(obj: dict) -> float | None:
    """Max achieved reward from a Harbor result.json. Tries the trial-level
    shape first (verifier_result.rewards.reward — older/single-trial
    harbor), then the job-level shape actually written today
    (stats.evals[*].reward_stats.reward buckets and .metrics[*].mean),
    taking the max across every eval since a job can cover several."""
    vr = (obj.get("verifier_result") or {}).get("rewards") or {}
    if isinstance(vr.get("reward"), (int, float)):
        return float(vr["reward"])
    best = None
    for ev in ((obj.get("stats") or {}).get("evals") or {}).values():
        if not isinstance(ev, dict):
            continue
        rs = ((ev.get("reward_stats") or {}).get("reward")) or {}
        for k, ids in rs.items():
            if ids:  # non-empty id list => a trial actually achieved this reward
                try:
                    best = max(best if best is not None else float("-inf"), float(k))
                except (TypeError, ValueError):
                    pass
        for m in (ev.get("metrics") or []):
            if isinstance(m, dict) and isinstance(m.get("mean"), (int, float)):
                best = max(best if best is not None else float("-inf"), float(m["mean"]))
    return best


def _is_silent_death(art_dir: str) -> bool:
    """Conservative silent-session-death check off the copied trajectory.json
    in art_dir (see _collect_traces): true ONLY when the file exists, parses,
    has a non-empty `steps` list, and the LAST step's `source` is `"user"` —
    Claude Code's own "[Your previous response had no visible output...]"
    nudge left unanswered, ending the session with rc=0 and no error (see
    DEBUG_FAILED_TASK.md Step 3 and the trajectory_json schema documented
    there: {agent, session_id, schema_version, steps[], final_metrics}).
    False on ANY ambiguity — missing/unparseable file, empty steps, or a last
    step from the agent — so a task that finished cleanly and was simply
    graded WRONG (e.g. TB2.1's chess-best-move, which fails reproducibly with
    a different wrong move every run) is never mistaken for a transient death
    and rerun at real cost; only Queue.claim (coordinator/server.py) acts on
    this flag, and only after its own conservative read
    (Queue._is_silent_death_meta) confirms it.
    """
    path = os.path.join(art_dir, "trajectory.json")
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            traj = json.load(f)
    except (OSError, ValueError):
        return False
    steps = traj.get("steps") if isinstance(traj, dict) else None
    if not isinstance(steps, list) or not steps:
        return False
    last = steps[-1]
    return isinstance(last, dict) and last.get("source") == "user"


def run(worker, iid: str, scratch: str, abandon) -> tuple[bool, str, str, str]:
    """Run + grade ONE Terminal-Bench task via Harbor and report back to
    Worker._run_harness (worker-loop.py) in the harness_run contract:
    (resolved, report_text, patch, meta_text). `patch` is always '' — Harbor
    grades in-container (tests/test.sh -> pytest -> reward.txt, cited in the
    module docstring), there is no git diff to hand the coordinator. Exports
    ECON_TASK_DEADLINE_MS (absolute epoch-ms) into the agent's env, sized off
    the task's own task.toml budget rather than the outer GRACE_S ceiling.
    The claude/claude-real arms drive Harbor's own `claude-code` agent
    replaced by the custom `harbor_agents:ClaudeUnerrAgent` (--agent-import-path,
    PYTHONPATH pointed at this module's dir) unless TERMINAL_STOCK_AGENT=1 —
    see _arm_agent_config. Mints a per-instance LiteLLM virtual key up front
    for claude/econ (never claude-real, which doesn't touch the gateway) and,
    when minting succeeded, reads its real spend back after the harbor
    subprocess exits into both meta_text's `cost` field (the claude arm's
    run-benchmark.py's real-spend attribution for SWE-bench) and `telemetry`
    (turns/usd/in_tokens/out_tokens/by_tier — the shape econ-telemetry.py/
    tigris_archive.py already expect, previously left null for every TB row).
    claude-real instead stamps `cost` from Harbor's own job-level
    stats.cost_usd with source "claude-native" (real-Anthropic $, never
    LiteLLM spend — the same label/shape run-benchmark.py's claude-real path
    uses, which tigris_archive.py's _norm_cost already special-cases).
    `--max-retries` defaults to 1 for the claude/claude-real custom-agent
    path (recovers a transient OAuth 429 burst) and 0 for econ/stock-agent,
    overridable for any arm via TERMINAL_MAX_RETRIES — this function only
    reads that env var, the launcher forwards it as a knob only when set.
    meta_text also carries `silent_death` (bool, see `_is_silent_death`) —
    true only when the trajectory's LAST step is an unanswered Claude Code
    nudge (`source=='user'`), the discriminator coordinator/server.py's
    failure-rerun reads to retry a silently-dead session without also
    retrying a genuinely wrong-but-clean completion.
    @sem domain=benchmark-harness role=orchestration
    """
    task_dir = _task_dir(worker, iid)
    if not os.path.isdir(task_dir):
        return False, json.dumps({
            "error": f"terminal task dir not found: {task_dir}",
        }), "", ""

    art_dir = os.path.join(scratch, worker.run_id, "artifacts", iid)
    jobs_dir = os.path.join(scratch, "harbor-jobs")
    os.makedirs(jobs_dir, exist_ok=True)

    # Real-LiteLLM-spend attribution: mint a per-instance virtual key so its
    # spend can be read back in isolation after the run (mirrors
    # run-benchmark.py's open-models per-instance vk, ~line 490-501). Mint
    # failure — no master key, or a gateway hiccup — never aborts the run:
    # _arm_agent_config falls back to the master key and meta["cost"] is
    # simply omitted below. Skipped entirely for claude-real: that arm never
    # touches the gateway (real-Anthropic auth only), so minting a vk for it
    # would be pointless and its cost is stamped from Harbor's own
    # agent-reported figure instead (claude-native branch below).
    litellm_key = _litellm_key()
    gateway_root = os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_GATEWAY_URL)
    run_id_env = os.environ.get("RUN_ID", "")
    vk = None
    vk_alias = ""
    if litellm_key and not _is_native_claude(worker.arm):
        vk_alias = f"terminal-{run_id_env}-{iid}"
        vk = mint_instance_key(
            gateway_root, litellm_key, alias=vk_alias,
            metadata={"arm": worker.arm, "benchmark": "terminal",
                      "run": run_id_env, "instance_id": iid},
            max_budget=50.0)

    agent, model, extra_env = _arm_agent_config(worker, vk=vk)

    env = os.environ.copy()
    env.update(extra_env)
    # Mirror _resolve: worker VM is already x86_64, don't force cross-arch.
    env["DOCKER_DEFAULT_PLATFORM"] = ""
    # ClaudeUnerrAgent (harbor_agents.py) is loaded via --agent-import-path
    # below, which harbor resolves with `importlib.import_module` — it must
    # be importable off the HARBOR SUBPROCESS's own sys.path, so put this
    # file's directory (harbor_agents.py lives right beside it, same tools/
    # dir on both the dist image and a laptop checkout) on PYTHONPATH. Only
    # when we're actually loading it (colon in `agent`) — never for econ or
    # a TERMINAL_STOCK_AGENT=1 control run.
    if ":" in agent:
        tools_dir = os.path.dirname(os.path.abspath(__file__))
        existing_pp = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            f"{tools_dir}{os.pathsep}{existing_pp}" if existing_pp else tools_dir)

    # Per-task backstop knobs, read off the Worker so terminal and the resolve
    # path (worker-loop._wait_with_backstop) honor the SAME TASK_CEILING_S /
    # TASK_IDLE_S. Defaults match Worker.__init__ (4h / 45min).
    task_ceiling_s = getattr(worker, "task_ceiling_s", 14400)
    task_idle_s = getattr(worker, "task_idle_s", 2700)

    # Give the agent the full per-task budget: raise task.toml's [agent]
    # timeout_sec (stock terminal-bench 2.1 ships 900s, which Harbor enforces
    # INTERNALLY and would otherwise SIGKILL a legit long task mid-work — this is
    # what failed build-cython-ext) up to the ceiling before harbor reads it.
    if _bump_agent_timeout(task_dir, task_ceiling_s):
        worker.log(f"{iid}: raised task.toml [agent] timeout_sec -> {task_ceiling_s}s")

    ceiling = worker.timeout
    task_hint = _task_timeout_hint(task_dir)
    timeout_s = max(ceiling, task_hint) + GRACE_S

    # H4.4 (gap-closure-plan.md, round-2 finding) — deadline wire: export
    # ECON_TASK_DEADLINE_MS so the agent isn't clock-blind. econ reads it as
    # an ABSOLUTE epoch-ms deadline (packages/opencode/src/session/prompts/
    # preambles.ts timeBudgetLine(): `Number(process.env.ECON_TASK_DEADLINE_MS)`
    # compared straight against Date.now(); prompt.ts headlessHoldMs() same
    # convention) — never a duration. Hand it the AGENT's real budget
    # (task_ceiling_s, the [agent] timeout we just wrote), not the raw task.toml
    # sum (which folds in the verifier's separate grading budget).
    env["ECON_TASK_DEADLINE_MS"] = str(int(time.time() * 1000) + task_ceiling_s * 1000)

    # `agent` is a plain Harbor agent name ("claude-code"/"opencode") for
    # econ and a TERMINAL_STOCK_AGENT=1 control run, or "module.path:Class"
    # (UNERR_AGENT_IMPORT_PATH) for the claude/claude-real harness path — a
    # colon can't appear in a bare agent name, so it cleanly picks the flag.
    # --agent-import-path is deprecated-but-functional in harbor 0.20.0 (the
    # pinned version harbor_agents.py was verified against): it only logs a
    # warning (cli/utils.py warn_deprecated_flag), never fails; --agent
    # itself now also accepts the "module.path:Class" form, but the explicit
    # flag is kept for clarity that this is a custom, not first-party, agent.
    agent_flag = "--agent-import-path" if ":" in agent else "--agent"
    # --max-retries: 0 everywhere by default (econ, TERMINAL_STOCK_AGENT=1,
    # and any other bare-agent path), EXCEPT the custom-agent path (claude/
    # claude-real driving harbor_agents:ClaudeUnerrAgent, same ":" in agent
    # test as agent_flag above) where the default is 1 — that harness's big
    # tool-result payload (~235k in/req) can burst the OAuth per-minute rate
    # limit under parallel workers, and with --max-retries 0 Harbor treats a
    # single 429 as trial-fatal. TERMINAL_MAX_RETRIES, when set, overrides
    # the default for every arm — this worker only READS the env; the
    # launcher (run-distributed.sh) forwards it to worker machines via its
    # TERMINAL_MAX_RETRIES passthrough line, so setting it on the launcher
    # invocation reaches this read.
    default_max_retries = "1" if ":" in agent else "0"
    max_retries = os.environ.get("TERMINAL_MAX_RETRIES", default_max_retries)
    # --model is OMITTED when `model` is empty (the unerr gateway-claude path —
    # see _arm_agent_config: keeps Harbor from flattening the per-tier aliases).
    # Every other path (claude-real, econ, stock baseline) passes it.
    model_args = ["--model", model] if model else []
    cmd = [
        HARBOR_BIN, "run",
        "--path", task_dir,
        agent_flag, agent,
        *model_args,
        "--env", "docker",
        "--jobs-dir", jobs_dir,
        "--job-name", iid,
        "-n", "1",
        "--max-retries", max_retries,
        "--yes",
        "--quiet",
    ]
    worker.log(f"{iid}: harbor run (agent={agent} model={model} ceiling={timeout_s}s) "
               f"-> {' '.join(cmd)}")

    logpath = os.path.join(scratch, "harbor-run.log")
    rc = None
    stall_reason = None
    with open(logpath, "wb") as logf:
        proc = subprocess.Popen(cmd, env=env, stdout=logf,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + timeout_s
        last_size = -1
        last_activity = time.time()
        while True:
            try:
                rc = proc.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                now = time.time()
                if abandon.is_set():
                    stall_reason = "abandoned: lease reaped mid-run"
                    _kill_process_group(proc)
                    proc.wait()
                    break
                # No-output idle watchdog: harbor streams the agent's whole
                # session into logf, so a wedged run stops growing the file.
                # Reclaims a silent hang well before the outer `timeout_s`.
                try:
                    size = os.path.getsize(logpath)
                except OSError:
                    size = last_size
                if size != last_size:
                    last_size = size
                    last_activity = now
                if now - last_activity >= task_idle_s:
                    stall_reason = f"idle-watchdog: no output for {task_idle_s}s (hung task)"
                    _kill_process_group(proc)
                    proc.wait()
                    break
                if now >= deadline:
                    stall_reason = f"timeout: no completion within {timeout_s}s"
                    _kill_process_group(proc)
                    proc.wait()
                    break

    if abandon.is_set():
        # _process's idempotency guard discards whatever we return anyway
        # (another worker owns this instance now) — skip result parsing.
        return False, "", "", ""

    if stall_reason:
        worker.log(f"{iid}: harbor {stall_reason}")

    result_path = _find_result_json(jobs_dir)
    trial_dir = _find_trial_dir(jobs_dir, result_path)

    resolved = False
    result_obj: dict = {}
    inner: dict = {"rc": rc}
    if result_path:
        try:
            with open(result_path, encoding="utf-8") as f:
                raw_text = f.read()
            result_obj = json.loads(raw_text) if raw_text.strip() else {}
            reward = _extract_reward(result_obj)
            resolved = reward is not None and reward >= 1.0
            # Exceptions ride alongside resolved, they don't flip it: terminal-bench's
            # own headline metric is mean reward = accuracy, so a trial that hit
            # reward 1.0 counts as solved even if an exception (e.g. AgentTimeoutError
            # after the agent's work already passed the tests) was also recorded.
            exceptions: set[str] = set()
            for ev in ((result_obj.get("stats") or {}).get("evals") or {}).values():
                if isinstance(ev, dict):
                    exceptions.update((ev.get("exception_stats") or {}).keys())
            inner["exceptions"] = sorted(exceptions)
            inner["harbor_result"] = result_obj
        except (OSError, ValueError, TypeError) as e:
            inner["error"] = f"could not parse result.json: {e}"
            if stall_reason:
                inner["stall_reason"] = stall_reason
    else:
        inner["error"] = stall_reason or f"harbor run produced no result.json (rc={rc})"
    inner["resolved"] = resolved
    # Wrap as {"<iid>": {"resolved": bool, ...}} — the per-instance shape
    # merge-reports.py's ids_from_report() already normalizes (docstring
    # there: "This is what the distributed worker actually posts"). Harbor's
    # own result.json (cited in the module docstring) has no such per-iid
    # key, so this repo's OWN report_json convention wraps it — same fix-up
    # resolve_then_grade gets for free from the swebench harness's own
    # per-instance report shape.
    report_text = json.dumps({iid: inner})

    # Harbor's job-level result.json has no top-level agent_result/agent_info
    # (those were a trial-level assumption that never matched live data) —
    # the token/cost debug fields actually live under stats. agent/model
    # aren't in stats either, so they keep falling back to the values this
    # run was invoked with.
    stats = result_obj.get("stats") or {}
    meta = {
        "instance_id": iid,
        "arm": worker.arm,
        "agent": agent,
        # `model` is "" on the unerr gateway-claude path (no --model passed);
        # fall back to the pinned SONNET alias so meta still names the real
        # main-loop tier instead of an empty string.
        "model": model or extra_env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", ""),
        "n_input_tokens": stats.get("n_input_tokens"),
        "n_cache_tokens": stats.get("n_cache_tokens"),
        "n_output_tokens": stats.get("n_output_tokens"),
        "cost_usd": stats.get("cost_usd"),
        "rc": rc,
    }
    if _is_native_claude(worker.arm):
        # Real-Anthropic $ (never LiteLLM spend — this arm never touches the
        # gateway, see _arm_agent_config) — no vk was minted, no fetch_cost
        # call. Stamp whatever Harbor's own job-level stats.cost_usd carries
        # for this claude-code run (same debug field `cost_usd` above reads;
        # Harbor's ATIF-derived job stats, not litellm_spend_logs) with
        # source "claude-native", the SAME shape+label
        # e2e/reference/claude/local-docker/run-benchmark.py's own
        # claude-real cost-capture path uses, so tigris_archive.py's
        # _norm_cost/build_overview (cost_source == "claude-native") reads it
        # identically to the SWE-bench claude-real rows.
        meta["cost"] = {"usd": stats.get("cost_usd"), "source": "claude-native"}
        if any(stats.get(k) is not None for k in
               ("n_input_tokens", "n_output_tokens", "n_cache_tokens")):
            meta["telemetry"] = {
                "in_tokens": stats.get("n_input_tokens"),
                "cached_in": stats.get("n_cache_tokens"),
                "out_tokens": stats.get("n_output_tokens"),
            }
    elif vk:
        # Real LiteLLM spend for this instance's vk — the field
        # tigris_archive.py's _norm_cost/build_overview reads (meta["cost"]).
        # cost_usd above stays Harbor's own model-priced agent_result figure
        # (debugging only) — "cost" always means real LiteLLM spend, per
        # repo convention, so this is the field that counts.
        cost = fetch_cost(gateway_root, litellm_key, vk, alias=vk_alias)
        meta["cost"] = cost
        # H4.3 (gap-closure-plan.md) — TB rows shipped meta_json.telemetry
        # null: unlike meta["cost"], build_overview/_row_from_meta
        # (tigris_archive.py, debug_instance.py) read turns/in_tokens/
        # out_tokens ONLY off meta["telemetry"], never off meta["cost"] —
        # so cost-only above left turn/token attribution blind even though
        # the vk-scoped spend already carries it. Reuse the SAME harvest
        # (litellm_cost.summarize_rows, called inside fetch_cost) instead of
        # re-parsing anything — its shape already matches e2e/econ/
        # econ-telemetry.py's convention (see that module's docstring), so
        # this is a straight field copy. Its per-vk LLM-call count
        # (`requests`) maps 1:1 to econ-telemetry.py's `turns` (each LiteLLM
        # call is one econ "step_finish").
        if cost.get("source") == "litellm_spend_logs":
            meta["telemetry"] = {
                "turns": cost.get("requests") or 0,
                "usd": cost.get("usd"),
                "in_tokens": cost.get("in_tokens"),
                "cached_in": cost.get("cached_in"),
                "out_tokens": cost.get("out_tokens"),
                "by_tier": cost.get("by_tier"),
            }
    _collect_traces(trial_dir, art_dir, resolved, harbor_log=logpath)

    # Silent-session-death discriminator (coordinator/server.py's
    # Queue._is_silent_death_meta reads this to decide failure-rerun
    # eligibility for a 'done'+resolved=0 row — see that function's
    # docstring). Computed off the just-copied art_dir trajectory.json (see
    # _collect_traces above) so it reads the exact bytes the coordinator
    # will also receive, not a possibly-stale trial_dir glob.
    meta["silent_death"] = _is_silent_death(art_dir)
    # No-gradeable-verdict death: harbor produced no parseable result.json, so
    # result_obj is still {} (an idle-watchdog / timeout kill or a crash before
    # grading — see the run-loop stall_reason branches and the
    # `no result.json` inner["error"] above). This is a TRANSIENT infra death,
    # NOT a capability miss: a clean run — even one graded WRONG
    # (chess-best-move, reward 0) — ALWAYS writes result.json, so result_obj is
    # non-empty for every genuine completion. A stalled/crashed terminal run
    # otherwise reports via /complete (report_text is always non-empty, so
    # worker-loop.py never routes it to /fail) and lands as done+resolved=0
    # with silent_death=false — invisible to the failure-rerun path. Flagged as
    # a DISTINCT signal (not folded into silent_death, which specifically means
    # the agent's own unanswered "no visible output" nudge) so the coordinator
    # (server.py Queue._eligible_rerun_ids / _is_no_result_death_meta) grants it
    # exactly one budgeted rerun.
    meta["no_result_death"] = not bool(result_obj)
    meta_text = json.dumps(meta)

    return resolved, report_text, "", meta_text
