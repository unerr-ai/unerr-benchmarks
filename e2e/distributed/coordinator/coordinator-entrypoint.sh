#!/usr/bin/env bash
# Coordinator PID1 for the distributed SWE-bench runner (Slice D).
#
# Runs INSIDE the fly coordinator machine (1 small volume mounted at /data;
# see PLAN.md §1 decision 1). Boots coordinator/server.py (Slice B: aiohttp +
# SQLite work-stealing queue), seeds it with the instance-id list, waits for
# the worker fleet to drain the queue, then AGGREGATES every worker's
# POST /complete payload (patch / report_json / meta_json, see schema.sql)
# straight off the queue DB into one preds.json + merged grade report +
# meta.jsonl, runs report.py verbatim for the cost/token summary (PLAN.md §1
# "never reimplements... report.py is reused verbatim"), bundles /data, and
# holds for the host's SFTP pull — same beacon/bundle/HOLD convention as the
# single-machine e2e/econ/fly-remote/fullresolve/entrypoint.sh so the host
# babysitter recognises this machine too.
#
# NOTE — protocol additions beyond PLAN.md §2's HTTP table: this script also
# relies on `GET /health` (readiness) and `POST /seed {run_id, instance_ids}`
# (queue seeding). Neither is in PLAN.md's endpoint table but both are
# required by this entrypoint — Slice B's server.py must implement them to
# this contract (Slice D was built against the PLAN's schema/protocol before
# server.py existed).
#
# Env in:
#   RUN_ID              run identifier used to seed + filter queue rows
#                        (defaults to LABEL if unset — one coordinator = one run)
#   LABEL                report / bundle label (defaults to RUN_ID if unset)
#   TASKS                comma-separated instance_ids to seed the queue with
#   TASKS_FILE           OR a file of newline/comma-separated instance_ids
#                        (TASKS wins if both are set)
#   ARM                  which per-arm predictor the fleet ran (default econ)
#                        — only used for the preds.json model_name_or_path tag
#   DATASET / SPLIT      HF dataset (default: SWE-bench Verified, test split)
#                        — carried through for parity with the single-machine
#                        path; the queue itself is instance-id agnostic
#   HOLD                 seconds to hold open after finishing, for the SFTP
#                        pull (default 5400)
#   MAXWAIT              ceiling on drain-wait before giving up on a wedged
#                        fleet and aggregating whatever is done (default 14400)
#   PORT                 coordinator server.py listen port (default 8080)
#   DB_PATH              queue sqlite path (default /data/queue.db)
#   REPORT_PY             path to report.py (default /work/report.py; guard
#                        for arm-specific report paths)
set -uo pipefail

DATA=/data
OUT="$DATA/results"
REPORTS="$DATA/reports"
LOGDIR="$DATA/logs"
mkdir -p "$OUT" "$REPORTS" "$LOGDIR"

PY="${PYTHON_BIN:-python3}"
PORT="${PORT:-8080}"
DB_PATH="${DB_PATH:-/data/queue.db}"
export PORT DB_PATH

LABEL="${LABEL:-${RUN_ID:-dist}}"
RUN_ID="${RUN_ID:-$LABEL}"
ARM="${ARM:-econ}"
DATASET="${DATASET:-princeton-nlp/SWE-bench_Verified}"
SPLIT="${SPLIT:-test}"
HOLD="${HOLD:-5400}"
MAXWAIT="${MAXWAIT:-14400}"
REPORT_PY="${REPORT_PY:-/work/report.py}"

BASE="http://127.0.0.1:${PORT}"
RUNDIR="$OUT/$LABEL"
GRADE_DIR="$LOGDIR/grade-merged"
MERGED_GRADE="$REPORTS/merged.$LABEL.json"

log() { printf '[dist-coordinator] %s\n' "$*" >&2; }
emit() { printf '{"ev":%s}\n' "$1"; }   # one-line JSON beacons the host scrapes

# ── 1. Launch the coordinator server ────────────────────────────────────────
log "starting server.py (port=$PORT db=$DB_PATH)"
"$PY" /work/distributed/coordinator/server.py >"$LOGDIR/server.log" 2>&1 &
SERVER_PID=$!

HEALTH_OK=0
for i in $(seq 1 30); do
  if curl -sf "$BASE/health" >/dev/null 2>&1; then HEALTH_OK=1; break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    log "server.py died on boot — tail of server.log:"; tail -40 "$LOGDIR/server.log" >&2
    emit '"fatal","stage":"server"'; exit 21
  fi
  sleep 1
done
if [ "$HEALTH_OK" != "1" ]; then
  log "server /health not ready after 30s"; tail -40 "$LOGDIR/server.log" >&2
  emit '"fatal","stage":"server-timeout"'; exit 21
fi
log "coordinator server up on :$PORT (pid $SERVER_PID)"
emit '"server_up"'

# ── 2. Resolve TASKS/TASKS_FILE and seed the queue ──────────────────────────
IDS_CSV=""
if [ -n "${TASKS:-}" ]; then
  IDS_CSV="$TASKS"
elif [ -n "${TASKS_FILE:-}" ] && [ -f "$TASKS_FILE" ]; then
  IDS_CSV="$(tr -s ' \t\n' ',' < "$TASKS_FILE" | sed 's/,,*/,/g; s/^,//; s/,$//')"
elif [ -f /work/distributed/tools/suite.py ]; then
  # Slice E's suite.py (SUITE=full|mini|lite → instance-id list). Best-effort:
  # if it's not there yet, or errors, fall back to requiring TASKS/TASKS_FILE.
  log "no TASKS/TASKS_FILE — trying tools/suite.py (SUITE=${SUITE:-full})"
  IDS_CSV="$("$PY" /work/distributed/tools/suite.py --dataset "$DATASET" --split "$SPLIT" \
             --suite "${SUITE:-full}" 2>>"$LOGDIR/suite.log" || true)"
fi
if [ -z "$IDS_CSV" ]; then
  log "no instance ids resolved (set TASKS or TASKS_FILE)"; emit '"fatal","stage":"no-tasks"'; exit 22
fi

SEED_BODY="$("$PY" -c '
import json, sys
ids = [i.strip() for i in sys.argv[2].split(",") if i.strip()]
print(json.dumps({"run_id": sys.argv[1], "instance_ids": ids}))
' "$RUN_ID" "$IDS_CSV")"
N_SEED=$(printf '%s' "$IDS_CSV" | tr ',' '\n' | grep -c . || true)

log "seeding $N_SEED instance(s) run_id=$RUN_ID"
if ! curl -sf -X POST "$BASE/seed" -H 'Content-Type: application/json' -d "$SEED_BODY" \
     >"$LOGDIR/seed-resp.json" 2>"$LOGDIR/seed.log"; then
  log "seed request failed — tail:"; tail -20 "$LOGDIR/seed.log" >&2
  emit '"fatal","stage":"seed"'; exit 23
fi
log "seeded"
emit "\"seeded\",\"n\":$N_SEED"

# ── 3. Wait for drain (work-stealing fleet claims/completes/fails rows) ─────
log "waiting for queue to drain (poll 20s, status log ~60s, maxwait ${MAXWAIT}s)"
START_TS=$(date +%s)
LAST_STATUS_LOG=0
DRAINED=0
while :; do
  NOW=$(date +%s)
  ELAPSED=$((NOW - START_TS))
  if [ "$ELAPSED" -ge "$MAXWAIT" ]; then
    log "MAXWAIT (${MAXWAIT}s) exceeded — fleet did not drain, aggregating whatever is done"
    emit '"drain_timeout"'
    break
  fi
  DRAIN_JSON="$(curl -sf "$BASE/drain" 2>/dev/null || true)"
  if printf '%s' "$DRAIN_JSON" | grep -q '"drained"[[:space:]]*:[[:space:]]*true'; then
    DRAINED=1
    log "queue drained after ${ELAPSED}s"
    emit '"drained"'
    break
  fi
  if [ $((ELAPSED - LAST_STATUS_LOG)) -ge 60 ]; then
    STATUS_JSON="$(curl -sf "$BASE/status" 2>/dev/null || true)"
    log "status (t+${ELAPSED}s): ${STATUS_JSON:-<no response>}"
    LAST_STATUS_LOG=$ELAPSED
  fi
  sleep 20
done

# ── 4. Aggregate: pull every row off the queue DB into preds.json / ─────────
#      meta.jsonl / per-instance report.json dumps (logs/grade-merged/<iid>/)
log "aggregating results from $DB_PATH (run_id=$RUN_ID)"
mkdir -p "$RUNDIR" "$GRADE_DIR"
AGG_SUMMARY="$("$PY" - "$DB_PATH" "$RUN_ID" "$RUNDIR" "$GRADE_DIR" "$ARM" <<'PYEOF'
import base64, json, pathlib, sqlite3, sys

db_path, run_id, rundir, grade_dir, arm = sys.argv[1:6]
rundir = pathlib.Path(rundir)
grade_dir = pathlib.Path(grade_dir)

con = sqlite3.connect(db_path)
con.row_factory = sqlite3.Row
rows = con.execute("SELECT * FROM tasks WHERE run_id=?", (run_id,)).fetchall()
con.close()

preds = {}
counts = {}
meta_lines = []
n_artifacts = 0
for row in rows:
    counts[row["status"]] = counts.get(row["status"], 0) + 1
    if row["status"] != "done":
        continue
    iid = row["instance_id"]
    preds[iid] = {
        "instance_id": iid,
        "model_name_or_path": arm,
        "model_patch": row["patch"] or "",
    }
    # S7b: write back the per-instance transcript synced over /complete,
    # mirroring the single-machine fullresolve layout (results/<label>/
    # artifacts/<iid>/) so a distributed near-miss stays reconstructable.
    row_keys = row.keys()
    events_jsonl = row["events_jsonl"] if "events_jsonl" in row_keys else None
    err_txt = row["err_txt"] if "err_txt" in row_keys else None
    db_b64 = row["db_b64"] if "db_b64" in row_keys else None
    if events_jsonl or err_txt or db_b64:
        art_dir = rundir / "artifacts" / iid
        art_dir.mkdir(parents=True, exist_ok=True)
        if events_jsonl:
            (art_dir / "events.jsonl").write_text(events_jsonl, encoding="utf-8")
        if err_txt:
            (art_dir / "err.txt").write_text(err_txt, encoding="utf-8")
        if db_b64:
            try:
                (art_dir / "opencode.db").write_bytes(base64.b64decode(db_b64))
            except (ValueError, TypeError):
                pass
        n_artifacts += 1
    if row["report_json"]:
        try:
            report = json.loads(row["report_json"])
        except (TypeError, ValueError):
            report = None
        if report is not None:
            out_dir = grade_dir / iid
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    if row["meta_json"]:
        meta_lines.append(row["meta_json"])

rundir.mkdir(parents=True, exist_ok=True)
(rundir / "preds.json").write_text(json.dumps(preds, indent=2), encoding="utf-8")
with (rundir / "meta.jsonl").open("w", encoding="utf-8") as fh:
    for line in meta_lines:
        fh.write(line.rstrip("\n") + "\n")

print(json.dumps({
    "counts": counts, "n_preds": len(preds), "n_meta": len(meta_lines),
    "n_artifacts": n_artifacts,
}))
PYEOF
)"
log "aggregate summary: $AGG_SUMMARY"
emit "\"aggregated\",\"summary\":$AGG_SUMMARY"

# ── 5. Merge per-instance grade reports (+ dead-lettered rows) into one ─────
#      grade summary compatible with report.py's --grade-report reader.
log "merging per-instance grade reports -> $MERGED_GRADE"
if "$PY" /work/distributed/tools/merge-reports.py \
     --reports-dir "$GRADE_DIR" --db "$DB_PATH" --run-id "$RUN_ID" \
     --out "$MERGED_GRADE" >"$LOGDIR/merge-reports.log" 2>&1; then
  log "merge-reports: done"
else
  log "merge-reports: nonzero exit — tail:"; tail -30 "$LOGDIR/merge-reports.log" >&2
fi

RESOLVED=$(jq -r '.resolved_instances // 0' "$MERGED_GRADE" 2>/dev/null | head -1)
TOTAL=$(jq -r '.submitted_instances // .total_instances // 0' "$MERGED_GRADE" 2>/dev/null | head -1)
log "grade merged: resolved=${RESOLVED:-?}/${TOTAL:-?}"
emit "\"grade_done\",\"run_id\":\"$RUN_ID\",\"resolved\":${RESOLVED:-0},\"total\":${TOTAL:-0}"

# ── 6. Cost/tier report (report.py joins meta.jsonl + the merged grade) ─────
# Reused verbatim, not reimplemented (PLAN.md §1 consolidation guarantee).
# report.py has no third-party deps (stdlib only) so plain python3 runs it.
log "aggregating cost report"
if [ -f "$REPORT_PY" ]; then
  if ! "$PY" "$REPORT_PY" --meta "$RUNDIR/meta.jsonl" --grade-report "$MERGED_GRADE" \
        --label "$LABEL" --out "$RUNDIR" >"$LOGDIR/report-$LABEL.log" 2>&1; then
    log "report.py nonzero exit — tail:"; tail -20 "$LOGDIR/report-$LABEL.log" >&2
  fi
else
  log "REPORT_PY=$REPORT_PY not found — skipping cost report (guard for arm-specific path)"
fi

emit '"resolve_summary"'
echo "----- COST REPORT (begin) -----"; cat "$RUNDIR/cost-report.md" 2>/dev/null; echo "----- COST REPORT (end) -----"

# ── 7. Bundle the durable output and HOLD so the host can SFTP it ───────────
log "bundling /data -> bundle.tgz"
QDB_BASENAME="$(basename "$DB_PATH")"
tar czf "$DATA/bundle.tgz" -C "$DATA" results reports logs "$QDB_BASENAME" 2>/dev/null || true
BSZ=$(du -h "$DATA/bundle.tgz" 2>/dev/null | cut -f1)

log "ALL DONE — bundle.tgz=${BSZ:-?} ready on $DATA; holding ${HOLD}s for host pull"
emit "\"bundle_ready\",\"path\":\"/data/bundle.tgz\",\"size\":\"${BSZ:-?}\""
sync
sleep "$HOLD"
exit 0
