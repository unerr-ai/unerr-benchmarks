#!/usr/bin/env bash
# Tiered codex ±unerr benchmark launcher — routes each tier to the right backend.
#
#   smoke  (1  instance)  -> local-docker      full resolve + cost bill (on your laptop)
#   pilot  (5  instances) -> local OR fly      local = full bill ; fly = localization A/B
#   mini   (50 instances) -> fly               localization A/B at scale (file-naming)
#
# WHY the split: local-docker produces the real resolve-rate + $ bill but needs Docker
# + ~30GB on your machine; fly runs off-laptop on an ephemeral machine but today does
# the *localization* A/B only (codex names the buggy file — no patch-apply/grade), on
# SWE-bench Lite. So the FULL-BILL mini stays on the laptop on purpose:
#
#   ./run-tier.sh mini --backend local        # full resolve+cost mini (Verified, 50) on your laptop
#
# Usage:
#   ./run-tier.sh smoke                        # laptop, full bill, 1 instance
#   ./run-tier.sh pilot                        # laptop, full bill, 5   (default backend=local)
#   ./run-tier.sh pilot --backend fly          # fly, localization A/B, 5
#   ./run-tier.sh mini                          # fly, localization A/B, 50 (default backend=fly)
#   ./run-tier.sh <tier> --dry-run             # print the resolved command, run nothing
#
# Env passthrough: SELECT (override the fly repo:n picks), plus anything the underlying
# backend reads (OPENAI_API_KEY, APP/REGION/MEM/CPUS for fly, --dataset etc. for local).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LOCAL="$HERE/local-docker"
FLY="$HERE/fly-remote"

TIER="${1:-}"; [ -n "$TIER" ] && shift || true
BACKEND=""; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --backend) BACKEND="${2:-}"; shift 2 ;;
    --backend=*) BACKEND="${1#*=}"; shift ;;
    --dry-run) DRY=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# tier -> size, default backend, allowed backends, fly repo:n picks summing to size
case "$TIER" in
  smoke) SIZE=1;  DEFAULT_BACKEND=local; ALLOWED="local";     FLY_SELECT="" ;;
  pilot) SIZE=5;  DEFAULT_BACKEND=local; ALLOWED="local fly"; FLY_SELECT="requests:3,flask:2" ;;
  mini)  SIZE=50; DEFAULT_BACKEND=fly;   ALLOWED="fly local"; FLY_SELECT="django:20,sympy:12,scikit-learn:8,pytest:6,requests:4" ;;
  *)
    cat >&2 <<EOF
usage: $(basename "$0") <smoke|pilot|mini> [--backend local|fly] [--dry-run]
  smoke   1 instance  -> local-docker            full resolve + cost bill
  pilot   5 instances -> local (bill) | fly (localization A/B)
  mini   50 instances -> fly (localization A/B) | local (full bill)
EOF
    exit 2 ;;
esac
BACKEND="${BACKEND:-$DEFAULT_BACKEND}"

# enforce the routing policy
case " $ALLOWED " in
  *" $BACKEND "*) : ;;
  *) echo "tier '$TIER' does not allow backend '$BACKEND' (allowed: $ALLOWED)" >&2; exit 2 ;;
esac

if [ "$BACKEND" = "local" ]; then
  echo "== tier=$TIER backend=local size=$SIZE -> full resolve + cost bill ==" >&2
  CMD=(python3 "$LOCAL/run-benchmark.py" --slice "0:$SIZE" --instances "$SIZE" --mode both)
else
  SEL="${SELECT:-$FLY_SELECT}"
  echo "== tier=$TIER backend=fly size=$SIZE -> localization A/B (NOT the full resolve bill) ==" >&2
  echo "   select=$SEL  (SWE-bench Lite; codex names the buggy file, scored vs gold)" >&2
  CMD=(env SELECT="$SEL" LIMIT="$SIZE" "$FLY/run.sh")
fi

echo "+ ${CMD[*]}" >&2
[ "$DRY" = "1" ] && { echo "(dry-run: nothing executed)" >&2; exit 0; }
exec "${CMD[@]}"
