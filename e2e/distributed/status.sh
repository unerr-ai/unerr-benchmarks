#!/usr/bin/env bash
# status.sh — at-a-glance status of a LIVE distributed run: for each fleet it
# curls the coordinator's /status and prints one line — armed?, workers checked
# in, queue counts (pending/leased/done/dead/failed), and resolved/total. Run it
# any time during a run to see where every combo stands; --watch turns it into a
# live monitor. Read-only: never touches the fleet, never tears anything down.
#
# Fleet set (pick one; default = the run you most recently launched):
#   (no args)            the newest out/bench-*/manifest.tsv (last bench.sh matrix)
#   --matrix <id>        that matrix's fleets (out/bench-<id>/manifest.tsv)
#   --manifest <path>    an explicit manifest.tsv
#   <LABEL> [APP]        one fleet by label (APP inferred: arm from the label's
#                        arm token — econ/claude-native/claude-gpt/claude-open/
#                        legacy claude(-real) — benchmark from $BENCHMARK else
#                        the label's -pro/-terminal/-live_verified/-lite suffix,
#                        else verified)
#
# Options:
#   --watch [secs]   re-print every <secs> (default 15) until Ctrl-C — the monitor
#   --instances      also print the per-instance table (id, status, resolved, worker,
#                    attempt count). Combined with --cost, each row is ALSO enriched
#                    with that task's OWN $ cost, turns, in/out tokens, and a per-tier
#                    (conductor/oracle/reasoner/executor) breakdown — or, when the cost
#                    record carries a by_model dict (any claude-<mix> gateway arm), a
#                    per-model breakdown (real model name + cache-hit-rate) instead —
#                    i.e. which model/sub-agent tier spent what on THAT instance, for
#                    live-run monitoring at task granularity (not just the fleet aggregate).
#                    Reuses the same --cost meta dump — no second coordinator
#                    round-trip. A task with no cost record yet (still running)
#                    prints "-"; an unbilled claude-native row prints "n/a" (never a
#                    fake $0). --instances alone and --cost alone are unaffected.
#   --cost           add cost + per-tier (conductor/oracle/reasoner/executor) token·turn·$
#                    breakdown, read live from the coordinator queue.db per-instance meta
#                    (econ: telemetry/tier_cost_db; claude: litellm_spend_logs; claude-real:
#                    meta.cost.source=="claude-native" — real Anthropic $, broken out on
#                    its own line below the fleet total, never counted as litellm spend).
#                    When the cost record ALSO carries a by_model dict (any claude-<mix>
#                    gateway arm), that REPLACES the per-tier lines with a per-model
#                    breakdown instead — real model name (e.g. gpt-5.6-terra[conductor]),
#                    $, in/out tokens, requests, and a cache-hit-rate note — so per-model
#                    attribution (= per-sub-agent attribution on gateway arms) survives
#                    even for a model litellm_cost.py's TIER_BY_MODEL hasn't mapped yet,
#                    instead of collapsing into one misleading "other" tier bucket. econ
#                    (no by_model) keeps the per-tier rendering, byte-identical.
#                    Fleet total + a MATRIX TOTAL (cost across all runs in view). Only
#                    completed instances have cost (it accrues on completion).
#   --json           dump each fleet's raw /status JSON instead of the summary line
#
# Examples:
#   ./status.sh                                  # newest matrix, one snapshot
#   ./status.sh --matrix smk-e --watch           # live-monitor that matrix every 15s
#   ./status.sh smk-e-econ --instances           # one fleet + its per-instance rows
#   ./status.sh --matrix smk-e --cost            # + total $ and per-tier token/turn/cost
#   ./status.sh smk-e-econ --instances --cost    # per-instance rows enriched w/ per-task
#                                                 # $/turns/tokens/tier, plus the fleet total
#   ./status.sh mini-claude swebench-dist-claude-verif   # explicit APP overrides inference
#   BENCHMARK=pro ./status.sh <LABEL>            # non-verified fleet whose label lacks the suffix
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=tools/fleet-common.sh
source "$HERE/tools/fleet-common.sh"

MATRIX=""; MANIFEST=""; WATCH=0; WATCH_SECS=15; INSTANCES=0; JSON=0; COST=0
POS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --matrix)    MATRIX="${2:?--matrix needs a value}"; shift 2 ;;
    --manifest)  MANIFEST="${2:?--manifest needs a value}"; shift 2 ;;
    --watch)     WATCH=1; shift
                 case "${1:-}" in ''|--*|-*) : ;; *[!0-9]*) : ;; *) WATCH_SECS="$1"; shift ;; esac ;;
    --instances) INSTANCES=1; shift ;;
    --cost)      COST=1; shift ;;
    --json)      JSON=1; shift ;;
    -h|--help)   sed -n '2,46p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)          echo "ERROR: unknown flag '$1'" >&2; exit 2 ;;
    *)           POS+=("$1"); shift ;;
  esac
done

fc_fly_token || exit 1

# ── benchmark key for an APP-less <LABEL>: $BENCHMARK env wins (for a non-verified
# fleet whose label carries no suffix); else the label's own -<benchmark> suffix,
# also matching a grade-rerun tail (<base>-<benchmark>-graderr-<time>); else verified. ──
_bench_from_label() {  # <label>
  local lbl="$1"
  if [ -n "${BENCHMARK:-}" ]; then echo "$BENCHMARK"; return; fi
  case "$lbl" in
    *-pro|*-pro-graderr-*)                     echo pro ;;
    *-terminal|*-terminal-graderr-*)           echo terminal ;;
    *-live_verified|*-live_verified-graderr-*) echo live_verified ;;
    *-lite|*-lite-graderr-*)                   echo lite ;;
    *)                                         echo verified ;;
  esac
}

# Resolve the fleet set into parallel arrays labels[]/apps[]/combos[].
labels=(); apps=(); combos=()
[ -n "$MATRIX" ] && [ -z "$MANIFEST" ] && MANIFEST="$HERE/out/bench-$MATRIX/manifest.tsv"
if [ "${#POS[@]}" -ge 1 ]; then
  # explicit LABEL [APP] — infer app from the label's arm token (fc_arm_from_label:
  # strips the benchmark/graderr suffix then matches the known arm set
  # most-specific-first, so claude-gpt/claude-open/claude-native each resolve to
  # their OWN app instead of collapsing into a shared "claude" fallback) +
  # benchmark suffix if not given.
  lbl="${POS[0]}"; app="${POS[1]:-}"
  if [ -z "$app" ]; then
    bench="$(_bench_from_label "$lbl")"
    arm="$(fc_arm_from_label "$lbl" "$bench")"
    app="$(fc_default_app "$arm" "$bench")"
  fi
  labels+=("$lbl"); apps+=("$app"); combos+=("$lbl")
else
  [ -n "$MANIFEST" ] || MANIFEST="$(fc_newest_manifest)"
  [ -n "$MANIFEST" ] && [ -f "$MANIFEST" ] || {
    echo "ERROR: no fleet given and no manifest found." >&2
    echo "       pass <LABEL> [APP], --matrix <id>, or run a bench.sh matrix first." >&2
    exit 1; }
  echo "==> fleets from $MANIFEST" >&2
  while IFS=$'\t' read -r a b l ap; do
    labels+=("$l"); apps+=("$ap"); combos+=("${a}:${b}")
  done < <(fc_read_manifest "$MANIFEST")
fi
[ "${#labels[@]}" -gt 0 ] || { echo "ERROR: no fleets to query" >&2; exit 1; }

# Render one fleet's status line (or JSON / instance table) from its /status JSON.
render_one() {  # <combo> <label> <app>
  local combo="$1" label="$2" app="$3"
  local coord rc; coord="$(fc_coord "$app" "$label")"; rc=$?
  if [ "$rc" -eq 3 ]; then
    # The fly API call itself failed — we could not ASK. Saying "torn down" here
    # would be a false statement about a possibly-healthy fleet (seen live during
    # a fly GraphQL 503 while the run was progressing normally over 6PN).
    printf '  %-26s %-22s  UNKNOWN — fly API unreachable (%s)\n' \
      "$combo" "$label" "$(fc_last_error)"
    return
  fi
  if [ -z "$coord" ]; then
    printf '  %-26s %-22s  no coordinator (torn down, or not prepared on %s)\n' "$combo" "$label" "$app"
    return
  fi
  local js; js="$(fc_status "$app" "$coord")"
  if [ -z "$js" ]; then
    printf '  %-26s %-22s  coord=%s  unreachable (booting? ssh not ready?)\n' "$combo" "$label" "$coord"
    return
  fi
  if [ "$JSON" = 1 ]; then
    printf '── %s  (label=%s app=%s coord=%s) ─────\n' "$combo" "$label" "$app" "$coord"
    printf '%s\n' "$js" | "$PY_HOST" -m json.tool 2>/dev/null || printf '%s\n' "$js"
    return
  fi
  # --cost: pull per-instance meta (cost/turns/by_tier) from the coordinator queue.db
  # into a temp file the renderer reads (kept off argv so a 500-instance dump can't
  # blow ARG_MAX). Empty file when not requested.
  local metaf=""
  if [ "$COST" = 1 ]; then
    metaf="$(mktemp "${TMPDIR:-/tmp}/status-meta.XXXXXX")"
    fc_meta_rows "$app" "$coord" >"$metaf" 2>/dev/null || true
  fi
  # /status JSON + fields as argv (script on stdin via `-`), same pattern as download-all.sh.
  "$PY_HOST" - "$js" "$combo" "$label" "$coord" "$INSTANCES" "$COST" "$metaf" <<'PY'
import sys, json
js, combo, label, coord = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
show, want_cost, metapath = sys.argv[5] == "1", sys.argv[6] == "1", sys.argv[7]
try:
    d = json.loads(js)
except Exception:
    print(f"  {combo:<26} {label:<22}  coord={coord}  /status not JSON")
    raise SystemExit
c = d.get("counts") or {}
armed = "armed" if d.get("armed") else "WARM "   # WARM = prepared, not yet armed
ws = len(d.get("workers_seen") or [])
res, tot = d.get("resolved") or 0, d.get("total") or 0
insts = d.get("instances") or []
# retries: reatt = total re-attempts so far (attempt_count>1); f=failed rows are
# "up for retry" at end-of-run (fail-rerun budget), x=dead = permanently failed.
reatt = sum(max(0, (it.get("attempt_count") or 0) - 1) for it in insts)
pct = f"{(100 * res // tot)}%" if tot else "?"
q = "p{p} l{l} d{d} x{x} f{f}".format(
    p=c.get("pending", 0), l=c.get("leased", 0), d=c.get("done", 0),
    x=c.get("dead", 0), f=c.get("failed", 0))
retry = f"reatt={reatt} up4retry={c.get('failed', 0)} dead={c.get('dead', 0)}"
print(f"  {combo:<26} {label:<22}  {armed}  w={ws}  {q:<22}  {retry:<28}  resolved={res}/{tot} ({pct})")
# ── per-instance meta, keyed by instance_id — loaded ONCE from the metaf dump
# fc_meta_rows already fetched (no second coordinator round-trip). Feeds BOTH the
# --instances per-row enrichment right below AND the fleet-wide aggregate further
# down, so --cost alone still computes exactly the same totals it always has.
def _kfmt(n):
    n = n or 0
    return f"{n/1_000_000:.2f}M" if n >= 1_000_000 else f"{n/1000:.0f}k"

TIER_ORDER = ["conductor", "oracle", "reasoner", "executor", "fast", "other"]

def _model_label(model, tier):
    # "other"/absent tier is the fallback bucket for a model TIER_BY_MODEL doesn't
    # know about yet (litellm_cost.py) — tagging every row "[other]" would be noise,
    # so only show the tier when it's a real, known bucket.
    return f"{model}[{tier}]" if tier and tier != "other" else model

def _model_part(model, d):
    # One model's $/tokens/requests segment, styled like the by-tier line; appends a
    # cache-hit-rate note whenever the field is present — on a gateway arm cached
    # input can dwarf fresh input (~10x seen live on claude-gpt), so it's a
    # first-order cost driver, not a footnote.
    label = _model_label(model, d.get("tier"))
    usd = float(d.get("usd") or 0)
    calls = int(d.get("requests") or 0)
    seg = f"{label}=${usd:.4f}(in={_kfmt(d.get('in_tokens'))} out={_kfmt(d.get('out_tokens'))} x{calls}"
    rate = d.get("cache_hit_rate")
    cached_in = d.get("cached_in") or 0
    if rate is not None or cached_in:
        seg += f" cache={100 * (rate or 0):.0f}%"
    return seg + ")"

meta_rows = []
meta_by_id = {}
if want_cost and metapath:
    try:
        meta_rows = json.load(open(metapath))
    except Exception:
        meta_rows = []
    for row in meta_rows:
        if isinstance(row, list) and len(row) >= 4:
            meta_by_id[row[0]] = row[3]

def _row_cost(meta):
    # meta is one instance's meta_json blob, or None (not completed yet). Returns
    # None when there's no record at all (still running); else a dict whose usd is
    # None when a record exists but nothing has posted yet, so the caller renders
    # "n/a"/"-" instead of a fake $0.
    if not isinstance(meta, dict):
        return None
    tel = meta.get("telemetry") or {}
    tcd = meta.get("tier_cost_db") or {}
    cst = meta.get("cost") or {}
    cst = cst if isinstance(cst, dict) else {}
    is_native = cst.get("source") == "claude-native"
    # same authoritative-usd precedence as the fleet aggregate below: claude
    # litellm cost -> econ sqlite tier_cost_db -> telemetry stream estimate.
    u = cst.get("usd")
    if u is None:
        u = tcd.get("usd")
    if u is None:
        u = tel.get("usd")
    bt = cst.get("by_tier") or tcd.get("by_tier") or tel.get("by_tier") or {}
    bm = cst.get("by_model") or {}  # gateway arms (claude-<mix>) only — see litellm_cost.py
    return dict(
        usd=(float(u) if u is not None else None),
        turns=int(tel.get("turns") or 0),
        tin=int(tel.get("in_tokens") or cst.get("in_tokens") or 0),
        tout=int(tel.get("out_tokens") or cst.get("out_tokens") or 0),
        by_tier=(bt if isinstance(bt, dict) else {}),
        by_model=(bm if isinstance(bm, dict) else {}),
        is_native=is_native,
    )

if show:
    for it in insts:
        r = "R" if it.get("resolved") else "."
        iid = it.get("instance_id", "?")
        st = it.get("status", "?")
        wk = it.get("worker_id") or "-"
        att = it.get("attempt_count", 0)
        rt = f" retried×{att-1}" if att and att > 1 else ""
        line = f"      {iid:<34} {st:<8} {r}  worker={wk}  att={att}{rt}"
        tier_line = ""
        if want_cost:
            info = _row_cost(meta_by_id.get(iid))
            if info is None:
                line += "  cost=-  turns=-  in=- out=-"
            elif info["usd"] is None:
                # no $ posted yet: native rows say "n/a" (never litellm spend),
                # anything else falls back to "-" — neither ever reads as $0.
                line += f"  cost={'n/a' if info['is_native'] else '-'}  turns=-  in=- out=-"
            else:
                line += (f"  cost=${info['usd']:.4f}  turns={info['turns']}"
                         f"  in={_kfmt(info['tin'])} out={_kfmt(info['tout'])}")
                if info["by_model"]:
                    # Per-model attribution (gateway arms) takes precedence over
                    # by-tier — the model name is always ground-truth, unlike a
                    # tier that may still be unmapped ("other") for a new model.
                    ordered_models = sorted(
                        info["by_model"].items(), key=lambda kv: -float(kv[1].get("usd") or 0))
                    parts = [_model_part(m, d) for m, d in ordered_models if isinstance(d, dict)]
                    if parts:
                        tier_line = f"          by-model: {'  '.join(parts)}"
                elif info["by_tier"]:
                    ordered = [t for t in TIER_ORDER if t in info["by_tier"]] \
                        + [t for t in info["by_tier"] if t not in TIER_ORDER]
                    parts = []
                    for tier in ordered:
                        t = info["by_tier"].get(tier) or {}
                        if not isinstance(t, dict):
                            continue
                        tusd = float(t.get("usd") or 0)
                        calls = int(t.get("requests") or t.get("messages") or 0)
                        parts.append(f"{tier}=${tusd:.4f}(in={_kfmt(t.get('in_tokens'))} "
                                      f"out={_kfmt(t.get('out_tokens'))} x{calls})")
                    if parts:
                        tier_line = f"          by-tier: {'  '.join(parts)}"
        print(line)
        if tier_line:
            print(tier_line)

# ── cost + per-tier breakdown, fleet-wide — from the SAME meta_rows loaded above ──
fleet_usd = 0.0
if want_cost and metapath:
    rows = meta_rows
    tot_turns = tot_in = tot_out = priced_insts = 0
    tiers = {}   # tier -> {usd,in,out,calls,insts}
    models = {}  # bare model -> {tier,usd,tin,tout,cached_in,calls,insts} — populated only
                 # for gateway arms (claude-<mix>) whose cost record carries by_model;
                 # stays empty for econ, so the tier printout below is untouched there.
    # claude-native (claude-real arm): real-Anthropic $, meta.cost.source=="claude-native".
    # Folded into fleet_usd/priced_insts like any other row, but tracked separately too
    # so the line below can label it distinctly — NEVER as litellm spend — and so an
    # untracked native row (usd None) shows as "n/a" rather than silently reading as $0.
    native_usd = 0.0
    native_priced = native_na = 0
    for row in rows:
        meta = row[3] if isinstance(row, list) and len(row) >= 4 else None
        if not isinstance(meta, dict):
            continue
        tel = meta.get("telemetry") or {}
        tcd = meta.get("tier_cost_db") or {}
        cst = meta.get("cost") or {}
        cst = cst if isinstance(cst, dict) else {}
        is_native = cst.get("source") == "claude-native"
        # authoritative usd: prefer claude litellm cost, else econ sqlite, else stream
        u = cst.get("usd")
        if u is None:
            u = tcd.get("usd")
        if u is None:
            u = tel.get("usd")
        if u is None:
            if is_native:
                native_na += 1
            continue                     # no cost record yet (e.g. still running)
        fleet_usd += float(u or 0)
        priced_insts += 1
        if is_native:
            native_usd += float(u or 0)
            native_priced += 1
        tot_turns += int(tel.get("turns") or 0)
        tot_in += int(tel.get("in_tokens") or cst.get("in_tokens") or 0)
        tot_out += int(tel.get("out_tokens") or cst.get("out_tokens") or 0)
        # by_model (gateway arms — real model name, e.g. gpt-5.6-terra) takes
        # precedence over by_tier: a claude cost.by_tier can still collapse onto a
        # single "other" bucket until TIER_BY_MODEL knows the model (litellm_cost.py).
        bm = cst.get("by_model") or {}
        if isinstance(bm, dict) and bm:
            for model, m in bm.items():
                if not isinstance(m, dict):
                    continue
                a = models.setdefault(model, dict(
                    tier=m.get("tier") or "other", usd=0.0, tin=0, tout=0,
                    cached_in=0, calls=0, insts=0))
                a["usd"] += float(m.get("usd") or 0)
                a["tin"] += int(m.get("in_tokens") or 0)
                a["tout"] += int(m.get("out_tokens") or 0)
                a["cached_in"] += int(m.get("cached_in") or 0)
                a["calls"] += int(m.get("requests") or 0)
                a["insts"] += 1
            continue
        # by_tier: claude cost.by_tier | econ tier_cost_db.by_tier | telemetry.by_tier
        bt = cst.get("by_tier") or tcd.get("by_tier") or tel.get("by_tier") or {}
        if isinstance(bt, dict):
            for tier, t in bt.items():
                if not isinstance(t, dict):
                    continue
                a = tiers.setdefault(tier, dict(usd=0.0, tin=0, tout=0, calls=0, insts=0))
                a["usd"] += float(t.get("usd") or 0)
                a["tin"] += int(t.get("in_tokens") or 0)
                a["tout"] += int(t.get("out_tokens") or 0)
                a["calls"] += int(t.get("requests") or t.get("messages") or 0)
                a["insts"] += 1
    print(f"      cost=${fleet_usd:.4f}  turns={tot_turns}  in={_kfmt(tot_in)} out={_kfmt(tot_out)}  (priced {priced_insts} inst)")
    if native_priced or native_na:
        native_str = f"${native_usd:.4f}" if native_priced else "n/a (claude-native)"
        na_note = f"  ({native_na} n/a)" if native_na else ""
        print(f"        of which claude-native (Anthropic $, NOT litellm spend): {native_str}  ×{native_priced}{na_note}")
    if models:
        # Per-model attribution (gateway arms) replaces the tier breakdown, which
        # would otherwise dump every dollar into one "other" bucket until
        # TIER_BY_MODEL knows the model (litellm_cost.py::tier_of).
        for model, a in sorted(models.items(), key=lambda kv: -kv[1]["usd"]):
            label = _model_label(model, a["tier"])
            share = (100 * a["usd"] / fleet_usd) if fleet_usd else 0
            denom = a["tin"] + a["cached_in"]
            cache_note = f"  cache={100 * a['cached_in'] / denom:.0f}%" if denom else ""
            print(f"        {label:<26} ${a['usd']:.4f} ({share:4.0f}%)  in={_kfmt(a['tin'])} out={_kfmt(a['tout'])}  calls={a['calls']}  ×{a['insts']}{cache_note}")
    else:
        ordered = [t for t in TIER_ORDER if t in tiers] + [t for t in tiers if t not in TIER_ORDER]
        for tier in ordered:
            a = tiers[tier]
            share = (100 * a["usd"] / fleet_usd) if fleet_usd else 0
            print(f"        {tier:<10} ${a['usd']:.4f} ({share:4.0f}%)  in={_kfmt(a['tin'])} out={_kfmt(a['tout'])}  calls={a['calls']}  ×{a['insts']}")

# machine-readable tail for the matrix roll-up (grade % + total cost), filtered out in snapshot().
print(f"__ROLLUP__\t{res}\t{tot}\t{reatt}\t{c.get('failed',0)}\t{c.get('dead',0)}\t{fleet_usd:.6f}")
PY
  [ -n "$metaf" ] && rm -f "$metaf"
  return 0
}

snapshot() {
  echo "──────── run status  ($(date '+%H:%M:%S'))  ${#labels[@]} fleet(s) ────────"
  echo "  legend: armed|WARM  w=workers_seen  p/l/d/x/f=pending/leased/done/dead/failed  up4retry=failed(reruns at drain)  (NN%)=grade"
  # Pipe every fleet's render through a filter that strips the __ROLLUP__ sentinel
  # lines and folds them into one MATRIX TOTAL (grade % across all fleets in view).
  {
    local i=0
    while [ "$i" -lt "${#labels[@]}" ]; do
      render_one "${combos[$i]}" "${labels[$i]}" "${apps[$i]}"
      i=$(( i + 1 ))
    done
  } | "$PY_HOST" -c '
import sys
R = T = RE = F = DE = nf = 0
USD = 0.0
for line in sys.stdin:
    if line.startswith("__ROLLUP__\t"):
        p = line.rstrip("\n").split("\t")
        R += int(p[1]); T += int(p[2]); RE += int(p[3]); F += int(p[4]); DE += int(p[5]); nf += 1
        if len(p) > 6:
            USD += float(p[6] or 0)
    else:
        sys.stdout.write(line)
if nf > 1:
    mt = "MATRIX TOTAL"
    pct = f"{100 * R // T}%" if T else "?"
    cost = f"  cost=${USD:.4f} (all runs)" if USD > 0 else ""
    print(f"  {mt:<26} {nf} fleet(s)             resolved={R}/{T} ({pct})  reatt={RE} up4retry={F} dead={DE}{cost}")
'
}

if [ "$WATCH" = 1 ]; then
  echo "==> watching every ${WATCH_SECS}s — Ctrl-C to stop" >&2
  while true; do
    clear 2>/dev/null || true
    snapshot
    sleep "$WATCH_SECS" || break
  done
else
  snapshot
fi
