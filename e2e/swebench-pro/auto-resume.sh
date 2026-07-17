#!/usr/bin/env bash
# Drive mirror-sweap-images.sh to COMPLETION across Docker Hub's source pull
# limit. One `./mirror-sweap-images.sh` pass copies as many units as the
# refilled pull bucket allows, then the rest 429 ("TOOMANYREQUESTS"). This
# wrapper just re-runs the (resumable) mirror every SLEEP seconds until the
# destination has every source unit -- hands-off, safe to leave running.
# Dataset-aware (DATASET=pro default | verified), same as mirror-sweap-images.sh:
# it just computes the right src/dst TOTAL for whichever shape is selected and
# passes DATASET straight through to the mirror script it drives.
#
# Usage:
#   cd e2e/swebench-pro
#   docker login -u 51jaswanth15            # Read & Write PAT (once)
#   nohup ./auto-resume.sh >> mirror-logs/auto-resume.out 2>&1 &   # pro (default)
#   DATASET=verified nohup ./auto-resume.sh >> mirror-logs/auto-resume.out 2>&1 &
#
# Env vars:
#   DATASET      'pro' (default) or 'verified' -- passed through to the mirror script
#   SLEEP        seconds between passes (default 3700 -- just over the 1h window
#                so the pull bucket fully refills before the next pass)
#   MAX_PASSES   safety cap on passes (default 12)
#   SRC_REPO / DST_REPO   same defaults as mirror-sweap-images.sh (dataset-dependent)
# All other mirror knobs (CONCURRENCY, RETRIES, VERIFIED_*, ...) pass straight through the env.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; cd "$HERE"
SLEEP="${SLEEP:-3700}"
MAX_PASSES="${MAX_PASSES:-12}"
DATASET="${DATASET:-pro}"
case "$DATASET" in
  pro|verified) : ;;
  * ) echo "[auto-resume] DATASET must be 'pro' or 'verified' (got '$DATASET')" >&2; exit 2 ;;
esac
if [ "$DATASET" = "verified" ]; then
  SRC_REPO="${SRC_REPO:-swebench}"
  DST_REPO="${DST_REPO:-51jaswanth15}"
  VERIFIED_HF_DATASET="${VERIFIED_HF_DATASET:-princeton-nlp/SWE-bench_Verified}"
  VERIFIED_HF_SPLIT="${VERIFIED_HF_SPLIT:-test}"
else
  SRC_REPO="${SRC_REPO:-jefzda/sweap-images}"
  DST_REPO="${DST_REPO:-51jaswanth15/sweap-images}"
fi
export DATASET SRC_REPO DST_REPO   # inherited by ./mirror-sweap-images.sh below
mkdir -p mirror-logs
log() { printf '[auto-resume] %s %s\n' "$(date +%H:%M:%S)" "$*"; }

# -- total source units: pro = tag count of the single shared SRC_REPO;
# verified = row count of the HF Verified split (no swebench name computation
# needed here -- that only matters for the per-unit diff mirror-sweap-images.sh
# itself does).
src_total() {
  if [ "$DATASET" = "verified" ]; then
    python3 - "$VERIFIED_HF_DATASET" "$VERIFIED_HF_SPLIT" <<'PY'
import sys
from datasets import load_dataset
print(len(load_dataset(sys.argv[1], split=sys.argv[2])))
PY
  else
    python3 - "$SRC_REPO" <<'PY'
import sys, json, urllib.request
repo = sys.argv[1]
url = f"https://hub.docker.com/v2/repositories/{repo}/tags/?page_size=100"
n = 0
while url:
    d = json.load(urllib.request.urlopen(url, timeout=60))
    n += len(d.get("results", [])); url = d.get("next")
print(n)
PY
  fi
}

# -- dest units already mirrored: pro = tag count of the single shared
# DST_REPO (crane ls); verified = repo count under the DST_REPO namespace
# whose name starts with "sweb.eval." (crane has no "list repos in a
# namespace" verb, so this uses the same Hub web API call mirror-sweap-images.sh
# uses for its verified dest diff).
dst_have() {
  if [ "$DATASET" = "verified" ]; then
    python3 - "$DST_REPO" <<'PY'
import sys, json, urllib.request
namespace = sys.argv[1]
url = f"https://hub.docker.com/v2/repositories/{namespace}/?page_size=100"
n = 0
while url:
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            d = json.load(r)
    except Exception:
        break
    for repo in d.get("results", []):
        name = repo.get("name")
        if name and name.startswith("sweb.eval."):
            n += 1
    url = d.get("next")
print(n)
PY
  else
    crane ls "$DST_REPO" 2>/dev/null | grep -c . || true
  fi
}

SRC_N="$(src_total)"; SRC_N="${SRC_N:-0}"
log "target: full sync of $DST_REPO ($SRC_N source unit(s), DATASET=$DATASET)"

pass=1
while [ "$pass" -le "$MAX_PASSES" ]; do
  have="$(dst_have)"; have="${have:-0}"
  miss=$(( SRC_N - have )); [ "$miss" -lt 0 ] && miss=0
  log "pass $pass/$MAX_PASSES: $have/$SRC_N mirrored, $miss missing"
  if [ "$miss" -le 0 ]; then log "COMPLETE: $have/$SRC_N mirrored."; exit 0; fi

  ./mirror-sweap-images.sh || true          # resumable; exits 1 while any remain

  after="$(dst_have)"; after="${after:-0}"
  log "pass $pass added $(( after - have )); now $after/$SRC_N"
  if [ "$after" -ge "$SRC_N" ]; then log "COMPLETE: $after/$SRC_N mirrored."; exit 0; fi

  log "sleeping ${SLEEP}s for the pull bucket to refill..."
  sleep "$SLEEP"
  pass=$(( pass + 1 ))
done
log "stopped after $MAX_PASSES passes ($(dst_have)/$SRC_N). Re-run to continue."
exit 1
