"""Subprocess-driven tests for cc-harness-hooks.py.

Drives the hooks script exactly as Claude Code would: one hook-event JSON
object piped to stdin per subcommand invocation (record/gate/deny), state
isolated per test via CC_HARNESS_STATE pointed at a pytest tmp_path. Covers
the single always-on "universal" profile (the HARNESS_HOOKS/HARNESS_PROFILE
toggles were removed 2026-07-22 — cc-harness-hooks.py no longer reads any env
switch; a legacy "generic"/"1"/"swe" value left in the env is harmlessly
ignored): Gate Z/R/V/E driven purely by the
agent-declared `# unerr:verify` Bash marker (there is no fixed
test-runner sensor), Rule T's one-time, override-able deny on
test-shaped edit paths, and Rule N's one-time, capped, fail-open
anti-tautology nudge on a marked verify command that only compares a
file the agent wrote this session against a string literal.

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
    """Build a subprocess env with CC_HARNESS_STATE isolated to tmp_path and the
    ESCALATION_PANEL contract var set explicitly (removed when None, never
    inherited ambiently from the test runner's own env). HARNESS_HOOKS /
    HARNESS_PROFILE are still accepted for backward-compat and set here when
    passed, but cc-harness-hooks.py no longer reads them (removed 2026-07-22) —
    they're harmless. escalation_panel=None (the default) leaves ESCALATION_PANEL
    unset -> LADDER mode, matching the harness's own default."""
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


def deny_bash(env, command):
    return run_hook("deny", {"tool_name": "Bash", "tool_input": {"command": command}}, env)


def block_reason(result):
    assert result is not None and result.get("decision") == "block", f"expected a block, got {result!r}"
    return result["reason"]


def deny_reason(result):
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    return hso["permissionDecisionReason"]


# ── universal gate (Z/R/V/E) + rule T/B deny ─────────────────────────────


def test_z_blocks_idle_finish(tmp_path):
    env = _env(tmp_path, "generic")
    result = gate(env)
    reason = block_reason(result)
    assert "not modified" in reason or "no evidence any work happened" in reason


def test_v_blocks_unverified_finish_message_contains_marker(tmp_path):
    env = _env(tmp_path, "generic")
    record_edit(env)  # keeps Gate Z from firing (edits > 0)
    result = gate(env)
    reason = block_reason(result)
    assert "# unerr:verify" in reason


def test_marker_success_then_finish_allowed(tmp_path):
    env = _env(tmp_path, "generic")
    record_edit(env)
    time.sleep(0.02)
    record_bash(env, "make check # unerr:verify", ok=True)
    result = gate(env)
    assert result is None


def test_edit_after_green_verify_blocks_v_again(tmp_path):
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


def test_verify_cmd_green_then_red_blocks_r(tmp_path):
    env = _env(tmp_path, "generic")
    record_edit(env)
    time.sleep(0.02)
    record_bash(env, "make check # unerr:verify", ok=True)
    time.sleep(0.02)
    record_bash(env, "make check # unerr:verify", ok=False)  # same command -> same ledger key
    result = gate(env)
    reason = block_reason(result)
    assert "previously passed now fails" in reason


def test_two_v_blocks_then_finish_demands_escalation_panel(tmp_path):
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


def test_t_denies_test_edit_once_then_allows_same_edit_as_override(tmp_path):
    """Rule T is a one-time, override-able nudge, not a hard read-only deny:
    the first edit to a test-shaped path is denied with a message pointing
    at the grader running its own copy of the checks (fake-progress risk);
    re-issuing the SAME edit a second time is then treated as an
    evidence-cited override and allowed through."""
    env = _env(tmp_path, "generic")
    result = deny(env, "Edit", "tests/test_x.py")
    reason = deny_reason(result)
    assert "grader runs its own copy" in reason
    assert "fakes progress" in reason

    result2 = deny(env, "Edit", "tests/test_x.py")  # same file, re-issued
    assert result2 is None


# ── Rule N (anti-tautology nudge) ────────────────────────────────────────


def test_n_denies_tautological_verify_once_then_allows_same_command_as_override(tmp_path):
    """The chess-best-move false-green: the agent writes its answer to a
    file, then "verifies" by reading that SAME file's contents back and
    comparing to a literal it chose — that proves the write happened, not
    that the value is right. Fires once with a soft nudge; re-issuing the
    identical command a second time is an evidence-cited override."""
    env = _env(tmp_path, "generic")
    record_edit(env, file_path="move.txt")
    cmd = "test \"$(cat move.txt)\" = 'g2g4'  # unerr:verify"
    result = deny_bash(env, cmd)
    reason = deny_reason(result)
    assert "proves the file was written" in reason
    assert "not that the value is correct" in reason

    result2 = deny_bash(env, cmd)  # same command, re-issued
    assert result2 is None


def test_n_matches_bracket_test_and_grep_shapes_too(tmp_path):
    """The narrow detector covers all three documented shapes, not just
    `test "$(cat X)" = 'lit'`. Two SEPARATE state dirs (Rule N is capped at
    one denial per run — see the cap test below)."""
    env = _env(tmp_path / "bracket", "generic")
    record_edit(env, file_path="answer.txt")
    result = deny_bash(env, '[ "$(cat answer.txt)" = "42" ]  # unerr:verify')
    assert deny_reason(result)

    env2 = _env(tmp_path / "grep", "generic")
    record_edit(env2, file_path="answer2.txt")
    result2 = deny_bash(env2, "grep -q 'ok' answer2.txt  # unerr:verify")
    assert deny_reason(result2)


def test_n_does_not_fire_on_legitimate_build_or_curl_verify(tmp_path):
    """A real build/run/curl proof command — never a restatement of the
    agent's own output — must never be flagged, even against a file the
    agent wrote this session."""
    env = _env(tmp_path, "generic")
    record_edit(env, file_path="move.txt")
    assert deny_bash(env, "npm run build && npm test  # unerr:verify") is None
    assert deny_bash(env, "curl -sf http://localhost:8080/health  # unerr:verify") is None
    assert deny_bash(env, "cat move.txt  # unerr:verify") is None  # no comparison at all


def test_n_does_not_fire_when_file_was_not_written_this_session(tmp_path):
    """A literal comparison against a file the agent never edited this
    session (e.g. a fixture pre-seeded by the task) is not flagged — the
    detector only reads the ledger's existing edit events, never a new
    filesystem check."""
    env = _env(tmp_path, "generic")
    cmd = "test \"$(cat move.txt)\" = 'g2g4'  # unerr:verify"
    assert deny_bash(env, cmd) is None


def test_n_is_capped_at_one_denial_per_run_even_across_different_commands(tmp_path):
    """CAPPED: once Rule N has denied once this run, a DIFFERENT tautological
    command (different file, different literal) is no longer denied either —
    a hard-block-forever posture would false-positive on PRODUCE tasks where
    a literal comparison is legitimate."""
    env = _env(tmp_path, "generic")
    record_edit(env, file_path="a.txt")
    record_edit(env, file_path="b.txt")
    first = deny_bash(env, "test \"$(cat a.txt)\" = 'x'  # unerr:verify")
    assert deny_reason(first)

    second = deny_bash(env, "test \"$(cat b.txt)\" = 'y'  # unerr:verify")
    assert second is None


def test_rule_c_removed_does_not_deny_datetime_now(tmp_path, tmp_path_factory):
    env = _env(tmp_path, "generic")
    conv_file = tmp_path_factory.mktemp("conv") / "conv_utcnow.py"
    conv_file.write_text("import datetime\nx = datetime.datetime.utcnow()\n")
    result = deny(env, "Edit", str(conv_file), {"new_string": "y = datetime.now()"})
    assert result is None


def test_unmarked_failing_command_never_triggers_r(tmp_path):
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


def test_ladder_rung1_demands_opus_only_under_legacy_hooks_value(tmp_path):
    """Same rung-1 shape via HARNESS_HOOKS="1" (a legacy value that still
    resolves to the single universal profile) — ESCALATION_PANEL is
    orthogonal to HARNESS_HOOKS's exact value."""
    env = _env(tmp_path, "1")  # HARNESS_HOOKS=1, no ESCALATION_PANEL -> LADDER
    record_edit(env)
    gate(env)  # V (1st)
    gate(env)  # V (cap 2)
    r3 = gate(env)  # Gate E rung 1
    reason3 = block_reason(r3)
    assert "unerr-opus" in reason3
    assert "unerr-fable" not in reason3


def test_panel_mode_demands_both_agents_under_legacy_hooks_value(tmp_path):
    """ESCALATION_PANEL=1 under HARNESS_HOOKS="1" reproduces today's
    spawn-both-in-parallel message, same as under "generic" — both legacy
    values collapse to the same universal profile."""
    env = _env(tmp_path, "1", escalation_panel="1")
    record_edit(env)
    gate(env)  # V (1st)
    gate(env)  # V (cap 2)
    r3 = gate(env)  # Gate E, panel shape
    reason3 = block_reason(r3)
    assert "unerr-opus" in reason3 and "unerr-fable" in reason3


# ── legacy HARNESS_HOOKS="1" regression-lock (still maps to universal) ───


def test_t_denies_tests_dir_edit(tmp_path):
    env = _env(tmp_path, "1")  # HARNESS_HOOKS=1 -> universal
    result = deny(env, "Edit", "tests/test_foo.py")
    reason = deny_reason(result)
    assert "grader runs its own copy" in reason


def test_unmarked_test_run_never_satisfies_v_marked_run_does(tmp_path):
    """Universal Gate V has no fixed test-runner sensor: an unmarked pytest
    run never counts toward verification, whether it looks broad (a bare
    file target) or narrow (a `-k` filter) — shape is irrelevant. Only the
    literal `# unerr:verify` marker satisfies V."""
    env = _env(tmp_path, "1")
    record_edit(env)
    time.sleep(0.02)
    record_bash(env, BROAD_TEST_CMD, ok=True)  # unmarked broad-shaped run
    time.sleep(0.02)
    record_bash(env, NARROW_TEST_CMD, ok=True)  # unmarked narrow-shaped run
    result = gate(env)
    reason = block_reason(result)
    assert "# unerr:verify" in reason  # neither unmarked run satisfied V

    time.sleep(0.02)
    record_bash(env, BROAD_TEST_CMD + " # unerr:verify", ok=True)  # marked -> satisfies V
    assert gate(env) is None


def test_v_then_r_canned_sequence(tmp_path):
    env = _env(tmp_path, "1")
    record_edit(env)
    result_v = gate(env)
    reason_v = block_reason(result_v)
    assert "# unerr:verify" in reason_v  # universal V message names the marker

    time.sleep(0.02)
    record_bash(env, BROAD_TEST_CMD + " # unerr:verify", ok=True)
    time.sleep(0.02)
    record_bash(env, BROAD_TEST_CMD + " # unerr:verify", ok=False)  # regression: passed, then failed
    result_r = gate(env)
    reason_r = block_reason(result_r)
    assert "previously passed now fails" in reason_r


# ── claude session-transcript sync (record hook, piggybacked) ───────────────


def _write_session(config_dir, subdir, name, content, mtime=None):
    d = config_dir / "projects" / subdir
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(content)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def test_record_syncs_claude_session_when_hooks_on(tmp_path):
    """The PostToolUse record hook also best-effort-copies Claude Code's OWN
    session .jsonl into CC_HARNESS_SESSIONS_DEST on every call — this is
    what lets a killed/timed-out trial (no trajectory.json, no err.txt)
    still leave a transcript behind (see module docstring)."""
    config_dir = tmp_path / "claude-config"
    dest_dir = tmp_path / "sessions-dest"
    src = _write_session(config_dir, "-app", "session-uuid.jsonl",
                          '{"type":"user","message":"hi"}\n')

    env = _env(tmp_path / "state", "1")
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["CC_HARNESS_SESSIONS_DEST"] = str(dest_dir)

    record_edit(env)

    dest = dest_dir / "claude-session.jsonl"
    assert dest.is_file()
    assert dest.read_text() == src.read_text()


def test_record_syncs_oldest_session_when_no_isSidechain_field(tmp_path):
    """If more than one session .jsonl exists under projects/ and none of
    them carries an isSidechain field (older Claude Code, or no Task
    sub-agent spawned yet), the sync picks the candidate whose FIRST
    record has the earliest "timestamp" — the session's own start time.
    This must stay correct even after the main session file has been
    APPENDED to (Claude Code writes it incrementally all run long): a
    selection keyed on filesystem ctime would be wrong here, since ctime
    is bumped by every append and would make the actively-growing main
    session look "newer" than a short-lived, untouched-since sub-agent
    file — reproducing the exact flip-flop this fix exists to prevent."""
    config_dir = tmp_path / "claude-config"
    dest_dir = tmp_path / "sessions-dest"
    older = _write_session(
        config_dir, "-app", "old.jsonl",
        '{"type":"user","message":"hi","timestamp":"2026-07-21T00:00:00.000Z"}\n',
    )
    time.sleep(0.05)
    _write_session(
        config_dir, "-app", "new.jsonl",
        '{"type":"user","message":"bye","timestamp":"2026-07-21T00:00:05.000Z"}\n',
    )
    time.sleep(0.05)
    with open(older, "a") as f:  # bump old.jsonl's ctime past new.jsonl's
        f.write('{"type":"assistant","message":"more"}\n')

    env = _env(tmp_path / "state", "1")
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["CC_HARNESS_SESSIONS_DEST"] = str(dest_dir)

    record_edit(env)

    dest = dest_dir / "claude-session.jsonl"
    assert dest.read_text() == older.read_text()


def test_record_prefers_non_sidechain_session_over_newer_sidechain(tmp_path):
    """A Task sub-agent's session .jsonl marks its records
    "isSidechain": true; the sync must pick the non-sidechain (main-agent)
    session even when the sidechain file is the more-recently-created one
    — this is the exact defect fixed here (mtime picked whichever session
    was touched last, silently flipping mid-run to a sub-agent transcript,
    observed live 2026-07-21)."""
    config_dir = tmp_path / "claude-config"
    dest_dir = tmp_path / "sessions-dest"
    main = _write_session(config_dir, "-app", "main.jsonl", '{"type":"user","message":"hi"}\n')
    time.sleep(0.05)
    _write_session(
        config_dir, "-app", "sidechain.jsonl",
        '{"type":"assistant","isSidechain":true,"message":"sub-agent turn"}\n',
    )

    env = _env(tmp_path / "state", "1")
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["CC_HARNESS_SESSIONS_DEST"] = str(dest_dir)

    record_edit(env)

    dest = dest_dir / "claude-session.jsonl"
    assert dest.read_text() == main.read_text()


# ── additive sync: EVERY session (main + Task sub-agent sidechains) ─────────


def test_sub_agent_sessions_synced_under_distinct_names(tmp_path):
    """Additive to the single claude-session.jsonl main-session copy: EVERY
    candidate session .jsonl — main AND every Task sub-agent sidechain —
    must ALSO be synced into the sessions dest dir under its own
    "<sessionId>.jsonl" filename, so escalation (which runs in a Task
    sub-agent) leaves its own transcript behind too — the gap this closes."""
    config_dir = tmp_path / "claude-config"
    dest_dir = tmp_path / "sessions-dest"
    main = _write_session(
        config_dir, "-app", "main.jsonl",
        '{"type":"user","message":"hi","sessionId":"main-uuid-1"}\n',
    )
    time.sleep(0.05)
    sub = _write_session(
        config_dir, "-app", "sidechain.jsonl",
        '{"type":"assistant","isSidechain":true,"message":"sub-agent turn",'
        '"sessionId":"sub-uuid-2"}\n',
    )

    env = _env(tmp_path / "state", "1")
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["CC_HARNESS_SESSIONS_DEST"] = str(dest_dir)

    record_edit(env)

    assert (dest_dir / "main-uuid-1.jsonl").read_text() == main.read_text()
    assert (dest_dir / "sub-uuid-2.jsonl").read_text() == sub.read_text()


def test_additive_sync_does_not_change_main_session_selection(tmp_path):
    """Guard against regressing the just-landed main-session SELECTION fix:
    adding the per-session sync must not change WHICH file claude-
    session.jsonl is a copy of — the sidechain must still be excluded from
    that one selection, even though it now ALSO gets its own per-session
    copy alongside it."""
    config_dir = tmp_path / "claude-config"
    dest_dir = tmp_path / "sessions-dest"
    main = _write_session(
        config_dir, "-app", "main.jsonl",
        '{"type":"user","message":"hi","sessionId":"main-uuid-3"}\n',
    )
    time.sleep(0.05)
    _write_session(
        config_dir, "-app", "sidechain.jsonl",
        '{"type":"assistant","isSidechain":true,"message":"sub-agent turn",'
        '"sessionId":"sub-uuid-4"}\n',
    )

    env = _env(tmp_path / "state", "1")
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["CC_HARNESS_SESSIONS_DEST"] = str(dest_dir)

    record_edit(env)

    dest = dest_dir / "claude-session.jsonl"
    assert dest.read_text() == main.read_text(), (
        "main-session SELECTION regressed — must still prefer the "
        "non-sidechain candidate, unaffected by the additive per-session sync"
    )
    assert (dest_dir / "main-uuid-3.jsonl").is_file()
    assert (dest_dir / "sub-uuid-4.jsonl").is_file()


def test_unchanged_session_is_not_recopied(tmp_path):
    """A candidate whose per-session destination copy already has the SAME
    size+mtime as the current source must be SKIPPED, not re-copied — this
    hook runs on EVERY PostToolUse call, so re-copying an unchanged
    multi-hundred-KB session file every call would be real, wasted IO.
    Proven by planting a STALE destination file with a matching size+mtime
    but WRONG content: if the sync genuinely skips (rather than always
    overwriting), the stale content survives the hook call untouched."""
    config_dir = tmp_path / "claude-config"
    dest_dir = tmp_path / "sessions-dest"
    dest_dir.mkdir(parents=True)
    src = _write_session(
        config_dir, "-app", "main.jsonl",
        '{"type":"user","message":"hi","sessionId":"stable-uuid-5"}\n',
    )
    src_stat = os.stat(src)

    stale = dest_dir / "stable-uuid-5.jsonl"
    stale_content = '{"type":"user","message":"STALE-DO-NOT-OVERWRITE"}\n'
    pad = len(src.read_text()) - len(stale_content)
    assert pad >= 0, "test fixture: stale content must not be longer than src"
    stale.write_text(stale_content + (" " * pad))
    os.utime(stale, (src_stat.st_mtime, src_stat.st_mtime))

    env = _env(tmp_path / "state", "1")
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["CC_HARNESS_SESSIONS_DEST"] = str(dest_dir)

    record_edit(env)

    assert stale.read_text() == stale_content + (" " * pad), (
        "destination was re-copied even though size+mtime matched the "
        "unchanged source — the skip check is not working"
    )
