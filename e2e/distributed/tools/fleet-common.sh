#!/usr/bin/env bash
# fleet-common.sh — shared fleet-lookup helpers for the LIVE-fleet monitoring
# scripts (status.sh, debug-workers.sh). SOURCE this; it is not executable on
# its own. Single-sources the same primitives run-distributed.sh uses inline
# (fly-token read, metadata-scoped machine lookup, coordinator /status probe)
# so the monitors never drift from the runner.
#
#   source "$(dirname "$0")/tools/fleet-common.sh"    # from e2e/distributed/*.sh
#
# Provides:
#   fc_fly_token            -> exports FLY_API_TOKEN (env first, else ~/.fly/config.yml). Never prints it.
#   fc_default_app <arm>    -> the app for an arm (econ->swebench-agent-dist, claude->...-claude)
#   fc_machines <app> <label> [role]   -> machine ids, one per line (role optional: coordinator|worker)
#   fc_coord <app> <label>             -> the coordinator machine id (first match), or empty
#   fc_status <app> <coord_mid>        -> raw /status JSON (curl'd inside the coordinator via ssh)
#   fc_newest_manifest                 -> path of the most-recently-modified out/bench-*/manifest.tsv, or empty
#   fc_read_manifest <path>            -> emits "arm<TAB>bench<TAB>label<TAB>app" rows (comments/blank skipped)
#
# PY_HOST + FC_DIST (e2e/distributed dir) are resolved on source.
FC_DIST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # e2e/distributed
PY_HOST="${PYTHON:-python3}"

# ── auth: fly token — env first, else the saved token (verbatim from run-distributed.sh/pull_results.sh) ──
# Reads ~/.fly/config.yml only to export FLY_API_TOKEN for flyctl; the token value is never echoed.
fc_fly_token() {
  if [ -z "${FLY_API_TOKEN:-}" ]; then
    FLY_API_TOKEN="$(node -e "const fs=require('fs');const y=fs.readFileSync(process.env.HOME+'/.fly/config.yml','utf8');const m=y.match(/access_token:\s*(\S+)/);process.stdout.write(m?m[1]:'')" 2>/dev/null || true)"
  fi
  export FLY_API_TOKEN
  [ -n "${FLY_API_TOKEN:-}" ] || { echo "[fleet-common] no fly token (run: flyctl auth login)" >&2; return 1; }
}

# ── arm -> app (mirrors run-distributed.sh DEFAULT_APP / the -claude fold) ──
fc_default_app() {
  case "${1:-}" in
    claude) echo "swebench-agent-dist-claude" ;;
    *)      echo "swebench-agent-dist" ;;
  esac
}

# ── machine ids for a fleet, optionally role-scoped (same query as run-distributed.sh fleet_ids) ──
# metadata is only in the --json view, so we parse that. A flyctl error -> empty (caller decides).
fc_machines() {  # <app> <label> [role]
  local app="$1" label="$2" role="${3:-}"
  flyctl machines list -a "$app" --json 2>/dev/null | "$PY_HOST" -c '
import sys, json
role, label = sys.argv[1], sys.argv[2]
try:
    ms = json.load(sys.stdin)
except Exception:
    ms = []
for m in ms:
    md = (m.get("config") or {}).get("metadata") or {}
    if md.get("fleet") != label:
        continue
    if role and md.get("role") != role:
        continue
    print(m.get("id"))
' "$role" "$label"
}

fc_coord() {  # <app> <label>
  fc_machines "$1" "$2" coordinator | head -1
}

# ── /status probe: curl INSIDE the coordinator via ssh (host is off 6PN) — verbatim from run-distributed.sh poll_status ──
fc_status() {  # <app> <coord_mid>
  flyctl ssh console -a "$1" --machine "$2" -C "curl -s localhost:8080/status" 2>/dev/null \
    | grep -vE 'Connecting|Waiting|Connected|already'
}

# ── cost/telemetry probe: read the coordinator's queue.db directly (the per-instance
#    meta_json holds the cost record — econ: telemetry.usd/turns + tier_cost_db.by_tier;
#    claude: top-level cost.by_tier from litellm_spend_logs). Emits ONE JSON line:
#    [[instance_id, status, resolved, meta_obj_or_null], ...]. python3 is always on the
#    coordinator (it runs server.py); no sqlite3 CLI or rebake needed. meta_json exists
#    only for completed/failed rows (leased/pending are null) — cost accrues on completion.
fc_meta_rows() {  # <app> <coord_mid>
  flyctl ssh console -a "$1" --machine "$2" -C 'python3 -c "import sqlite3,json; c=sqlite3.connect(\"/data/queue.db\"); rows=c.execute(\"SELECT instance_id,status,resolved,meta_json FROM tasks\").fetchall(); print(json.dumps([[r[0],r[1],r[2],(json.loads(r[3]) if r[3] else None)] for r in rows]))"' 2>/dev/null \
    | grep -E '^\['
}

# ── newest matrix manifest (so a bare `./status.sh` shows the run you just launched) ──
fc_newest_manifest() {
  ls -t "$FC_DIST"/out/bench-*/manifest.tsv 2>/dev/null | head -1
}

# ── manifest rows -> "arm<TAB>bench<TAB>label<TAB>app" (skips the two comment/header lines) ──
fc_read_manifest() {  # <path>
  local path="$1"
  [ -f "$path" ] || return 1
  while IFS=$'\t' read -r a b l ap _rest; do
    case "$a" in ''|'#'*) continue ;; esac
    [ -n "${l:-}" ] && [ -n "${ap:-}" ] || continue
    printf '%s\t%s\t%s\t%s\n' "$a" "$b" "$l" "$ap"
  done <"$path"
}
