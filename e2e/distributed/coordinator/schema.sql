-- Coordinator queue schema for the distributed SWE-bench runner (Slice B).
-- One row per instance; the whole queue is claimed with a single atomic
-- UPDATE ... RETURNING (see server.py claim()). SQLite in WAL mode, single
-- writer guarded by a threading.Lock in the server.

CREATE TABLE IF NOT EXISTS tasks (
  instance_id     TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending',   -- pending|leased|done|dead
  attempt_count   INTEGER NOT NULL DEFAULT 0,
  worker_id       TEXT,
  lease_until     INTEGER,                            -- epoch s
  last_heartbeat  INTEGER,                            -- epoch s
  resolved        INTEGER,                            -- 1/0 from grade, NULL until graded
  patch           TEXT,                               -- model_patch
  report_json     TEXT,                               -- per-instance swebench report
  meta_json       TEXT,                               -- cost/telemetry record
  events_jsonl    TEXT,                               -- S7b: raw agent event stream
  err_txt         TEXT,                               -- S7b: raw agent stderr transcript
  db_b64          TEXT,                               -- S7b: base64 opencode.db (bounded, may be absent)
  completed_by    TEXT,
  completed_at    INTEGER                             -- epoch s
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
