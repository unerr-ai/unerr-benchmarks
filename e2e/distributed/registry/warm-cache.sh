#!/usr/bin/env bash
# Pre-seed ("warm") the shared swebench-registry pull-through cache with a
# task set's SWE-bench testbed images, so the FIRST distributed run against
# that set doesn't pay the ~11min/~$11-of-GPU-clock cold `docker pull` tax on
# every worker (see README.md "Why").
#
# WHY WARM-BY-PULL, NOT PUSH: swebench-registry (fly.toml,
# REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io) runs in PROXY /
# pull-through mode. A proxy registry REJECTS direct pushes (skopeo/docker
# push -> 405) — the only way to populate its cache is to pull each image
# THROUGH the mirror (http://swebench-registry.flycast:5000), exactly like a
# real worker does.
#
# HOW (no docker, HTTP only): a pull-through registry populates its cache on
# ANY blob GET — on a miss it fetches from Docker Hub, writes the blob to
# Tigris, and streams it to the client. So warming needs neither dockerd nor
# an image extract: an ephemeral warmer machine on the fleet app runs a
# stdlib-only Python script (embedded below, base64'd into WARM_PY_B64) that
# HTTP GETs each image's manifest + every blob straight through the mirror,
# discarding the bytes, with CONCURRENCY images in flight at once. The prior
# version booted DinD and ran `docker pull` per image — which DOWNLOADS *and
# EXTRACTS* each ~2GB image to overlay2 (then deletes it), ~7-10 min/image,
# sequential: the measured cost was 100% warmer-side extraction +
# serialization (the registry itself sits at load 0.00 while warming).
# Dropping docker + extraction + sequential pulls is the whole speedup.
#
# WHY BATCHES: each batch launches its own ephemeral warmer machine and keeps
# a batch's WARM_IMAGES_B64 env var a sane size. Docker Hub's anonymous-pull
# limit (100 manifest pulls/6h/IP) no longer bounds BATCH the way it used to
# once a Hub token is set on the proxy (see README.md "Docker Hub token") —
# with a token the cap is gone, so BATCH defaults large (120) and SLEEP=0.
#
# Idempotent + cron-friendly: re-running only re-fetches what isn't already
# cached (a cache hit is a cheap GET, not a re-fetch-and-store), so it's safe
# to schedule every few days as SWE-bench adds/changes testbed images.
#
# Usage:
#   cd e2e/distributed/registry
#   FLY_ORG=vamsee-k-933 ./warm-cache.sh --suite mini
#   FLY_ORG=vamsee-k-933 ./warm-cache.sh --suite verified
#   FLY_ORG=vamsee-k-933 ./warm-cache.sh --tasks "django__django-11790,django__django-11815"
#   FLY_ORG=vamsee-k-933 ./warm-cache.sh --file ids.txt
#
# id -> instance-id resolution REUSES ../tools/suite.py (same --suite|--tasks|
# --file selectors, same precedence, as run-distributed.sh). --suite mini and
# --tasks/--file need only the stdlib on the HOST running this script;
# --suite verified/lite need `pip install datasets` on the HOST. There is NO
# mini-50 suite in suite.py — verified (500 ids) ⊇ mini-50 ⊇ mini (10 ids),
# so warming --suite verified once covers every smaller tier.
#
# id -> image-ref: reimplements docker_image_for() (source of truth:
# e2e/econ/local-docker/run-benchmark.py:49) —
#   iid = instance_id.replace("__", "_1776_")
#   img = f"docker.io/swebench/sweb.eval.x86_64.{iid}:latest".lower()
# The warmer strips the "docker.io/" prefix itself: repo=
# swebench/sweb.eval.x86_64.<iid>, tag=latest, GET through the mirror at
# <MIRROR>/v2/<repo>/manifests|blobs/...
#
# Env vars (all optional except FLY_ORG):
#   FLY_ORG        (required) fly org, e.g. vamsee-k-933
#   MIRROR         pull-through endpoint the warmer fetches through
#                  (default http://swebench-registry.flycast:5000)
#   FLEET_APP      fly app the ephemeral warmer launches on — reuses its
#                  already-built dist image (default swebench-agent-dist)
#   IMAGE          override the dist image ref directly (default: read
#                  from a live machine on FLEET_APP's .config.image)
#   CONCURRENCY    images fetched in parallel per warmer, thread-pool size
#                  (default 16 — the registry sits idle while warming, so
#                  there's headroom; raise it if the mirror keeps up)
#   HTTP_TIMEOUT   per manifest/blob GET timeout, seconds (default 300).
#                  PULL_TIMEOUT is accepted as a back-compat alias from the
#                  old docker-pull warmer.
#   BATCH          image refs per warmer machine / per WARM_IMAGES_B64 env
#                  var (default 120)
#   SLEEP          seconds to sleep between batches (default 0). Only
#                  matters without a Hub token on the proxy — see
#                  README.md "Docker Hub token" / "Batching".
#   WARMER_VM_MEM  warmer machine memory, MB (default 2048)
#   WARMER_VM_CPUS warmer machine CPUs (default 2)
#   WARMER_REGION  fly region for the warmer, co-located with the registry so
#                  mirror fetches stay in-region (default iad)
#   KEEP_WARMER    1 = leave each batch's warmer machine up (stopped) for
#                  inspection instead of destroying it (default unset)
#
# Requires: flyctl logged in (same auth run-distributed.sh uses); python3 on
#           PATH on the HOST (+ `datasets` only for --suite verified/lite).
#           The warmer machine itself only needs python3 stdlib — already on
#           the dist image, no dockerd/boot.sh/DinD/big rootfs needed.
#
# Does NOT launch anything by itself if IMAGE resolution fails (no live
# fleet + no IMAGE=) — see the IMG resolution step below.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"        # e2e/distributed/registry
DIST_DIR="$(cd "$HERE/.." && pwd)"                          # e2e/distributed
cd "$HERE"

usage() {
  cat <<'EOF' >&2
Usage: FLY_ORG=<org> ./warm-cache.sh [--suite mini|verified|lite] [--tasks "a,b,c"] [--file ids.txt]

  --suite <mini|verified|lite>  passthrough to ../tools/suite.py (no mini-50;
                                 verified ⊇ mini-50 ⊇ mini)
  --tasks "a,b,c"                explicit comma-separated instance ids
  --file <path>                  file of newline/comma-separated instance ids
  -h, --help                     this message

See the header comment of this script, and README.md ("Refreshing the
cache"), for the full env-var list (FLY_ORG, MIRROR, FLEET_APP, IMAGE,
CONCURRENCY, HTTP_TIMEOUT, BATCH, SLEEP, WARMER_VM_MEM, KEEP_WARMER).
EOF
}

# ── config ──────────────────────────────────────────────────────────────────
FLY_ORG="${FLY_ORG:-}"
[ -n "$FLY_ORG" ] || { echo "warm-cache.sh: set FLY_ORG=<fly org> (e.g. FLY_ORG=vamsee-k-933)" >&2; exit 1; }
MIRROR="${MIRROR:-http://swebench-registry.flycast:5000}"
FLEET_APP="${FLEET_APP:-swebench-agent-dist}"
IMAGE="${IMAGE:-}"
CONCURRENCY="${CONCURRENCY:-16}"
HTTP_TIMEOUT="${HTTP_TIMEOUT:-${PULL_TIMEOUT:-300}}"   # per-GET timeout, seconds; PULL_TIMEOUT is a
                                                         # back-compat alias from the old docker-pull warmer
BATCH="${BATCH:-120}"
SLEEP="${SLEEP:-0}"
WARMER_VM_MEM="${WARMER_VM_MEM:-2048}"
WARMER_VM_CPUS="${WARMER_VM_CPUS:-2}"
WARMER_REGION="${WARMER_REGION:-iad}"           # co-locate warmer with the registry (iad) for fast in-region fetches
KEEP_WARMER="${KEEP_WARMER:-0}"
PY_HOST="${PYTHON:-python3}"
RUN_RETRIES="${RUN_RETRIES:-6}"    # transient machine-create retry ceiling (matches run-distributed.sh)

log() { printf '[warm-cache] %s\n' "$*" >&2; }

# ── selector passthrough: --suite|--tasks|--file straight to suite.py (same
#    precedence as suite.py itself: tasks > file > suite > default) ───────────
SUITE="${SUITE:-}"; TASKS="${TASKS:-}"; TASKS_FILE="${TASKS_FILE:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --suite)
      [ $# -ge 2 ] || { echo "warm-cache.sh: --suite needs a value" >&2; exit 2; }
      SUITE="$2"; shift 2 ;;
    --tasks)
      [ $# -ge 2 ] || { echo "warm-cache.sh: --tasks needs a value" >&2; exit 2; }
      TASKS="$2"; shift 2 ;;
    --file)
      [ $# -ge 2 ] || { echo "warm-cache.sh: --file needs a value" >&2; exit 2; }
      TASKS_FILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "warm-cache.sh: unknown arg '$1'" >&2; usage; exit 2 ;;
  esac
done

SUITE_PY_ARGS=()
[ -n "$SUITE" ] && SUITE_PY_ARGS+=(--suite "$SUITE")
[ -n "$TASKS" ] && SUITE_PY_ARGS+=(--tasks "$TASKS")
[ -n "$TASKS_FILE" ] && SUITE_PY_ARGS+=(--file "$TASKS_FILE")

log "resolving instance ids via ../tools/suite.py ${SUITE_PY_ARGS[*]:-<default: full Verified>}"
INSTANCE_IDS="$("$PY_HOST" "$DIST_DIR/tools/suite.py" "${SUITE_PY_ARGS[@]+"${SUITE_PY_ARGS[@]}"}")"
if [ -z "$INSTANCE_IDS" ]; then
  log "suite.py resolved 0 instance ids — aborting"
  exit 1
fi

# ── id -> image-ref (reimplements docker_image_for(), see header comment) ────
IMAGE_REFS=()
while IFS= read -r id; do
  [ -n "$id" ] || continue
  iid="${id//__/_1776_}"
  img="docker.io/swebench/sweb.eval.x86_64.${iid}:latest"
  img="$(printf '%s' "$img" | tr 'A-Z' 'a-z')"
  IMAGE_REFS+=("$img")
done < <(printf '%s\n' "$INSTANCE_IDS" | tr ',' '\n')   # trailing \n: without it, `read`
                                                          # drops the LAST id (no terminator
                                                          # on its line) — verified via dry-run

TOTAL=${#IMAGE_REFS[@]}
[ "$TOTAL" -gt 0 ] || { log "no image refs resolved — nothing to warm"; exit 1; }
NUM_BATCHES=$(( (TOTAL + BATCH - 1) / BATCH ))
log "$TOTAL instance id(s) -> $TOTAL image ref(s), $NUM_BATCHES batch(es) of <=$BATCH, CONCURRENCY=$CONCURRENCY per batch"
log "  (Docker Hub anonymous limit: 100 manifest pulls/6h/IP if no Hub token set on the proxy; with REGISTRY_PROXY_USERNAME/PASSWORD set the cap is gone — see README.md)"

# ── resolve the dist image ref (IMAGE= override, else a live machine's
#    config.image on FLEET_APP) ────────────────────────────────────────────
if [ -n "$IMAGE" ]; then
  IMG="$IMAGE"
  log "using IMAGE override: $IMG"
else
  log "resolving dist image ref from a live machine on '$FLEET_APP' (fly machines list --json .config.image)"
  IMG="$(flyctl machines list -a "$FLEET_APP" --json 2>/dev/null | "$PY_HOST" -c '
import sys, json
try:
    ms = json.load(sys.stdin)
except Exception:
    ms = []
for m in ms:
    img = (m.get("config") or {}).get("image") or ""
    if img:
        print(img)
        break
')"
  if [ -z "$IMG" ]; then
    log "could not resolve a dist image ref — no live machines found on app '$FLEET_APP' (or flyctl/auth unavailable)."
    log "  run a fleet first (e.g. run-distributed.sh ... prepare) so a machine's .config.image is discoverable, or pass IMAGE=<ref> explicitly."
    exit 1
  fi
  log "resolved dist image: $IMG"
fi

# ── warmer lifecycle: retry transient machine-create errors (mirrors
#    run-distributed.sh's run_machine), trap-driven cleanup so an abort never
#    leaks a warmer machine ────────────────────────────────────────────────
WARMER_MID=""
cleanup_warmer() {
  [ -n "$WARMER_MID" ] || return 0
  if [ "$KEEP_WARMER" = "1" ]; then
    log "KEEP_WARMER=1 — leaving warmer $WARMER_MID in place for inspection"
    return 0
  fi
  log "destroying warmer $WARMER_MID"
  flyctl machine destroy "$WARMER_MID" -a "$FLEET_APP" --force >/dev/null 2>&1 || true
  WARMER_MID=""
}
trap cleanup_warmer EXIT INT TERM

run_warmer_machine() {                      # run_warmer_machine <logfile> <flyctl machine run args...>
  local logf="$1"; shift
  local tries=0 max="$RUN_RETRIES"
  while [ "$tries" -lt "$max" ]; do
    tries=$((tries + 1))
    if flyctl machine run "$@" >"$logf" 2>&1; then
      return 0
    fi
    if grep -qiE 'MANIFEST_UNKNOWN|429|rate limit|capacity|please try again' "$logf"; then
      log "  transient machine-create error (try $tries/$max) — retry in $((tries * 5))s"
      sleep $((tries * 5)); continue
    fi
    log "  warmer machine-create FAILED (non-transient) — tail:"; tail -6 "$logf" >&2
    return 1
  done
  log "  warmer machine-create gave up after $tries tries"; return 1
}

# state="..." exit_code="..." for a machine id, via `machines list --json`
# (machine status has no --json flag; this reuses the same list+filter idiom
# as run-distributed.sh's fleet_ids). exit_code search is key-based, not a
# fixed nesting path, since the exact events shape can vary by flyctl version.
warmer_state_and_exit() {                    # warmer_state_and_exit <id>
  flyctl machines list -a "$FLEET_APP" --json 2>/dev/null | "$PY_HOST" -c '
import sys, json

def find_all(o, key):
    if isinstance(o, dict):
        if key in o:
            yield o[key]
        for v in o.values():
            yield from find_all(v, key)
    elif isinstance(o, list):
        for v in o:
            yield from find_all(v, key)

mid = sys.argv[1]
try:
    ms = json.load(sys.stdin)
except Exception:
    ms = []
m = next((x for x in ms if x.get("id") == mid), None)
if not m:
    print("state=unknown exit_code=")
else:
    codes = list(find_all(m.get("events") or [], "exit_code"))
    print("state=%s exit_code=%s" % (m.get("state", "unknown"), codes[0] if codes else ""))
' "$1"
}

# ── in-warmer script: stdlib-only Python that HTTP GETs each image's
#    manifest + every blob straight through the mirror (MIRROR), discarding
#    the bytes. A pull-through registry populates its cache on ANY blob GET —
#    no docker pull/extract needed, just force the fetch. Single-quoted
#    heredoc: no bash expansion happens inside, it's pure Python. Encoded to
#    base64 once (constant across batches), decoded+run inside the warmer via
#    `echo "$WARM_PY_B64" | base64 -d | python3 -`. ─────────────────────────
read -r -d '' WARM_PY <<'PYEOF' || true
import base64
import concurrent.futures
import json
import os
import sys
import time
import urllib.request

MIRROR = os.environ.get("MIRROR", "http://swebench-registry.flycast:5000").rstrip("/")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "16"))
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "300"))
RETRIES = 3

MANIFEST_ACCEPT = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
])


def parse_images(b64):
    images = []
    for line in base64.b64decode(b64 or "").decode().splitlines():
        line = line.strip()
        if not line:
            continue
        ref = line[len("docker.io/"):] if line.startswith("docker.io/") else line
        repo, _, tag = ref.rpartition(":")
        images.append((repo or ref, tag or "latest"))
    return images


def http_get(url, accept=None, stream=False):
    req = urllib.request.Request(url)
    if accept:
        req.add_header("Accept", accept)
    err = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                if not stream:
                    return resp.read(), resp.headers.get("Content-Type", "")
                nbytes = 0
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    nbytes += len(chunk)
                return nbytes, None
        except Exception as e:
            err = e
            if attempt < RETRIES:
                time.sleep(attempt * 2)
    raise err


def warm_one(repo, tag):
    # Manifest GET forces the proxy to fetch+cache the manifest; a
    # multi-arch list/index is resolved to the amd64/linux entry and
    # re-GET'd for the real image manifest. Blob GETs (config + every
    # layer) force the proxy to fetch+cache each blob from Hub into Tigris.
    # Blobs are fetched sequentially per image (simplest correct option) —
    # total concurrency is bounded by the outer CONCURRENCY images in
    # flight, not by per-image blob fan-out.
    t0 = time.time()
    body, ctype = http_get(f"{MIRROR}/v2/{repo}/manifests/{tag}", accept=MANIFEST_ACCEPT)
    manifest = json.loads(body)
    media_type = manifest.get("mediaType") or ctype or ""

    if "manifest.list." in media_type or "image.index." in media_type:
        entries = manifest.get("manifests", [])
        if not entries:
            raise RuntimeError("manifest list/index had no entries")
        chosen = next(
            (e for e in entries
             if (e.get("platform") or {}).get("architecture") == "amd64"
             and (e.get("platform") or {}).get("os") == "linux"),
            entries[0],
        )
        body, _ = http_get(f"{MIRROR}/v2/{repo}/manifests/{chosen['digest']}", accept=MANIFEST_ACCEPT)
        manifest = json.loads(body)

    digests = [manifest["config"]["digest"]] + [l["digest"] for l in manifest.get("layers", [])]
    total_bytes = 0
    for digest in digests:
        nbytes, _ = http_get(f"{MIRROR}/v2/{repo}/blobs/{digest}", stream=True)
        total_bytes += nbytes
    return total_bytes, time.time() - t0


def run_one(repo, tag):
    try:
        nbytes, elapsed = warm_one(repo, tag)
        return repo, True, nbytes, elapsed, None
    except Exception as e:
        return repo, False, 0, 0.0, str(e)


def main():
    images = parse_images(os.environ.get("WARM_IMAGES_B64", ""))
    if not images:
        print("[warm] no images to warm")
        return 0
    ok = fail = 0
    total_bytes = 0
    wall_start = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(run_one, repo, tag) for repo, tag in images]
        for fut in concurrent.futures.as_completed(futures):
            repo, success, nbytes, elapsed, err = fut.result()
            if success:
                ok += 1
                total_bytes += nbytes
                print("[warm] ok %s %.1fMB %.1fs" % (repo, nbytes / (1 << 20), elapsed))
            else:
                fail += 1
                print("[warm] FAIL %s %s" % (repo, err))
    total = ok + fail
    wall = time.time() - wall_start
    print("[warm] done: %s/%s ok, %s failed, %.1fs, %.1fMB" % (ok, total, fail, wall, total_bytes / (1 << 20)))
    if total > 0 and fail * 100 / total > 20:
        return 1
    return 0


sys.exit(main())
PYEOF
WARM_PY_B64="$(printf '%s' "$WARM_PY" | base64 | tr -d '\n')"

# ── batch loop ─────────────────────────────────────────────────────────────
LOGDIR="$(mktemp -d "${TMPDIR:-/tmp}/warm-cache.XXXXXX")"
log "per-batch logs: $LOGDIR"
OK_TOTAL=0
FAIL_TOTAL=0
BATCH_NUM=0
START_ALL=$(date +%s)

for ((i = 0; i < TOTAL; i += BATCH)); do
  BATCH_NUM=$((BATCH_NUM + 1))
  BATCH_REFS=("${IMAGE_REFS[@]:i:BATCH}")
  BSIZE=${#BATCH_REFS[@]}
  log "batch $BATCH_NUM/$NUM_BATCHES: $BSIZE image(s)"

  WARM_IMAGES_B64="$(printf '%s\n' "${BATCH_REFS[@]}" | base64 | tr -d '\n')"
  RUNLOG="$LOGDIR/batch-$BATCH_NUM-run.log"

  if ! run_warmer_machine "$RUNLOG" "$IMG" \
      -a "$FLEET_APP" --org "$FLY_ORG" --region "$WARMER_REGION" \
      --entrypoint /bin/bash --restart no \
      --vm-memory "$WARMER_VM_MEM" --vm-cpus "$WARMER_VM_CPUS" \
      --metadata role=warmer --metadata batch="$BATCH_NUM" \
      -e MIRROR="$MIRROR" \
      -e WARM_IMAGES_B64="$WARM_IMAGES_B64" \
      -e CONCURRENCY="$CONCURRENCY" \
      -e HTTP_TIMEOUT="$HTTP_TIMEOUT" \
      -e WARM_PY_B64="$WARM_PY_B64" \
      -- -lc 'echo "$WARM_PY_B64" | base64 -d | python3 -'; then
    log "batch $BATCH_NUM: warmer machine-create failed (see $RUNLOG) — counting all $BSIZE image(s) as failed, continuing"
    FAIL_TOTAL=$((FAIL_TOTAL + BSIZE))
    continue
  fi

  WARMER_MID="$(grep -oE 'Machine ID: [0-9a-f]+' "$RUNLOG" | head -1 | awk '{print $3}')"
  if [ -z "$WARMER_MID" ]; then
    log "batch $BATCH_NUM: could not scrape warmer machine id from $RUNLOG — counting all $BSIZE image(s) as failed, continuing"
    FAIL_TOTAL=$((FAIL_TOTAL + BSIZE))
    continue
  fi
  log "batch $BATCH_NUM: warmer $WARMER_MID launched, polling until it stops"

  # DON'T use `flyctl machine wait --state stopped`: on a long run it returns a
  # Fly *proxy* deadline_exceeded (~90s) FAR before its own --wait-timeout, which
  # made us destroy a still-running warmer after a single pull. Poll the machine
  # state directly (machines list --json) until the WARM_PY process exits and the
  # machine (created --restart no) settles to stopped.
  # WAIT_TIMEOUT is a generous poll ceiling, not the expected runtime: HTTP-only
  # fetch+cache is far faster than the old docker-pull warmer (no extraction),
  # scaled by CONCURRENCY images in flight at once, plus one HTTP_TIMEOUT margin
  # for a straggler and 300s boot slack.
  WAIT_TIMEOUT=$(( BSIZE * HTTP_TIMEOUT / CONCURRENCY + HTTP_TIMEOUT + 300 ))
  DEADLINE=$(( $(date +%s) + WAIT_TIMEOUT ))
  STATE=""; INFO=""
  while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    INFO="$(warmer_state_and_exit "$WARMER_MID")"
    STATE="$(printf '%s' "$INFO" | sed -n 's/.*state=\([a-z][a-z]*\).*/\1/p')"
    case "$STATE" in stopped|destroyed|failed) break ;; esac
    sleep 20
  done
  [ "$STATE" = "stopped" ] || log "batch $BATCH_NUM: warmer $WARMER_MID did not cleanly stop (state=$STATE) after ${WAIT_TIMEOUT}s"
  EXIT_CODE="$(printf '%s' "$INFO" | sed -n 's/.*exit_code=\(-\{0,1\}[0-9]*\)$/\1/p')"

  # fly's log store is eventually-consistent AND batch-flushes: a machine's app
  # stdout can lag its `stopped` transition by minutes, and on a long run every
  # [warm] line lands at once at the very end (observed: all 125 ok lines stamped
  # to a single second ~57min after launch). Retry the fetch until the Python's
  # `[warm] done:` sentinel lands (or give up after ~3min) so accounting reflects
  # what actually happened. NOTE: `flyctl logs --no-tail` also TRUNCATES to a
  # recent window, so on a big batch it may return only some ok lines — the
  # registry catalog (see README "Verifying") is the real ground truth, not this.
  BATCHLOG="$LOGDIR/batch-$BATCH_NUM-warmer.log"
  for _ltry in $(seq 1 18); do
    flyctl logs -a "$FLEET_APP" --machine "$WARMER_MID" --no-tail >"$BATCHLOG" 2>&1 || true
    grep -q '\[warm\] done:' "$BATCHLOG" && break
    sleep 10
  done
  grep -q '\[warm\] done:' "$BATCHLOG" || log "batch $BATCH_NUM: [warm] done: sentinel never appeared in logs after ~3min — counts below may under-report (fly log lag/truncation); catalog is ground truth"
  log "batch $BATCH_NUM: warmer $WARMER_MID $INFO — output:"
  cat "$BATCHLOG" >&2

  # fly prefixes each app-stdout line with "<ts> app[<id>] <region> [info]", so the
  # [warm] markers are NOT at start-of-line — match them anywhere (no ^ anchor; the
  # original ^-anchored greps silently returned 0 on every real run). Prefer the
  # Python's own authoritative `[warm] done: X/Y ok, Z failed` summary if it
  # flushed; fall back to counting individual ok/FAIL lines only if it didn't.
  DONE_LINE="$(grep '\[warm\] done:' "$BATCHLOG" | tail -1)"
  if [ -n "$DONE_LINE" ]; then
    B_OK=$(printf '%s' "$DONE_LINE" | sed -n 's|.*done: \([0-9][0-9]*\)/[0-9][0-9]* ok.*|\1|p')
    B_FAIL=$(printf '%s' "$DONE_LINE" | sed -n 's|.*ok, \([0-9][0-9]*\) failed.*|\1|p')
  else
    B_OK=$(grep -c '\[warm\] ok ' "$BATCHLOG")
    B_FAIL=$(grep -c '\[warm\] FAIL ' "$BATCHLOG")
  fi
  B_OK=${B_OK:-0}; B_FAIL=${B_FAIL:-0}
  OK_TOTAL=$((OK_TOTAL + B_OK))
  FAIL_TOTAL=$((FAIL_TOTAL + B_FAIL))

  if [ "${EXIT_CODE:-1}" = "0" ]; then
    log "batch $BATCH_NUM/$NUM_BATCHES: OK ($B_OK ok, $B_FAIL failed of $BSIZE)"
  else
    log "batch $BATCH_NUM/$NUM_BATCHES: WARMER EXITED NON-ZERO (exit=${EXIT_CODE:-unknown}) — $B_OK ok, $B_FAIL failed of $BSIZE"
  fi

  if [ "$KEEP_WARMER" = "1" ]; then
    log "  KEEP_WARMER=1 — leaving warmer $WARMER_MID for inspection"
  else
    log "  destroying warmer $WARMER_MID"
    flyctl machine destroy "$WARMER_MID" -a "$FLEET_APP" --force >/dev/null 2>&1 || true
  fi
  WARMER_MID=""    # cleared: this batch's warmer is handled, EXIT trap won't re-touch it

  if [ "$SLEEP" -gt 0 ] && [ $((i + BATCH)) -lt "$TOTAL" ]; then
    log "sleeping ${SLEEP}s before next batch (Docker Hub rate-limit pacing)"
    sleep "$SLEEP"
  fi
done

WALL=$(( $(date +%s) - START_ALL ))
log "==> done: $OK_TOTAL/$TOTAL image(s) warmed ok, $FAIL_TOTAL failed, $NUM_BATCHES batch(es), ${WALL}s wall"
log "    per-batch logs: $LOGDIR"
if [ "$FAIL_TOTAL" -gt 0 ]; then
  log "    some fetches failed — warm-cache.sh is idempotent (re-fetching an already-cached image is cheap), safe to re-run"
  exit 1
fi
exit 0
