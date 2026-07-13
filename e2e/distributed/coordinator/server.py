#!/usr/bin/env python3
"""Coordinator HTTP server for the distributed SWE-bench runner (Slice B).

A single-file, stdlib-only work queue: a ThreadingHTTPServer fronting a SQLite
`tasks` table. Workers claim one instance at a time via an atomic
`UPDATE ... RETURNING`, heartbeat while resolving, and POST results back on
completion; a background reaper requeues leases whose heartbeat went stale so a
crashed worker's instance is retried by a survivor.

Stdlib only (http.server + sqlite3 + json + threading) so the coordinator image
stays minimal and dependency-free. PLAN.md mentions aiohttp aspirationally;
ThreadingHTTPServer + a single write lock is simpler and needs no install.

Config via env: PORT (8080), DB_PATH (/data/queue.db), LEASE_TTL (2700s),
MAX_ATTEMPTS (2), HEARTBEAT_TIMEOUT (120s), REAPER_INTERVAL (30s).
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

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            port=int(os.environ.get("PORT", "8080")),
            db_path=os.environ.get("DB_PATH", "/data/queue.db"),
            lease_ttl=int(os.environ.get("LEASE_TTL", "2700")),
            max_attempts=int(os.environ.get("MAX_ATTEMPTS", "2")),
            heartbeat_timeout=int(os.environ.get("HEARTBEAT_TIMEOUT", "120")),
            reaper_interval=int(os.environ.get("REAPER_INTERVAL", "30")),
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

    # -- reads (lock held by caller) --------------------------------------- #
    def _counts(self) -> dict[str, int]:
        counts = {"pending": 0, "leased": 0, "done": 0, "dead": 0}
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

        Returns `{instance_id}` on success. When nothing is claimable it
        distinguishes `{done:true}` (queue truly drained — no pending and no
        leased rows) from `{wait:true}` (leased rows still in flight, so the
        worker should poll again shortly).
        """
        now = int(time.time())
        lease_until = now + self._cfg.lease_ttl
        with self._lock:
            row = self._conn.execute(
                "UPDATE tasks SET status='leased', worker_id=?, "
                "  attempt_count=attempt_count+1, lease_until=?, last_heartbeat=? "
                "WHERE instance_id = ("
                "  SELECT instance_id FROM tasks "
                "  WHERE status='pending' OR (status='leased' AND lease_until < ?) "
                "  ORDER BY attempt_count, instance_id LIMIT 1) "
                "RETURNING instance_id",
                (worker_id, lease_until, now, now),
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
    ) -> dict[str, bool]:
        """Idempotent completion upsert: mark `done` and store the results.

        Accepted even if the lease already expired — at-least-once delivery plus
        an idempotent overwrite makes a double-run effectively-once.

        events_jsonl/err_txt/db_b64 (S7b) and engine_log (S7c: the full,
        tail-capped econ engine log — orchestration markers) are the instance's
        raw transcript — optional, since the worker bounds/omits them (see
        worker-loop.py _read_artifacts) — stored verbatim so
        coordinator-entrypoint.sh can write them back out under
        results/<label>/artifacts/<iid>/ at drain.
        """
        now = int(time.time())
        resolved_int = _as_int_bool(resolved)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET status='done', patch=?, report_json=?, "
                "  meta_json=?, resolved=?, events_jsonl=?, err_txt=?, db_b64=?, "
                "  engine_log=?, completed_by=?, completed_at=? "
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
        """Requeue a failed instance, or dead-letter it once attempts are spent.

        attempt_count was already incremented at claim, so it is only read here:
        `>= MAX_ATTEMPTS` → `dead`, otherwise back to `pending`. The error is also
        persisted into `failure_reason` (capped) so a dead instance's cause is
        durably queryable — not just in server.log, which requires SSH+grep and
        doesn't survive the coordinator machine.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT attempt_count FROM tasks WHERE instance_id=?", (instance_id,)
            ).fetchone()
            if row is None:
                status = "pending"
            else:
                status = "dead" if row[0] >= self._cfg.max_attempts else "pending"
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
        """Full snapshot: counts by status, resolved tally, and per-instance rows."""
        with self._lock:
            counts = self._counts()
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
            "counts": counts,
            "resolved": resolved,
            "total": total,
            "instances": instances,
        }

    def drain(self) -> dict[str, bool]:
        """`{drained:true}` once no pending and no leased rows remain."""
        with self._lock:
            counts = self._counts()
        return {"drained": counts["pending"] == 0 and counts["leased"] == 0}

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
        )

    def _post_fail(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._q.fail(body["instance_id"], body["worker_id"], body.get("error"))

    def do_POST(self) -> None:  # noqa: N802
        routes: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "/seed": self._post_seed,
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
