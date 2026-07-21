"""Focused tests for collect-failed.py's benchmark-aware WHY_FAILED.txt.

collect-failed.py writes one WHY_FAILED.txt per failed (resolved=0) instance.
Its content used to be hardcoded SWE-bench-shaped (FAIL_TO_PASS/patch_exists),
which is structurally meaningless for a harness_run benchmark (Terminal-Bench
via Harbor): Harbor grades in-container on reward, not test transitions, and
there is no patch at all. These tests build synthetic bundle fixtures (no
docker, no network, no real fly/harbor calls) for the two harness_run shapes
that matter most — a trial KILLED mid-run (caffe-cifar-10's real
report.json shape, 2026-07-21) vs a trial that COMPLETED but was graded
wrong (chess-best-move's real report.json shape) — plus a completed trial
whose trajectory ends on an unanswered user-role nudge (silent session
death), and confirm the pre-existing SWE-bench (resolve_then_grade) path is
byte-for-byte unchanged.

Run: python3 -m pytest e2e/distributed/tools/test_collect_failed.py -q
"""
import importlib.util
import json
import os
import pathlib

_TOOLS_DIR = pathlib.Path(__file__).resolve().parent


def _import_by_path(name, path):
    """Import collect-failed.py by file path — "collect-failed.py" isn't a
    valid module name to `import` (hyphen), same pattern debug_instance.py
    and make_submission.py already use to reuse its parsing helpers."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collect_failed = _import_by_path("collect_failed_tool", _TOOLS_DIR / "collect-failed.py")


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def _write_report(bundle, iid, entry):
    _write_json(os.path.join(bundle, "logs", "grade-merged", iid, "report.json"),
                {iid: entry})


def _art_dir(bundle, label, iid):
    return os.path.join(bundle, "results", label, "artifacts", iid)


def _why_failed_text(dest, iid):
    with open(os.path.join(dest, iid, "WHY_FAILED.txt"), encoding="utf-8") as f:
        return f.read()


def _index_text(dest):
    with open(os.path.join(dest, "INDEX.md"), encoding="utf-8") as f:
        return f.read()


# ── real report.json shapes, trimmed to the fields collect-failed.py reads ──

# caffe-cifar-10, 2026-07-21 — killed after exhausting its [agent] timeout_sec
# budget. rc stays None (harness_terminal.run()'s wait loop only assigns rc on
# the clean-exit branch), and Harbor never got to write the trial-level
# result.json (only the job-level one, aggregated over trials that never
# finished) — see DEBUG_FAILED_TASK.md.
_KILLED_HARBOR_RESULT = {
    "id": "job-uuid",
    "started_at": "2026-07-21T00:00:00Z",
    "updated_at": "2026-07-21T00:10:00Z",
    "finished_at": None,
    "n_total_trials": 1,
    "stats": {
        "n_completed_trials": 0,
        "n_errored_trials": 0,
        "n_running_trials": 1,
        "n_pending_trials": 0,
        "n_cancelled_trials": 0,
        "n_retries": 0,
        "evals": {},
    },
}
_KILLED_ENTRY = {
    "rc": None,
    "exceptions": [],
    "harbor_result": _KILLED_HARBOR_RESULT,
    "resolved": False,
}

# chess-best-move — the trial COMPLETED (Harbor wrote the trial-level
# result.json, with finished_at set and a verifier_result), the agent
# answered, and the answer was wrong (reward 0.0). This is the exact case the
# task calls out: "confirms resolved=False and nothing else" under the old
# hardcoded FAIL_TO_PASS/patch_exists report.
_COMPLETED_HARBOR_RESULT = {
    "id": "trial-uuid",
    "started_at": "2026-07-21T00:00:00Z",
    "finished_at": "2026-07-21T00:05:00Z",
    "verifier_result": {"rewards": {"reward": 0.0}},
}
_COMPLETED_ENTRY = {
    "rc": 0,
    "exceptions": [],
    "harbor_result": _COMPLETED_HARBOR_RESULT,
    "resolved": False,
}

_AGENT_ENDING_TRAJECTORY = {
    "schema_version": 1,
    "session_id": "sid",
    "agent": "claude-code",
    "steps": [
        {"step_id": 1, "source": "user", "message": "Write the best move to move.txt"},
        {"step_id": 2, "source": "agent", "message": "Wrote g2g4 to move.txt", "model_name": "gpt-5.6-terra"},
    ],
    "final_metrics": {"total_cost_usd": 7.84},
}

# Mirrors the discriminator from the silent-session-death finding
# (build-pmars/build-cython-ext, 2026-07-21): the session ends on Claude
# Code's own unanswered "[Your previous response had no visible output...]"
# nudge (source == "user"), not on the agent.
_USER_NUDGE_ENDING_TRAJECTORY = {
    "schema_version": 1,
    "session_id": "sid2",
    "agent": "claude-code",
    "steps": [
        {"step_id": 1, "source": "user", "message": "do the task"},
        {"step_id": 2, "source": "agent", "message": ""},
        {"step_id": 3, "source": "user",
         "message": "[Your previous response had no visible output. Please continue.]"},
    ],
    "final_metrics": {"total_cost_usd": 1.2},
}

_SWE_ENTRY = {
    "resolved": False,
    "patch_exists": True,
    "patch_successfully_applied": True,
    "tests_status": {
        "FAIL_TO_PASS": {"success": ["test_a"], "failure": ["test_b"]},
        "PASS_TO_PASS": {"success": ["test_c"], "failure": []},
    },
}


def test_harness_killed_trial_reports_killed_not_faketest(tmp_path):
    bundle = str(tmp_path / "bundle")
    label = "tb-run"
    iid = "caffe-cifar-10"
    _write_report(bundle, iid, _KILLED_ENTRY)
    # no trajectory.json — the trial never completed, so Harbor never wrote one.
    os.makedirs(_art_dir(bundle, label, iid), exist_ok=True)

    dest = str(tmp_path / "dest")
    n = collect_failed.collect(bundle, label, dest)
    assert n == 1

    body = _why_failed_text(dest, iid)
    assert "flow: harness_run" in body
    assert "KILLED MID-RUN" in body
    assert "n_completed_trials=0" in body
    assert "n_running_trials=1" in body
    # the old hardcoded SWE fields must NOT appear for a harness_run instance
    assert "FAIL_TO_PASS" not in body
    assert "patch_exists=" not in body
    assert "trajectory.json=no" in body
    assert "trajectory.json missing" in body

    idx = _index_text(dest)
    assert "KILLED" in idx


def test_harness_completed_agent_finish_is_capability_miss(tmp_path):
    bundle = str(tmp_path / "bundle")
    label = "tb-run"
    iid = "chess-best-move"
    _write_report(bundle, iid, _COMPLETED_ENTRY)
    art = _art_dir(bundle, label, iid)
    os.makedirs(art, exist_ok=True)
    _write_json(os.path.join(art, "trajectory.json"), _AGENT_ENDING_TRAJECTORY)

    dest = str(tmp_path / "dest")
    collect_failed.collect(bundle, label, dest)

    body = _why_failed_text(dest, iid)
    assert "flow: harness_run" in body
    assert "trial status: COMPLETED" in body
    assert "harbor_reward=0.0" in body
    assert "trajectory.json=yes" in body
    assert "last trajectory step source: agent" in body
    assert "capability miss" in body
    assert "FAIL_TO_PASS" not in body
    assert "patch_exists=" not in body

    idx = _index_text(dest)
    assert "capability miss" in idx
    # trajectory.json must actually be copied into the triage dir, not just
    # reported as present — the whole point of the archive is that it
    # outlives the ephemeral fleet.
    assert os.path.isfile(os.path.join(dest, iid, "trajectory.json"))


def test_harness_completed_user_nudge_is_transient_death(tmp_path):
    bundle = str(tmp_path / "bundle")
    label = "tb-run"
    iid = "build-pmars"
    _write_report(bundle, iid, _COMPLETED_ENTRY)
    art = _art_dir(bundle, label, iid)
    os.makedirs(art, exist_ok=True)
    _write_json(os.path.join(art, "trajectory.json"), _USER_NUDGE_ENDING_TRAJECTORY)

    dest = str(tmp_path / "dest")
    collect_failed.collect(bundle, label, dest)

    body = _why_failed_text(dest, iid)
    assert "last trajectory step source: user" in body
    assert "TRANSIENT silent session death" in body

    idx = _index_text(dest)
    assert "TRANSIENT silent session death" in idx


def test_harness_report_carries_caveats(tmp_path):
    bundle = str(tmp_path / "bundle")
    label = "tb-run"
    iid = "chess-best-move"
    _write_report(bundle, iid, _COMPLETED_ENTRY)
    os.makedirs(_art_dir(bundle, label, iid), exist_ok=True)

    dest = str(tmp_path / "dest")
    collect_failed.collect(bundle, label, dest)

    body = _why_failed_text(dest, iid)
    assert "err.txt is Harbor's SETUP log" in body
    assert "FALSE POSITIVES" in body
    assert "total_cost_usd" in body
    assert "LiteLLM spend" in body


def test_swe_path_unchanged(tmp_path):
    """The pre-existing resolve_then_grade (SWE-bench) output must be
    byte-for-byte identical to before this change — no "flow:"/"harbor"
    text, same FAIL_TO_PASS/PASS_TO_PASS layout."""
    bundle = str(tmp_path / "bundle")
    label = "swe-run"
    iid = "django__django-11790"
    _write_report(bundle, iid, _SWE_ENTRY)
    os.makedirs(_art_dir(bundle, label, iid), exist_ok=True)

    dest = str(tmp_path / "dest")
    n = collect_failed.collect(bundle, label, dest)
    assert n == 1

    body = _why_failed_text(dest, iid)
    expected = (
        "instance: %s\n"
        "patch_exists=True  patch_applied=True  resolved=False\n"
        "\nFAIL_TO_PASS: 1 passed, 1 still failing\n"
        "  FAIL  test_b\n"
        "\nPASS_TO_PASS: 1 passed, 0 regressed\n"
    ) % iid
    assert body == expected
    assert "flow: harness_run" not in body
    assert "harbor_reward" not in body

    idx = _index_text(dest)
    assert "patch applied, 1/2 FAIL_TO_PASS still failing" in idx


def test_flow_detection_is_structural_not_label_based(tmp_path):
    """The flow branch must key off the report.json shape (presence of
    "harbor_result", the field only harness_terminal.py's run() ever writes)
    — never off the run label or a benchmark name string. Use a label that
    reads as SWE-ish ("verified-smoke") for a harness_run entry and confirm
    it still gets the harness report, proving there's no string-matching on
    the label anywhere in the branch."""
    bundle = str(tmp_path / "bundle")
    label = "verified-smoke"  # deliberately SWE-shaped label
    iid = "chess-best-move"
    _write_report(bundle, iid, _COMPLETED_ENTRY)
    art = _art_dir(bundle, label, iid)
    os.makedirs(art, exist_ok=True)
    _write_json(os.path.join(art, "trajectory.json"), _AGENT_ENDING_TRAJECTORY)

    dest = str(tmp_path / "dest")
    collect_failed.collect(bundle, label, dest)

    body = _why_failed_text(dest, iid)
    assert "flow: harness_run" in body
    assert "FAIL_TO_PASS" not in body
