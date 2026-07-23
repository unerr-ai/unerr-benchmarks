#!/usr/bin/env python3
"""Coordinator HTTP server for the distributed SWE-bench runner (Slice B).

A single-file, stdlib-only work queue: a ThreadingHTTPServer fronting a SQLite
`tasks` table. Workers claim one instance at a time via an atomic
`UPDATE ... RETURNING`, heartbeat while resolving, and POST results back on
completion; a background reaper requeues leases whose heartbeat went stale so a
crashed worker's instance is retried by a survivor.

Failure-rerun (sibling of the stale-lease reaper): once the fresh `pending`
queue is drained, Queue.claim gives a rerun-eligible instance up to
MAX_FAILURE_RERUN more tries — either a `failed` row (MAX_ATTEMPTS exhaustion
via /fail) or a `done`+`resolved=0` row that harness_terminal.py flagged as a
TRANSIENT death: a silent session death (an unanswered Claude Code nudge — see
Queue._is_silent_death_meta) or a no-gradeable-verdict harbor death (no
result.json — an idle-watchdog/timeout kill or crash before grading, which
still reports via /complete not /fail — see Queue._is_no_result_death_meta).
Neither is a genuine capability miss. A rerun's eventual /complete overwrites
the earlier outcome in place. MAX_FAILURE_RERUN=0 disables both paths
(current/original behaviour: exhausted attempts dead-letter straight to `dead`;
a transient death is never retried).

Stdlib only (http.server + sqlite3 + json + threading) so the coordinator image
stays minimal and dependency-free. PLAN.md mentions aiohttp aspirationally;
ThreadingHTTPServer + a single write lock is simpler and needs no install.

Config via env: PORT (8080), DB_PATH (/data/queue.db), LEASE_TTL (2700s),
MAX_ATTEMPTS (2), HEARTBEAT_TIMEOUT (120s), REAPER_INTERVAL (30s),
MAX_FAILURE_RERUN (1).
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


# --------------------------------------------------------------------------- #
# Config + logging
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Config:
    """Runtime configuration resolved from the environment on startup."""

    port: int
    db_path: str
    lease_ttl: int
    max_attempts: int
    heartbeat_timeout: int
    reaper_interval: int
    max_failure_rerun: int
    armed: bool

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            port=int(os.environ.get("PORT", "8080")),
            db_path=os.environ.get("DB_PATH", "/data/queue.db"),
            lease_ttl=int(os.environ.get("LEASE_TTL", "2700")),
            max_attempts=int(os.environ.get("MAX_ATTEMPTS", "2")),
            heartbeat_timeout=int(os.environ.get("HEARTBEAT_TIMEOUT", "120")),
            reaper_interval=int(os.environ.get("REAPER_INTERVAL", "30")),
            # Failure-rerun (parameterable, sibling of the stale-lease reaper):
            # once the fresh queue is drained, retry a 'failed' (attempts-
            # exhausted) instance up to this many more times. 0 = disabled,
            # matching the original dead-letter-on-exhaustion behaviour.
            max_failure_rerun=int(os.environ.get("MAX_FAILURE_RERUN", "1")),
            # Prepare/run gate: default 1 → coordinator is armed from boot, so the
            # all-in-one path is unchanged. `prepare` boots workers with COORD_ARMED=0
            # so they warm and hold until an external POST /arm releases the fleet.
            armed=(os.environ.get("COORD_ARMED", "1").strip().lower()
                   in ("1", "true", "yes")),
        )


def log(event: str, **fields: Any) -> None:
    """Emit one line of structured JSON to stderr for a state transition.

    The coordinator entrypoint (Slice D) tails these to observe the run.
    """
    record = {"ts": int(time.time()), "event": event, **fields}
    print(json.dumps(record, default=str), file=sys.stderr, flush=True)


def _as_int_bool(value: Any) -> Optional[int]:
    """Coerce a JSON truthy/falsey `resolved` value to 1/0 (or None)."""
    if value is None:
        return None
    if isinstance(value, str):
        return 1 if value.strip().lower() in ("1", "true", "yes") else 0
    return 1 if value else 0


def _as_json_text(value: Any) -> Optional[str]:
    """Store report_json/meta_json as TEXT: pass strings through, dump objects."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


# --------------------------------------------------------------------------- #
# Queue — all SQLite access, single-writer under one lock
# --------------------------------------------------------------------------- #
class Queue:
    """SQLite-backed task queue guarded by a single write lock.

    Every method acquires `_lock` for its whole body; helpers prefixed `_` and
    the SQL they run assume the lock is already held. SQLite is opened with
    `check_same_thread=False` + WAL, so the ThreadingHTTPServer worker threads
    share one connection but never write concurrently.
    """

    def __init__(self, conn: sqlite3.Connection, cfg: Config) -> None:
        self._conn = conn
        self._cfg = cfg
        self._lock = threading.Lock()
        # Worker ids that have polled /claim at least once — the readiness signal
        # `prepare` waits on (a warm worker polls before any work is armed).
        self._workers_seen: set[str] = set()
        # Armed gate (prepare/run split): while False, /claim returns {wait:true}
        # so warm workers hold (toolbox built, polling, NOT exiting) and /drain
        # never reports drained. Persisted in a `control` row so a coordinator
        # restart keeps the state; COORD_ARMED (default 1, via cfg.armed) is the
        # value the FIRST boot writes — the all-in-one path is armed from boot and
        # behaves exactly as before.
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT)"
        )
        row = self._conn.execute(
            "SELECT value FROM control WHERE key='armed'"
        ).fetchone()
        if row is None:
            self._armed = cfg.armed
            self._conn.execute(
                "INSERT INTO control (key, value) VALUES ('armed', ?)",
                ("1" if self._armed else "0",),
            )
            self._conn.commit()
        else:
            self._armed = row[0] == "1"

    # -- reads (lock held by caller) --------------------------------------- #
    def _counts(self) -> dict[str, int]:
        counts = {"pending": 0, "leased": 0, "done": 0, "dead": 0, "failed": 0}
        for status, n in self._conn.execute(
            "SELECT status, COUNT(*) FROM tasks GROUP BY status"
        ).fetchall():
            counts[status] = n
        return counts

    # -- endpoints --------------------------------------------------------- #
    def seed(self, run_id: str, instance_ids: list[str]) -> dict[str, int]:
        """Insert every id as `pending`, ignoring ids already present. Idempotent."""
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO tasks (instance_id, run_id, status) "
                "VALUES (?, ?, 'pending')",
                [(iid, run_id) for iid in instance_ids],
            )
            self._conn.commit()
            seeded = self._conn.total_changes - before
            total = self._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        log("seed", run_id=run_id, seeded=seeded, total=total)
        return {"seeded": seeded, "total": total}

    def claim(self, worker_id: str) -> dict[str, Any]:
        """Atomically lease one claimable row (pending or lease-expired).

        Returns `{instance_id}` on success. When the fresh queue has nothing
        claimable, and MAX_FAILURE_RERUN>0, it makes ONE attempt to pull a
        rerun-eligible instance back for another try — the failure-rerun
        mechanism, a sibling of the stale-lease reap below but keyed on
        `fail_reruns` instead of heartbeat staleness, and deliberately gated
        on `pending==0` so fresh work always drains first (end-of-run only).
        Eligible rows come in two shapes (see `_eligible_rerun_ids`): a
        `failed` row (attempts exhausted — an execution failure) or a `done`
        row with `resolved=0` that harness_terminal.py flagged as a transient
        death — a silent session death (unanswered Claude Code nudge — see
        `_is_silent_death_meta`) or a no-gradeable-verdict harbor death (see
        `_is_no_result_death_meta`), neither a genuine capability miss. Failing
        that too, it
        distinguishes `{done:true}` (queue truly drained — no pending, no
        leased, no row with rerun budget left) from `{wait:true}` (leased
        rows still in flight, or armed-but-idle, so the worker should poll
        again shortly).
        """
        now = int(time.time())
        lease_until = now + self._cfg.lease_ttl
        lease_sql = (
            "UPDATE tasks SET status='leased', worker_id=?, "
            "  attempt_count=attempt_count+1, lease_until=?, last_heartbeat=? "
            "WHERE instance_id = ("
            "  SELECT instance_id FROM tasks "
            "  WHERE status='pending' OR (status='leased' AND lease_until < ?) "
            "  ORDER BY attempt_count, instance_id LIMIT 1) "
            "RETURNING instance_id"
        )
        with self._lock:
            self._workers_seen.add(worker_id)
            if not self._armed:
                # Warm but not released yet: keep the worker in its poll loop
                # (wait, not done) so it stays alive until the run is armed.
                return {"wait": True}
            row = self._conn.execute(lease_sql, (worker_id, lease_until, now, now)).fetchone()
            self._conn.commit()
            if row is None:
                counts = self._counts()
                if counts["pending"] == 0 and self._cfg.max_failure_rerun > 0:
                    candidates = self._eligible_rerun_ids()
                    frow = None
                    cause = None
                    if candidates:
                        pick, cause = candidates[0]
                        frow = self._conn.execute(
                            "UPDATE tasks SET status='pending', fail_reruns=fail_reruns+1 "
                            "WHERE instance_id=? RETURNING instance_id",
                            (pick,),
                        ).fetchone()
                        self._conn.commit()
                    if frow is not None:
                        log("failure_rerun", worker_id=worker_id, instance_id=frow[0], cause=cause)
                        # Fresh 'pending' row exists now (and only this one, since
                        # counts["pending"] was 0 a moment ago under the same
                        # lock) — the primary lease query picks it up next.
                        row = self._conn.execute(
                            lease_sql, (worker_id, lease_until, now, now)
                        ).fetchone()
                        self._conn.commit()
            if row is not None:
                instance_id = row[0]
                log("claim", worker_id=worker_id, instance_id=instance_id)
                return {"instance_id": instance_id}
            counts = self._counts()
        if counts["pending"] == 0 and counts["leased"] == 0:
            return {"done": True}
        return {"wait": True}

    def heartbeat(self, instance_id: str, worker_id: str) -> dict[str, bool]:
        """Extend the lease + last_heartbeat while the worker still owns the row.

        Returns `{ok:true}`, or `{stale:true}` if the row was already reaped or
        reassigned (the worker no longer owns it and should abandon).
        """
        now = int(time.time())
        lease_until = now + self._cfg.lease_ttl
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET lease_until=?, last_heartbeat=? "
                "WHERE instance_id=? AND worker_id=? AND status='leased'",
                (lease_until, now, instance_id, worker_id),
            )
            self._conn.commit()
            ok = cur.rowcount > 0
        return {"ok": True} if ok else {"stale": True}

    def complete(
        self,
        instance_id: str,
        worker_id: str,
        patch: Optional[str],
        report_json: Any,
        meta_json: Any,
        resolved: Any,
        events_jsonl: Optional[str] = None,
        err_txt: Optional[str] = None,
        db_b64: Optional[str] = None,
        engine_log: Optional[str] = None,
        trajectory_json: Optional[str] = None,
        sessions_cast: Optional[str] = None,
        harbor_run_log: Optional[str] = None,
        claude_session_jsonl: Optional[str] = None,
        claude_sessions_tgz_b64: Optional[str] = None,
    ) -> dict[str, bool]:
        """Idempotent completion upsert: mark `done` and store the results.

        Accepted even if the lease already expired — at-least-once delivery plus
        an idempotent overwrite makes a double-run effectively-once. The UPDATE
        is unconditional on prior status, so a failure-rerun's success overwrites
        an earlier `failed` row's patch/report_json/meta_json/resolved/artifacts
        in place — the bundle reflects the rerun's outcome, not the original fail.

        events_jsonl/err_txt/db_b64 (S7b) and engine_log (S7c: the full,
        tail-capped econ engine log — orchestration markers) are the instance's
        raw transcript — optional, since the worker bounds/omits them (see
        worker-loop.py _read_artifacts) — stored verbatim so
        coordinator-entrypoint.sh can write them back out under
        results/<label>/artifacts/<iid>/ at drain. trajectory_json/sessions_cast
        are the harness_run (Terminal) equivalents (Harbor agent trajectory +
        asciinema session); harbor_run_log is Harbor's own captured stdout+stderr
        (the only place a SETUP-phase RuntimeError, raised before the agent ever
        runs, is captured); claude_session_jsonl is Claude Code's OWN
        incrementally-written session transcript (claude-* arms only,
        synced by cc-harness-hooks.py's _sync_claude_session) — unlike
        trajectory_json, it is written AS THE AGENT RUNS, so it survives a
        trial killed mid-run (no trajectory.json, no err.txt); claude_sessions_tgz_b64
        is EVERY Claude Code session .jsonl for the trial (main + every Task
        sub-agent sidechain, cc-harness-hooks.py's additive
        _sync_all_claude_sessions), gzip-tarred and base64-encoded by
        harness_terminal.py's _collect_traces — claude_session_jsonl only ever
        holds the main session, so this is the only trace of a sub-agent
        (escalation: unerr-opus/unerr-fable) misbehaving — benchmarks
        the worker never sends any of these leave the columns NULL.
        """
        now = int(time.time())
        resolved_int = _as_int_bool(resolved)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET status='done', patch=?, report_json=?, "
                "  meta_json=?, resolved=?, events_jsonl=?, err_txt=?, db_b64=?, "
                "  engine_log=?, trajectory_json=?, sessions_cast=?, "
                "  harbor_run_log=?, claude_session_jsonl=?, claude_sessions_tgz_b64=?, "
                "  completed_by=?, completed_at=? "
                "WHERE instance_id=?",
                (
                    patch,
                    _as_json_text(report_json),
                    _as_json_text(meta_json),
                    resolved_int,
                    events_jsonl,
                    err_txt,
                    db_b64,
                    engine_log,
                    trajectory_json,
                    sessions_cast,
                    harbor_run_log,
                    claude_session_jsonl,
                    claude_sessions_tgz_b64,
                    worker_id,
                    now,
                    instance_id,
                ),
            )
            self._conn.commit()
            found = cur.rowcount > 0
        log(
            "complete",
            worker_id=worker_id,
            instance_id=instance_id,
            resolved=resolved_int,
            found=found,
        )
        return {"ok": True}

    def fail(self, instance_id: str, worker_id: str, error: Any) -> dict[str, str]:
        """Requeue a failed instance, or park/dead-letter it once attempts are spent.

        attempt_count was already incremented at claim, so it is only read here:
        `>= MAX_ATTEMPTS` → `failed` (if MAX_FAILURE_RERUN>0 — claim() may still
        rerun it at end-of-run, spending one unit of `fail_reruns` each time) or
        `dead` (MAX_FAILURE_RERUN=0, the original behaviour), otherwise back to
        `pending`. The error is also persisted into `failure_reason` (capped) so
        a dead/failed instance's cause is durably queryable — not just in
        server.log, which requires SSH+grep and doesn't survive the coordinator
        machine.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT attempt_count FROM tasks WHERE instance_id=?", (instance_id,)
            ).fetchone()
            if row is None:
                status = "pending"
            else:
                if row[0] >= self._cfg.max_attempts:
                    status = "failed" if self._cfg.max_failure_rerun > 0 else "dead"
                else:
                    status = "pending"
                reason = str(error)[:8000] if error is not None else None
                self._conn.execute(
                    "UPDATE tasks SET status=?, worker_id=NULL, lease_until=NULL, "
                    "failure_reason=? WHERE instance_id=?",
                    (status, reason, instance_id),
                )
                self._conn.commit()
        log(
            "fail",
            worker_id=worker_id,
            instance_id=instance_id,
            status=status,
            error=str(error)[:4000] if error is not None else None,
        )
        return {"status": status}

    def status(self) -> dict[str, Any]:
        """Full snapshot: counts by status, resolved tally, per-instance rows,
        and `pending_reruns` (rows still eligible for a failure-rerun — see
        `_eligible_rerun_ids` — so `status.sh`'s `up4retry` can reflect a
        silent-death-eligible `done` row, not just `counts.failed`)."""
        with self._lock:
            armed = self._armed
            workers_seen = sorted(self._workers_seen)
            counts = self._counts()
            pending_reruns = self._pending_reruns()
            total = self._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            resolved = self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE resolved=1"
            ).fetchone()[0]
            run_row = self._conn.execute("SELECT run_id FROM tasks LIMIT 1").fetchone()
            rows = self._conn.execute(
                "SELECT instance_id, status, attempt_count, resolved, worker_id "
                "FROM tasks ORDER BY instance_id"
            ).fetchall()
        instances = [
            {
                "instance_id": r[0],
                "status": r[1],
                "attempt_count": r[2],
                "resolved": r[3],
                "worker_id": r[4],
            }
            for r in rows
        ]
        return {
            "run_id": run_row[0] if run_row else None,
            "armed": armed,
            "workers_seen": workers_seen,
            "counts": counts,
            "pending_reruns": pending_reruns,
            "resolved": resolved,
            "total": total,
            "instances": instances,
        }

    def _is_silent_death_meta(self, meta_json: Optional[str]) -> bool:
        """True only when a `done` row's `meta_json` EXPLICITLY carries
        `"silent_death": true` — harness_terminal.py's run() sets this after
        the trial, when the trajectory exists and its LAST step's `source`
        is `"user"` (an unanswered Claude Code "no visible output" nudge)
        rather than `"agent"` (see harness_terminal.py's `_is_silent_death`
        and DEBUG_FAILED_TASK.md Step 3). Any parse failure, missing field,
        or falsey value reads False, never True — a `done`+`resolved=0` row
        WITHOUT this flag is the dominant case (the agent ran to completion
        and was simply graded wrong) and must never be mistaken for a
        transient death and rerun at real cost (e.g. TB2.1's
        chess-best-move, which fails reproducibly with a different wrong
        move every run — rerunning it is pure waste).
        """
        if not meta_json:
            return False
        try:
            meta = json.loads(meta_json)
        except (ValueError, TypeError):
            return False
        return isinstance(meta, dict) and meta.get("silent_death") is True

    def _is_no_result_death_meta(self, meta_json: Optional[str]) -> bool:
        """True only when a `done` row's `meta_json` EXPLICITLY carries
        `"no_result_death": true` — harness_terminal.py's run() sets this when
        the harbor trial produced NO gradeable result.json at all (an
        idle-watchdog / timeout kill or a crash before grading), a TRANSIENT
        infra death distinct from `silent_death` (which is the agent's OWN
        unanswered "no visible output" nudge, rc=0 WITH a result.json). Such a
        run still reports via /complete (harness_terminal always returns a
        non-empty report_text, so worker-loop.py never routes it to /fail) and
        lands as `done`+`resolved=0` — so without this flag it looks identical
        to a genuine capability miss and never gets a rerun. A clean run — even
        one graded WRONG — ALWAYS writes result.json, so this never fires on a
        real miss (chess-best-move stays excluded). Same conservative contract
        as `_is_silent_death_meta`: any parse failure / missing field / falsey
        value reads False, never True.
        """
        if not meta_json:
            return False
        try:
            meta = json.loads(meta_json)
        except (ValueError, TypeError):
            return False
        return isinstance(meta, dict) and meta.get("no_result_death") is True

    def _eligible_rerun_ids(self) -> list[tuple[str, str]]:
        """`(instance_id, cause)` pairs currently eligible for a
        failure-rerun, in claim order (lowest instance_id first) — the
        shared selection query for both `claim()`'s end-of-run pull and
        `_pending_reruns()`'s status/drain count. `cause` is `"failed"`
        (execution failure, MAX_ATTEMPTS exhausted via /fail), `"silent_death"`
        (finished `done` with `resolved=0` but flagged a silent session death
        in `meta_json` — see `_is_silent_death_meta`), or `"no_result"`
        (finished `done` with `resolved=0` but flagged a no-gradeable-verdict
        harbor death — see `_is_no_result_death_meta`). All bounded by
        `fail_reruns < MAX_FAILURE_RERUN`. A `done`+`resolved=0` row WITHOUT
        either transient-death flag (a genuine capability miss) is deliberately
        excluded — see `_is_silent_death_meta` / `_is_no_result_death_meta`.
        The `cause` is a log label only; claim() requeues every cause
        identically (status='pending', fail_reruns+1).
        """
        rows = self._conn.execute(
            "SELECT instance_id, status, meta_json FROM tasks "
            "WHERE fail_reruns < ? AND (status='failed' OR (status='done' AND resolved=0)) "
            "ORDER BY instance_id",
            (self._cfg.max_failure_rerun,),
        ).fetchall()
        out: list[tuple[str, str]] = []
        for iid, status, meta_json in rows:
            if status == "failed":
                out.append((iid, "failed"))
            elif self._is_silent_death_meta(meta_json):
                out.append((iid, "silent_death"))
            elif self._is_no_result_death_meta(meta_json):
                out.append((iid, "no_result"))
        return out

    def _pending_reruns(self) -> int:
        """Count of rows still eligible for a failure-rerun (see
        `_eligible_rerun_ids`) under the MAX_FAILURE_RERUN budget — `failed`
        rows AND `done`+`resolved=0` rows flagged as a silent session death or
        a no-gradeable-verdict harbor death.

        Read-only helper shared by `drain()`, `status()`, and `claim()`'s
        done-check — an eligible row is NOT terminal yet (claim() will still
        requeue it), so callers must not treat it as done. 0 whenever
        failure-rerun is disabled (MAX_FAILURE_RERUN<=0), matching the
        original behaviour where `failed` never exists as a status.
        """
        if self._cfg.max_failure_rerun <= 0:
            return 0
        return len(self._eligible_rerun_ids())

    def drain(self) -> dict[str, bool]:
        """`{drained:true}` once ARMED, no pending/leased rows, and no row
        still eligible for a failure-rerun (see `_eligible_rerun_ids` —
        `failed` OR a `done`+`resolved=0` row flagged as a transient death:
        silent-death or no-gradeable-verdict).

        The `armed` guard means an un-released (prepare-phase) queue — seeded but
        not yet armed, or momentarily empty — never reads as drained, so the
        coordinator won't aggregate/bundle before the run is released. The
        failure-rerun guard means the entrypoint's drain-wait won't bundle out
        from under an instance that claim() would still retry."""
        with self._lock:
            armed = self._armed
            counts = self._counts()
            pending_reruns = self._pending_reruns()
        return {
            "drained": armed and counts["pending"] == 0 and counts["leased"] == 0
            and pending_reruns == 0
        }

    def arm(self) -> dict[str, Any]:
        """Release the fleet: flip `armed` on (persisted). Idempotent — after this
        /claim hands out work and /drain can report drained. The prepare→run split
        calls this once workers are warm and the GPU is up."""
        with self._lock:
            self._armed = True
            self._conn.execute(
                "INSERT INTO control (key, value) VALUES ('armed', '1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
            )
            self._conn.commit()
        log("arm")
        return {"armed": True}

    def reap(self) -> int:
        """Requeue leases whose heartbeat is stale; dead-letter spent ones.

        Heartbeat-based (catches alive-but-hung faster than lease TTL); does NOT
        touch attempt_count (already bumped at claim) but honours MAX_ATTEMPTS.
        Returns the number of rows reaped.
        """
        now = int(time.time())
        cutoff = now - self._cfg.heartbeat_timeout
        reaped: list[tuple[str, Optional[str], int, str]] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT instance_id, attempt_count, worker_id FROM tasks "
                "WHERE status='leased' AND (last_heartbeat IS NULL OR last_heartbeat < ?)",
                (cutoff,),
            ).fetchall()
            for instance_id, attempt, worker_id in rows:
                new_status = "dead" if attempt >= self._cfg.max_attempts else "pending"
                self._conn.execute(
                    "UPDATE tasks SET status=?, worker_id=NULL, lease_until=NULL "
                    "WHERE instance_id=?",
                    (new_status, instance_id),
                )
                reaped.append((instance_id, worker_id, attempt, new_status))
            if rows:
                self._conn.commit()
        for instance_id, worker_id, attempt, new_status in reaped:
            log(
                "reap",
                instance_id=instance_id,
                worker_id=worker_id,
                attempt_count=attempt,
                status=new_status,
            )
        return len(reaped)


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    """Routes JSON requests to the shared `Queue`; one instance per request."""

    protocol_version = "HTTP/1.1"
    queue: Optional[Queue] = None  # injected on the class in main()

    # silence the noisy default access log; we emit our own structured logs
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    @property
    def _q(self) -> Queue:
        assert self.queue is not None, "Queue not initialized"
        return self.queue

    # -- POST endpoints ---------------------------------------------------- #
    def _post_seed(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._q.seed(body["run_id"], body["instance_ids"])

    def _post_arm(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._q.arm()

    def _post_claim(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._q.claim(body["worker_id"])

    def _post_heartbeat(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._q.heartbeat(body["instance_id"], body["worker_id"])

    def _post_complete(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._q.complete(
            body["instance_id"],
            body["worker_id"],
            body.get("patch"),
            body.get("report_json"),
            body.get("meta_json"),
            body.get("resolved"),
            body.get("events_jsonl"),
            body.get("err_txt"),
            body.get("db_b64"),
            body.get("engine_log"),
            body.get("trajectory_json"),
            body.get("sessions_cast"),
            body.get("harbor_run_log"),
            body.get("claude_session_jsonl"),
            body.get("claude_sessions_tgz_b64"),
        )

    def _post_fail(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._q.fail(body["instance_id"], body["worker_id"], body.get("error"))

    def do_POST(self) -> None:  # noqa: N802
        routes: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "/seed": self._post_seed,
            "/arm": self._post_arm,
            "/claim": self._post_claim,
            "/heartbeat": self._post_heartbeat,
            "/complete": self._post_complete,
            "/fail": self._post_fail,
        }
        route = routes.get(self.path)
        if route is None:
            self._send(404, {"error": "not found"})
            return
        try:
            body = self._read_json()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": f"invalid json: {exc}"})
            return
        try:
            self._send(200, route(body))
        except KeyError as exc:
            self._send(400, {"error": f"missing field: {exc}"})
        except Exception as exc:  # pragma: no cover - defensive
            log("error", path=self.path, error=str(exc))
            self._send(500, {"error": str(exc)})

    # -- GET endpoints ----------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path == "/health":
                self._send(200, {"ok": True})
            elif self.path == "/status":
                self._send(200, self._q.status())
            elif self.path == "/drain":
                self._send(200, self._q.drain())
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:  # pragma: no cover - defensive
            log("error", path=self.path, error=str(exc))
            self._send(500, {"error": str(exc)})


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #
def open_db(cfg: Config) -> sqlite3.Connection:
    """Open (creating if absent) the queue DB and apply schema.sql idempotently."""
    if cfg.db_path != ":memory:":
        parent = os.path.dirname(cfg.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    with open(SCHEMA_FILE, encoding="utf-8") as fh:
        conn.executescript(fh.read())
    # Guarded migration: CREATE TABLE IF NOT EXISTS above only shapes a brand-new
    # DB. A coordinator restarting on an EXISTING db.sqlite (pre-failure-rerun)
    # is missing `fail_reruns` — ALTER TABLE has no IF NOT EXISTS for columns, so
    # check pragma table_info first (idempotent, safe to run every boot).
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "fail_reruns" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN fail_reruns INTEGER NOT NULL DEFAULT 0")
    # Same guarded migration for the harness_run (Terminal) trace columns — a
    # coordinator restarting on a pre-Terminal db.sqlite is missing these.
    if "trajectory_json" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN trajectory_json TEXT")
    if "sessions_cast" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN sessions_cast TEXT")
    if "harbor_run_log" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN harbor_run_log TEXT")
    # Claude Code's own session-transcript sync (claude-* terminal arms only,
    # cc-harness-hooks.py's _sync_claude_session) — same guarded pattern.
    if "claude_session_jsonl" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN claude_session_jsonl TEXT")
    # Every Claude Code session .jsonl for the trial (main + Task sub-agent
    # sidechains), gzip-tarred + base64-encoded — claude-* terminal arms only,
    # cc-harness-hooks.py's additive _sync_all_claude_sessions — same guarded
    # pattern as claude_session_jsonl above.
    if "claude_sessions_tgz_b64" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN claude_sessions_tgz_b64 TEXT")
    conn.commit()
    return conn


def _reaper_loop(queue: Queue, cfg: Config, stop: threading.Event) -> None:
    """Sweep stale leases every REAPER_INTERVAL until the server stops."""
    while not stop.wait(cfg.reaper_interval):
        try:
            n = queue.reap()
            if n:
                log("reap_sweep", reaped=n)
        except Exception as exc:  # pragma: no cover - defensive
            log("reaper_error", error=str(exc))


def main() -> None:
    cfg = Config.from_env()
    conn = open_db(cfg)
    queue = Queue(conn, cfg)
    Handler.queue = queue

    stop = threading.Event()
    reaper = threading.Thread(
        target=_reaper_loop, args=(queue, cfg, stop), name="reaper", daemon=True
    )
    reaper.start()

    # Fly's 6PN private network (.internal) is IPv6-only, so workers reach the
    # coordinator at an IPv6 address — an IPv4 ("0.0.0.0") listener gives them
    # "[Errno 111] Connection refused" over 6PN (found in smoke, 2026-07-10).
    # Bind :: (IPv6) with IPV6_V6ONLY off so the one socket serves both 6PN
    # (IPv6, worker->coordinator) and localhost health-checks (IPv4-mapped).
    class _DualStackHTTPServer(ThreadingHTTPServer):
        address_family = socket.AF_INET6

        def server_bind(self):
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except (AttributeError, OSError):
                pass
            super().server_bind()

    server = _DualStackHTTPServer(("::", cfg.port), Handler)
    log(
        "startup",
        port=cfg.port,
        db_path=cfg.db_path,
        lease_ttl=cfg.lease_ttl,
        max_attempts=cfg.max_attempts,
        heartbeat_timeout=cfg.heartbeat_timeout,
        reaper_interval=cfg.reaper_interval,
        max_failure_rerun=cfg.max_failure_rerun,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        stop.set()
        server.shutdown()
        conn.close()


if __name__ == "__main__":
    main()
