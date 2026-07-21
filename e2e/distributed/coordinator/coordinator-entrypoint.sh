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
#   ARM                  which arm the fleet ran: econ | claude-<mix> | claude-native
#                        (default econ) — tags preds.json model_name_or_path and picks
#                        the cost report (gateway claude -> cost_report.py, else report.py)
#   DATASET / SPLIT      HF dataset (default: SWE-bench Verified, test split)
#                        — carried through for parity with the single-machine
#                        path; the queue itself is instance-id agnostic
#   HOLD                 seconds to hold open after finishing, for the SFTP
#                        pull (default 5400)
#   MAXWAIT              ceiling on drain-wait before giving up on a wedged
#                        fleet and aggregating whatever is done (default 14400)
#   PORT                 coordinator server.py listen port (default 8080)
#   DB_PATH              queue sqlite path (default /data/queue.db)
#   MAX_FAILURE_RERUN    failure-rerun budget: once the fresh queue drains,
#                        server.py's /claim gives a `failed` (attempts-
#                        exhausted) instance this many more tries, replacing
#                        the earlier failure with the rerun's result on
#                        success (default 1; 0 disables — original behaviour)
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
MAX_FAILURE_RERUN="${MAX_FAILURE_RERUN:-1}"
export PORT DB_PATH MAX_FAILURE_RERUN

LABEL="${LABEL:-${RUN_ID:-dist}}"
RUN_ID="${RUN_ID:-$LABEL}"
ARM="${ARM:-econ}"
# Gateway claude arms (claude-<mix> ensembles via LiteLLM: claude-gpt, claude-open, …)
# bill via cost_report.py; econ + claude-native (real Anthropic, no LiteLLM) use
# report.py. The launcher normalizes legacy claude->claude-open and
# claude-real->claude-native before ARM reaches us; a bare "claude" is treated as
# gateway defensively.
is_gateway_claude() {
  case "$1" in
    claude-native)   return 1 ;;
    claude|claude-*) return 0 ;;
    *)               return 1 ;;
  esac
}
DATASET="${DATASET:-princeton-nlp/SWE-bench_Verified}"
SPLIT="${SPLIT:-test}"
HOLD="${HOLD:-5400}"
MAXWAIT="${MAXWAIT:-172800}"   # absolute drain backstop (48h); the wait below is PROGRESS-aware (wedged-only early giveup), not a flat timer
REPORT_PY="${REPORT_PY:-/work/report.py}"
BENCHMARK="${BENCHMARK:-verified}"
# ── Tigris archive (opt-in): upload results/traces/grading/submission/overview to
#    S3 at end-of-run so the fleet can be destroyed and the data still be looked
#    up. AWS_* creds arrive as fleet-app secrets (flyctl storage create -a <app>);
#    a failed archive is NON-fatal (the bundle is still held for the host pull).
ARCHIVE_TIGRIS="${ARCHIVE_TIGRIS:-0}"
TIGRIS_BUCKET="${TIGRIS_BUCKET:-}"
TIGRIS_PREFIX="${TIGRIS_PREFIX:-runs}"
RUN_STARTED_AT="${RUN_STARTED_AT:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
export BENCHMARK TIGRIS_BUCKET TIGRIS_PREFIX RUN_STARTED_AT
ARCHIVE_PY="/work/distributed/tools/tigris_archive.py"
# tigris_archive.py lazily `import boto3` on its UPLOAD path (§8). boto3 lives in
# the /work/.venv venv (Dockerfile.dist), NOT the base-image python3 that $PY
# defaults to — so running the archive under $PY makes the upload raise "boto3 not
# installed", upload nothing, and beacon archive_failed (the "N objects" line is the
# PLAN, printed before the upload). Run the archive under the venv python, which has
# boto3 + pandas + everything the overview/submission builders need. Fall back to
# $PY for a host/standalone run where /work/.venv is absent.
ARCHIVE_PYBIN="/work/.venv/bin/python"; [ -x "$ARCHIVE_PYBIN" ] || ARCHIVE_PYBIN="$PY"
# harness_run benchmarks (Terminal) have no git patch → no submission to build.
ARCHIVE_NOSUB=""; [ "$BENCHMARK" = "terminal" ] && ARCHIVE_NOSUB="--no-submission"

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

# ── 2.5 Wait until the run is ARMED (prepare/run split) ─────────────────────
# COORD_ARMED=1 (default, all-in-one) → server.py boots armed, so this gate
# passes on the first /status poll and behaviour is unchanged. When `prepare`
# booted the coordinator with COORD_ARMED=0, the server holds /claim at
# {wait:true} (warm workers idle, toolbox already built) and /drain never
# reports drained; we block HERE — deliberately BEFORE START_TS so the warm-hold
# does NOT count against MAXWAIT — until the host's `run` step POSTs /arm. The
# `prepared` beacon (with worker-ready count) lets the host know the fleet is warm.
PREPARE_MAXWAIT="${PREPARE_MAXWAIT:-43200}"   # 12h ceiling on the warm-hold
ARM_START=$(date +%s)
LAST_PREP_LOG=0
while :; do
  PELAPSED=$(( $(date +%s) - ARM_START ))
  STATUS_JSON="$(curl -sf "$BASE/status" 2>/dev/null || true)"
  if printf '%s' "$STATUS_JSON" | grep -q '"armed"[[:space:]]*:[[:space:]]*true'; then
    log "armed after ${PELAPSED}s — releasing fleet"; emit '"armed"'; break
  fi
  if [ "$PELAPSED" -ge "$PREPARE_MAXWAIT" ]; then
    log "PREPARE_MAXWAIT (${PREPARE_MAXWAIT}s) exceeded without /arm — proceeding"
    emit '"prepare_timeout"'; break
  fi
  if [ $((PELAPSED - LAST_PREP_LOG)) -ge 30 ]; then
    NW=$(printf '%s' "$STATUS_JSON" | "$PY" -c 'import json,sys
try: print(len((json.load(sys.stdin).get("workers_seen") or [])))
except Exception: print(0)' 2>/dev/null || echo 0)
    log "prepare: warm & holding (t+${PELAPSED}s) — workers_ready=${NW}"
    emit "\"prepared\",\"workers_ready\":${NW}"
    LAST_PREP_LOG=$PELAPSED
  fi
  sleep 10
done

# ── 3. Wait for drain (work-stealing fleet claims/completes/fails rows) ─────
# PROGRESS-aware wait, NOT a flat wall-clock timer: a real 500-instance run with
# >4h hard tasks legitimately takes many hours, so keep waiting as long as the
# fleet completes work (done+dead rising) OR has anything actively leased. Give up
# EARLY only when the fleet is WEDGED — nothing leased AND no completion for
# NO_PROGRESS_GIVEUP seconds (workers gone, tasks stranded pending). MAXWAIT is an
# absolute anti-runaway backstop, not the normal limiter.
NO_PROGRESS_GIVEUP="${NO_PROGRESS_GIVEUP:-7200}"
log "waiting for queue to drain (poll 20s, status ~60s, maxwait ${MAXWAIT}s, wedged-giveup ${NO_PROGRESS_GIVEUP}s)"
START_TS=$(date +%s)
LAST_STATUS_LOG=0
LAST_PROGRESS_TS=$START_TS
LAST_TERMINAL=-1
DRAINED=0
while :; do
  NOW=$(date +%s)
  ELAPSED=$((NOW - START_TS))
  if [ "$ELAPSED" -ge "$MAXWAIT" ]; then
    log "MAXWAIT (${MAXWAIT}s) absolute backstop hit — aggregating whatever is done"
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
    # Wedged detection: parse "done+dead" (terminal) and "leased" from /status.
    STATS="$(printf '%s' "$STATUS_JSON" | "$PY" -c 'import sys,json
try:
  c=json.load(sys.stdin).get("counts",{}); print(int(c.get("done",0))+int(c.get("dead",0)), int(c.get("leased",0)))
except Exception: print(-1,-1)' 2>/dev/null)"
    TERMINAL="${STATS%% *}"; LEASED="${STATS##* }"
    if [ "${TERMINAL:--1}" -ge 0 ] 2>/dev/null; then
      if [ "$TERMINAL" -gt "$LAST_TERMINAL" ]; then
        LAST_TERMINAL="$TERMINAL"
        LAST_PROGRESS_TS="$NOW"
      elif [ "${LEASED:-0}" -eq 0 ] && [ $((NOW - LAST_PROGRESS_TS)) -ge "$NO_PROGRESS_GIVEUP" ]; then
        log "WEDGED: nothing leased + no completion for $((NOW - LAST_PROGRESS_TS))s (workers gone?) — aggregating whatever is done"
        emit '"drain_wedged"'
        break
      fi
    fi
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
dead_rows = []
for row in rows:
    counts[row["status"]] = counts.get(row["status"], 0) + 1
    row_keys = row.keys()
    if row["status"] in ("dead", "failed"):
        # dead-instance capture: dump while the queue DB is still live so
        # WAL-mode rows are visible — an offline `sqlite3 queue.db` copy in
        # the bundle sees zero rows (no bundled -wal sidecar). This plain
        # file is what survives for tools/collect-failed.py. `failed` rows
        # reach here only once /drain reports drained, so any that remain
        # have spent their MAX_FAILURE_RERUN budget (server.py Queue.drain
        # gates on `_pending_reruns()==0`) — captured the same as `dead`.
        dead_rows.append({
            "instance_id": row["instance_id"],
            "failure_reason": row["failure_reason"] if "failure_reason" in row_keys else None,
            "attempt_count": row["attempt_count"] if "attempt_count" in row_keys else None,
            "worker_id": row["worker_id"] if "worker_id" in row_keys else None,
            "last_heartbeat": row["last_heartbeat"] if "last_heartbeat" in row_keys else None,
            "fail_reruns": row["fail_reruns"] if "fail_reruns" in row_keys else None,
        })
        continue
    if row["status"] != "done":
        continue
    iid = row["instance_id"]
    preds[iid] = {
        "instance_id": iid,
        "model_name_or_path": arm,
        "model_patch": row["patch"] or "",
    }
    # S7b/S7c: write back the per-instance transcript synced over /complete,
    # mirroring the single-machine fullresolve layout (results/<label>/
    # artifacts/<iid>/) so a distributed near-miss stays reconstructable.
    # claude_session_jsonl (see below) rides the SAME /complete row as the
    # others — it only reaches here for a `done` row, same as every other
    # trace field; its value is a written-incrementally transcript rather
    # than a write-on-completion one, so within a `done` row it is the
    # field most likely to still be populated when trajectory_json/err_txt
    # are thin or empty (a trial that barely scraped past its timeout).
    events_jsonl = row["events_jsonl"] if "events_jsonl" in row_keys else None
    err_txt = row["err_txt"] if "err_txt" in row_keys else None
    db_b64 = row["db_b64"] if "db_b64" in row_keys else None
    engine_log = row["engine_log"] if "engine_log" in row_keys else None
    # harness_run (Terminal) traces — NULL for resolve_then_grade benchmarks.
    trajectory_json = row["trajectory_json"] if "trajectory_json" in row_keys else None
    sessions_cast = row["sessions_cast"] if "sessions_cast" in row_keys else None
    # Harbor's own captured stdout+stderr — the only place a SETUP-phase
    # RuntimeError (before the agent ever runs, so trial_dir/events_jsonl/
    # err_txt don't exist yet) is ever captured.
    harbor_run_log = row["harbor_run_log"] if "harbor_run_log" in row_keys else None
    # Claude Code's OWN session .jsonl (claude-* arms only) — written
    # incrementally as the agent runs, so unlike trajectory_json it survives
    # a trial killed mid-run (no trajectory.json/err.txt at all).
    claude_session_jsonl = row["claude_session_jsonl"] if "claude_session_jsonl" in row_keys else None
    # EVERY Claude Code session .jsonl for the trial (main + Task sub-agent
    # sidechains, cc-harness-hooks.py's additive _sync_all_claude_sessions),
    # gzip-tarred + base64-encoded by harness_terminal.py's _collect_traces —
    # claude_session_jsonl above only ever holds the main session, so this is
    # the only trace of a sub-agent (escalation: unerr-opus/unerr-fable)
    # misbehaving. Decoded back to a real .tgz below, same idiom as db_b64.
    claude_sessions_tgz_b64 = (
        row["claude_sessions_tgz_b64"] if "claude_sessions_tgz_b64" in row_keys else None
    )
    if (events_jsonl or err_txt or db_b64 or engine_log or trajectory_json
            or sessions_cast or harbor_run_log or claude_session_jsonl
            or claude_sessions_tgz_b64):
        art_dir = rundir / "artifacts" / iid
        art_dir.mkdir(parents=True, exist_ok=True)
        if events_jsonl:
            (art_dir / "events.jsonl").write_text(events_jsonl, encoding="utf-8")
        if err_txt:
            (art_dir / "err.txt").write_text(err_txt, encoding="utf-8")
        if engine_log:
            (art_dir / "engine.log").write_text(engine_log, encoding="utf-8")
        if trajectory_json:
            (art_dir / "trajectory.json").write_text(trajectory_json, encoding="utf-8")
        if sessions_cast:
            (art_dir / "sessions.cast").write_text(sessions_cast, encoding="utf-8")
        if harbor_run_log:
            (art_dir / "harbor-run.log").write_text(harbor_run_log, encoding="utf-8")
        if claude_session_jsonl:
            (art_dir / "claude-session.jsonl").write_text(claude_session_jsonl, encoding="utf-8")
        if claude_sessions_tgz_b64:
            try:
                (art_dir / "claude-sessions.tgz").write_bytes(base64.b64decode(claude_sessions_tgz_b64))
            except (ValueError, TypeError):
                pass
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
# dead-instance capture: one JSON object per dead-lettered row, so
# tools/collect-failed.py can enumerate + triage them from the bundle alone
# (no SSH+grep of server.log needed).
with (rundir / "dead.jsonl").open("w", encoding="utf-8") as fh:
    for d in dead_rows:
        fh.write(json.dumps(d) + "\n")

print(json.dumps({
    "counts": counts, "n_preds": len(preds), "n_meta": len(meta_lines),
    "n_artifacts": n_artifacts, "n_dead": len(dead_rows),
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

# ── 6. Cost/tier report (arm-aware: claude uses cost_report.py, else report.py
#      joins meta.jsonl + the merged grade) ─────────────────────────────────
# Reused verbatim, not reimplemented (PLAN.md §1 consolidation guarantee).
# report.py has no third-party deps (stdlib only) so plain python3 runs it.
log "aggregating cost report"
if is_gateway_claude "$ARM"; then
  CLAUDE_COST_PY="/work/claude/local-docker/cost_report.py"
  if [ -f "$CLAUDE_COST_PY" ]; then
    if ! "$PY" "$CLAUDE_COST_PY" "$RUNDIR" --mode on --grade "$MERGED_GRADE" \
          >"$LOGDIR/report-$LABEL.log" 2>&1; then
      log "cost_report.py nonzero exit — tail:"; tail -20 "$LOGDIR/report-$LABEL.log" >&2
    fi
  else
    log "CLAUDE_COST_PY=$CLAUDE_COST_PY not found — skipping cost report (guard for arm-specific path)"
  fi
elif [ -f "$REPORT_PY" ]; then
  if ! "$PY" "$REPORT_PY" --meta "$RUNDIR/meta.jsonl" --grade-report "$MERGED_GRADE" \
        --label "$LABEL" --out "$RUNDIR" >"$LOGDIR/report-$LABEL.log" 2>&1; then
    log "report.py nonzero exit — tail:"; tail -20 "$LOGDIR/report-$LABEL.log" >&2
  fi
else
  log "REPORT_PY=$REPORT_PY not found — skipping cost report (guard for arm-specific path)"
fi

emit '"resolve_summary"'
echo "----- COST REPORT (begin) -----"; cat "$RUNDIR/cost-report.md" 2>/dev/null; echo "----- COST REPORT (end) -----"

# ── 6.9 Generate overview.json + submission BEFORE bundling ─────────────────
#      so both ride bundle.tgz (host pull) as well as the Tigris archive below.
#      Runs regardless of ARCHIVE_TIGRIS (cheap, and the overview is useful in
#      every bundle); the actual S3 upload in §8 is what's gated.
if [ -f "$ARCHIVE_PY" ]; then
  log "generating overview.json + submission (results/$LABEL/)"
  "$ARCHIVE_PYBIN" "$ARCHIVE_PY" --data-dir "$DATA" --label "$LABEL" --arm "$ARM" \
    --benchmark "$BENCHMARK" --dataset "$DATASET" $ARCHIVE_NOSUB --generate-only \
    >"$LOGDIR/overview-gen.log" 2>&1 || { log "overview/submission gen: nonzero (non-fatal) — tail:"; tail -15 "$LOGDIR/overview-gen.log" >&2; }
fi

# ── 7. Bundle the durable output (archived in §8, host-pulled after §9) ──────
log "bundling /data -> bundle.tgz"
QDB_BASENAME="$(basename "$DB_PATH")"
tar czf "$DATA/bundle.tgz" -C "$DATA" results reports logs "$QDB_BASENAME" 2>/dev/null || true
BSZ=$(du -h "$DATA/bundle.tgz" 2>/dev/null | cut -f1)

log "bundle.tgz=${BSZ:-?} built on $DATA"

# ── 8. Archive the DATA to Tigris (opt-in) — BEFORE signalling bundle_ready ───
#      Uploads traces/grading/submission/overview/logs/bundle under a stable
#      taxonomy. NON-fatal: a failed upload never blocks the host pull. Creds
#      come from fleet-app secrets (AWS_*); bucket from TIGRIS_BUCKET.
#      MUST run before `emit bundle_ready`: the host tears the fleet down (destroys
#      the coordinator machine + its volume) the instant it sees bundle_ready, so a
#      post-signal upload would race teardown and silently never finish.
if [ "$ARCHIVE_TIGRIS" = "1" ] && [ -f "$ARCHIVE_PY" ]; then
  export RUN_FINISHED_AT; RUN_FINISHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  log "archiving run to Tigris (bucket=${TIGRIS_BUCKET:-<unset>} prefix=$TIGRIS_PREFIX)"
  if "$ARCHIVE_PYBIN" "$ARCHIVE_PY" --data-dir "$DATA" --label "$LABEL" --arm "$ARM" \
       --benchmark "$BENCHMARK" --dataset "$DATASET" $ARCHIVE_NOSUB \
       >"$LOGDIR/tigris-archive.log" 2>&1; then
    log "tigris archive: done -> $(grep -oE 's3://[^ ]+' "$LOGDIR/tigris-archive.log" | tail -1)"
    emit "\"archived\",\"bucket\":\"${TIGRIS_BUCKET:-}\""
  else
    log "tigris archive: FAILED (non-fatal) — tail:"; tail -25 "$LOGDIR/tigris-archive.log" >&2
    emit '"archive_failed"'
  fi
else
  log "ARCHIVE_TIGRIS=$ARCHIVE_TIGRIS — Tigris upload skipped (results held for host pull only)"
fi

# ── 9. Signal ready + HOLD so the host can SFTP the bundle, then exit ─────────
#      Emitted AFTER §8: the host pulls the bundle and destroys the fleet on this
#      beacon, so signalling here (not before §8) is what keeps the archive safe.
log "ALL DONE — bundle.tgz=${BSZ:-?} ready on $DATA"
emit "\"bundle_ready\",\"path\":\"/data/bundle.tgz\",\"size\":\"${BSZ:-?}\""
# Durable, race-safe terminal sentinel on /data (written AFTER §8 archive, same as
# the beacon → tearing down on it can't clobber the archive). The host polls this
# over the reliable `ssh curl /status` channel, so a dropped `flyctl logs` stream
# (which silently strands the stdout beacon on long runs) can no longer hang the
# launcher past MAXWAIT. Content = the same beacon line for host-side triage.
printf '{"ev":"bundle_ready","path":"/data/bundle.tgz","size":"%s"}\n' "${BSZ:-?}" > "$DATA/BUNDLE_READY"
sync

log "holding ${HOLD}s for host pull"
sleep "$HOLD"
exit 0
