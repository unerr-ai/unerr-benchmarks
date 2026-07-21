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
Note the split of labor: REPRODUCE-FIRST stays prompt-enforced only — this
harness has no way to sense "did you reproduce the issue before editing";
what it mechanically checks is PROOF OF WORK (Gate Z), a REGRESSION signal
(Gate R), and VERIFICATION-BEFORE-FINISH (Gate V), plus the escalation
hand-off (Gate E).

FAIL-OPEN IS THE #1 CONTRACT: any internal exception here must never break
the benchmark run. All subcommands below run their real work inside a
try/except that falls through to a silent, unconditional exit(0). A bug in
this script degrades to a no-op gate/deny, never a stuck or crashed agent turn.

Subcommands (each reads one hook-event JSON object from stdin):
  record   PostToolUse recorder. Silent. Appends one compact JSON line per
           edit/cmd/task event to the state log. Never prints, never blocks.
  gate     Stop hook. Reads the state log, evaluates the finish gates in a
           fixed order (Z, R, V, E — first hit wins), and on a hit prints
           {"decision":"block","reason":"..."} so Claude Code returns the
           agent to work instead of ending the turn. An OVERALL cap (3 total
           blocks, across all gates) guarantees this can never loop forever
           even if the agent never satisfies a gate. Gate V requires a GREEN
           `# unerr:verify`-marked command with no edit landed since — there
           is no fixed test-runner assumption, the agent declares which
           command IS the proof. Gate E escalates on a VERIFICATION-revealed
           trigger (a prior R-block, or V capped at 2) OR a light mechanical
           "stuck" signal (the same command failing STUCK_FAIL_THRESHOLD+
           times with no intervening success) — the raw edit-count arm was
           removed, see Phase A findings. ESCALATION_PANEL (unset/"0",
           default) -> LADDER: unerr-opus alone, then unerr-fable only if the
           trigger persists past opus; "1" -> PANEL: the original
           spawn-both-in-parallel single block.
  deny     PreToolUse hook. Reads the state log, evaluates the deny rules in
           a fixed order (T, B — first match wins), and on a match prints a
           hookSpecificOutput permissionDecision:"deny" object so Claude Code
           refuses the tool call before it runs. T (an edit into a
           test-shaped path fires a one-time nudge — the grader runs its own
           copy of the checks, so editing a test usually only fakes progress;
           an identical re-issue on the same file is treated as an
           evidence-cited override and allowed through) and B (5+ un-greened
           edits on one file AND verification has already blocked -> forced
           escalation, reads the same state log gate uses) are the only two
           rules — the old python/django-specific time-source convention
           check (Rule C) has been removed, it does not belong in a universal
           harness. Every deny also appends a "deny" event to the state log.

PROFILE: HARNESS_HOOKS unset/""/"0" turns every subcommand into a no-op; ANY
other value ("1", "generic", "swe", "universal", ...) turns hooks on with the
single "universal" profile — there used to be a separate "swe"
(TEST_CMD_RE-gated pytest/manage.py-test/tox/bin-test/repro* sensor) and
"generic" (agent-declared `# unerr:verify` marker) profile; they have been
collapsed into one (the old "generic" sensor, unconditionally): every Bash
command is ledgered as a "cmd" event, and a command counts as verification
only when the agent suffixes it with the literal `# unerr:verify` (or
`#unerr:verify`) marker. This works uniformly whether the task is a
SWE-bench repo with a fixed test layout or a Terminal-Bench-style task with
none and a hidden grader. HARNESS_PROFILE is accepted for env-wiring compat
but its value is never read.

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
# HARNESS_HOOKS: unset/""/"0" -> hooks off, every subcommand a no-op. ANY
# other value ("1", "generic", "swe", "universal", ...) -> hooks on, the
# single "universal" profile. There used to be a "swe" sensor (a fixed
# TEST_CMD_RE pytest/manage.py-test/tox/bin-test/repro* pattern) and a
# "generic" one (agent-declared `# unerr:verify` markers on ANY Bash
# command); they have been collapsed into one — the old "generic" sensor,
# unconditionally. HARNESS_PROFILE_ENV is kept defined for env-wiring
# compat but its value is never read (see _profile()).
#
# ESCALATION_PANEL: orthogonal to HARNESS_HOOKS — controls Gate E's
# escalation SHAPE, not whether it fires. unset/"0"/anything else -> LADDER
# (the default): a mechanical two-rung hand-off, unerr-opus alone first,
# unerr-fable only if the trigger persists after opus. "1" -> PANEL (the
# original behavior): spawn unerr-opus AND unerr-fable together as a
# decorrelated judge panel. See _escalation_panel().
HARNESS_HOOKS_ENV = "HARNESS_HOOKS"
HARNESS_PROFILE_ENV = "HARNESS_PROFILE"  # compat only — never read, see _profile()
ESCALATION_PANEL_ENV = "ESCALATION_PANEL"

# Literal substrings only (no whitespace-tolerant regex) — both forms are
# accepted verbatim, then stripped before the command is hashed into a
# ledger key so a marked/unmarked re-run of the same command still collapses
# to one ledger entry.
VERIFY_MARKERS = ("# unerr:verify", "#unerr:verify")
CMDS_CAP = 200

EDIT_TOOL_RE = re.compile(r"^(Edit|Write|MultiEdit|NotebookEdit|mcp__unerr__file_edit)$")

OVERALL_CAP = 3
GATE_CAPS = {"Z": 1, "R": 1, "V": 2, "E": 1}
# LADDER mode's own Gate-E cap (2 rungs: opus alone, then fable) — separate
# from GATE_CAPS["E"] (1), which stays PANEL mode's cap (a single
# spawn-both-in-parallel block). See _escalation_panel()/_gate_e_ladder().
LADDER_E_CAP = 2
# A light, deliberately conservative Layer-3 mechanical trigger for Gate E:
# the same command key (by _cmd_key) failing this many times with no
# intervening success is treated as "stuck, no progress" and routes to the
# SAME capped escalation ladder as the R/V triggers — it never adds a new
# hard block on its own. See _repeated_failure()/_gate_e_ladder/_gate_e_panel.
STUCK_FAIL_THRESHOLD = 4

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
    """Resolve whether the harness is active. HARNESS_HOOKS unset/""/"0" ->
    None (hooks off, every subcommand is a no-op). ANY other value ("1",
    "generic", "swe", "universal", ...) -> the string "universal" — there is
    only one profile now, so any truthy value opts in. HARNESS_PROFILE is
    accepted for env-wiring compat but its value is never read."""
    hh = os.environ.get(HARNESS_HOOKS_ENV) or ""
    if hh in ("", "0"):
        return None
    return "universal"


def _escalation_panel():
    """Resolve Gate E's escalation SHAPE once, early, same style as
    _profile(). "1" -> True (PANEL: today's single block demanding
    unerr-opus AND unerr-fable spawned together in parallel, byte-identical
    message). Unset/"0"/anything else -> False (LADDER, the default: a
    mechanical two-rung hand-off — unerr-opus alone first, unerr-fable only
    if the trigger persists after opus). Orthogonal to HARNESS_HOOKS."""
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
    """Ledger key: first 12 hex chars of the sha1 of the whitespace-collapsed
    command with the verify marker stripped."""
    collapsed = " ".join(_strip_verify_marker(cmd).split())
    return hashlib.sha1(collapsed.encode("utf-8", "replace")).hexdigest()[:12]


def _cmd_ledger(cmd_events):
    """Fold `cmd` events into a per-key ledger: key -> {"ok": <latest run's
    outcome>, "n": <run count>, "verify": <marker ever seen for this key>,
    "was_ok": <ever succeeded>}. Insertion-ordered and capped at CMDS_CAP
    distinct keys — once a NEW key would exceed the cap, the oldest-inserted
    key is dropped first, so an unbounded agent session can't grow this
    without bound."""
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
    """Latest `t` among `cmd` events that were BOTH verify-marked and
    successful, or None if no such event exists yet."""
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

def build_record_event(data, profile="universal"):
    """Pure: hook stdin dict -> event dict to append, or None to record
    nothing. Split out from cmd_record for direct unit testing. Edit/Write/
    MultiEdit/NotebookEdit/mcp__unerr__file_edit -> an "edit" event (file
    path only, dropped on tool error). Bash (any non-empty command) -> a
    "cmd" event ledgered by content hash (key = _cmd_key(cmd)), independent
    of ok/verify — a run is "verify"-flagged only when the agent suffixes
    the command with the literal `# unerr:verify` marker; there is no fixed
    test-runner sensor, this harness has no notion of a repo's test layout.
    Task -> a "task" event (records which sub-agent ran, for the escalation
    ladder). profile is accepted for API compat; there is only one profile
    now so it is otherwise unused."""
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

def _repeated_failure(cmd_events):
    """Light, deliberately conservative Layer-3 signal for Gate E: True if
    any single command key (by _cmd_key) has failed (ok=False) at least
    STUCK_FAIL_THRESHOLD times across the whole log — the same command
    tried repeatedly with no success, independent of the `# unerr:verify`
    marker. Feeds Gate E's rung-1 trigger as a THIRD disjunct alongside the
    verification-revealed R/V triggers; it never blocks on its own and never
    changes the ladder's caps or rung-2 logic."""
    fail_counts = Counter()
    for e in cmd_events:
        if e.get("ev") == "cmd" and not e.get("ok"):
            fail_counts[e.get("key", "")] += 1
    return any(n >= STUCK_FAIL_THRESHOLD for n in fail_counts.values())


def _gate_e_panel(tasks, block_counts, repeated_failure):
    """Gate E, PANEL mode (ESCALATION_PANEL=1): byte-identical to the
    original single-arm behavior — one block, once, demanding unerr-opus
    AND unerr-fable spawned together as a decorrelated judge panel. Capped
    at GATE_CAPS["E"] (1). Trigger is a prior R-block, V capped at 2, OR the
    light mechanical `repeated_failure` signal (STUCK_FAIL_THRESHOLD+
    failures of the same command with no success) — the third disjunct
    never changes the cap, it only widens what counts as "stuck"."""
    if block_counts.get("E", 0) >= GATE_CAPS["E"]:
        return None
    r_already = block_counts.get("R", 0) >= 1
    v_capped = block_counts.get("V", 0) >= 2
    escalated = any(t.get("agent") in ("unerr-opus", "unerr-fable") for t in tasks)
    if not ((r_already or v_capped or repeated_failure) and not escalated):
        return None
    if r_already:
        trigger = "a previously-passing test was regressed and one rework did not recover it"
    elif v_capped:
        trigger = "the verification gate (V) has blocked twice without a green finish"
    else:
        trigger = "the same command has failed repeatedly without progress"
    return (
        "E",
        f"Escalation trigger hit: {trigger}. Per the escalation contract: "
        "spawn unerr-opus and unerr-fable in parallel with the same "
        "evidence brief (issue text, observations, attempts, all "
        "candidate sites — not your preferred hypothesis), reconcile "
        "their verdicts, implement the winner, verify, then finish.",
    )


def _gate_e_ladder(tasks, blocks, block_counts, repeated_failure):
    """Gate E, LADDER mode (the default): two rungs, tracked by WHICH agent
    ran rather than "any" escalation. Rung 1 fires once neither unerr-opus
    nor unerr-fable has run, on a prior R-block, V capped at 2, OR the light
    mechanical `repeated_failure` signal (STUCK_FAIL_THRESHOLD+ failures of
    the same command with no success — thrash without progress) — demanding
    unerr-opus ALONE — one decorrelated-enough second opinion for a
    same-family reasoning-effort ladder, not the full panel. Rung 2 fires
    only once opus has run, fable has not, AND a NEW R- or V-block was
    recorded strictly AFTER the opus Task event's own timestamp (i.e. the
    trigger PERSISTED past opus's fix) — demanding unerr-fable, primed with
    opus's proposal and why it failed. Capped at LADDER_E_CAP (2) total
    E-blocks; once fable has also run (or the cap is spent), Gate E never
    blocks again. The repeated_failure disjunct only feeds rung 1 — it never
    changes rung 2's logic or either cap."""
    if block_counts.get("E", 0) >= LADDER_E_CAP:
        return None

    fable_used = any(t.get("agent") == "unerr-fable" for t in tasks)
    if fable_used:
        return None

    opus_ts = [t.get("t", 0) for t in tasks if t.get("agent") == "unerr-opus"]

    if not opus_ts:
        r_already = block_counts.get("R", 0) >= 1
        v_capped = block_counts.get("V", 0) >= 2
        if not (r_already or v_capped or repeated_failure):
            return None
        if r_already:
            trigger = "a previously-passing test was regressed and one rework did not recover it"
        elif v_capped:
            trigger = "the verification gate (V) has blocked twice without a green finish"
        else:
            trigger = "the same command has failed repeatedly without progress"
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
    # no separate persistence mechanism needed). Unchanged by repeated_failure.
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


def evaluate_gate(events, profile="universal"):
    """Pure: full event list -> None (allow) or (gate_letter, reason) to
    block. Evaluated in fixed order Z, R, V, E; first hit wins. Universal
    profile: every Bash command is ledgered as a "cmd" event (key =
    _cmd_key); a run counts as verification only when the agent suffixes it
    with the literal `# unerr:verify` marker — there is no fixed
    test-runner assumption, so this works whether the task is a repo with a
    fixed test layout or a Terminal-Bench-style task with none and a hidden
    grader. The overall cap gates the Z/R/V nudges; Gate E is EXEMPT from it
    so escalation stays deliverable even after the cap is spent — Gate E's
    OWN cap (one-shot PANEL, or two-rung LADDER) is decided by
    _escalation_panel(), see _gate_e_panel/_gate_e_ladder. Gate E's rung-1
    trigger is a prior R-block, V capped at 2, OR a light mechanical
    "stuck" signal (_repeated_failure: the same command key failing
    STUCK_FAIL_THRESHOLD+ times) — thrash without progress routes to the
    same capped ladder, never a new hard block. profile is accepted for API
    compat; there is only one profile now so it is otherwise unused."""
    edits = [e for e in events if e.get("ev") == "edit"]
    cmds = [e for e in events if e.get("ev") == "cmd"]
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
    ledger = _cmd_ledger(cmds)
    repeated_failure = _repeated_failure(cmds)

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

    # Gate E — a VERIFICATION-REVEALED escalation trigger (a prior R-block,
    # or V capped at 2) OR a light mechanical "stuck" signal
    # (repeated_failure) fired, but no unerr-opus/unerr-fable ran (or, in
    # LADDER mode, the trigger persisted past the first hand-off). This gate
    # is reached even when over_cap (see the exemption above), so a run
    # whose cap was spent on Z/V blocks still receives the escalation.
    # ESCALATION_PANEL selects the SHAPE (single spawn-both block vs a
    # two-rung opus-then-fable ladder) — see _gate_e_panel/_gate_e_ladder.
    e_result = (
        _gate_e_panel(tasks, block_counts, repeated_failure)
        if _escalation_panel()
        else _gate_e_ladder(tasks, blocks, block_counts, repeated_failure)
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
    "This file looks like a test: the grader runs its own copy of the "
    "checks, so editing it usually only fakes progress rather than fixing "
    "anything — fix the real source at the definition site instead. If you "
    "genuinely need to edit this test (e.g. the task is to write tests), "
    "re-issue the same edit and it will be allowed."
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


def rule_t(events, file_path):
    """Rule T: a one-time, override-able nudge (softened from a hard deny —
    a universal harness has no way to KNOW a test edit is illegitimate, only
    to flag it). is_test_path(file_path) AND no prior `deny` event with
    rule=="T" for this same file -> deny once. A second identical attempt on
    the same file (a prior T-deny already recorded for it) is treated as an
    evidence-cited override and allowed through — same pattern the removed
    Rule C used."""
    if not file_path:
        return None
    if not is_test_path(file_path):
        return None
    prior_t = any(
        e.get("ev") == "deny" and e.get("rule") == "T" and e.get("file") == file_path
        for e in events
    )
    if prior_t:
        return None
    return ("T", TEST_DENY_MSG)


def _edits_since_last_green_verify(events, file_path):
    """Counts `edit` events for file_path that landed after the last GREEN
    verify-marked `cmd` event (last_green_verify_ts) in the whole log; with
    no such event yet, counts every edit ever recorded for that file."""
    last_ok_t = _last_green_verify_ts([e for e in events if e.get("ev") == "cmd"])
    matching = [e for e in events if e.get("ev") == "edit" and e.get("file") == file_path]
    if last_ok_t is None:
        return len(matching)
    return len([e for e in matching if e.get("t", 0) > last_ok_t])


def rule_b(events, file_path):
    """Rule B: 5+ un-greened edits on the same file with no unerr-opus/
    unerr-fable escalation yet -> force-escalate. "Un-greened" is measured
    against the last GREEN `# unerr:verify`-marked command
    (_edits_since_last_green_verify) — there is no fixed test-runner sensor.
    Throttled to fire only once verification has REVEALED a gap (a prior
    V-block or R-block); raw edit-thrash alone no longer triggers escalation
    (Phase A showed count-triggered escalation billed the reasoner/oracle
    tiers for zero conversions). Capped at RULE_B_DENY_CAP denials per run so
    a still-stuck agent can't be denied forever."""
    if not file_path:
        return None
    if _edits_since_last_green_verify(events, file_path) < RULE_B_EDIT_THRESHOLD:
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
        f"You have edited {basename} 5+ times without a green `# unerr:verify` "
        "run — escalation trigger (b) has fired. STOP editing. In ONE message "
        "spawn BOTH unerr-opus and unerr-fable via the Task tool with the same "
        "evidence brief (task text, what you observed, what you tried, all "
        "candidate sites), reconcile their verdicts, then implement the "
        "agreed fix once.",
    )


def evaluate_deny(events, data, profile="universal"):
    """Pure: state events + hook stdin dict -> None (allow) or (rule_letter,
    reason) to deny. Evaluated in fixed order T, B; first match wins.
    Universal profile: T is a one-time, override-able nudge on test-shaped
    paths (rule_t); B force-escalates on 5+ un-greened edits on one file
    once verification has already revealed a gap (rule_b, rewired off the
    `# unerr:verify`-marked cmd ledger). The old Rule C (a
    datetime.now()-vs-utcnow() time-source convention check) is GONE — it
    was python/django-specific and does not belong in a universal harness.
    profile is accepted for API compat; there is only one profile now so it
    is otherwise unused."""
    tool_input = data.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""

    result = rule_t(events, file_path)
    if result is not None:
        return result

    result = rule_b(events, file_path)
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
    # Force the harness ON (the single universal profile) regardless of the
    # ambient env so gate_once/deny_once's profile-off short-circuit doesn't
    # turn every case below into a no-op. HARNESS_PROFILE is popped too — it
    # is never read by _profile() anymore, this is belt-and-suspenders. Also
    # isolate ESCALATION_PANEL to its LADDER default — none of the cases
    # below assert panel-vs-ladder message shape (only gate letters), so
    # this is determinism, not a behavior requirement.
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
        # V's own cap is spent, but Gate E's widened V-capped arm now fires —
        # the mechanical "V exhausted, still not verified -> escalate"
        # handoff the arm exists for, not a bug.
        fresh_dir("case2-edit-no-verify")
        append_event({"t": 1.0, "ev": "edit", "file": "a.py"})
        r1 = gate_once()
        r2 = gate_once()
        r3 = gate_once()
        check("edit-no-verify call 1 -> V", r1 is not None and r1[0] == "V")
        check("edit-no-verify call 2 -> V", r2 is not None and r2[0] == "V")
        check(
            "edit-no-verify call 3 -> E (V cap spent, E's V-block arm hands off)",
            r3 is not None and r3[0] == "E",
        )

        # Case 3: a verify-marked command passed, then the same key failed ->
        # Gate R.
        fresh_dir("case3-regression")
        append_event({"t": 1.0, "ev": "edit", "file": "b.py"})
        append_event({"t": 2.0, "ev": "cmd", "key": "k3", "ok": True, "verify": True})
        append_event({"t": 3.0, "ev": "cmd", "key": "k3", "ok": False, "verify": True})
        r = gate_once()
        check("pass-then-fail verify-marked cmd -> R", r is not None and r[0] == "R")

        # Case 4: same file edited 3x, then a green verify-marked run, no V/R
        # block -> the hot-file arm is GONE, so this now ALLOWS (no
        # escalation on raw edit count alone).
        fresh_dir("case4-hot-file-no-longer-escalates")
        append_event({"t": 1.0, "ev": "edit", "file": "c.py"})
        append_event({"t": 2.0, "ev": "edit", "file": "c.py"})
        append_event({"t": 3.0, "ev": "edit", "file": "c.py"})
        append_event({"t": 4.0, "ev": "cmd", "key": "k4", "ok": True, "verify": True})
        r = gate_once()
        check("3-edits + green verify, no V/R block -> ALLOW (hot-file arm removed)", r is None)

        # Case 5: same setup with an unerr-opus Task -> still allow (no
        # trigger, and escalated anyway).
        fresh_dir("case5-escalated")
        append_event({"t": 1.0, "ev": "edit", "file": "d.py"})
        append_event({"t": 2.0, "ev": "edit", "file": "d.py"})
        append_event({"t": 3.0, "ev": "edit", "file": "d.py"})
        append_event({"t": 4.0, "ev": "cmd", "key": "k5", "ok": True, "verify": True})
        append_event({"t": 5.0, "ev": "task", "agent": "unerr-opus"})
        r = gate_once()
        check("green verify + unerr-opus task -> no block", r is None)

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
        append_event({"t": 2.0, "ev": "cmd", "key": "k6b", "ok": True, "verify": True})
        append_event({"t": 3.0, "ev": "block", "gate": "Z"})
        append_event({"t": 4.0, "ev": "block", "gate": "V"})
        append_event({"t": 5.0, "ev": "block", "gate": "V"})
        r = gate_once()
        check("cap spent on Z+V+V, not escalated -> E still fires (no deadlock)",
              r is not None and r[0] == "E")

        # Case 7: Rule T — test paths get a one-time nudge, non-test path
        # allowed outright.
        fresh_dir("case7-rule-t")
        rt1 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "tests/test_x.py", "new_string": "x"}})
        rt2 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "tests/regressiontests/foo.py", "new_string": "x"}})
        rt3 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "src/utils.py", "new_string": "x"}})
        check("rule T denies (once) tests/test_x.py", rt1 is not None and rt1[0] == "T")
        check("rule T denies (once) tests/regressiontests/foo.py", rt2 is not None and rt2[0] == "T")
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
        check("rule T denies (once) root-level test_requests.py (basename rule)", rt6 is not None and rt6[0] == "T")
        check("rule T denies (once) testing/ segment (pytest's suite dir)", rt7 is not None and rt7[0] == "T")

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

        # Case 11: Rule C is GONE — a datetime.now( edit into a file whose
        # current on-disk contents already use utcnow() is now ALLOWED (the
        # python/django-specific time-source convention check does not
        # belong in a universal harness).
        fresh_dir("case11-rule-c-removed")
        conv_file = os.path.join(tmp_root, "conv_utcnow.py")
        with open(conv_file, "w") as f:
            f.write("import datetime\nx = datetime.datetime.utcnow()\n")
        rc1 = deny_once({"tool_name": "Edit", "tool_input": {"file_path": conv_file, "new_string": "y = datetime.now()"}})
        check("rule C removed: datetime.now( into a utcnow-convention file is ALLOWED", rc1 is None)

        # Case 12: Gate E's V-block arm fires standalone on 2 prior V-blocks
        # (no hot file, no R-block, no repeated_failure) once the edit IS
        # verified — isolates the arm from V's own natural cap-exhaustion
        # sequence covered in case 2.
        fresh_dir("case12-gate-e-vblock-arm")
        append_event({"t": 1.0, "ev": "edit", "file": "h.py"})
        append_event({"t": 2.0, "ev": "cmd", "key": "k12", "ok": True, "verify": True})
        append_event({"t": 3.0, "ev": "block", "gate": "V"})
        append_event({"t": 4.0, "ev": "block", "gate": "V"})
        r = gate_once()
        check("2 prior V-blocks -> E's V-block arm fires", r is not None and r[0] == "E")

        # Case 13: record — a Bash command carrying the verify marker records
        # a ledgered "cmd" event with the right key/verify/ok. Direct unit
        # test of build_record_event (pure function, no state/env needed).
        cmd_text = "pytest tests/ # unerr:verify"
        rec_ev = build_record_event({
            "tool_name": "Bash",
            "tool_input": {"command": cmd_text},
            "exit_code": 0,
        })
        check(
            "record: Bash w/ verify marker -> cmd event, right key/verify/ok",
            rec_ev is not None
            and rec_ev["ev"] == "cmd"
            and rec_ev["key"] == _cmd_key(cmd_text)
            and rec_ev["verify"] is True
            and rec_ev["ok"] is True,
        )

        # Case 14: Rule T's one-time override — a second identical edit on the
        # SAME test-path file is allowed through once a prior T-deny for that
        # file exists (evidence-cited override, same pattern the removed Rule
        # C used).
        fresh_dir("case14-rule-t-override")
        rt_first = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "tests/test_override.py", "new_string": "x"}})
        rt_second = deny_once({"tool_name": "Edit", "tool_input": {"file_path": "tests/test_override.py", "new_string": "x"}})
        check("rule T denies a test-path edit once", rt_first is not None and rt_first[0] == "T")
        check("rule T allows the identical re-issue on the same file (override)", rt_second is None)

        # Case 15: the light mechanical "stuck" trigger — the same
        # verify-marked command key fails STUCK_FAIL_THRESHOLD times (with an
        # unrelated key already green, so Gate V itself is satisfied and does
        # not confound the result) -> Gate E fires via repeated_failure alone,
        # with no prior R-block and V not yet capped.
        fresh_dir("case15-stuck-trigger")
        append_event({"t": 1.0, "ev": "edit", "file": "s.py"})
        append_event({"t": 2.0, "ev": "cmd", "key": "greenkey", "ok": True, "verify": True})
        for i in range(STUCK_FAIL_THRESHOLD):
            append_event({"t": float(3 + i), "ev": "cmd", "key": "stuckkey", "ok": False, "verify": True})
        r = gate_once()
        check(
            f"{STUCK_FAIL_THRESHOLD}x identical failing verify-marked cmd, no R/V-cap -> "
            "Gate E fires via the stuck trigger",
            r is not None and r[0] == "E",
        )
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
