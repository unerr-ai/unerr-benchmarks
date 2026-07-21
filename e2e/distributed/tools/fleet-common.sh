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
#   fc_default_app <arm> [benchmark] -> the app for a combo (swebench-dist-<arm>-<slug>; slug = benchmark with '_'->'-' and 'verified'->'verif' — fly blocks names containing "verified")
#   fc_arm_from_label <label> [bench] -> the arm token embedded in a fleet LABEL (e.g. "claude-gpt" out of "mtx-abc-claude-gpt-terminal")
#   fc_resolve_arm <label> [bench]     -> explicit-wins arm resolution: $ARM env (normalized) if set,
#                                          else fc_arm_from_label. Sets GLOBALS (not stdout — call it
#                                          directly, never via $(...), see the function's own comment):
#                                          FC_RESOLVED_ARM (the arm) and FC_ARM_INFERRED, how it was
#                                          resolved — "env" | "label" (a real arm token matched) |
#                                          "guess" (fell through to the default, nothing to go on) — so
#                                          a caller can warn instead of silently trusting a blind guess.
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

# ── (arm, benchmark) -> app (mirrors run-distributed.sh DEFAULT_APP — keep the
# two derivations byte-identical). Per-(arm×benchmark) apps: each combo builds
# and runs on its OWN fly app so independent triggers never contend on one
# app's remote builder. Fly app names allow [a-z0-9-] only, so the benchmark
# key's '_' -> '-' (live_verified -> live-verified). benchmark defaults to
# verified when omitted (back-compat with older single-arg callers). ──
fc_default_app() {  # <arm> [benchmark]
  local arm="${1:-econ}" bench="${2:-verified}"
  # Normalize legacy arm aliases (SAME mapping as run-distributed.sh/bench.sh) so a
  # monitor keyed on a legacy name resolves the canonical app: claude -> claude-open,
  # claude-real -> claude-native.
  case "$arm" in claude) arm="claude-open" ;; claude-real) arm="claude-native" ;; esac
  bench="${bench//_/-}"
  # fly's abuse filter BLOCKS app names containing "verified" (phishing target),
  # so the app slug shortens it: verified -> verif, live-verified -> live-verif.
  bench="${bench//verified/verif}"
  echo "swebench-dist-${arm}-${bench}"
}

# ── label -> arm (the mix token embedded in a fleet LABEL, e.g. "claude-gpt" out
# of "mtx-abc-claude-gpt-terminal"). Strips the benchmark/graderr suffix first
# (skipped for a verified label — it carries no suffix, see bench.sh) so the
# arm token is trailing, then matches the known arm set MOST-SPECIFIC-FIRST —
# claude-native/claude-gpt/claude-open MUST be checked before the bare legacy
# "claude" (every one of those labels also ends in "...claude") or they'd all
# mislabel as claude's own app. An unrecognized "claude-<mix>" falls through to
# the future-proof branch, which reconstructs the FULL "claude-<mix>" (NOT just
# "<mix>" — a naive ${tmp##*-} would wrongly yield just "gpt"). ──
fc_arm_from_label() {  # <label> [bench]
  local lbl="$1" bench="${2:-verified}" tmp="$lbl"
  if [ "$bench" != "verified" ]; then
    tmp="${tmp%-graderr-*}"
    tmp="${tmp%-$bench}"
  fi
  case "$tmp" in
    *-econ)          echo econ ;;
    *-claude-native) echo claude-native ;;
    *-claude-gpt)    echo claude-gpt ;;
    *-claude-open)   echo claude-open ;;
    *-claude-real)   echo claude-real ;;
    *-claude)        echo claude ;;
    *-claude-*)      echo "claude-${tmp##*-claude-}" ;;
    *)               echo econ ;;
  esac
}

# ── explicit ARM wins over label inference (the bug this fixes: an operator's
# LABEL that doesn't embed a recognized arm token — e.g. "cgpt-tb21-val10" using
# "cgpt" — used to silently fall through fc_arm_from_label's `*) echo econ`
# default with NO signal it was a guess, so a live claude-gpt fleet got reported
# as "no coordinator (torn down...)" against the wrong (econ) app). This wraps
# fc_arm_from_label without touching it: $ARM env (normalized via the SAME
# legacy-alias mapping as fc_default_app: claude->claude-open,
# claude-real->claude-native) takes highest precedence when set; otherwise it
# falls back to fc_arm_from_label, and ALSO records whether the label actually
# contained a recognized arm token ("label") or the result is a blind
# fallthrough guess ("guess"), so a caller can warn on a guess instead of
# asserting the fleet is gone.
#
# Sets FC_RESOLVED_ARM + FC_ARM_INFERRED + FC_ARM_CONFLICT as GLOBALS rather
# than echoing — call it directly (`fc_resolve_arm "$lbl" "$bench"`), NOT via
# `$(...)`: a command substitution runs in a subshell, so a global set inside
# it (the whole point of FC_ARM_INFERRED/FC_ARM_CONFLICT) would silently
# vanish once the subshell exits. ──
FC_RESOLVED_ARM=""
FC_ARM_INFERRED=""
FC_ARM_CONFLICT=""
fc_resolve_arm() {  # <label> [bench] -> sets FC_RESOLVED_ARM, FC_ARM_INFERRED, FC_ARM_CONFLICT
  local lbl="$1" bench="${2:-verified}"
  FC_ARM_CONFLICT=""
  # Same label-stripping fc_arm_from_label does internally — used on BOTH
  # branches below: to tell whether a real arm token matched the label vs the
  # catch-all default fired (as before), and — when $ARM ALSO wins outright —
  # whether the label disagrees with it. A stale/leaked $ARM (commonly left
  # exported from an earlier claude-arm command) silently targeting the wrong
  # app is the same bug class this resolver exists to catch, so surface the
  # disagreement even though $ARM still wins (precedence unchanged).
  local tmp="$lbl"
  if [ "$bench" != "verified" ]; then
    tmp="${tmp%-graderr-*}"
    tmp="${tmp%-$bench}"
  fi
  local label_kind
  case "$tmp" in
    *-econ|*-claude|*-claude-*) label_kind="label" ;;
    *)                          label_kind="guess" ;;
  esac
  if [ -n "${ARM:-}" ]; then
    local arm="$ARM"
    case "$arm" in claude) arm="claude-open" ;; claude-real) arm="claude-native" ;; esac
    FC_ARM_INFERRED="env"
    FC_RESOLVED_ARM="$arm"
    if [ "$label_kind" = "label" ]; then
      local label_arm; label_arm="$(fc_arm_from_label "$lbl" "$bench")"
      [ "$label_arm" != "$arm" ] && FC_ARM_CONFLICT="$label_arm"
    fi
    return
  fi
  FC_ARM_INFERRED="$label_kind"
  FC_RESOLVED_ARM="$(fc_arm_from_label "$lbl" "$bench")"
}

# ── machine ids for a fleet, optionally role-scoped (same query as run-distributed.sh fleet_ids) ──
# metadata is only in the --json view, so we parse that.
#
# EXIT CODES — a caller MUST be able to tell "fleet is gone" from "we could not ask":
#   0  query succeeded; stdout = matching machine ids (possibly NONE => fleet really is gone)
#   3  the fly API call FAILED (auth/5xx/timeout) => fleet state is UNKNOWN, not empty
# Previously this swallowed flyctl's stderr and returned empty on failure, so a fly
# control-plane outage (e.g. GraphQL 503) was indistinguishable from a torn-down
# fleet — and status.sh then asserted "torn down, or not prepared" about a live run.
# The last error text is left in $FC_LAST_ERR_FILE for the caller to surface.
FC_LAST_ERR_FILE="${TMPDIR:-/tmp}/fc-last-error.$$"
fc_machines() {  # <app> <label> [role]
  local app="$1" label="$2" role="${3:-}"
  local raw rc
  raw="$(flyctl machines list -a "$app" --json 2>"$FC_LAST_ERR_FILE")"; rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$raw" ]; then
    return 3
  fi
  printf '%s' "$raw" | "$PY_HOST" -c '
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

# Propagates fc_machines' exit code (3 = fly API unreachable => state UNKNOWN).
# Deliberately NOT piped into `head` directly: a pipeline's exit status is the LAST
# command's, which would mask the API-failure code behind head's success.
fc_coord() {  # <app> <label>
  local out rc
  out="$(fc_machines "$1" "$2" coordinator)"; rc=$?
  [ "$rc" -ne 0 ] && return "$rc"
  printf '%s\n' "$out" | head -1
}

# Human-readable reason for the last fc_machines failure (empty if none).
fc_last_error() {
  [ -f "$FC_LAST_ERR_FILE" ] || return 0
  tr '\n' ' ' <"$FC_LAST_ERR_FILE" | sed 's/  */ /g' | cut -c1-200
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
