#!/usr/bin/env bash
# e2e/econ/smoke.sh — local smoke: run the LATEST built econ on ONE tiny task and
# verify the whole telemetry pipeline works end-to-end BEFORE spending on a real
# SWE-bench mini run. Zero SWE-bench Docker needed — this proves the plumbing:
#   1. econ runs headless and actually edits the file  (agent works)
#   2. it emits a `cost_breakdown` event               (per-model feature live)
#   3. econ-telemetry.py yields sane usd + by_tier      (stream parser works)
#   4. opencode.db populates; econ-tier-cost.py yields   (SQLite per-tier works,
#      per-tier tokens INCLUDING the executor subagent)   incl. executor volume)
#   5. reports WHICH tiers actually fired
#
# Uses a REAL LiteLLM-gateway call (a few cents). Key: $LITELLM_API_KEY, else the
# gitignored e2e/econ/.env.local.
#
# Usage: bash smoke.sh   [ECON_TIMEOUT=600] [KEEP=1 to keep the workdir]

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ECON_REPO="${ECON_REPO:-$SCRIPT_DIR/../../../econ-coding-agent}"
ECON_TIMEOUT="${ECON_TIMEOUT:-600}"
OUT="$SCRIPT_DIR/results/smoke"
log() { printf '[smoke] %s\n' "$*" >&2; }
fail() { printf '[smoke] FAIL: %s\n' "$*" >&2; exit 1; }

# ── auth: shell env, else .env.local ──────────────────────────────────────────
if [ -z "${LITELLM_API_KEY:-}" ] && [ -f "$SCRIPT_DIR/.env.local" ]; then
  # shellcheck disable=SC1090
  LITELLM_API_KEY="$(grep -E '^LITELLM_API_KEY=' "$SCRIPT_DIR/.env.local" | head -1 | sed 's/^LITELLM_API_KEY=//; s/^["'"'"']//; s/["'"'"']$//')"
  export LITELLM_API_KEY
fi
[ -n "${LITELLM_API_KEY:-}" ] || fail "LITELLM_API_KEY not set (shell or .env.local)"
log "LITELLM_API_KEY: set (len ${#LITELLM_API_KEY})"

# ── resolve the built econ binary (PLATFORM-AWARE — dist holds every OS/arch) ──
if [ -z "${ECON_BIN:-}" ]; then
  _os="$(uname -s | tr '[:upper:]' '[:lower:]')"          # darwin / linux
  case "$(uname -m)" in arm64|aarch64) _arch=arm64 ;; x86_64|amd64) _arch=x64 ;; *) _arch="$(uname -m)" ;; esac
  ECON_BIN="$(find "$ECON_REPO/packages/opencode/dist" -type f -name opencode -path "*${_os}-${_arch}*" 2>/dev/null | head -1)"
fi
[ -n "$ECON_BIN" ] && [ -x "$ECON_BIN" ] || fail "no built econ binary for this platform under $ECON_REPO/packages/opencode/dist (run: cd $ECON_REPO && bun install && bun run --cwd packages/opencode build)"
_ver="$("$ECON_BIN" --version 2>/dev/null | head -1)"
[ -n "$_ver" ] || fail "econ binary $ECON_BIN did not run (--version empty) — wrong platform build?"
log "econ: $ECON_BIN ($_ver)"

# ── throwaway repo WITH econ's tiered config so routing activates ─────────────
# Config source: $ECON_CONFIG (a baked opencode.json — used in-container on fly),
# else the econ repo's opencode.json (local dev). Same for the optional .opencode/.
ECON_CONFIG="${ECON_CONFIG:-$ECON_REPO/opencode.json}"
ECON_PLUGINS="${ECON_PLUGINS:-$ECON_REPO/.opencode}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/econ-smoke.XXXXXX")"
cp "$ECON_CONFIG" "$WORK/opencode.json" 2>/dev/null || fail "no econ opencode.json at ECON_CONFIG=$ECON_CONFIG"
[ -d "$ECON_PLUGINS" ] && cp -R "$ECON_PLUGINS" "$WORK/.opencode"
cat > "$WORK/calc.py" <<'PY'
def add(a, b):
    return a - b  # BUG: should return a + b


def multiply(a, b):
    return a * b
PY
( cd "$WORK" && git init -q && git config user.email s@x.co && git config user.name smoke && git add -A && git commit -qm init )
log "workdir: $WORK (throwaway git repo + econ opencode.json)"

# ── run econ headless, pin a FRESH per-run session DB ─────────────────────────
mkdir -p "$OUT"; rm -f "$OUT/opencode.db" "$OUT/events.jsonl" "$OUT/err.txt"
export OPENCODE_DB="$OUT/opencode.db"
PROMPT="In calc.py the add(a, b) function returns a - b, which is a bug. Fix it so add returns a + b. Change nothing else."
log "running econ (timeout ${ECON_TIMEOUT}s)…"
t0=$(date +%s)
timeout "$ECON_TIMEOUT" "$ECON_BIN" run --format json --dir "$WORK" --dangerously-skip-permissions "$PROMPT" \
  > "$OUT/events.jsonl" 2> "$OUT/err.txt"
RC=$?
t1=$(date +%s)
log "econ exit=$RC wall=$((t1 - t0))s  events=$(wc -l < "$OUT/events.jsonl" | tr -d ' ') lines"
[ "$RC" -eq 124 ] && log "WARN: econ hit the ${ECON_TIMEOUT}s timeout"

# ── capture edit + sessionID, run both telemetry paths ────────────────────────
DIFF="$(cd "$WORK" && git diff)"
printf '%s\n' "$DIFF" > "$OUT/patch.diff"
SID="$(python3 -c "
import json,sys
for l in open('$OUT/events.jsonl'):
    l=l.strip()
    if not l: continue
    try: sid=json.loads(l).get('sessionID')
    except: continue
    if sid: print(sid); break
" 2>/dev/null)"
python3 "$SCRIPT_DIR/econ-telemetry.py" "$OUT/events.jsonl" > "$OUT/telemetry.json" 2>>"$OUT/err.txt" || echo '{}' > "$OUT/telemetry.json"
if [ -f "$OUT/opencode.db" ]; then
  python3 "$SCRIPT_DIR/econ-tier-cost.py" --db "$OUT/opencode.db" ${SID:+--session "$SID"} > "$OUT/tier-cost.json" 2>>"$OUT/err.txt" || echo '{}' > "$OUT/tier-cost.json"
else
  echo '{"source":"sqlite","error":"opencode.db not created"}' > "$OUT/tier-cost.json"
fi

# ── verdict ───────────────────────────────────────────────────────────────────
python3 - "$OUT/events.jsonl" "$OUT/telemetry.json" "$OUT/tier-cost.json" "$OUT/patch.diff" "$SID" "$RC" <<'PY'
import json, sys
events, telf, tierf, difff, sid, rc = sys.argv[1:7]
def load(p):
    try: return json.load(open(p))
    except Exception: return {}
tel, tier = load(telf), load(tierf)
diff = open(difff).read()
# detect the cost_breakdown event
saw_cb = False
for l in open(events):
    l=l.strip()
    if not l: continue
    try:
        if json.loads(l).get("type")=="cost_breakdown": saw_cb=True; break
    except Exception: pass

edited   = ("a + b" in diff) or ("a - b" not in diff and diff.strip()!="")
checks = []
checks.append(("econ edited calc.py (a-b → a+b)", edited))
checks.append(("cost_breakdown event emitted", saw_cb))
checks.append(("stream telemetry has cost", tel.get("usd",0) > 0 or tel.get("usd_source")=="cost_breakdown"))
checks.append(("stream by_tier populated", bool(tel.get("by_tier"))))
checks.append(("opencode.db read (per-tier)", tier.get("source")=="sqlite" and not tier.get("error")))
checks.append(("db by_tier populated", bool(tier.get("by_tier"))))

print("\n========================= SMOKE VERDICT =========================")
for name, ok in checks:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
print("-----------------------------------------------------------------")
print(f"  sessionID: {sid or '(none captured)'}   econ rc={rc}")
print(f"  stream: usd=${tel.get('usd',0):.6f} (source={tel.get('usd_source')}) "
      f"upstream=${tel.get('usd_upstream',0):.6f} turns={tel.get('turns')} "
      f"tools={tel.get('tool_calls')} graph={tel.get('graph_tool_calls')}")
st = tel.get("by_tier") or {}
print("  stream tiers fired: " + (", ".join(f"{k} ${v.get('usd',0):.6f}" for k,v in st.items()) or "(none)"))
dbt = tier.get("by_tier") or {}
print(f"  DB tiers (incl executor): " + (", ".join(
    f"{k} ${v.get('usd',0):.6f}/{v.get('in_tokens',0)+v.get('out_tokens',0)}tok" for k,v in dbt.items()) or "(none)"))
print(f"  DB total: usd=${tier.get('usd',0):.6f} tokens_in={tier.get('in_tokens',0)} sessions={tier.get('sessions',0)}")
hard = all(ok for name,ok in checks[:4])   # edit + cost_breakdown + stream cost + stream tier
allok = all(ok for _,ok in checks)
print("-----------------------------------------------------------------")
print("  RESULT:", "ALL GREEN — ready for the fly smoke" if allok else
      ("CORE GREEN (DB path needs a look)" if hard else "NOT READY — see FAILs above"))
print("=================================================================")
sys.exit(0 if hard else 1)
PY
VERDICT=$?

if [ "${KEEP:-0}" = "1" ]; then log "kept workdir: $WORK"; else rm -rf "$WORK"; fi
log "artifacts in $OUT/ (events.jsonl, telemetry.json, tier-cost.json, patch.diff, err.txt)"
exit "$VERDICT"
