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
           even if the agent never satisfies a gate. Gate V requires a BROAD
           green run (a whole test module, not a single method) so sibling
           tests catch regressions the target test misses. Gate E escalates
           only on a VERIFICATION-revealed trigger: a prior R-block or 2+ prior
           V-blocks (the raw edit-count arm was removed — see Phase A findings).
           ESCALATION_PANEL (unset/"0", default) -> LADDER: unerr-opus alone,
           then unerr-fable only if the trigger persists past opus; "1" ->
           PANEL: the original spawn-both-in-parallel single block.
  deny     PreToolUse hook. Reads the state log, evaluates the deny rules in
           a fixed order (T, B, C — first match wins), and on a match prints
           a hookSpecificOutput permissionDecision:"deny" object so Claude
           Code refuses the tool call before it runs. T (test files are
           read-only) and C (time-source convention divergence) are cheap
           per-call checks; B (5+ un-greened edits AND verification has already
           blocked -> forced escalation) reads the same state log gate uses —
           it no longer fires on edit-count alone. Every deny also appends a
           "deny" event to the state log.

PROFILE-DRIVEN: HARNESS_HOOKS unset/"0" turns every subcommand into a no-op;
"1" turns hooks on with the profile from HARNESS_PROFILE (default "swe");
"generic" turns hooks on with profile "generic" outright. "swe" is the
original sensor above (TEST_CMD_RE-gated pytest/manage.py-test/tox/bin-test/
repro* "test" events). "generic" instead ledgers EVERY Bash command as a
"cmd" event and treats a command as verification only when the agent
suffixes it with the literal `# unerr:verify` (or `#unerr:verify`) marker —
the same Z/R/V/E gates and T/B/C deny rules then run against that
agent-declared signal instead of a fixed test runner, so the harness works
on a Terminal-Bench-style task with no fixed repo/test layout and a hidden
grader. T and C are SWE-world rules and are no-ops under "generic".

State lives at $CC_HARNESS_STATE/state.jsonl (default /tmp/cc-harness/),
deliberately OUTSIDE the repo — it must never appear in the graded
model_patch diff. (run-instance.sh's diff step already excludes .claude/ as
a belt-and-suspenders measure, but /tmp is the real reason it's safe.)

`--selftest` runs an in-process simulation (no stdin) against a temp state
dir and prints PASS/FAIL per case; exits non-zero on any FAIL. Useful as a
pre-flight check that the gate logic still does what the docstring says.
"""

import hashlib
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

# ── profile resolution ───────────────────────────────────────────────────
# HARNESS_HOOKS: unset/"0" -> hooks off; "1" -> on, profile from
# HARNESS_PROFILE (default "swe"); "generic" -> on with profile "generic"
# outright (self-selecting, HARNESS_PROFILE not consulted). Frozen contract —
# see _profile(). "swe" is the original SWE-bench sensor (TEST_CMD_RE);
# "generic" senses agent-DECLARED verification instead (any Bash command
# suffixed with the `# unerr:verify` marker), so the same gates work on a
# Terminal-Bench-style task with no fixed repo/test layout and a hidden
# grader.
#
# ESCALATION_PANEL: orthogonal to HARNESS_PROFILE, applies under BOTH "swe"
# and "generic" — controls Gate E's escalation SHAPE, not whether it fires.
# unset/"0"/anything else -> LADDER (the default): a mechanical two-rung
# hand-off, unerr-opus alone first, unerr-fable only if the trigger persists
# after opus. "1" -> PANEL (the original behavior): spawn unerr-opus AND
# unerr-fable together as a decorrelated judge panel. See _escalation_panel().
HARNESS_HOOKS_ENV = "HARNESS_HOOKS"
HARNESS_PROFILE_ENV = "HARNESS_PROFILE"
ESCALATION_PANEL_ENV = "ESCALATION_PANEL"

# Literal substrings only (no whitespace-tolerant regex) — both forms are
# accepted verbatim, then stripped before the command is hashed into a
# ledger key so a marked/unmarked re-run of the same command still collapses
# to one ledger entry.
VERIFY_MARKERS = ("# unerr:verify", "#unerr:verify")
CMDS_CAP = 200

EDIT_TOOL_RE = re.compile(r"^(Edit|Write|MultiEdit|NotebookEdit|mcp__unerr__file_edit)$")
TEST_CMD_RE = re.compile(
    r"(runtests\.py|pytest|py\.test|-m\s+unittest|manage\.py\s+test|\btox\b"
    r"|\bbin/test\b|\brepro\w*\.(py|sh))",
    re.IGNORECASE,
)
# A BROAD verification runs a recognized test-suite runner over a whole
# module/file/class/dir/app; only that exercises the sibling tests a regression
# hides in. A bare reproduction script (`python repro_issue.py`) is NOT a suite
# run — it must never satisfy the regression gate, or an agent that only reruns
# its repro after each edit would sail through (adversarial-review finding 4).
_SUITE_RUNNER_RE = re.compile(
    r"(runtests\.py|pytest|py\.test|-m\s+unittest|manage\.py\s+test|\btox\b|\bnox\b"
    r"|\bbin/test\b)",  # sympy's native runner — its docs point agents at bin/test, not pytest
    re.IGNORECASE,
)
# A verification is NARROW when it targets a single method/function: a pytest
# `::test_...` function/method node id (a `::TestClass` whole-class run stays
# BROAD), a `-k` keyword filter, or a dotted `CapWordsClass.test_method` path (a
# lowercase `module.test_file` dotted path is a whole MODULE and stays BROAD —
# django names test files `test_*.py`; adversarial-review findings 2/3).
# Case-sensitive on purpose: `::test` vs `::Test` and the leading Capital in
# `Class.test_method` are the discriminating signals.
NARROW_TEST_RE = re.compile(
    r"(::test[A-Za-z0-9_]*"
    r"|(^|\s)-k(\s|=)"
    r"|[A-Z][A-Za-z0-9_]*\.test_[A-Za-z0-9_]+(\s|$|::))"
)

OVERALL_CAP = 3
GATE_CAPS = {"Z": 1, "R": 1, "V": 2, "E": 1}
# LADDER mode's own Gate-E cap (2 rungs: opus alone, then fable) — separate
# from GATE_CAPS["E"] (1), which stays PANEL mode's cap (a single
# spawn-both-in-parallel block). See _escalation_panel()/_gate_e_ladder().
LADDER_E_CAP = 2

# Deliberately NO bare "test": django/test/ is real product source (TestCase,
# Client, override_settings live there), and every benchmark repo's actual test
# tree is `tests/` (plural) or `testing/` (pytest). A gold fix touching
# django/test/*.py must stay editable — denying it leaves the agent no legal
# path to the fix (adversarial-review finding). Loose test_*.py files anywhere
# are still caught by is_test_path's basename rules.
TEST_PATH_SEGMENTS = {"tests", "testing"}
RULE_B_EDIT_THRESHOLD = 5
RULE_B_DENY_CAP = 2


# ── profile ───────────────────────────────────────────────────────────────

def _profile():
    """Resolve the active profile once, early, from the frozen env contract.
    HARNESS_HOOKS unset/"0" -> None (hooks off, every subcommand is a
    no-op); "1" -> on, profile taken from HARNESS_PROFILE (default "swe");
    "generic" -> on, profile "generic" outright — HARNESS_PROFILE is not
    consulted in that case, it's self-selecting. An unrecognized
    HARNESS_PROFILE value under HARNESS_HOOKS=1 falls back to "swe" (the
    well-tested legacy sensor), never to a state the contract doesn't name."""
    hh = os.environ.get(HARNESS_HOOKS_ENV) or ""
    if hh == "generic":
        return "generic"
    if hh != "1":
        return None
    prof = os.environ.get(HARNESS_PROFILE_ENV) or "swe"
    return prof if prof == "generic" else "swe"


def _escalation_panel():
    """Resolve Gate E's escalation SHAPE once, early, same style as
    _profile(). "1" -> True (PANEL: today's single block demanding
    unerr-opus AND unerr-fable spawned together in parallel, byte-identical
    message). Unset/"0"/anything else -> False (LADDER, the default: a
    mechanical two-rung hand-off — unerr-opus alone first, unerr-fable only
    if the trigger persists after opus). Orthogonal to HARNESS_PROFILE —
    applies identically under "swe" and "generic"."""
    return (os.environ.get(ESCALATION_PANEL_ENV) or "") == "1"


def _verify_marker_present(cmd):
    """True if `cmd` carries the agent-declared verify marker in either
    accepted literal form."""
    return any(m in cmd for m in VERIFY_MARKERS)


def _strip_verify_marker(cmd):
    """Remove the verify marker (either accepted form) so a marked and an
    unmarked run of the same underlying command hash to the same ledger
    key."""
    out = cmd
    for m in VERIFY_MARKERS:
        out = out.replace(m, "")
    return out


def _cmd_key(cmd):
    """Generic-profile ledger key: first 12 hex chars of the sha1 of the
    whitespace-collapsed command with the verify marker stripped."""
    collapsed = " ".join(_strip_verify_marker(cmd).split())
    return hashlib.sha1(collapsed.encode("utf-8", "replace")).hexdigest()[:12]


def _cmd_ledger(cmd_events):
    """Fold generic-profile `cmd` events into a per-key ledger: key ->
    {"ok": <latest run's outcome>, "n": <run count>, "verify": <marker ever
    seen for this key>, "was_ok": <ever succeeded>}. Insertion-ordered and
    capped at CMDS_CAP distinct keys — once a NEW key would exceed the cap,
    the oldest-inserted key is dropped first, so an unbounded agent session
    can't grow this without bound."""
    ledger = {}
    for e in cmd_events:
        if e.get("ev") != "cmd":
            continue
        key = e.get("key", "")
        entry = ledger.get(key)
        if entry is None:
            if len(ledger) >= CMDS_CAP:
                del ledger[next(iter(ledger))]
            entry = {"ok": False, "n": 0, "verify": False, "was_ok": False}
            ledger[key] = entry
        entry["n"] += 1
        entry["ok"] = bool(e.get("ok"))
        if e.get("ok"):
            entry["was_ok"] = True
        if e.get("verify"):
            entry["verify"] = True
    return ledger


def _last_green_verify_ts(cmd_events):
    """Latest `t` among generic-profile `cmd` events that were BOTH
    verify-marked and successful, or None if no such event exists yet."""
    ts = None
    for e in cmd_events:
        if e.get("ev") == "cmd" and e.get("verify") and e.get("ok"):
            t = e.get("t", 0)
            if ts is None or t > ts:
                ts = t
    return ts


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

def build_record_event(data, profile="swe"):
    """Pure: hook stdin dict -> event dict to append, or None to record
    nothing. Split out from cmd_record for direct unit testing. profile
    != "generic" runs the ORIGINAL swe-sensor code path unchanged (edit/
    TEST_CMD_RE-gated Bash/task). profile == "generic" keeps edit- and
    task-recording identical but LEDGERS EVERY Bash command instead of
    gating on TEST_CMD_RE: key = first 12 hex of sha1(whitespace-collapsed
    command, verify marker stripped), ok/verify derived the same way as the
    swe path derives test pass/fail."""
    if profile != "generic":
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

    # ── generic profile ──
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
        if not cmd.strip():
            return None
        key = _cmd_key(cmd)
        verify = _verify_marker_present(cmd)
        exit_code = data.get("exit_code")
        ok = (exit_code == 0) if exit_code is not None else (not is_error)
        return {"t": t, "ev": "cmd", "key": key, "ok": ok, "verify": verify}

    if tool_name == "Task":
        return {"t": t, "ev": "task", "agent": tool_input.get("subagent_type") or ""}

    return None


def cmd_record():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    profile = _profile()
    if profile is None:
        return
    ev = build_record_event(data, profile)
    if ev is not None:
        append_event(ev)


# ── gate ──────────────────────────────────────────────────────────────────

def is_broad_test(key):
    """True only if `key` invokes a recognized test-suite runner over a broad
    target. Two gates: (1) it must match `_SUITE_RUNNER_RE` — a bare repro
    script (`python repro_issue.py`) never counts; (2) it must carry no NARROW
    signal (a `::test_...` method node id, a `-k` filter, or a dotted
    `Class.test_method` path). A whole-module/class/app suite run passes both."""
    k = key or ""
    if not _SUITE_RUNNER_RE.search(k):
        return False
    return not NARROW_TEST_RE.search(k)


def _gate_e_panel(tasks, block_counts):
    """Gate E, PANEL mode (ESCALATION_PANEL=1): byte-identical to the
    original single-arm behavior — one block, once, demanding unerr-opus
    AND unerr-fable spawned together as a decorrelated judge panel. Capped
    at GATE_CAPS["E"] (1). Shared verbatim by both the "swe" and "generic"
    evaluate_gate branches — the message never differed between them."""
    if block_counts.get("E", 0) >= GATE_CAPS["E"]:
        return None
    r_already = block_counts.get("R", 0) >= 1
    v_capped = block_counts.get("V", 0) >= 2
    escalated = any(t.get("agent") in ("unerr-opus", "unerr-fable") for t in tasks)
    if not ((r_already or v_capped) and not escalated):
        return None
    trigger = (
        "a previously-passing test was regressed and one rework did not recover it"
        if r_already
        else "the verification gate (V) has blocked twice without a green finish"
    )
    return (
        "E",
        f"Escalation trigger hit: {trigger}. Per the escalation contract: "
        "spawn unerr-opus and unerr-fable in parallel with the same "
        "evidence brief (issue text, observations, attempts, all "
        "candidate sites — not your preferred hypothesis), reconcile "
        "their verdicts, implement the winner, verify, then finish.",
    )


def _gate_e_ladder(tasks, blocks, block_counts):
    """Gate E, LADDER mode (the default): two rungs, tracked by WHICH agent
    ran rather than "any" escalation. Rung 1 fires the same
    verification-revealed trigger as the panel (a prior R-block, or V capped
    at 2) once neither unerr-opus nor unerr-fable has run, demanding
    unerr-opus ALONE — one decorrelated-enough second opinion for a
    same-family reasoning-effort ladder, not the full panel. Rung 2 fires
    only once opus has run, fable has not, AND a NEW R- or V-block was
    recorded strictly AFTER the opus Task event's own timestamp (i.e. the
    trigger PERSISTED past opus's fix) — demanding unerr-fable, primed with
    opus's proposal and why it failed. Capped at LADDER_E_CAP (2) total
    E-blocks; once fable has also run (or the cap is spent), Gate E never
    blocks again."""
    if block_counts.get("E", 0) >= LADDER_E_CAP:
        return None

    fable_used = any(t.get("agent") == "unerr-fable" for t in tasks)
    if fable_used:
        return None

    opus_ts = [t.get("t", 0) for t in tasks if t.get("agent") == "unerr-opus"]

    if not opus_ts:
        r_already = block_counts.get("R", 0) >= 1
        v_capped = block_counts.get("V", 0) >= 2
        if not (r_already or v_capped):
            return None
        trigger = (
            "a previously-passing test was regressed and one rework did not recover it"
            if r_already
            else "the verification gate (V) has blocked twice without a green finish"
        )
        return (
            "E",
            f"Escalation trigger hit: {trigger}. Per the escalation contract "
            "(ladder mode, rung 1): spawn unerr-opus ALONE via the Task tool "
            "with the evidence brief (task text, what you observed, what you "
            "tried, all candidate approaches) — but NOT your preferred "
            "hypothesis. Let it investigate and return a one-line root cause "
            "plus an exact minimal proposal, WITHOUT editing files. Implement "
            "that proposal, then re-run verification.",
        )

    # Rung 2: opus already ran, fable has not — fire only if the trigger
    # PERSISTED, i.e. a NEW R- or V-block landed strictly after opus's own
    # Task event was recorded (reuses the existing block-event timestamps —
    # no separate persistence mechanism needed).
    first_opus_t = min(opus_ts)
    new_trouble = any(
        b.get("gate") in ("R", "V") and b.get("t", 0) > first_opus_t
        for b in blocks
    )
    if not new_trouble:
        return None
    return (
        "E",
        "Escalation trigger PERSISTED after rung 1: unerr-opus's proposal did "
        "not resolve it. Per the escalation contract (ladder mode, rung 2): "
        "spawn unerr-fable via the Task tool. Include unerr-opus's proposal "
        "AND exactly why it failed. Prefer the verdict that explains ALL "
        "observed evidence and fixes a definition site over one that "
        "compensates at a flow site. Implement the winner, verify, then "
        "finish.",
    )


def evaluate_gate(events, profile="swe"):
    """Pure: full event list -> None (allow) or (gate_letter, reason) to
    block. Evaluated in fixed order Z, R, V, E; first hit wins. The overall
    cap gates the Z/R/V nudges; Gate E is EXEMPT from it so escalation stays
    deliverable even after the cap is spent — Gate E's OWN cap (one-shot
    PANEL, or two-rung LADDER) is decided by _escalation_panel(), see
    _gate_e_panel/_gate_e_ladder. profile != "generic" runs the ORIGINAL
    swe-sensor gates unchanged (TEST_CMD_RE "test" events); profile ==
    "generic" senses agent-declared `# unerr:verify` "cmd" events instead —
    same gate letters, caps, and Gate E handoff, different sensor."""
    if profile != "generic":
        edits = [e for e in events if e.get("ev") == "edit"]
        tests = [e for e in events if e.get("ev") == "test"]
        tasks = [e for e in events if e.get("ev") == "task"]
        blocks = [e for e in events if e.get("ev") == "block"]

        block_counts = Counter(b.get("gate") for b in blocks)
        # The overall cap stops the "keep working" nudges (Z/R/V) from nagging
        # forever. Gate E — the one-shot terminal escalation — is EXEMPT from it:
        # OVERALL_CAP (3) == V_cap (2) + E_cap (1), so a run that spends its cap on
        # Z/V blocks would otherwise short-circuit here and never deliver the
        # escalation instruction (the verification-revealed trigger unreachable —
        # adversarial-review finding 1). E is capped independently at 1, so
        # exempting it stays bounded. NB: any 3-block combo reaching the cap
        # necessarily includes an R-block or 2 V-blocks, so hitting the cap
        # guarantees E's trigger is satisfiable.
        over_cap = len(blocks) >= OVERALL_CAP

        # Gate Z — nothing was ever edited.
        if not over_cap and block_counts.get("Z", 0) < GATE_CAPS["Z"]:
            if len(edits) == 0:
                return (
                    "Z",
                    "You have not modified any repository source files, so there is "
                    "no fix to submit. Edit the source now and implement the most "
                    "reasonable fix for the issue — do not finish with an empty change.",
                )

        # Gate R — a verification command that used to pass now fails.
        if not over_cap and block_counts.get("R", 0) < GATE_CAPS["R"]:
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

        # Gate V — edited but never re-verified with a BROAD run afterward. A
        # narrow single-method green does NOT satisfy V: it leaves sibling tests in
        # the touched module unrun, which is exactly how a fix that passes its
        # target test still regresses PASS_TO_PASS tests (django-11885, pylint-7277).
        # Requiring a broad green forces the module suite to run, so Gate R (above)
        # can then catch any regression it surfaces.
        if not over_cap and block_counts.get("V", 0) < GATE_CAPS["V"]:
            if edits:
                last_edit_t = max(e.get("t", 0) for e in edits)
                ok_after = [
                    e for e in tests
                    if e.get("ok") and e.get("t", 0) > last_edit_t
                ]
                broad_ok_after = any(is_broad_test(e.get("key", "")) for e in ok_after)
                if not broad_ok_after:
                    seen = []
                    for e in edits:
                        f = e.get("file", "?")
                        if f not in seen:
                            seen.append(f)
                    basenames = ", ".join(os.path.basename(f) for f in seen[:5])
                    if ok_after:
                        # verified, but only NARROWLY — demand the full module run.
                        reason = (
                            "Your only post-edit verification was NARROW — a single "
                            "method, a -k filter, or just a reproduction script, not "
                            "the existing test suite. Run the FULL existing test "
                            "module/file for each edited file — the whole module, not "
                            f"just the target test (files edited: {basenames}) — and "
                            "make ALL of them pass. The sibling tests in that module "
                            "are what catch regressions your targeted test cannot. "
                            "Then finish."
                        )
                    else:
                        reason = (
                            "You edited source files but ran no successful "
                            "verification afterward. Re-run your reproduction of the "
                            "issue AND the full existing test module covering each "
                            f"edited file (files edited: {basenames}); make them "
                            "pass, then finish."
                        )
                    return ("V", reason)

        # Gate E — a VERIFICATION-REVEALED escalation trigger fired but no
        # unerr-opus/unerr-fable ran (or, in LADDER mode, the trigger persisted
        # past the first hand-off). Two trigger arms only: a prior R-block (a
        # regression one rework did not recover) or 2+ prior V-blocks
        # (verification has capped without a green finish). The old
        # raw-edit-count arm (a "hot" 3+-edited file) is deliberately GONE:
        # Phase A showed edit-count escalation fires on hard cases the
        # sub-agents can't fix either, billing the reasoner/oracle tiers for
        # zero conversions. Escalate only when verification proves the agent
        # is stuck, not merely because it edited a file several times. This
        # gate is reached even when over_cap (see the exemption above), so a
        # run whose cap was spent on Z/V blocks still receives the
        # escalation. ESCALATION_PANEL selects the SHAPE (single
        # spawn-both block vs a two-rung opus-then-fable ladder) — see
        # _gate_e_panel/_gate_e_ladder.
        e_result = (
            _gate_e_panel(tasks, block_counts)
            if _escalation_panel()
            else _gate_e_ladder(tasks, blocks, block_counts)
        )
        if e_result is not None:
            return e_result

        return None

    # ── generic profile ──
    edits = [e for e in events if e.get("ev") == "edit"]
    cmds = [e for e in events if e.get("ev") == "cmd"]
    tasks = [e for e in events if e.get("ev") == "task"]
    blocks = [e for e in events if e.get("ev") == "block"]

    block_counts = Counter(b.get("gate") for b in blocks)
    over_cap = len(blocks) >= OVERALL_CAP
    ledger = _cmd_ledger(cmds)

    # Gate Z — nothing was ever edited AND no Bash command has ever succeeded
    # (no evidence any work happened on the environment).
    if not over_cap and block_counts.get("Z", 0) < GATE_CAPS["Z"]:
        if len(edits) == 0 and not any(e.get("ok") for e in cmds):
            return (
                "Z",
                "You have not modified anything and no command you ran has "
                "succeeded, so there is no evidence any work happened. This "
                "task requires acting on the environment — make the change it "
                "requires and run a command that proves it, then finish.",
            )

    # Gate R — a verify-marked command that used to pass now fails.
    if not over_cap and block_counts.get("R", 0) < GATE_CAPS["R"]:
        regressed_key = None
        for key, entry in ledger.items():
            if entry["verify"] and entry["was_ok"] and not entry["ok"]:
                regressed_key = key
                break
        if regressed_key is not None:
            return (
                "R",
                "A verification command that previously passed now fails: "
                f"{regressed_key}. Your change introduced a regression. "
                "Rework your fix so the task's success condition AND that "
                "verify-marked command are green — do not finish while it is "
                "red. If one focused rework cannot recover it, escalate per "
                "the escalation contract.",
            )

    # Gate V — no verify-marked command has EVER succeeded, or a file edit
    # landed after the last green verify-marked run.
    if not over_cap and block_counts.get("V", 0) < GATE_CAPS["V"]:
        last_green_ts = _last_green_verify_ts(cmds)
        edit_after_green = last_green_ts is not None and any(
            e.get("t", 0) > last_green_ts for e in edits
        )
        if last_green_ts is None or edit_after_green:
            return (
                "V",
                "You have not proven this task works: establish the command "
                "that proves success for this task and run it with `# "
                "unerr:verify` appended; re-run it after your final change. "
                "This benchmark has no fixed test layout — the marker is how "
                "you declare which command IS the proof. Finish only once "
                "that verify-marked command is green and no edit has landed "
                "since.",
            )

    # Gate E — same skeleton/messages as the swe profile (shared via
    # _gate_e_panel/_gate_e_ladder): a VERIFICATION-REVEALED escalation
    # trigger fired (a prior R-block or 2+ prior V-blocks) but no
    # unerr-opus/unerr-fable ran (or, in LADDER mode, the trigger persisted
    # past the first hand-off). Exempt from the overall cap so it still
    # fires even when the cap was spent on Z/R/V. ESCALATION_PANEL selects
    # the SHAPE, same as the swe branch above.
    e_result = (
        _gate_e_panel(tasks, block_counts)
        if _escalation_panel()
        else _gate_e_ladder(tasks, blocks, block_counts)
    )
    if e_result is not None:
        return e_result

    return None


def gate_once():
    """Read current state, evaluate, and (on a block) persist the block
    event. Shared by cmd_gate and the selftest so both exercise the exact
    same on-disk state path. Resolves the profile itself: hooks off
    (_profile() is None) is a pure allow, no evaluation, no state write —
    both callers (cmd_gate, run_selftest) already treat a None return as
    allow, so this needs no caller-side change."""
    profile = _profile()
    if profile is None:
        return None
    events = read_events()
    result = evaluate_gate(events, profile)
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
    case-sensitive) names a test file: a `tests`/`testing` path segment, or
    a basename starting `test_` / ending `_test.py`. A bare `test` segment
    is NOT a test marker — django/test/ is product source."""
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


def _edits_since_last_green_verify(events, file_path):
    """Generic-profile counterpart of _edits_since_last_good_test: counts
    `edit` events for file_path that landed after the last GREEN
    verify-marked `cmd` event (last_green_verify_ts) in the whole log; with
    no such event yet, counts every edit ever recorded for that file."""
    last_ok_t = _last_green_verify_ts([e for e in events if e.get("ev") == "cmd"])
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
    # Throttle: force-escalate only once verification has REVEALED a gap — a
    # prior V-block (a verification was demanded and not met) or an R-block (a
    # regression was flagged). Raw edit-thrash alone no longer triggers
    # escalation: Phase A showed count-triggered escalation billed the
    # reasoner/oracle tiers for zero conversions on cases the sub-agents could
    # not fix either. The 5+-edit threshold above stays as the "still stuck"
    # guard; this adds "and verification says so".
    verification_revealed = any(
        e.get("ev") == "block" and e.get("gate") in ("V", "R") for e in events
    )
    if not verification_revealed:
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


def rule_b_generic(events, file_path):
    """Generic-profile counterpart of rule_b: identical 5+-edit / prior
    V-or-R-block / not-escalated / RULE_B_DENY_CAP counters, but "since last
    green" is rewired from the last good pytest-family run to the last
    GREEN verify-marked command (last_green_verify_ts, via
    _edits_since_last_green_verify) — there is no fixed test runner to sense
    on a generic/Terminal-Bench-style task."""
    if not file_path:
        return None
    if _edits_since_last_green_verify(events, file_path) < RULE_B_EDIT_THRESHOLD:
        return None
    verification_revealed = any(
        e.get("ev") == "block" and e.get("gate") in ("V", "R") for e in events
    )
    if not verification_revealed:
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
        f"You have edited {basename} 5+ times without a green `# unerr:verify` "
        "run — escalation trigger (b) has fired. STOP editing. In ONE message "
        "spawn BOTH unerr-opus and unerr-fable via the Task tool with the same "
        "evidence brief (task text, what you observed, what you tried, all "
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


def evaluate_deny(events, data, profile="swe"):
    """Pure: state events + hook stdin dict -> None (allow) or (rule_letter,
    reason) to deny. Evaluated in fixed order T, B, C; first match wins.
    profile != "generic" runs the ORIGINAL swe-sensor rules unchanged.
    profile == "generic": T and C are SWE-world rules and are NO-OPs (allow
    immediately) — a generic/Terminal-Bench-style task has no fixed
    test-path convention and no swe-specific time-source convention to
    check; only B fires, rewired to the verify-marked ledger (rule_b_generic)."""
    if profile != "generic":
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

    # ── generic profile ── T and C are no-ops; only B (rewired) applies.
    tool_input = data.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""

    result = rule_b_generic(events, file_path)
    if result is not None:
        return result

    return None


def deny_once(data):
    """Read current state, evaluate the deny rules for this hook-input dict,
    and (on a deny) persist the deny event. Shared by cmd_deny and the
    selftest so both exercise the exact same on-disk state path. Resolves
    the profile itself: hooks off (_profile() is None) is a pure allow, no
    evaluation, no state write — both callers (cmd_deny, run_selftest)
    already treat a None return as allow, so this needs no caller-side
    change."""
    profile = _profile()
    if profile is None:
        return None
    events = read_events()
    result = evaluate_deny(events, data, profile)
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
    orig_hooks = os.environ.get(HARNESS_HOOKS_ENV)
    orig_profile = os.environ.get(HARNESS_PROFILE_ENV)
    orig_panel = os.environ.get(ESCALATION_PANEL_ENV)
    # This selftest exercises the original swe-sensor gates/rules; force that
    # profile regardless of the ambient env so gate_once/deny_once's new
    # profile-off short-circuit doesn't turn every case below into a no-op.
    # Also isolate ESCALATION_PANEL to its LADDER default — none of the
    # cases below assert panel-vs-ladder message shape (only gate letters),
    # so this is belt-and-suspenders determinism, not a behavior req.
    os.environ[HARNESS_HOOKS_ENV] = "1"
    os.environ.pop(HARNESS_PROFILE_ENV, None)
    os.environ.pop(ESCALATION_PANEL_ENV, None)

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

        # Case 4: same file edited 3x, BROAD-verified clean, no V/R block ->
        # the hot-file arm is GONE, so this now ALLOWS (no escalation on raw
        # edit count). A broad green satisfies V; nothing else triggers.
        fresh_dir("case4-hot-file-no-longer-escalates")
        append_event({"t": 1.0, "ev": "edit", "file": "c.py"})
        append_event({"t": 2.0, "ev": "edit", "file": "c.py"})
        append_event({"t": 3.0, "ev": "edit", "file": "c.py"})
        append_event({"t": 4.0, "ev": "test", "key": "pytest tests/test_c.py", "ok": True})
        r = gate_once()
        check("3-edits + broad green, no V/R block -> ALLOW (hot-file arm removed)", r is None)

        # Case 5: same setup with an unerr-opus Task -> still allow (no trigger,
        # and escalated anyway).
        fresh_dir("case5-escalated")
        append_event({"t": 1.0, "ev": "edit", "file": "d.py"})
        append_event({"t": 2.0, "ev": "edit", "file": "d.py"})
        append_event({"t": 3.0, "ev": "edit", "file": "d.py"})
        append_event({"t": 4.0, "ev": "test", "key": "pytest tests/test_d.py", "ok": True})
        append_event({"t": 5.0, "ev": "task", "agent": "unerr-opus"})
        r = gate_once()
        check("broad green + unerr-opus task -> no block", r is None)

        # Case 6: overall cap reached AND escalation already done -> terminal
        # allow. Gate E is exempt from the cap but capped independently at 1;
        # the recorded unerr-opus task satisfies `escalated`, so nothing blocks
        # and the agent may finish.
        fresh_dir("case6-overall-cap-terminal")
        append_event({"t": 1.0, "ev": "block", "gate": "Z"})
        append_event({"t": 2.0, "ev": "block", "gate": "V"})
        append_event({"t": 3.0, "ev": "block", "gate": "V"})
        append_event({"t": 4.0, "ev": "task", "agent": "unerr-opus"})
        r = gate_once()
        check("overall cap + already escalated -> terminal allow", r is None)

        # Case 6b: DEADLOCK REGRESSION GUARD (adversarial-review finding 1) — the
        # overall cap is reached by Z + V + V (== OVERALL_CAP) with NO escalation
        # yet. Gate E must STILL fire (it is exempt from the cap); otherwise the
        # verification-revealed escalation is unreachable and the run silently
        # allows without ever escalating.
        fresh_dir("case6b-cap-does-not-starve-E")
        append_event({"t": 1.0, "ev": "edit", "file": "z.py"})
        append_event({"t": 2.0, "ev": "test", "key": "pytest tests/test_z.py", "ok": True})
        append_event({"t": 3.0, "ev": "block", "gate": "Z"})
        append_event({"t": 4.0, "ev": "block", "gate": "V"})
        append_event({"t": 5.0, "ev": "block", "gate": "V"})
        r = gate_once()
        check("cap spent on Z+V+V, not escalated -> E still fires (no deadlock)",
              r is not None and r[0] == "E")

        # Case 7: Rule T — test paths denied, non-test path allowed.
        fresh_dir("case7-rule-t")
        rt1 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "tests/test_x.py", "new_string": "x"}})
        rt2 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "tests/regressiontests/foo.py", "new_string": "x"}})
        rt3 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "src/utils.py", "new_string": "x"}})
        check("rule T denies tests/test_x.py", rt1 is not None and rt1[0] == "T")
        check("rule T denies tests/regressiontests/foo.py", rt2 is not None and rt2[0] == "T")
        check("rule T allows src/utils.py", rt3 is None)
        # Case 7b: django/test/ is PRODUCT SOURCE (TestCase/Client live there) —
        # a bare `test` segment must NOT deny, or a gold fix touching it has no
        # legal path (adversarial-review finding). Basename + plural-segment
        # rules still catch actual test files.
        rt4 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "django/test/utils.py", "new_string": "x"}})
        rt5 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "django/test/testcases.py", "new_string": "x"}})
        rt6 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "test_requests.py", "new_string": "x"}})
        rt7 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "testing/code/source.py", "new_string": "x"}})
        check("rule T allows django/test/utils.py (product source)", rt4 is None)
        check("rule T allows django/test/testcases.py (product source)", rt5 is None)
        check("rule T denies root-level test_requests.py (basename rule)", rt6 is not None and rt6[0] == "T")
        check("rule T denies testing/ segment (pytest's suite dir)", rt7 is not None and rt7[0] == "T")

        # Case 8: Rule B — 5 prior un-greened edits on one file AND a prior
        # V-block (verification revealed the gap) -> deny the 6th edit attempt.
        fresh_dir("case8-rule-b-fires")
        for i in range(5):
            append_event({"t": float(i + 1), "ev": "edit", "file": "e.py"})
        append_event({"t": 6.0, "ev": "block", "gate": "V"})
        rb1 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "e.py", "new_string": "x"}})
        check("rule B denies 6th un-greened edit (with prior V-block)", rb1 is not None and rb1[0] == "B")

        # Case 8b: THROTTLE — 5 un-greened edits but NO verification block yet
        # -> Rule B does NOT fire (raw edit-thrash alone is not enough).
        fresh_dir("case8b-rule-b-throttled")
        for i in range(5):
            append_event({"t": float(i + 1), "ev": "edit", "file": "e2.py"})
        rb_throttle = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "e2.py", "new_string": "x"}})
        check("rule B throttled: 5 edits but no V/R block -> allow", rb_throttle is None)

        # Case 9: Rule B — same eligible setup, but an unerr-opus Task already
        # ran -> no deny.
        fresh_dir("case9-rule-b-escalated")
        for i in range(5):
            append_event({"t": float(i + 1), "ev": "edit", "file": "f.py"})
        append_event({"t": 6.0, "ev": "block", "gate": "V"})
        append_event({"t": 10.0, "ev": "task", "agent": "unerr-opus"})
        rb2 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "f.py", "new_string": "x"}})
        check("rule B not fired once unerr-opus task recorded", rb2 is None)

        # Case 10: Rule B stops firing after 2 denials (cap).
        fresh_dir("case10-rule-b-cap")
        for i in range(5):
            append_event({"t": float(i + 1), "ev": "edit", "file": "g.py"})
        append_event({"t": 6.0, "ev": "block", "gate": "V"})
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

        # Case 13: is_broad_test — narrow signals vs broad runs.
        check("is_broad_test: bare file is broad", is_broad_test("pytest lib/x/tests/test_foo.py"))
        check("is_broad_test: django app run is broad", is_broad_test("./tests/runtests.py delete"))
        check("is_broad_test: pytest node id is narrow", not is_broad_test("pytest tests/test_foo.py::test_bar"))
        check("is_broad_test: -k filter is narrow", not is_broad_test("pytest tests/ -k test_large"))
        check("is_broad_test: dotted Class.test_method is narrow",
              not is_broad_test("./tests/runtests.py delete.tests.DeletionTests.test_large_delete"))
        # Case 13b: findings 2/3 — commands that LOOK narrow but run a whole
        # module/class must stay BROAD.
        check("is_broad_test: django dotted whole-module is broad",
              is_broad_test("./tests/runtests.py utils_tests.test_datastructures"))
        check("is_broad_test: `-m unittest` dotted module is broad",
              is_broad_test("python -m unittest tests.test_foo"))
        check("is_broad_test: pytest ::TestClass (whole class) is broad",
              is_broad_test("pytest tests/test_foo.py::TestFooClass"))
        check("is_broad_test: pytest ::Class::method (single method) is narrow",
              not is_broad_test("pytest tests/test_foo.py::TestFooClass::test_bar"))
        # Case 13c: finding 4 — a bare reproduction script is NOT a suite run,
        # so it never satisfies the regression gate.
        check("is_broad_test: bare repro script is not broad",
              not is_broad_test("python repro_issue.py"))
        # Case 13d: sympy's native runner — bin/test must be recognized by BOTH
        # the recorder (TEST_CMD_RE, else no test events at all -> Gate R blind)
        # and the classifier (whole-file run -> broad).
        check("TEST_CMD_RE records sympy bin/test",
              TEST_CMD_RE.search("./bin/test -C --verbose sympy/core/tests/test_basic.py") is not None)
        check("is_broad_test: sympy bin/test whole-file run is broad",
              is_broad_test("./bin/test -C --verbose sympy/core/tests/test_basic.py"))

        # Case 14: REGRESSION GATE — edited, but the only post-edit green run is
        # NARROW (a single method) -> V blocks and demands the full module.
        fresh_dir("case14-narrow-only-verify")
        append_event({"t": 1.0, "ev": "edit", "file": "django/db/models/deletion.py"})
        append_event({"t": 2.0, "ev": "test",
                      "key": "./tests/runtests.py delete.tests.DeletionTests.test_target", "ok": True})
        r = gate_once()
        check("narrow-only green -> V blocks", r is not None and r[0] == "V")
        check("narrow-only V message demands the full module", r is not None and "NARROW" in r[1])

        # Case 15: broad green after edit -> V satisfied, nothing else triggers.
        fresh_dir("case15-broad-verify-ok")
        append_event({"t": 1.0, "ev": "edit", "file": "django/db/models/deletion.py"})
        append_event({"t": 2.0, "ev": "test", "key": "./tests/runtests.py delete", "ok": True})
        r = gate_once()
        check("broad green after edit -> allow", r is None)
    finally:
        if orig_env is None:
            os.environ.pop(STATE_ENV, None)
        else:
            os.environ[STATE_ENV] = orig_env
        if orig_hooks is None:
            os.environ.pop(HARNESS_HOOKS_ENV, None)
        else:
            os.environ[HARNESS_HOOKS_ENV] = orig_hooks
        if orig_profile is None:
            os.environ.pop(HARNESS_PROFILE_ENV, None)
        else:
            os.environ[HARNESS_PROFILE_ENV] = orig_profile
        if orig_panel is None:
            os.environ.pop(ESCALATION_PANEL_ENV, None)
        else:
            os.environ[ESCALATION_PANEL_ENV] = orig_panel
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
