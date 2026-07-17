#!/usr/bin/env bash
# Mirror SWE-bench eval images from their upstream PUBLIC Docker Hub source(s)
# into an OWN Docker Hub account, on demand -- run it whenever you want to
# (re)sync. It is idempotent and resumable: a re-run only copies what's
# missing on the destination, so an interrupted or rate-limited sync just
# needs another run. Shared by TWO datasets, selected with DATASET (or
# --dataset):
#
#   DATASET=pro (default)  -- SWE-bench Pro (Scale AI)
#     SOURCE  jefzda/sweap-images         (public; ONE repo, one TAG per
#             instance. The HuggingFace dataset ScaleAI/SWE-bench_Pro
#             `dockerhub_tag` column names each instance's tag; ~1002 tags
#             total, ~1.43 TB compressed on the wire.)
#     DEST    51jaswanth15/sweap-images   (ours; SAME repo shape + IDENTICAL
#             tag strings, so the eval harness runs unchanged with
#             `--dockerhub_username=51jaswanth15` -- swe_bench_pro_eval.py
#             builds `{username}/sweap-images:{tag}`.)
#
#   DATASET=verified -- SWE-bench Verified (princeton-nlp, official harness)
#     SOURCE  swebench/*                  (public; MANY repos, ONE tag each --
#             `docker.io/swebench/sweb.eval.x86_64.<key>:latest` per instance.
#             <key> is NOT a guess: it's `TestSpec.instance_image_key` from
#             the installed `swebench` package's own
#             `swebench.harness.test_spec.test_spec` module, computed via
#             `make_test_spec(instance, namespace="swebench")` for every
#             instance_id in HF `princeton-nlp/SWE-bench_Verified` split
#             "test" -- the exact helper + `__` -> `_1776_` normalization the
#             harness itself uses to name/pull these images.)
#     DEST    51jaswanth15/*              (ours; SAME per-instance repo name +
#             tag under our namespace, e.g.
#             `51jaswanth15/sweb.eval.x86_64.<key>:latest`, so grading against
#             the mirror is just `swebench.harness.run_evaluation
#             --namespace 51jaswanth15`.)
#
# WHY crane + WHY IT'S USUALLY CHEAP: source and dest are on the SAME registry
# (registry-1.docker.io). `crane copy` streams registry->registry and, for a
# same-registry dest, asks Docker Hub to CROSS-REPO MOUNT each layer blob by
# digest (POST .../blobs/uploads/?mount=<digest>&from=<src-repo>) rather than
# re-uploading it. When Hub honours the mount the copy writes only the
# manifest + config (a few KB) and finishes in seconds -- no gigabytes of
# bytes shovelled through this machine. Tell which happened by the per-tag
# time this script prints: a mounted copy is a few seconds; a real
# byte-transfer is minutes. If it's transferring for real, run this from an
# in-region cloud box (fat pipe) rather than a laptop.
#
# AUTH -- needs a READ & WRITE PAT (read-only can pull but NOT push): either
#   docker login -u <user>            # paste a Read & Write personal access token
# once (crane reads ~/.docker/config.json), OR set DOCKERHUB_USER +
# DOCKERHUB_TOKEN and this script logs in for you. A read-only PAT authenticates
# and pulls fine but the push is rejected mid-copy with
#   access token has insufficient scopes
# -- regenerate the PAT as Read & Write (Docker Hub > Account Settings >
# Personal access tokens). This script never hardcodes, echoes, or writes the
# token anywhere -- it only assumes an existing login.
#
# RATE LIMITS: `crane copy` reads manifests/blobs from the source, which counts
# against Docker Hub's pull limit (authenticated free ~200/6h). A full
# first-time sync can therefore hit the cap and stall -- that's fine: this
# script is resumable (it skips units already on the dest, by name, using a
# single cheap catalog call per side -- NOT a per-unit pull), so re-run after
# the window, or use an account / plan with a higher pull allowance. Source
# images are commit-pinned and effectively immutable, so name-presence ==
# mirrored; use FORCE=1 only to repair a partial/corrupt earlier push.
#
# Usage:
#   cd e2e/swebench-pro
#   docker login -u 51jaswanth15                 # once, with a Read & Write PAT
#   ./mirror-sweap-images.sh                      # mirror ALL missing (pro, default)
#   DATASET=verified ./mirror-sweap-images.sh     # mirror ALL missing (verified)
#   ./mirror-sweap-images.sh --status             # how many of N are mirrored
#   DATASET=verified ./mirror-sweap-images.sh --status
#   DRY_RUN=1 ./mirror-sweap-images.sh            # list what WOULD copy, do nothing
#   LIMIT=5 ./mirror-sweap-images.sh              # copy only the first 5 missing (smoke)
#   TAGS_FILE=retry.txt ./mirror-sweap-images.sh  # restrict to these units (one per line)
#   FORCE=1 ./mirror-sweap-images.sh              # re-copy even units already on the dest
#   CONCURRENCY=12 ./mirror-sweap-images.sh       # more parallel copies
#   --dataset verified / --dataset=verified       # same as DATASET=verified (either works)
#
# Env vars (all optional):
#   DATASET         'pro' (default) or 'verified' -- selects the source/dest shape above
#   SRC_REPO        source repo/namespace  (default: jefzda/sweap-images [pro] | swebench [verified])
#   DST_REPO        dest repo/namespace    (default: 51jaswanth15/sweap-images [pro] | 51jaswanth15 [verified])
#   CONCURRENCY     parallel copies        (default 8)
#   LIMIT           cap units processed this run, 0 = no cap (default 0)
#   TAGS_FILE       newline-separated unit allowlist to use INSTEAD of enumerating SRC
#   FORCE           1 = copy even when the unit already exists on the dest (default 0)
#   DRY_RUN         1 = plan only, copy nothing (default 0)
#   RETRIES         per-unit copy attempts on transient/429 errors (default 4)
#   LOG_DIR         run log + failed-tags.txt live here (default ./mirror-logs)
#   DOCKERHUB_USER + DOCKERHUB_TOKEN -- if BOTH set, `crane auth login` first
#   (verified only) VERIFIED_HF_DATASET  HF dataset id (default princeton-nlp/SWE-bench_Verified)
#   (verified only) VERIFIED_HF_SPLIT    HF split       (default test)
#   (verified only) VERIFIED_ARCH        image arch      (default x86_64)
#   (verified only) VERIFIED_VENV        scratch venv path for swebench+datasets
#                                         if not already importable (default ./.verified-venv)
#
# Portable to macOS's stock bash 3.2 (no mapfile / associative arrays -- the
# unit set is handled in temp files, like the other scripts in this repo).
# Requires: crane (`brew install crane`, or
#   `go install github.com/google/go-containerregistry/cmd/crane@latest`),
#   python3, curl. `verified` additionally needs the `swebench` + `datasets`
#   python packages importable -- either already on PATH (or at /work/.venv in
#   a swebench eval image), or this script bootstraps a scratch venv for them.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$HERE/$(basename "${BASH_SOURCE[0]}")"
cd "$HERE"

# -- DATASET: env var, or a --dataset/--dataset=<val> flag anywhere in argv
# (pre-scanned here, without shifting argv, so it applies before the SRC/DST
# defaults below AND before the --worker fast-path). --worker children never
# receive this flag -- they inherit DATASET via the export below. -----------
DATASET="${DATASET:-pro}"
_ds_prev=""
for _a in "$@"; do
  if [ "$_ds_prev" = "1" ]; then DATASET="$_a"; _ds_prev=""; continue; fi
  case "$_a" in
    --dataset) _ds_prev="1" ;;
    --dataset=*) DATASET="${_a#--dataset=}" ;;
  esac
done
unset _ds_prev _a
case "$DATASET" in
  pro|verified) : ;;
  * ) echo "mirror-sweap-images.sh: DATASET/--dataset must be 'pro' or 'verified' (got '$DATASET')" >&2; exit 2 ;;
esac

if [ "$DATASET" = "verified" ]; then
  SRC_REPO="${SRC_REPO:-swebench}"
  DST_REPO="${DST_REPO:-51jaswanth15}"
  UNIT_NOUN="image"
else
  SRC_REPO="${SRC_REPO:-jefzda/sweap-images}"
  DST_REPO="${DST_REPO:-51jaswanth15/sweap-images}"
  UNIT_NOUN="tag"
fi
CONCURRENCY="${CONCURRENCY:-8}"
LIMIT="${LIMIT:-0}"
TAGS_FILE="${TAGS_FILE:-}"
FORCE="${FORCE:-0}"
DRY_RUN="${DRY_RUN:-0}"
RETRIES="${RETRIES:-4}"
LOG_DIR="${LOG_DIR:-$HERE/mirror-logs}"
PY="${PYTHON:-python3}"
VERIFIED_HF_DATASET="${VERIFIED_HF_DATASET:-princeton-nlp/SWE-bench_Verified}"
VERIFIED_HF_SPLIT="${VERIFIED_HF_SPLIT:-test}"
VERIFIED_ARCH="${VERIFIED_ARCH:-x86_64}"
VERIFIED_VENV="${VERIFIED_VENV:-$HERE/.verified-venv}"
export SRC_REPO DST_REPO RETRIES DATASET  # inherited by --worker child processes

log() { printf '[mirror] %s\n' "$*" >&2; }

# -- internal worker: copy ONE unit (invoked by xargs, one process per unit) --
# Prints a single status line: OK / FAIL / AUTH. Exit 3 (AUTH) marks a
# read-only PAT so the parent can surface it instead of failing N times.
# pro:      unit is a bare tag inside the single shared SRC_REPO/DST_REPO.
# verified: unit is the per-instance repo name (no namespace, no ":latest" --
#           both are re-added here); SRC_REPO/DST_REPO are namespaces.
if [ "${1:-}" = "--worker" ]; then
  tag="${2:?--worker needs a tag}"
  if [ "$DATASET" = "verified" ]; then
    src="$SRC_REPO/$tag:latest"; dst="$DST_REPO/$tag:latest"
  else
    src="$SRC_REPO:$tag"; dst="$DST_REPO:$tag"
  fi
  t0=$(date +%s); attempt=1
  while :; do
    if err="$(crane copy "$src" "$dst" 2>&1)"; then
      printf 'OK   %s  %ss\n' "$tag" "$(( $(date +%s) - t0 ))"; exit 0
    fi
    if printf '%s' "$err" | grep -qiE 'insufficient scopes|denied|unauthorized'; then
      printf 'AUTH %s  push denied (PAT needs Read & Write)\n' "$tag"; exit 3
    fi
    if [ "$attempt" -ge "$RETRIES" ]; then
      printf 'FAIL %s  %s\n' "$tag" "$(printf '%s' "$err" | tr '\n' ' ' | cut -c1-180)"; exit 1
    fi
    sleep $(( attempt * 5 )); attempt=$(( attempt + 1 ))
  done
fi

# -- main mode ------------------------------------------------------------
MODE="mirror"
while [ $# -gt 0 ]; do
  case "$1" in
    --status) MODE="status"; shift ;;
    --dataset) shift 2 ;;          # already applied by the pre-scan above
    --dataset=*) shift ;;          # already applied by the pre-scan above
    -h|--help) sed -n '2,103p' "$SELF"; exit 0 ;;
    "") shift ;;
    * ) echo "mirror-sweap-images.sh: unknown arg '$1' (try --help)" >&2; exit 2 ;;
  esac
done

command -v crane >/dev/null 2>&1 || {
  echo "mirror-sweap-images.sh: 'crane' not found. Install: brew install crane" >&2
  echo "  (or: go install github.com/google/go-containerregistry/cmd/crane@latest)" >&2
  exit 1
}

# optional convenience login from env
if [ -n "${DOCKERHUB_USER:-}" ] && [ -n "${DOCKERHUB_TOKEN:-}" ]; then
  log "crane auth login index.docker.io as $DOCKERHUB_USER"
  printf '%s' "$DOCKERHUB_TOKEN" | crane auth login index.docker.io -u "$DOCKERHUB_USER" --password-stdin \
    || crane auth login index.docker.io -u "$DOCKERHUB_USER" -p "$DOCKERHUB_TOKEN"
fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
SRC_TXT="$TMP/src.txt"; DST_TXT="$TMP/dst.txt"; WORK_TXT="$TMP/work.txt"

# -- pro source tags: TAGS_FILE allowlist, else enumerate SRC via the Hub web --
# API. The web API (hub.docker.com/v2) is free, needs no auth for a public
# repo, and does NOT count against the registry pull limit -- unlike a
# per-tag pull.
list_src_tags() {
  "$PY" - "$SRC_REPO" <<'PY'
import sys, json, urllib.request
repo = sys.argv[1]
url = f"https://hub.docker.com/v2/repositories/{repo}/tags/?page_size=100"
seen = set()
while url:
    with urllib.request.urlopen(url, timeout=60) as r:
        d = json.load(r)
    for t in d.get("results", []):
        n = t.get("name")
        if n and n not in seen:
            seen.add(n); print(n)
    url = d.get("next")
PY
}

# -- verified: python interpreter with `swebench` + `datasets` importable.
# Prefers an already-usable one (this host's python3, or a swebench eval
# image's /work/.venv); else bootstraps a scratch venv once, cached at
# VERIFIED_VENV. Never touches Docker Hub auth.
verified_python() {
  if "$PY" -c 'import swebench, datasets' >/dev/null 2>&1; then
    echo "$PY"; return 0
  fi
  if [ -x /work/.venv/bin/python3 ] && /work/.venv/bin/python3 -c 'import swebench, datasets' >/dev/null 2>&1; then
    echo /work/.venv/bin/python3; return 0
  fi
  if [ ! -x "$VERIFIED_VENV/bin/python3" ]; then
    log "verified: bootstrapping a scratch venv for swebench+datasets ($VERIFIED_VENV)..."
    "$PY" -m venv "$VERIFIED_VENV" >&2 || return 1
    "$VERIFIED_VENV/bin/pip" install -q --upgrade pip >&2 || return 1
    "$VERIFIED_VENV/bin/pip" install -q swebench datasets >&2 || return 1
  fi
  "$VERIFIED_VENV/bin/python3" -c 'import swebench, datasets' >/dev/null 2>&1 || return 1
  echo "$VERIFIED_VENV/bin/python3"
}

# -- verified source units: one per instance_id, the per-instance repo name
# with NO namespace and NO ":latest" (both re-added by --worker / DRY_RUN
# below). Computed via swebench's OWN TestSpec.instance_image_key property
# (swebench.harness.test_spec.test_spec) against namespace="swebench" -- the
# SAME helper + "__" -> "_1776_" normalization the eval harness itself uses
# to name/pull these images, so this script never hand-rolls it.
list_verified_units() {
  VPY="$(verified_python)" || return 1
  "$VPY" - "$VERIFIED_HF_DATASET" "$VERIFIED_HF_SPLIT" "$VERIFIED_ARCH" <<'PY'
import sys
from datasets import load_dataset
from swebench.harness.test_spec.test_spec import make_test_spec

hf_dataset, split, arch = sys.argv[1], sys.argv[2], sys.argv[3]
ds = load_dataset(hf_dataset, split=split)
seen = set()
for row in ds:
    ts = make_test_spec(dict(row), namespace="swebench", arch=arch)
    key = ts.instance_image_key  # "swebench/sweb.eval.<arch>.<id, __->_1776_>:latest"
    prefix = "swebench/"
    unit = key[len(prefix):] if key.startswith(prefix) else key
    if unit.endswith(":latest"):
        unit = unit[: -len(":latest")]
    if unit not in seen:
        seen.add(unit); print(unit)
PY
}

# -- verified dest units already mirrored: one paginated Hub web API call
# listing every repo in the DST_REPO namespace (public, free, no per-repo
# pull), filtered to the "sweb.eval." prefix -- the verified-shape analogue
# of `crane ls` on a single shared repo.
list_dst_repos_verified() {
  "$PY" - "$DST_REPO" <<'PY'
import sys, json, urllib.request
namespace = sys.argv[1]
url = f"https://hub.docker.com/v2/repositories/{namespace}/?page_size=100"
seen = set()
while url:
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            d = json.load(r)
    except Exception:
        break
    for repo in d.get("results", []):
        name = repo.get("name")
        if name and name.startswith("sweb.eval.") and name not in seen:
            seen.add(name); print(name)
    url = d.get("next")
PY
}

if [ -n "$TAGS_FILE" ]; then
  [ -f "$TAGS_FILE" ] || { echo "mirror-sweap-images.sh: TAGS_FILE not found: $TAGS_FILE" >&2; exit 1; }
  grep -vE '^[[:space:]]*(#|$)' "$TAGS_FILE" | tr -d '\r' | grep . > "$SRC_TXT" || true
  log "source: $(grep -c . "$SRC_TXT" || true) $UNIT_NOUN(s) from $TAGS_FILE"
elif [ "$DATASET" = "verified" ]; then
  log "source: deriving eval-image names via swebench (namespace=$SRC_REPO) from $VERIFIED_HF_DATASET/$VERIFIED_HF_SPLIT..."
  list_verified_units | grep . > "$SRC_TXT" || true
  log "source: $(grep -c . "$SRC_TXT" || true) $UNIT_NOUN(s) derived"
else
  log "source: enumerating tags of $SRC_REPO (Hub web API)..."
  list_src_tags | grep . > "$SRC_TXT" || true
  log "source: $(grep -c . "$SRC_TXT" || true) tag(s) in $SRC_REPO"
fi
SRC_N="$(grep -c . "$SRC_TXT" 2>/dev/null || true)"; SRC_N="${SRC_N:-0}"; SRC_N="${SRC_N//[!0-9]/}"; SRC_N="${SRC_N:-0}"
[ "$SRC_N" -gt 0 ] || { echo "mirror-sweap-images.sh: no source $UNIT_NOUN(s) resolved -- aborting" >&2; exit 1; }

# -- dest units already present (one cheap authed catalog call; works for a
#    PRIVATE dest and returns empty for a not-yet-created repo/namespace) ----
log "dest: listing $UNIT_NOUN(s) already in $DST_REPO..."
if [ "$DATASET" = "verified" ]; then
  list_dst_repos_verified | grep . > "$DST_TXT" || true
else
  crane ls "$DST_REPO" 2>/dev/null | grep . > "$DST_TXT" || true
fi
DST_COUNT="$(grep -c . "$DST_TXT" 2>/dev/null || true)"; DST_COUNT="${DST_COUNT:-0}"; DST_COUNT="${DST_COUNT//[!0-9]/}"; DST_COUNT="${DST_COUNT:-0}"
log "dest: $DST_COUNT $UNIT_NOUN(s) already in $DST_REPO"

# -- work set = SRC - DST (unless FORCE), preserving source order, then LIMIT --
if [ "$FORCE" = "1" ]; then
  cp "$SRC_TXT" "$WORK_TXT"
else
  # -F fixed strings, -x whole line, -v invert, -f patterns-from-dst. Empty dst
  # file => zero patterns => all src lines pass. || true: grep exits 1 when the
  # (fully-synced) result is empty.
  grep -Fxv -f "$DST_TXT" "$SRC_TXT" > "$WORK_TXT" || true
fi
if [ "$LIMIT" -gt 0 ]; then
  head -n "$LIMIT" "$WORK_TXT" > "$WORK_TXT.lim" && mv "$WORK_TXT.lim" "$WORK_TXT"
fi
WORK_N="$(grep -c . "$WORK_TXT" 2>/dev/null || true)"; WORK_N="${WORK_N:-0}"; WORK_N="${WORK_N//[!0-9]/}"; WORK_N="${WORK_N:-0}"
MISSING=$(( SRC_N - DST_COUNT )); [ "$MISSING" -lt 0 ] && MISSING=0

# -- --status: report and exit ------------------------------------------------
if [ "$MODE" = "status" ]; then
  if [ "$DATASET" = "verified" ]; then
    echo "src  ($SRC_REPO):   $SRC_N image(s) ($VERIFIED_HF_DATASET/$VERIFIED_HF_SPLIT)"
    echo "dest ($DST_REPO):   $DST_COUNT image(s) mirrored"
    echo "missing on dest:    $MISSING"
    [ "$MISSING" -gt 0 ] && echo "run DATASET=verified ./mirror-sweap-images.sh to copy the remaining $MISSING."
  else
    echo "src  ($SRC_REPO):   $SRC_N tags"
    echo "dest ($DST_REPO):   $DST_COUNT tags mirrored"
    echo "missing on dest:    $MISSING"
    [ "$MISSING" -gt 0 ] && echo "run ./mirror-sweap-images.sh to copy the remaining $MISSING."
  fi
  exit 0
fi

log "plan: $WORK_N $UNIT_NOUN(s) to copy (FORCE=$FORCE, LIMIT=$LIMIT, CONCURRENCY=$CONCURRENCY)"
if [ "$WORK_N" -eq 0 ]; then
  log "nothing to do -- dest is already in sync. (ok)"
  exit 0
fi

# -- DRY_RUN: show the plan, copy nothing -------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  if [ "$DATASET" = "verified" ]; then
    echo "[DRY_RUN] would copy $WORK_N $UNIT_NOUN(s) from $SRC_REPO/* to $DST_REPO/*:"
    head -n 20 "$WORK_TXT" | while IFS= read -r u; do
      printf '  %s/%s:latest -> %s/%s:latest\n' "$SRC_REPO" "$u" "$DST_REPO" "$u"
    done
  else
    echo "[DRY_RUN] would copy $WORK_N tag(s) from $SRC_REPO to $DST_REPO:"
    head -n 20 "$WORK_TXT" | sed 's/^/  /'
  fi
  [ "$WORK_N" -gt 20 ] && echo "  ... and $(( WORK_N - 20 )) more"
  exit 0
fi

# -- run: fan out one --worker process per unit, CONCURRENCY at a time --------
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
RUN_LOG="$LOG_DIR/mirror-$STAMP.log"
FAILED="$LOG_DIR/failed-tags.txt"
log "copying $WORK_N $UNIT_NOUN(s) -> $DST_REPO ... (log: $RUN_LOG)"

# BSD/GNU xargs: -n1 appends one unit per invocation after `$SELF --worker`.
# Units are [A-Za-z0-9._-] only (validated upstream / derived from
# instance_id), so no quoting hazards.
xargs -P "$CONCURRENCY" -n1 "$SELF" --worker < "$WORK_TXT" | tee "$RUN_LOG"

OK_N="$(grep -c '^OK '   "$RUN_LOG" 2>/dev/null || true)"; OK_N="${OK_N:-0}"; OK_N="${OK_N//[!0-9]/}"; OK_N="${OK_N:-0}"
FAIL_N="$(grep -c '^FAIL ' "$RUN_LOG" 2>/dev/null || true)"; FAIL_N="${FAIL_N:-0}"; FAIL_N="${FAIL_N//[!0-9]/}"; FAIL_N="${FAIL_N:-0}"
AUTH_N="$(grep -c '^AUTH ' "$RUN_LOG" 2>/dev/null || true)"; AUTH_N="${AUTH_N:-0}"; AUTH_N="${AUTH_N//[!0-9]/}"; AUTH_N="${AUTH_N:-0}"
grep -E '^(FAIL|AUTH) ' "$RUN_LOG" 2>/dev/null | awk '{print $2}' > "$FAILED" || true

echo
log "done: $OK_N copied, $FAIL_N failed, $AUTH_N auth-denied  (of $WORK_N attempted)"
if [ "$AUTH_N" -gt 0 ]; then
  log "AUTH errors -> your Docker Hub PAT is read-only. Regenerate it with Read &"
  log "Write scope (Account Settings > Personal access tokens), then re-login."
fi
if [ "$FAIL_N" -gt 0 ] || [ "$AUTH_N" -gt 0 ]; then
  log "failed units written to $FAILED -- retry just those with:"
  log "  TAGS_FILE=$FAILED ./mirror-sweap-images.sh"
  exit 1
fi
log "all copied. verify: ./mirror-sweap-images.sh --status"
