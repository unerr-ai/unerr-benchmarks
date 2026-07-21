#!/usr/bin/env bash
# pull_traces.sh — pull per-instance AGENT TRACES off a LIVE fleet's coordinator,
# straight out of its queue.db, WITHOUT waiting for drain and WITHOUT tearing
# anything down.
#
# WHY THIS EXISTS (the gap it closes)
# -----------------------------------
# The two existing trace tools both operate on a POST-DRAIN BUNDLE:
#   - tools/collect-failed.py   batch-archives failed instances from out/dist-<L>/bundle
#   - tools/debug_instance.py   single-instance triage from that same bundle
# and tools/pull_results.sh pulls /data/bundle.tgz — which the coordinator only
# writes at end-of-run. So between "a task failed" and "the run drained" there was
# NO supported way to get a trace off the fleet, even though the evidence is
# already durable: the worker reads its artifacts into memory BEFORE deleting its
# scratch dir, so the moment it POSTs /complete the trace lives in the
# coordinator's queue.db (see DEBUG_FAILED_TASK.md). This script is that missing
# mid-run path — triage a failure while the rest of the run is still executing,
# or rescue traces just before an imminent teardown.
#
# It is benchmark-agnostic: filenames come from the per-benchmark "traces" map in
# tools/benchmarks.py when that module is importable on the coordinator, so a new
# benchmark's artifacts are picked up with no change here. If it isn't importable,
# it falls back to dumping every artifact-shaped column it finds on `tasks`.
#
# Usage:
#   tools/pull_traces.sh <LABEL> [APP] [--failed-only] [--ids a,b,c] [--out DIR]
#
#   <LABEL>        fleet label (the LOOKUP label — includes the benchmark suffix
#                  for non-verified runs, e.g. cgpt-tb21-val10-terminal)
#   [APP]          fly app; omitted => resolved exactly like status.sh
#                  ($ARM env wins, else the label's arm token, else a guess)
#   --failed-only  only instances that did not resolve (resolved IS NOT 1)
#   --ids a,b,c    explicit instance ids (comma-separated); overrides --failed-only
#   --out DIR      output dir (default: out/dist-<LABEL>/traces-live)
#
# Examples:
#   tools/pull_traces.sh cgpt-tb21-val10-terminal --failed-only
#   ARM=claude-gpt tools/pull_traces.sh cgpt-tb21-val10-terminal --ids build-pmars
#
# Non-destructive: read-only on the coordinator apart from a scratch dir under
# /tmp, which it removes. The fleet keeps running (and billing) after it returns.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"     # e2e/distributed
# shellcheck source=tools/fleet-common.sh
source "$HERE/tools/fleet-common.sh"

log() { printf '[pull_traces] %s\n' "$*" >&2; }

LABEL=""; APP=""; MODE="all"; IDS=""; OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --failed-only) MODE="failed"; shift ;;
    --ids)         IDS="${2:?--ids needs a value}"; MODE="ids"; shift 2 ;;
    --out)         OUT="${2:?--out needs a value}"; shift 2 ;;
    -h|--help)     sed -n '2,40p' "$0"; exit 0 ;;
    -*)            log "unknown flag: $1"; exit 2 ;;
    *)             if [ -z "$LABEL" ]; then LABEL="$1"; elif [ -z "$APP" ]; then APP="$1"; fi; shift ;;
  esac
done
[ -n "$LABEL" ] || { log "usage: pull_traces.sh <LABEL> [APP] [--failed-only|--ids a,b] [--out DIR]"; exit 2; }

fc_fly_token

# ── benchmark key + app resolution: same precedence as status.sh ──
BENCH="${BENCHMARK:-}"
if [ -z "$BENCH" ]; then
  case "$LABEL" in
    *-terminal) BENCH=terminal ;; *-pro) BENCH=pro ;; *-lite) BENCH=lite ;;
    *-live_verified|*-live-verified) BENCH=live_verified ;; *) BENCH=verified ;;
  esac
fi
if [ -z "$APP" ]; then
  # called directly (NOT via $(...)) — fc_resolve_arm sets globals.
  fc_resolve_arm "$LABEL" "$BENCH"
  APP="$(fc_default_app "$FC_RESOLVED_ARM" "$BENCH")"
  [ "${FC_ARM_INFERRED:-}" = "guess" ] && \
    log "note: arm not present in label and \$ARM unset — guessed '$FC_RESOLVED_ARM' (app $APP). Pass APP or set ARM= if wrong."
  [ -n "${FC_ARM_CONFLICT:-}" ] && log "WARNING: \$ARM=$FC_RESOLVED_ARM overrides the label's own arm token ($FC_ARM_CONFLICT)"
fi

COORD="$(fc_coord "$APP" "$LABEL")" || true
[ -n "$COORD" ] || { log "no coordinator for label=$LABEL on app=$APP  ($(fc_last_error))"; exit 3; }
log "app=$APP benchmark=$BENCH coordinator=$COORD"

OUT="${OUT:-$HERE/out/dist-$LABEL/traces-live}"
mkdir -p "$OUT"

# ── the extractor, run ON the coordinator against the live queue.db ──
# WAL-safe: opens read-only via a file: URI so it never blocks the coordinator's
# own writers and can't mutate the queue.
TMPX="$(mktemp -t pulltraces).py"
cat > "$TMPX" <<'PYEOF'
import json, os, shutil, sqlite3, sys, tarfile

mode, ids_csv = sys.argv[1], sys.argv[2]
DB, STAGE, TGZ = "/data/queue.db", "/tmp/traces-live", "/tmp/traces-live.tgz"

# Filenames per benchmark come from the run's own contract when importable, so a
# new benchmark needs no edit here.
name_map = None
# Dockerfile.dist stages the tools at /work/distributed/tools (see its COPY lines);
# the rest are fallbacks so this keeps working if that layout ever moves.
for p in ("/work/distributed/tools", "/work/tools", "/work", "/app/tools", "/app"):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)
try:
    import benchmarks  # noqa
    for attr in ("_REGISTRY", "BENCHMARKS", "_BENCHMARKS", "ALL"):
        reg = getattr(benchmarks, attr, None)
        if isinstance(reg, dict):
            seen = {}
            for spec in reg.values():
                for fn, col in (spec or {}).get("traces", ()) or ():
                    seen[col] = fn
            if seen:
                name_map = seen
            break
except Exception as e:
    print("note: benchmarks.py not importable (%s) — using fallback names" % str(e)[:60])

conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
ARTIFACT_HINTS = ("json", "txt", "log", "jsonl", "cast", "db")
art_cols = [c for c in cols
            if c not in ("instance_id",) and any(h in c for h in ARTIFACT_HINTS)]

def fname(col):
    if name_map and col in name_map:
        return name_map[col]
    for suf in ("_jsonl", "_json", "_txt", "_log", "_cast"):
        if col.endswith(suf):
            return col[: -len(suf)] + "." + suf.lstrip("_")
    return col

if mode == "ids":
    wanted = [i.strip() for i in ids_csv.split(",") if i.strip()]
    q = "SELECT instance_id FROM tasks WHERE instance_id IN (%s)" % ",".join("?" * len(wanted))
    rows = [r[0] for r in conn.execute(q, wanted)]
elif mode == "failed":
    rows = [r[0] for r in conn.execute(
        "SELECT instance_id FROM tasks WHERE status='done' AND IFNULL(resolved,0)<>1 ORDER BY instance_id")]
else:
    rows = [r[0] for r in conn.execute(
        "SELECT instance_id FROM tasks WHERE status='done' ORDER BY instance_id")]

shutil.rmtree(STAGE, ignore_errors=True)
os.makedirs(STAGE, exist_ok=True)
if not rows:
    print("MATCHED 0 instances (mode=%s)" % mode)

manifest = []
for iid in rows:
    d = os.path.join(STAGE, iid)
    os.makedirs(d, exist_ok=True)
    got = []
    rec = dict(zip(art_cols, conn.execute(
        "SELECT %s FROM tasks WHERE instance_id=?" % ",".join(art_cols), (iid,)).fetchone()))
    for col, val in rec.items():
        if val in (None, ""):
            continue
        if isinstance(val, str):
            val = val.encode("utf-8", "replace")
        with open(os.path.join(d, fname(col)), "wb") as f:
            f.write(val)
        got.append("%s(%dB)" % (fname(col), len(val)))
    # trajectory.json is a JSON blob; also emit steps as real JSONL — one step per
    # line — which is what a trace reader/differ actually wants.
    tj = os.path.join(d, "trajectory.json")
    if os.path.exists(tj):
        try:
            with open(tj) as f:
                doc = json.load(f)
            steps = doc.get("steps") if isinstance(doc, dict) else doc
            if isinstance(steps, list):
                with open(os.path.join(d, "trajectory.jsonl"), "w") as f:
                    for s in steps:
                        f.write(json.dumps(s, ensure_ascii=False) + "\n")
                got.append("trajectory.jsonl(%d steps)" % len(steps))
        except Exception as e:
            print("  %s: trajectory.jsonl skipped (%s)" % (iid, str(e)[:60]))
    manifest.append({"instance_id": iid, "artifacts": got})
    print("  %-34s %s" % (iid, " ".join(got) or "(no artifacts)"))

with open(os.path.join(STAGE, "MANIFEST.json"), "w") as f:
    json.dump({"mode": mode, "count": len(rows), "instances": manifest}, f, indent=2)

with tarfile.open(TGZ, "w:gz") as t:
    t.add(STAGE, arcname=".")
print("STAGED %d instances -> %s (%d bytes)" % (len(rows), TGZ, os.path.getsize(TGZ)))
PYEOF

log "uploading extractor + dumping traces (mode=$MODE)"
flyctl ssh sftp shell -a "$APP" --machine "$COORD" >/dev/null 2>&1 <<SFTP
put $TMPX /tmp/pull_traces_extract.py
SFTP
rm -f "$TMPX"

flyctl ssh console -a "$APP" --machine "$COORD" \
  -C "python3 /tmp/pull_traces_extract.py $MODE ${IDS:-none}" 2>&1 \
  | grep -vE 'Connecting|Waiting|Connected|already' || true

log "downloading /tmp/traces-live.tgz"
# `fly ssh sftp get` REFUSES to overwrite — clear a stale tarball first (same
# gotcha pull_results.sh documents for bundle.tgz).
rm -f "$OUT/traces-live.tgz"
flyctl ssh sftp get /tmp/traces-live.tgz "$OUT/traces-live.tgz" \
  -a "$APP" --machine "$COORD" 2>&1 | tail -2 >&2 || true
[ -s "$OUT/traces-live.tgz" ] || { log "download produced no tarball — nothing matched, or sftp failed"; exit 4; }
tar -xzf "$OUT/traces-live.tgz" -C "$OUT" && rm -f "$OUT/traces-live.tgz"

# scratch cleanup on the coordinator (read-only apart from this)
flyctl ssh console -a "$APP" --machine "$COORD" \
  -C 'rm -rf /tmp/traces-live /tmp/traces-live.tgz /tmp/pull_traces_extract.py' >/dev/null 2>&1 || true

log "wrote:"
find "$OUT" -mindepth 1 -maxdepth 2 -type f | sed 's|^|  |' >&2
log "done -> $OUT"
