"""Focused tests for harness_terminal.py's trial-dir resolution.

Harbor 0.20.0 writes result.json at TWO depths under jobs_dir: a job-level
one (<jobs_dir>/<task>/result.json, always) and a trial-level one
(<jobs_dir>/<task>/<task>__<hash>/result.json, ONLY once the trial
COMPLETES). `_find_trial_dir` must anchor on trial.log (written
incrementally from trial start, so it survives a killed trial) rather than
on dirname(result_path) (which silently collapses to the job dir when the
trial never completed) — proven live 2026-07-21 on instance
`caffe-cifar-10`: the trial dir existed with a 766KB
agent/sessions/claude-session.jsonl that was never copied because trial_dir
had resolved to the job dir.

No docker, no network — pure filesystem fixtures under tmp_path.

Run: python3 -m pytest e2e/distributed/tools/test_harness_terminal.py -q
"""
import base64
import io
import os
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_terminal as ht  # noqa: E402  (module lives alongside this test)


def _write(path: str, content: str = "x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def test_killed_trial_resolves_trial_dir_via_trial_log(tmp_path):
    """Job-level result.json present, trial-level result.json ABSENT (the
    trial was killed before it could write one) — trial.log + a nested
    claude-session.jsonl exist under the trial dir. _find_trial_dir must
    still resolve to the trial dir (not the job dir), and _collect_traces
    must pick up both err.txt and claude-session.jsonl from it."""
    jobs_dir = str(tmp_path / "jobs")
    task_dir = os.path.join(jobs_dir, "caffe-cifar-10")
    trial_dir = os.path.join(task_dir, "caffe-cifar-10__9gZheEM")

    _write(os.path.join(task_dir, "result.json"), "{}")  # job-level only
    _write(os.path.join(trial_dir, "trial.log"), "trial started\n")
    _write(os.path.join(trial_dir, "agent", "sessions", "claude-session.jsonl"),
           '{"type": "user"}\n')

    result_path = ht._find_result_json(jobs_dir)
    assert result_path == os.path.join(task_dir, "result.json")

    resolved_trial_dir = ht._find_trial_dir(jobs_dir, result_path)
    assert resolved_trial_dir == trial_dir, (
        f"expected the nested trial dir, got {resolved_trial_dir!r} "
        f"(dirname(result_path) would have wrongly given {os.path.dirname(result_path)!r})"
    )

    art_dir = str(tmp_path / "artifacts")
    ht._collect_traces(resolved_trial_dir, art_dir, resolved=False)

    assert os.path.isfile(os.path.join(art_dir, "err.txt")), \
        "err.txt (from trial.log) missing — trial_dir resolution regressed"
    assert os.path.isfile(os.path.join(art_dir, "claude-session.jsonl")), \
        "claude-session.jsonl missing — trial_dir resolution regressed"


def test_completed_trial_still_resolves_trial_dir(tmp_path):
    """Both job-level and trial-level result.json present (the ordinary,
    non-killed case) — trial.log + claude-session.jsonl live under the
    trial dir alongside its own result.json. _find_trial_dir must still
    land on the trial dir, matching pre-fix behaviour (dirname of the
    deepest result.json) exactly."""
    jobs_dir = str(tmp_path / "jobs")
    task_dir = os.path.join(jobs_dir, "regex-log")
    trial_dir = os.path.join(task_dir, "regex-log__abc123")

    _write(os.path.join(task_dir, "result.json"), "{}")            # job-level
    _write(os.path.join(trial_dir, "result.json"), '{"verifier_result": {}}')  # trial-level
    _write(os.path.join(trial_dir, "trial.log"), "trial finished\n")
    _write(os.path.join(trial_dir, "agent", "sessions", "claude-session.jsonl"),
           '{"type": "user"}\n')

    result_path = ht._find_result_json(jobs_dir)
    assert result_path == os.path.join(trial_dir, "result.json")

    resolved_trial_dir = ht._find_trial_dir(jobs_dir, result_path)
    assert resolved_trial_dir == trial_dir

    art_dir = str(tmp_path / "artifacts")
    ht._collect_traces(resolved_trial_dir, art_dir, resolved=True)

    assert os.path.isfile(os.path.join(art_dir, "err.txt"))
    assert os.path.isfile(os.path.join(art_dir, "claude-session.jsonl"))


# ── claude-sessions.tgz.b64 (every session, incl. Task sub-agents) ──────────


def test_collect_traces_packages_every_session_into_tgz(tmp_path):
    """cc-harness-hooks.py's additive _sync_all_claude_sessions writes one
    .jsonl per candidate (main + every Task sub-agent sidechain) into
    agent/sessions/ alongside the single claude-session.jsonl. _collect_traces
    must tar+gzip the WHOLE dir and base64-encode it as claude-sessions.tgz.b64
    (text, not raw bytes, so it rides worker-loop.py's descriptor-driven text
    pipeline unmodified) — every session file present on disk must be a
    member of the resulting archive."""
    trial_dir = str(tmp_path / "trial")
    sessions_dir = os.path.join(trial_dir, "agent", "sessions")
    _write(os.path.join(trial_dir, "trial.log"), "trial started\n")
    sessions = {
        "main-uuid.jsonl": '{"type":"user","sessionId":"main-uuid"}\n',
        "sub-uuid-1.jsonl": '{"type":"assistant","isSidechain":true,"sessionId":"sub-uuid-1"}\n',
        "sub-uuid-2.jsonl": '{"type":"assistant","isSidechain":true,"sessionId":"sub-uuid-2"}\n',
    }
    for name, content in sessions.items():
        _write(os.path.join(sessions_dir, name), content)
    # the single main-session copy also lives in the same dir, unrelated to
    # this test but present in a real run — must not confuse the packaging.
    _write(os.path.join(sessions_dir, "claude-session.jsonl"), sessions["main-uuid.jsonl"])

    art_dir = str(tmp_path / "artifacts")
    ht._collect_traces(trial_dir, art_dir, resolved=True)

    tgz_b64_path = os.path.join(art_dir, "claude-sessions.tgz.b64")
    assert os.path.isfile(tgz_b64_path), "claude-sessions.tgz.b64 was not produced"
    with open(tgz_b64_path, "r", encoding="utf-8") as f:
        b64_text = f.read()
    assert b64_text.isascii(), "artifact must be pure-ASCII base64 text"
    raw = base64.b64decode(b64_text)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        members = {m.name for m in tar.getmembers() if m.isfile()}
    for name in sessions:
        assert f"sessions/{name}" in members, f"{name} missing from the tar"
    assert "sessions/claude-session.jsonl" in members


def test_collect_traces_sessions_dir_absent_is_non_fatal(tmp_path):
    """Non-Claude terminal agents never write agent/sessions/ at all —
    _collect_traces must not raise and must simply skip the tgz artifact,
    while every other trace still gets collected normally."""
    trial_dir = str(tmp_path / "trial")
    _write(os.path.join(trial_dir, "trial.log"), "trial started\n")

    art_dir = str(tmp_path / "artifacts")
    ht._collect_traces(trial_dir, art_dir, resolved=True)  # must not raise

    assert not os.path.isfile(os.path.join(art_dir, "claude-sessions.tgz.b64"))
    assert os.path.isfile(os.path.join(art_dir, "err.txt"))
