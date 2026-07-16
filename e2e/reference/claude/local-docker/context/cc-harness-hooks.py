#!/usr/bin/env python3
"""
cc-harness-hooks.py — mechanical finish-gate harness for the Claude Code
OPEN-MODELS SWE-bench arm (run-instance.sh writes .claude/settings.local.json
that wires this script in as a PostToolUse recorder + a Stop-time gate).

Stdlib-only, no deps, meant to run in well under 100ms per invocation. The
whole point of this harness is to make the "reproduce + verify before you
finish" and "escalate the hard tail" prompt guidance from run-instance.sh's
AUTONOMY_PROMPT MACHINE-CHECKED instead of advisory-only: prose triggers
measurably under-fired (see run-instance.sh's 2026-07-15 update comment).

FAIL-OPEN IS THE #1 CONTRACT: any internal exception here must never break
the benchmark run. All subcommands below run their real work inside a
try/except that falls through to a silent, unconditional exit(0). A bug in
this script degrades to a no-op gate/deny, never a stuck or crashed agent turn.

Subcommands (each reads one hook-event JSON object from stdin):
  record   PostToolUse recorder. Silent. Appends one compact JSON line per
           edit/test/task event to the state log. Never prints, never blocks.
  gate     Stop hook. Reads the state log, evaluates the finish gates in a
           fixed order (Z, R, V, E — first hit wins), and on a hit prints
           {"decision":"block","reason":"..."} so Claude Code returns the
           agent to work instead of ending the turn. An OVERALL cap (3 total
           blocks, across all gates) guarantees this can never loop forever
           even if the agent never satisfies a gate. Gate E fires on any of:
           a hot (3+ edited) file, a prior R-block, or 2+ prior V-blocks.
  deny     PreToolUse hook. Reads the state log, evaluates the deny rules in
           a fixed order (T, B, C — first match wins), and on a match prints
           a hookSpecificOutput permissionDecision:"deny" object so Claude
           Code refuses the tool call before it runs. T (test files are
           read-only) and C (time-source convention divergence) are cheap
           per-call checks; B (edit budget blown -> forced escalation) reads
           the same state log gate uses. Every deny also appends a "deny"
           event to the state log.

State lives at $CC_HARNESS_STATE/state.jsonl (default /tmp/cc-harness/),
deliberately OUTSIDE the repo — it must never appear in the graded
model_patch diff. (run-instance.sh's diff step already excludes .claude/ as
a belt-and-suspenders measure, but /tmp is the real reason it's safe.)

`--selftest` runs an in-process simulation (no stdin) against a temp state
dir and prints PASS/FAIL per case; exits non-zero on any FAIL. Useful as a
pre-flight check that the gate logic still does what the docstring says.
"""

import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter

STATE_ENV = "CC_HARNESS_STATE"
DEFAULT_STATE_DIR = "/tmp/cc-harness"

EDIT_TOOL_RE = re.compile(r"^(Edit|Write|MultiEdit|NotebookEdit|mcp__unerr__file_edit)$")
TEST_CMD_RE = re.compile(
    r"(runtests\.py|pytest|py\.test|-m unittest|manage\.py test|\btox\b|\brepro\w*\.(py|sh))",
    re.IGNORECASE,
)

OVERALL_CAP = 3
GATE_CAPS = {"Z": 1, "R": 1, "V": 2, "E": 1}

TEST_PATH_SEGMENTS = {"tests", "test", "testing"}
RULE_B_EDIT_THRESHOLD = 5
RULE_B_DENY_CAP = 2


# ── state I/O ─────────────────────────────────────────────────────────────

def _state_dir():
    return os.environ.get(STATE_ENV) or DEFAULT_STATE_DIR


def _state_file():
    return os.path.join(_state_dir(), "state.jsonl")


def append_event(ev):
    """Append one event dict as a JSON line. Errors are swallowed — a
    recorder that can't write state must never fail the tool call it's
    piggybacking on."""
    try:
        d = _state_dir()
        os.makedirs(d, exist_ok=True)
        with open(_state_file(), "a") as f:
            f.write(json.dumps(ev) + "\n")
    except Exception:
        pass


def read_events():
    """Missing/unreadable/corrupt state = no events, not an error."""
    events = []
    try:
        with open(_state_file()) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return events


# ── record ────────────────────────────────────────────────────────────────

def build_record_event(data):
    """Pure: hook stdin dict -> event dict to append, or None to record
    nothing. Split out from cmd_record for direct unit testing."""
    tool_name = data.get("tool_name") or ""
    is_error = bool(data.get("is_error"))
    tool_input = data.get("tool_input") or {}
    t = time.time()

    if EDIT_TOOL_RE.match(tool_name):
        if is_error:
            return None
        return {"t": t, "ev": "edit", "file": tool_input.get("file_path") or "?"}

    if tool_name == "Bash":
        cmd = tool_input.get("command") or ""
        if not TEST_CMD_RE.search(cmd):
            return None
        key = " ".join(cmd.split())[:200]
        exit_code = data.get("exit_code")
        ok = (exit_code == 0) if exit_code is not None else (not is_error)
        return {"t": t, "ev": "test", "key": key, "ok": ok}

    if tool_name == "Task":
        return {"t": t, "ev": "task", "agent": tool_input.get("subagent_type") or ""}

    return None


def cmd_record():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    ev = build_record_event(data)
    if ev is not None:
        append_event(ev)


# ── gate ──────────────────────────────────────────────────────────────────

def evaluate_gate(events):
    """Pure: full event list -> None (allow) or (gate_letter, reason) to
    block. Evaluated in fixed order Z, R, V, E; first hit wins. The overall
    cap is checked before any individual gate."""
    edits = [e for e in events if e.get("ev") == "edit"]
    tests = [e for e in events if e.get("ev") == "test"]
    tasks = [e for e in events if e.get("ev") == "task"]
    blocks = [e for e in events if e.get("ev") == "block"]

    if len(blocks) >= OVERALL_CAP:
        return None  # never loop forever

    block_counts = Counter(b.get("gate") for b in blocks)

    # Gate Z — nothing was ever edited.
    if block_counts.get("Z", 0) < GATE_CAPS["Z"]:
        if len(edits) == 0:
            return (
                "Z",
                "You have not modified any repository source files, so there is "
                "no fix to submit. Edit the source now and implement the most "
                "reasonable fix for the issue — do not finish with an empty change.",
            )

    # Gate R — a verification command that used to pass now fails.
    if block_counts.get("R", 0) < GATE_CAPS["R"]:
        by_key = {}
        for e in tests:
            by_key.setdefault(e.get("key", ""), []).append(bool(e.get("ok")))
        regressed_key = None
        for key, hist in by_key.items():
            if hist and (not hist[-1]) and (True in hist[:-1]):
                regressed_key = key
                break
        if regressed_key is not None:
            return (
                "R",
                "A verification command that previously passed now fails: "
                f"{regressed_key[:120]}. Your change introduced a regression. "
                "Rework your fix so the issue behavior AND the previously-passing "
                "tests are green — do not finish while it is red. If one focused "
                "rework cannot recover it, escalate per the escalation contract.",
            )

    # Gate V — edited but never re-verified afterward.
    if block_counts.get("V", 0) < GATE_CAPS["V"]:
        if edits:
            last_edit_t = max(e.get("t", 0) for e in edits)
            ok_after = any(
                e.get("ok") and e.get("t", 0) > last_edit_t for e in tests
            )
            if not ok_after:
                seen = []
                for e in edits:
                    f = e.get("file", "?")
                    if f not in seen:
                        seen.append(f)
                basenames = ", ".join(os.path.basename(f) for f in seen[:5])
                return (
                    "V",
                    "You edited source files but ran no successful verification "
                    "afterward. Re-run your reproduction of the issue AND the "
                    "narrowest existing test module covering each edited file "
                    f"(files edited: {basenames}); make them pass, then finish.",
                )

    # Gate E — an escalation trigger fired but no unerr-opus/unerr-fable ran.
    # Three arms: a hot (3+ edited) file, a prior R-block, or 2+ prior
    # V-blocks (V has capped out without a successful finish — hand off to
    # escalation rather than let the agent keep retrying an exhausted gate).
    if block_counts.get("E", 0) < GATE_CAPS["E"]:
        file_counts = Counter(e.get("file", "?") for e in edits)
        hot_file, hot_n = None, 0
        for f, n in file_counts.items():
            if n >= 3 and n > hot_n:
                hot_file, hot_n = f, n
        r_already = block_counts.get("R", 0) >= 1
        v_capped = block_counts.get("V", 0) >= 2
        escalated = any(
            t.get("agent") in ("unerr-opus", "unerr-fable") for t in tasks
        )
        if (hot_file is not None or r_already or v_capped) and not escalated:
            if hot_file is not None:
                trigger = f"file {os.path.basename(hot_file)} edited {hot_n} times"
            elif r_already:
                trigger = "a previously-passing test was regressed"
            else:
                trigger = "the verification gate (V) has blocked twice without a successful finish"
            return (
                "E",
                f"Escalation trigger hit: {trigger}. Per the escalation contract: "
                "spawn unerr-opus and unerr-fable in parallel with the same "
                "evidence brief (issue text, observations, attempts, all "
                "candidate sites — not your preferred hypothesis), reconcile "
                "their verdicts, implement the winner, verify, then finish.",
            )

    return None


def gate_once():
    """Read current state, evaluate, and (on a block) persist the block
    event. Shared by cmd_gate and the selftest so both exercise the exact
    same on-disk state path."""
    events = read_events()
    result = evaluate_gate(events)
    if result is not None:
        append_event({"t": time.time(), "ev": "block", "gate": result[0]})
    return result


def cmd_gate():
    try:
        json.load(sys.stdin)
    except Exception:
        pass  # gate doesn't need stdin's fields; just drain it politely
    result = gate_once()
    if result is None:
        return
    _letter, reason = result
    print(json.dumps({"decision": "block", "reason": reason}))


# ── deny ──────────────────────────────────────────────────────────────────

TEST_DENY_MSG = (
    "Test files are read-only in this benchmark: the grader runs its own copy "
    "of the tests, so a test edit cannot make the task pass — it only fakes "
    "progress. Fix the source at the definition site instead. If you believe "
    "the test itself encodes the bug, still fix the source and state that "
    "belief in your final message."
)


def is_test_path(file_path):
    """Rule T predicate. True if file_path (any separator style, resolved
    case-sensitive) names a test file: a `tests`/`test`/`testing` path
    segment, or a basename starting `test_` / ending `_test.py`."""
    if not file_path:
        return False
    posix = str(file_path).replace("\\", "/")
    parts = [seg for seg in posix.split("/") if seg]
    if not parts:
        return False
    if any(seg in TEST_PATH_SEGMENTS for seg in parts):
        return True
    basename = parts[-1]
    return basename.startswith("test_") or basename.endswith("_test.py")


def _edits_since_last_good_test(events, file_path):
    """Count `edit` events for file_path that landed after the last
    ev=="test" and ok==true event in the whole log; if no prior good test
    exists at all, count every edit ever recorded for that file."""
    last_ok_t = None
    for e in events:
        if e.get("ev") == "test" and e.get("ok") is True:
            t = e.get("t", 0)
            if last_ok_t is None or t > last_ok_t:
                last_ok_t = t
    matching = [e for e in events if e.get("ev") == "edit" and e.get("file") == file_path]
    if last_ok_t is None:
        return len(matching)
    return len([e for e in matching if e.get("t", 0) > last_ok_t])


def rule_b(events, file_path):
    """Rule B: 5+ un-greened edits on the same file with no unerr-opus/
    unerr-fable escalation yet -> force-escalate. Capped at RULE_B_DENY_CAP
    fires per run so a still-stuck agent can't be denied forever."""
    if not file_path:
        return None
    if _edits_since_last_good_test(events, file_path) < RULE_B_EDIT_THRESHOLD:
        return None
    escalated = any(
        e.get("ev") == "task"
        and ("unerr-opus" in (e.get("agent") or "") or "unerr-fable" in (e.get("agent") or ""))
        for e in events
    )
    if escalated:
        return None
    prior_b = len([e for e in events if e.get("ev") == "deny" and e.get("rule") == "B"])
    if prior_b >= RULE_B_DENY_CAP:
        return None
    basename = os.path.basename(file_path)
    return (
        "B",
        f"You have edited {basename} 5+ times without a green verification — "
        "escalation trigger (b) has fired. STOP editing. In ONE message spawn "
        "BOTH unerr-opus and unerr-fable via the Task tool with the same "
        "evidence brief (issue text, what you observed, what you tried, all "
        "candidate sites), reconcile their verdicts, then implement the "
        "agreed fix once.",
    )


def rule_c(events, file_path, tool_input):
    """Rule C: new_string/content introducing datetime.now(...) into a file
    whose current on-disk contents already use utcnow() diverges from that
    file's time-source convention. Fires once per file — a repeated
    identical attempt is treated as an evidence-cited override and allowed
    through (bounded by design, not a bug)."""
    if not file_path:
        return None
    text = (tool_input.get("new_string") or "") + "\n" + (tool_input.get("content") or "")
    if "datetime.now(" not in text and "datetime.datetime.now(" not in text:
        return None
    try:
        with open(file_path) as f:
            current = f.read()
    except Exception:
        return None
    if "utcnow" not in current:
        return None
    prior_c = any(
        e.get("ev") == "deny" and e.get("rule") == "C" and e.get("file") == file_path
        for e in events
    )
    if prior_c:
        return None
    return (
        "C",
        "Convention check: this file already uses utcnow() as its time "
        "source; your edit introduces datetime.now() (local time), which "
        "diverges from the file's convention — hidden callers/tests may pin "
        "the UTC seam. Re-check which time source the surrounding code uses "
        "and match it. If you have concrete in-file evidence local time is "
        "required here, cite that line and re-apply this exact edit — it "
        "will be accepted.",
    )


def evaluate_deny(events, data):
    """Pure: state events + hook stdin dict -> None (allow) or (rule_letter,
    reason) to deny. Evaluated in fixed order T, B, C; first match wins."""
    tool_name = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""

    if is_test_path(file_path):
        return ("T", TEST_DENY_MSG)

    result = rule_b(events, file_path)
    if result is not None:
        return result

    if EDIT_TOOL_RE.match(tool_name):
        result = rule_c(events, file_path, tool_input)
        if result is not None:
            return result

    return None


def deny_once(data):
    """Read current state, evaluate the deny rules for this hook-input dict,
    and (on a deny) persist the deny event. Shared by cmd_deny and the
    selftest so both exercise the exact same on-disk state path."""
    events = read_events()
    result = evaluate_deny(events, data)
    if result is not None:
        rule, _reason = result
        tool_input = data.get("tool_input") or {}
        file_path = tool_input.get("file_path") or "?"
        append_event({"t": time.time(), "ev": "deny", "rule": rule, "file": file_path})
    return result


def cmd_deny():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    result = deny_once(data)
    if result is None:
        return
    _rule, reason = result
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


# ── selftest ─────────────────────────────────────────────────────────────

def run_selftest():
    passed = 0
    failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond:
            print(f"PASS: {name}")
            passed += 1
        else:
            print(f"FAIL: {name}")
            failed += 1

    tmp_root = tempfile.mkdtemp(prefix="cc-harness-selftest-")
    orig_env = os.environ.get(STATE_ENV)

    def fresh_dir(name):
        d = os.path.join(tmp_root, name)
        os.makedirs(d, exist_ok=True)
        os.environ[STATE_ENV] = d

    try:
        # Case 1: zero edits -> Gate Z.
        fresh_dir("case1-no-edits")
        r = gate_once()
        check("no-edits -> Z", r is not None and r[0] == "Z")

        # Case 2: edited, never verified -> V twice (cap 2). On the 3rd call
        # V's own cap is spent, but Gate E's widened 3rd arm (2+ prior
        # V-blocks) now fires — the mechanical "V exhausted, still not
        # verified -> escalate" handoff the arm exists for, not a bug.
        fresh_dir("case2-edit-no-test")
        append_event({"t": 1.0, "ev": "edit", "file": "a.py"})
        r1 = gate_once()
        r2 = gate_once()
        r3 = gate_once()
        check("edit-no-test call 1 -> V", r1 is not None and r1[0] == "V")
        check("edit-no-test call 2 -> V", r2 is not None and r2[0] == "V")
        check(
            "edit-no-test call 3 -> E (V cap spent, E's V-block arm hands off)",
            r3 is not None and r3[0] == "E",
        )

        # Case 3: a test passed, then the same key failed -> Gate R.
        fresh_dir("case3-regression")
        append_event({"t": 1.0, "ev": "edit", "file": "b.py"})
        append_event({"t": 2.0, "ev": "test", "key": "pytest tests/test_b.py", "ok": True})
        append_event({"t": 3.0, "ev": "test", "key": "pytest tests/test_b.py", "ok": False})
        r = gate_once()
        check("pass-then-fail -> R", r is not None and r[0] == "R")

        # Case 4: same file edited 3x, verified clean, no escalation -> E.
        fresh_dir("case4-hot-file-no-escalation")
        append_event({"t": 1.0, "ev": "edit", "file": "c.py"})
        append_event({"t": 2.0, "ev": "edit", "file": "c.py"})
        append_event({"t": 3.0, "ev": "edit", "file": "c.py"})
        append_event({"t": 4.0, "ev": "test", "key": "pytest tests/test_c.py", "ok": True})
        r = gate_once()
        check("3-edits-one-file no-escalation -> E", r is not None and r[0] == "E")

        # Case 5: same hot-file trigger, but unerr-opus already ran -> no E.
        fresh_dir("case5-escalated")
        append_event({"t": 1.0, "ev": "edit", "file": "d.py"})
        append_event({"t": 2.0, "ev": "edit", "file": "d.py"})
        append_event({"t": 3.0, "ev": "edit", "file": "d.py"})
        append_event({"t": 4.0, "ev": "test", "key": "pytest tests/test_d.py", "ok": True})
        append_event({"t": 5.0, "ev": "task", "agent": "unerr-opus"})
        r = gate_once()
        check("task unerr-opus recorded -> E not fired", r is None)

        # Case 6: overall cap — 3 prior blocks means always allow, even with
        # a live Gate-Z condition (zero edits) still true.
        fresh_dir("case6-overall-cap")
        append_event({"t": 1.0, "ev": "block", "gate": "Z"})
        append_event({"t": 2.0, "ev": "block", "gate": "V"})
        append_event({"t": 3.0, "ev": "block", "gate": "V"})
        r = gate_once()
        check("overall cap (3 prior blocks) -> always allow", r is None)

        # Case 7: Rule T — test paths denied, non-test path allowed.
        fresh_dir("case7-rule-t")
        rt1 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "tests/test_x.py", "new_string": "x"}})
        rt2 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "tests/regressiontests/foo.py", "new_string": "x"}})
        rt3 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "src/utils.py", "new_string": "x"}})
        check("rule T denies tests/test_x.py", rt1 is not None and rt1[0] == "T")
        check("rule T denies tests/regressiontests/foo.py", rt2 is not None and rt2[0] == "T")
        check("rule T allows src/utils.py", rt3 is None)

        # Case 8: Rule B — 5 prior un-greened edits on one file -> deny the
        # 6th edit attempt on that file.
        fresh_dir("case8-rule-b-fires")
        for i in range(5):
            append_event({"t": float(i + 1), "ev": "edit", "file": "e.py"})
        rb1 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "e.py", "new_string": "x"}})
        check("rule B denies 6th un-greened edit", rb1 is not None and rb1[0] == "B")

        # Case 9: Rule B — same setup, but an unerr-opus Task already ran ->
        # no deny.
        fresh_dir("case9-rule-b-escalated")
        for i in range(5):
            append_event({"t": float(i + 1), "ev": "edit", "file": "f.py"})
        append_event({"t": 10.0, "ev": "task", "agent": "unerr-opus"})
        rb2 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "f.py", "new_string": "x"}})
        check("rule B not fired once unerr-opus task recorded", rb2 is None)

        # Case 10: Rule B stops firing after 2 denials (cap).
        fresh_dir("case10-rule-b-cap")
        for i in range(5):
            append_event({"t": float(i + 1), "ev": "edit", "file": "g.py"})
        rb3a = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "g.py", "new_string": "x"}})
        rb3b = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "g.py", "new_string": "x"}})
        rb3c = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "g.py", "new_string": "x"}})
        check("rule B fires 1st denial", rb3a is not None and rb3a[0] == "B")
        check("rule B fires 2nd denial", rb3b is not None and rb3b[0] == "B")
        check("rule B stops after 2 denials (3rd allowed)", rb3c is None)

        # Case 11: Rule C — datetime.now( into a file whose current contents
        # use utcnow -> deny once; identical 2nd attempt -> allowed (fires
        # once per file, evidence-cited re-apply); file with no utcnow ->
        # always allowed.
        fresh_dir("case11-rule-c")
        conv_file = os.path.join(tmp_root, "conv_utcnow.py")
        with open(conv_file, "w") as f:
            f.write("import datetime\nx = datetime.datetime.utcnow()\n")
        rc1 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": conv_file, "new_string": "y = datetime.now()"}})
        rc2 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": conv_file, "new_string": "y = datetime.now()"}})
        check("rule C denies datetime.now( into a utcnow-convention file", rc1 is not None and rc1[0] == "C")
        check("rule C allows the 2nd identical attempt (fires once per file)", rc2 is None)

        no_utc_file = os.path.join(tmp_root, "no_utcnow.py")
        with open(no_utc_file, "w") as f:
            f.write("x = 1\n")
        rc3 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": no_utc_file, "new_string": "y = datetime.now()"}})
        check("rule C allows when the file has no utcnow", rc3 is None)

        # Case 12: Gate E's V-block arm fires standalone on 2 prior V-blocks
        # (no hot file, no R-block) once the edit IS verified — isolates the
        # arm from V's own natural cap-exhaustion sequence covered in case 2.
        fresh_dir("case12-gate-e-vblock-arm")
        append_event({"t": 1.0, "ev": "edit", "file": "h.py"})
        append_event({"t": 2.0, "ev": "test", "key": "pytest tests/test_h.py", "ok": True})
        append_event({"t": 3.0, "ev": "block", "gate": "V"})
        append_event({"t": 4.0, "ev": "block", "gate": "V"})
        r = gate_once()
        check("2 prior V-blocks -> E's V-block arm fires", r is not None and r[0] == "E")
    finally:
        if orig_env is None:
            os.environ.pop(STATE_ENV, None)
        else:
            os.environ[STATE_ENV] = orig_env
        shutil.rmtree(tmp_root, ignore_errors=True)

    total = passed + failed
    print(f"selftest: {passed}/{total} PASS")
    return 0 if failed == 0 else 1


# ── entry point ──────────────────────────────────────────────────────────

def main():
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(run_selftest())

    if not argv:
        sys.exit(0)

    try:
        sub = argv[0]
        if sub == "record":
            cmd_record()
        elif sub == "gate":
            cmd_gate()
        elif sub == "deny":
            cmd_deny()
        # any other/unknown subcommand: no-op, fail-open.
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
