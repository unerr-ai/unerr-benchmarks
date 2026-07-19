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
      (src/harbor/agents/installed/claude_code.py). Rather than write a new
      agent class, this module reuses Harbor's OWN first-party installed
      agents, which already read gateway env vars straight off os.environ:
        - `claude_code.py` `run()`: `env["ANTHROPIC_API_KEY"] =
          self._get_env("ANTHROPIC_API_KEY") or ... "ANTHROPIC_AUTH_TOKEN"`;
          `env["ANTHROPIC_BASE_URL"] = os.environ.get("ANTHROPIC_BASE_URL")`;
          when ANTHROPIC_BASE_URL is set, `env["ANTHROPIC_MODEL"] =
          self.model_name` (the --model value, unmodified) and all the
          per-tier aliases (SONNET/OPUS/HAIKU/SUBAGENT) are set to match —
          the exact 2 env vars + defaulting shape
          e2e/reference/claude/local-docker/run-benchmark.py already uses.
        - `opencode.py` `run()`: for `--model openai/<id>`, forwards
          `OPENAI_API_KEY`/`OPENAI_BASE_URL` straight off os.environ into the
          container env, and `_build_register_config_command()` registers
          `provider.openai.options.baseURL = os.environ["OPENAI_BASE_URL"]`
          in opencode's own config so the "openai" provider actually calls
          that base URL. This is the OpenAI-compatible route the task's
          research question asked about.
        - `src/harbor/models/agent/name.py`: `AgentName.CLAUDE_CODE =
          "claude-code"`, `AgentName.OPENCODE = "opencode"` — the exact
          `--agent` values.
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
      every real terminal-bench-2-1 (2.1) task vendored here follows the SAME boilerplate
      (confirmed in the actual vendored `tests/test.sh` for both
      e2e/distributed/terminal-bench/tasks/{regex-log,chess-best-move}/):
      `uv run pytest /tests/test_outputs.py -rA` then
      `echo 1|0 > /logs/verifier/reward.txt` on the pytest exit code — i.e.
      pytest end-state, exactly as the brief described.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
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

# Wall-clock grace beyond the task's own declared timeout (task.toml's
# agent+verifier timeout_sec) or the worker's flat ceiling, covering harbor's
# own CLI/environment-build overhead — mirrors _resolve's `inst_timeout + 300`.
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

      claude -> --agent claude-code, ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN.
      claude-real -> --agent claude-code, CLAUDE_CODE_OAUTH_TOKEN only — real
                Anthropic subscription auth passed through from the worker's
                own env untouched, same as e2e/reference/claude/local-docker/
                run-benchmark.py's non-open-models auth path. No
                ANTHROPIC_BASE_URL, no ANTHROPIC_AUTH_TOKEN/API_KEY, no
                gateway/LiteLLM anything — this arm must never be able to
                route back through our gateway. Model is CLAUDE_MODEL (a
                stock Claude Code alias like "sonnet"/"opus") or "sonnet" if
                unset; raises (before Harbor ever launches) if
                CLAUDE_CODE_OAUTH_TOKEN is missing, so a misconfigured
                claude-real worker fails loudly instead of silently falling
                into the econ branch below.
      econ   -> --agent opencode, OPENAI_BASE_URL/OPENAI_API_KEY. econ's own
                toolbox binary is NOT invoked here — it isn't installable
                into an arbitrary per-task Harbor container built fresh from
                that task's own Dockerfile, so the econ arm reuses Harbor's
                opencode agent (opencode is econ's own base) against the
                same gateway + tier default instead.

    `vk` is run()'s per-instance LiteLLM virtual key (mint_instance_key
    result, or None on a mint miss/no master key) — when set it's used as
    the agent's gateway auth token INSTEAD of the shared master key, so
    run()'s later fetch_cost can read this instance's spend back in
    isolation from every other instance sharing the gateway. claude-real
    never touches the gateway so `vk` is irrelevant to it.
    """
    litellm_key = _litellm_key()
    token = vk or litellm_key
    gateway = os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_GATEWAY_URL)
    conductor = os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", DEFAULT_CONDUCTOR_MODEL)

    if worker.arm == "claude":
        env: dict[str, str] = {"ANTHROPIC_BASE_URL": gateway}
        if token:
            env["ANTHROPIC_AUTH_TOKEN"] = token
        return "claude-code", conductor, env

    if worker.arm == "claude-real":
        oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if not oauth_token:
            raise RuntimeError(
                "claude-real arm requires CLAUDE_CODE_OAUTH_TOKEN in the "
                "worker env (Claude Code CLI's native subscription auth) — "
                "refusing to fall through to gateway/econ auth")
        model = os.environ.get("CLAUDE_MODEL", "sonnet")
        return "claude-code", model, {"CLAUDE_CODE_OAUTH_TOKEN": oauth_token}

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
    ceiling either way — never fatal. Verified against the two tasks
    vendored under terminal-bench/tasks/ (both -> 1800)."""
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


def _find_result_json(jobs_dir: str) -> str | None:
    """The one trial's result.json under jobs_dir (TrialPaths.result_path —
    cited in the module docstring). Globs rather than assuming an exact
    trial_name (unconfirmed live), so a harbor version that renames the
    trial dir doesn't silently break this."""
    matches = glob.glob(os.path.join(jobs_dir, "**", "result.json"), recursive=True)
    return matches[0] if matches else None


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
    live for every agent). `harbor_log` (run()'s captured combined
    stdout+stderr of the `harbor run` subprocess) is copied whole as
    harbor-run.log — the ONLY place a SETUP-phase RuntimeError (raised before
    the agent ever starts, so trial_dir/trial.log don't exist yet) is ever
    captured, so it must land here even when trial_dir is absent."""
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


def run(worker, iid: str, scratch: str, abandon) -> tuple[bool, str, str, str]:
    """Run + grade ONE Terminal-Bench task via Harbor and report back to
    Worker._run_harness (worker-loop.py) in the harness_run contract:
    (resolved, report_text, patch, meta_text). `patch` is always '' — Harbor
    grades in-container (tests/test.sh -> pytest -> reward.txt, cited in the
    module docstring), there is no git diff to hand the coordinator. Also
    mints a per-instance LiteLLM virtual key up front and, when minting
    succeeded, reads its real spend back into meta_text's `cost` field after
    the harbor subprocess exits — the same real-spend attribution the claude
    arm's run-benchmark.py does for SWE-bench.
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
    # simply omitted below.
    litellm_key = _litellm_key()
    gateway_root = os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_GATEWAY_URL)
    run_id_env = os.environ.get("RUN_ID", "")
    vk = None
    vk_alias = ""
    if litellm_key:
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

    ceiling = worker.timeout
    task_hint = _task_timeout_hint(task_dir)
    timeout_s = max(ceiling, task_hint) + GRACE_S

    cmd = [
        HARBOR_BIN, "run",
        "--path", task_dir,
        "--agent", agent,
        "--model", model,
        "--env", "docker",
        "--jobs-dir", jobs_dir,
        "--job-name", iid,
        "-n", "1",
        "--max-retries", "0",
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
        while True:
            try:
                rc = proc.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                if abandon.is_set():
                    stall_reason = "abandoned: lease reaped mid-run"
                    _kill_process_group(proc)
                    proc.wait()
                    break
                if time.time() >= deadline:
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
    trial_dir = os.path.dirname(result_path) if result_path else None

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
        "model": model,
        "n_input_tokens": stats.get("n_input_tokens"),
        "n_cache_tokens": stats.get("n_cache_tokens"),
        "n_output_tokens": stats.get("n_output_tokens"),
        "cost_usd": stats.get("cost_usd"),
        "rc": rc,
    }
    if vk:
        # Real LiteLLM spend for this instance's vk — the field
        # tigris_archive.py's _norm_cost/build_overview reads (meta["cost"]).
        # cost_usd above stays Harbor's own model-priced agent_result figure
        # (debugging only) — "cost" always means real LiteLLM spend, per
        # repo convention, so this is the field that counts.
        meta["cost"] = fetch_cost(gateway_root, litellm_key, vk, alias=vk_alias)
    meta_text = json.dumps(meta)

    _collect_traces(trial_dir, art_dir, resolved, harbor_log=logpath)

    return resolved, report_text, "", meta_text
