"""Subprocess-driven tests for cc-harness-hooks.py.

Drives the hooks script exactly as Claude Code would: one hook-event JSON
object piped to stdin per subcommand invocation (record/gate/deny), state
isolated per test via CC_HARNESS_STATE pointed at a pytest tmp_path. Covers
both the profile-driven "generic" sensor (agent-declared `# unerr:verify`
Bash commands) and a regression-lock for the original "swe" sensor
(TEST_CMD_RE-gated pytest/manage.py-test/tox/bin-test/repro*), so a future
edit that accidentally changes swe behavior fails loudly here.

Run: python3 -m pytest e2e/reference/claude/local-docker/tests/test_cc_harness_hooks.py -q
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOOK_PATH = Path(__file__).resolve().parent.parent / "context" / "cc-harness-hooks.py"

BROAD_TEST_CMD = "pytest tests/test_foo.py"
NARROW_TEST_CMD = "pytest tests/ -k foo"


def _env(tmp_path, hooks, profile=None, escalation_panel=None):
    """Build a subprocess env with CC_HARNESS_STATE isolated to tmp_path and
    the HARNESS_HOOKS/HARNESS_PROFILE/ESCALATION_PANEL contract vars set
    explicitly (removed when None, never inherited ambiently from the test
    runner's own env). escalation_panel=None (the default) leaves
    ESCALATION_PANEL unset -> LADDER mode, matching the harness's own
    default."""
    env = dict(os.environ)
    env["CC_HARNESS_STATE"] = str(tmp_path)
    if hooks is None:
        env.pop("HARNESS_HOOKS", None)
    else:
        env["HARNESS_HOOKS"] = hooks
    if profile is None:
        env.pop("HARNESS_PROFILE", None)
    else:
        env["HARNESS_PROFILE"] = profile
    if escalation_panel is None:
        env.pop("ESCALATION_PANEL", None)
    else:
        env["ESCALATION_PANEL"] = escalation_panel
    return env


def run_hook(sub, payload, env):
    """Invoke `cc-harness-hooks.py <sub>` as a subprocess with `payload`
    JSON on stdin, exactly as Claude Code's hook runner does. Returns the
    parsed stdout JSON object, or None for a silent/no-decision response."""
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH), sub],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert proc.returncode == 0, (
        f"{sub} exited {proc.returncode}; stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    out = proc.stdout.strip()
    if not out:
        return None
    return json.loads(out)


def record_edit(env, file_path="src/thing.py"):
    run_hook("record", {"tool_name": "Edit", "tool_input": {"file_path": file_path, "new_string": "x"}}, env)


def record_bash(env, command, ok=True):
    run_hook(
        "record",
        {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "exit_code": 0 if ok else 1,
            "is_error": not ok,
        },
        env,
    )


def record_task(env, agent):
    run_hook("record", {"tool_name": "Task", "tool_input": {"subagent_type": agent}}, env)


def gate(env):
    return run_hook("gate", {}, env)


def deny(env, tool_name, file_path, tool_input_extra=None):
    tool_input = {"file_path": file_path}
    if tool_input_extra:
        tool_input.update(tool_input_extra)
    return run_hook("deny", {"tool_name": tool_name, "tool_input": tool_input}, env)


def block_reason(result):
    assert result is not None and result.get("decision") == "block", f"expected a block, got {result!r}"
    return result["reason"]


def deny_reason(result):
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    return hso["permissionDecisionReason"]


# ── generic profile ──────────────────────────────────────────────────────


def test_generic_z_blocks_idle_finish(tmp_path):
    env = _env(tmp_path, "generic")
    result = gate(env)
    reason = block_reason(result)
    assert "not modified" in reason or "no evidence any work happened" in reason


def test_generic_v_blocks_unverified_finish_message_contains_marker(tmp_path):
    env = _env(tmp_path, "generic")
    record_edit(env)  # keeps Gate Z from firing (edits > 0)
    result = gate(env)
    reason = block_reason(result)
    assert "# unerr:verify" in reason


def test_generic_marker_success_then_finish_allowed(tmp_path):
    env = _env(tmp_path, "generic")
    record_edit(env)
    time.sleep(0.02)
    record_bash(env, "make check # unerr:verify", ok=True)
    result = gate(env)
    assert result is None


def test_generic_edit_after_green_verify_blocks_v_again(tmp_path):
    env = _env(tmp_path, "generic")
    record_edit(env)
    time.sleep(0.02)
    record_bash(env, "make check # unerr:verify", ok=True)
    assert gate(env) is None  # satisfied once

    time.sleep(0.02)
    record_edit(env, file_path="src/other.py")  # new edit lands AFTER the green verify
    result = gate(env)
    reason = block_reason(result)
    assert "# unerr:verify" in reason


def test_generic_verify_cmd_green_then_red_blocks_r(tmp_path):
    env = _env(tmp_path, "generic")
    record_edit(env)
    time.sleep(0.02)
    record_bash(env, "make check # unerr:verify", ok=True)
    time.sleep(0.02)
    record_bash(env, "make check # unerr:verify", ok=False)  # same command -> same ledger key
    result = gate(env)
    reason = block_reason(result)
    assert "previously passed now fails" in reason


def test_generic_two_v_blocks_then_finish_demands_escalation(tmp_path):
    # PANEL mode (ESCALATION_PANEL=1): today's original single-block,
    # spawn-both-in-parallel escalation, byte-identical message — still
    # reachable as an opt-in. (LADDER is the harness default now; see the
    # ladder-specific tests below.)
    env = _env(tmp_path, "generic", escalation_panel="1")
    record_edit(env)  # never verified at all -> V fires repeatedly
    r1 = gate(env)
    r2 = gate(env)
    r3 = gate(env)
    assert "# unerr:verify" in block_reason(r1)
    assert "# unerr:verify" in block_reason(r2)
    reason3 = block_reason(r3)
    assert "unerr-opus" in reason3 and "unerr-fable" in reason3


def test_generic_t_does_not_deny_test_file_edit(tmp_path):
    env = _env(tmp_path, "generic")
    result = deny(env, "Edit", "tests/test_x.py")
    assert result is None


def test_generic_rule_c_does_not_deny_datetime_now(tmp_path, tmp_path_factory):
    env = _env(tmp_path, "generic")
    conv_file = tmp_path_factory.mktemp("conv") / "conv_utcnow.py"
    conv_file.write_text("import datetime\nx = datetime.datetime.utcnow()\n")
    result = deny(env, "Edit", str(conv_file), {"new_string": "y = datetime.now()"})
    assert result is None


def test_generic_unmarked_failing_command_never_triggers_r(tmp_path):
    env = _env(tmp_path, "generic")
    record_edit(env)
    time.sleep(0.02)
    record_bash(env, "grep TODO file.py", ok=True)  # no verify marker
    time.sleep(0.02)
    record_bash(env, "grep TODO file.py", ok=False)  # same key, unmarked, now failing
    result = gate(env)
    # some gate may still fire (V, since nothing was ever verify-marked), but
    # it must never be Gate R's regression message.
    if result is not None:
        assert "previously passed now fails" not in result["reason"]


# ── escalation ladder (ESCALATION_PANEL, default) ───────────────────────


def test_ladder_default_rung1_demands_opus_only(tmp_path):
    """ESCALATION_PANEL unset -> LADDER is the default: the same
    verification-revealed trigger fires rung 1, demanding unerr-opus ALONE,
    never mentioning unerr-fable."""
    env = _env(tmp_path, "generic")
    record_edit(env)  # never verified at all -> V fires repeatedly
    gate(env)  # V (1st)
    gate(env)  # V (cap 2)
    r3 = gate(env)  # V exhausted -> Gate E rung 1
    reason3 = block_reason(r3)
    assert "unerr-opus" in reason3
    assert "unerr-fable" not in reason3
    assert "rung 1" in reason3


def test_ladder_opus_recorded_no_new_trouble_allows(tmp_path):
    """Once unerr-opus has run and nothing has gone wrong since, Gate E must
    not nag for rung 2 — a clean finish is allowed."""
    env = _env(tmp_path, "generic")
    record_edit(env)
    time.sleep(0.02)
    record_bash(env, "make check # unerr:verify", ok=True)  # satisfies V
    time.sleep(0.02)
    record_task(env, "unerr-opus")
    result = gate(env)
    assert result is None


def test_ladder_rung2_fires_after_new_trouble_post_opus(tmp_path):
    """Rung 1 fires on a regression (R); unerr-opus is recorded; a NEW edit
    lands unverified afterward, which V catches again — that fresh V-block,
    timestamped after the opus Task event, is the "trigger persisted" signal
    that unlocks rung 2, demanding unerr-fable with opus's proposal + why it
    failed."""
    env = _env(tmp_path, "generic")
    record_edit(env)
    time.sleep(0.02)
    record_bash(env, "make check # unerr:verify", ok=True)
    time.sleep(0.02)
    record_bash(env, "make check # unerr:verify", ok=False)  # same key -> regression
    r_block = gate(env)
    assert "previously passed now fails" in block_reason(r_block)  # Gate R

    r_rung1 = gate(env)  # R capped -> falls through to Gate E rung 1
    reason1 = block_reason(r_rung1)
    assert "unerr-opus" in reason1 and "unerr-fable" not in reason1

    time.sleep(0.02)
    record_task(env, "unerr-opus")
    time.sleep(0.02)
    record_edit(env, file_path="src/other.py")  # new, unverified edit after opus

    r_v_again = gate(env)  # V fires again (edit landed after the last green verify)
    assert "# unerr:verify" in block_reason(r_v_again)

    r_rung2 = gate(env)  # over_cap now reached (R+E+V==3) -> only Gate E reachable
    reason2 = block_reason(r_rung2)
    assert "unerr-fable" in reason2
    assert "unerr-opus" in reason2  # must reference opus's proposal + why it failed
    assert "rung 2" in reason2


def test_ladder_no_further_block_after_both_agents_used(tmp_path):
    """Once both unerr-opus and unerr-fable have run, Gate E never blocks
    again, no matter how the trigger conditions look."""
    env = _env(tmp_path, "generic")
    record_edit(env)
    time.sleep(0.02)
    record_task(env, "unerr-opus")
    time.sleep(0.02)
    record_task(env, "unerr-fable")
    gate(env)  # V (1st)
    r2 = gate(env)  # V (cap 2) -> would have fed Gate E, but both agents used
    r3 = gate(env)  # Gate E must stay silent
    assert r3 is None
    assert r2 is not None  # sanity: V itself is unaffected by escalation state


def test_swe_ladder_default_rung1_demands_opus_only(tmp_path):
    """Same rung-1 shape under the swe profile — ESCALATION_PANEL is
    orthogonal to HARNESS_PROFILE."""
    env = _env(tmp_path, "1")  # HARNESS_HOOKS=1, no ESCALATION_PANEL -> LADDER
    record_edit(env)
    gate(env)  # V (1st)
    gate(env)  # V (cap 2)
    r3 = gate(env)  # Gate E rung 1
    reason3 = block_reason(r3)
    assert "unerr-opus" in reason3
    assert "unerr-fable" not in reason3


def test_swe_panel_mode_demands_both_agents(tmp_path):
    """ESCALATION_PANEL=1 under the swe profile reproduces today's
    spawn-both-in-parallel message, same as under generic."""
    env = _env(tmp_path, "1", escalation_panel="1")
    record_edit(env)
    gate(env)  # V (1st)
    gate(env)  # V (cap 2)
    r3 = gate(env)  # Gate E, panel shape
    reason3 = block_reason(r3)
    assert "unerr-opus" in reason3 and "unerr-fable" in reason3


# ── swe profile regression-lock ──────────────────────────────────────────


def test_swe_t_denies_tests_dir_edit(tmp_path):
    env = _env(tmp_path, "1")  # HARNESS_HOOKS=1, no HARNESS_PROFILE -> default "swe"
    result = deny(env, "Edit", "tests/test_foo.py")
    reason = deny_reason(result)
    assert "read-only" in reason


def test_swe_pytest_sensor_matches_broad_run(tmp_path):
    env = _env(tmp_path, "1")
    record_edit(env)
    time.sleep(0.02)
    record_bash(env, BROAD_TEST_CMD, ok=True)
    result = gate(env)
    assert result is None  # broad green post-edit satisfies V


def test_swe_narrow_k_filter_not_broad(tmp_path):
    env = _env(tmp_path, "1")
    record_edit(env)
    time.sleep(0.02)
    record_bash(env, NARROW_TEST_CMD, ok=True)
    result = gate(env)
    reason = block_reason(result)
    assert "NARROW" in reason


def test_swe_v_then_r_canned_sequence(tmp_path):
    env = _env(tmp_path, "1")
    record_edit(env)
    result_v = gate(env)
    reason_v = block_reason(result_v)
    assert "# unerr:verify" not in reason_v  # swe's V message never mentions the generic marker

    time.sleep(0.02)
    record_bash(env, BROAD_TEST_CMD, ok=True)
    time.sleep(0.02)
    record_bash(env, BROAD_TEST_CMD, ok=False)  # regression: passed, then failed
    result_r = gate(env)
    reason_r = block_reason(result_r)
    assert "previously passed now fails" in reason_r
