#!/usr/bin/env bash
# Distributed SWE-bench runner — HOST LAUNCHER.
#
# Sibling of e2e/econ/fly-remote/fullresolve/run.sh. Where run.sh drives ONE
# DinD machine serially, this fans the work across a work-stealing FLEET: one
# small COORDINATOR machine (SQLite queue) + N worker machines (16GB/8cpu, no
# volume) that claim → resolve → grade → report one instance at a time and
# self-stop when the queue drains. The host builds the image, seeds the queue,
# paces out the workers, polls the coordinator, pulls one merged bundle, and
# tears the fleet down by metadata.
#
# It REUSES run.sh's helpers verbatim where possible: fly-token read, app/volume
# ensure, the econ-binary vendor step, and the `flyctl deploy --build-only
# --remote-only --push` remote build (+ IMAGE= reuse + MANIFEST_UNKNOWN retry).
# resolve/grade themselves are never reimplemented — the worker shells out to the
# SAME e2e/<arm>/local-docker/run-benchmark.py + swebench harness the image bakes.
#
# Usage:
#   MACHINES=2 ARM=econ LABEL=dist-smoke TASKS="django__django-11880,django__django-11951,django__django-11790" ./run-distributed.sh
#   MACHINES=5 ARM=econ LABEL=mini SUITE=mini ./run-distributed.sh
#   MACHINES=25 ARM=econ LABEL=full-verified ./run-distributed.sh          # SUITE defaults to full Verified
#   DESTROY_ONLY=1 LABEL=dist-smoke ./run-distributed.sh                   # just tear a fleet down
#
# Prereqs: flyctl logged in (token auto-read from ~/.fly/config.yml); econ built
#          locally (see RUNBOOK §1); LITELLM_API_KEY exported or in e2e/econ/.env.local;
#          python3 on PATH (+ `datasets` only if SUITE=full/verified/lite).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"                               # e2e/distributed
E2E_DIR="$(cd "$HERE/.." && pwd)"                                   # e2e — the docker build context
ECON_DIR="$E2E_DIR/econ"                                            # arm pipeline + .env.local
ECON_REPO="${ECON_REPO:-$HERE/../../../econ-coding-agent}"         # sibling of this benchmark repo
cd "$HERE"

# ── config ────────────────────────────────────────────────────────────────────
APP="${APP:-unerr-bench-dist}"              # FIXED app; each run scoped by fleet=<LABEL> metadata
ORG="${FLY_ORG:-vamsee-k-933}"              # team space; override via FLY_ORG
REGION="${REGION:-iad}"
ARM="${ARM:-econ}"
LABEL="${LABEL:-}"                          # REQUIRED, unique per run — doubles as RUN_ID + fleet metadata
MACHINES="${MACHINES:-}"                    # REQUIRED (unless DESTROY_ONLY) — worker count N
DESTROY_ONLY="${DESTROY_ONLY:-0}"           # 1 = only tear down the fleet named by LABEL, then exit
IMAGE="${IMAGE:-}"                          # reuse a prior built image ref (skip the remote build)

# worker sizing (shared-cpu-8x / 16GB — SWE-bench env images + DinD need the room)
MEM="${MEM:-16384}"
CPUS="${CPUS:-8}"
ROOTFS_GB="${ROOTFS_GB:-}"                  # OPTIONAL ephemeral-rootfs size (see NOTE at worker create)
# fly hard-caps config.rootfs.size_gb at 50 (0/unset = image default). Clamp so a
# too-large value degrades to the max instead of failing every worker create.
if [ -n "$ROOTFS_GB" ] && [ "$ROOTFS_GB" -gt 50 ] 2>/dev/null; then
  echo "==> ROOTFS_GB=$ROOTFS_GB exceeds fly's 50 GB max — clamping to 50" >&2
  ROOTFS_GB=50
fi

# coordinator sizing (tiny — it only runs SQLite + aiohttp)
COORD_SIZE="${COORD_SIZE:-shared-cpu-1x}"
COORD_MEM="${COORD_MEM:-1024}"
# fly volume names allow only lowercase alphanumeric + underscores, ≤30 chars —
# LABEL may contain hyphens/uppercase, so sanitize it for the volume name only
# (metadata fleet=<LABEL> keeps the raw label).
_VOL_SLUG="$(printf 'dist_coord_%s' "$LABEL" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9_' '_' | cut -c1-30)"
COORD_VOL="${COORD_VOL:-$_VOL_SLUG}"            # the fleet's ONE small volume
COORD_VOL_GB="${COORD_VOL_GB:-10}"

# task set + grading
SUITE="${SUITE:-}"
TASKS="${TASKS:-}"
TASKS_FILE="${TASKS_FILE:-}"
SPLIT="${SPLIT:-test}"
# Grade against the dataset the suite came from unless DATASET is pinned.
if [ -z "${DATASET:-}" ]; then
  case "$(printf '%s' "${SUITE:-}" | tr 'A-Z' 'a-z')" in
    lite) DATASET="princeton-nlp/SWE-bench_Lite" ;;
    *)    DATASET="princeton-nlp/SWE-bench_Verified" ;;
  esac
fi

HOLD="${HOLD:-3600}"                        # coordinator holds open this long after bundle for the pull
MAXWAIT="${MAXWAIT:-172800}"                # host poll ceiling (48h — covers long/large runs; the fleet self-stops on drain regardless of this)
PER_INSTANCE_TIMEOUT="${PER_INSTANCE_TIMEOUT:-14400}"  # per-task resolve ceiling (4h); the worker kills a resolve that runs past this
GRADE_WORKERS="${GRADE_WORKERS:-6}"         # swebench harness --max_workers on the worker
HEARTBEAT_TIMEOUT="${HEARTBEAT_TIMEOUT:-300}"          # coordinator reaps a lease after this much heartbeat silence (10 beats @30s)
KEEP="${KEEP:-0}"                           # 1 = keep the coordinator volume on teardown
PY_HOST="${PYTHON:-python3}"

log()  { printf '[dist] %s\n' "$*" >&2; }

# ── auth: fly token — prefer env, else the saved token (verbatim from run.sh) ──
if [ -z "${FLY_API_TOKEN:-}" ]; then
  FLY_API_TOKEN="$(node -e "const fs=require('fs');const y=fs.readFileSync(process.env.HOME+'/.fly/config.yml','utf8');const m=y.match(/access_token:\s*(\S+)/);process.stdout.write(m?m[1]:'')")"
fi
export FLY_API_TOKEN
[ -n "$FLY_API_TOKEN" ] || { echo "no fly token (run: flyctl auth login)"; exit 1; }

# ── fleet helpers (metadata-scoped) ───────────────────────────────────────────
# Machine ids for this fleet (optionally filtered by role) via the JSON list —
# metadata is not shown in the table view. python3 (already a prereq) avoids a
# host jq dependency.
fleet_ids() {                               # fleet_ids [role]
  flyctl machines list -a "$APP" --json 2>/dev/null | "$PY_HOST" -c '
import sys, json
role  = sys.argv[1]
label = sys.argv[2]
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
' "${1:-}" "$LABEL"
}

destroy_fleet() {
  log "tearing down fleet '$LABEL' (metadata fleet=$LABEL) on app $APP"
  local ids; ids="$(fleet_ids || true)"
  if [ -z "$ids" ]; then
    log "  no machines with fleet=$LABEL"
  else
    for m in $ids; do
      log "  destroy machine $m"
      flyctl machine destroy "$m" -a "$APP" --force 2>&1 | tail -1 || true
    done
  fi
  if [ "$KEEP" = "1" ]; then
    log "  KEEP=1 — leaving coordinator volume $COORD_VOL in place"
  else
    local vids
    vids="$(flyctl volumes list -a "$APP" --json 2>/dev/null | "$PY_HOST" -c '
import sys, json
name = sys.argv[1]
try:
    vs = json.load(sys.stdin)
except Exception:
    vs = []
for v in vs:
    if v.get("name") == name:
        print(v.get("id"))
' "$COORD_VOL")"
    for v in $vids; do
      log "  destroy volume $v ($COORD_VOL)"
      flyctl volume destroy "$v" -a "$APP" --yes 2>&1 | tail -1 || true
    done
  fi
}

# ── DESTROY_ONLY: nuke the fleet named by LABEL and exit ──────────────────────
if [ "$DESTROY_ONLY" = "1" ]; then
  [ -n "$LABEL" ] || { echo "DESTROY_ONLY=1 needs LABEL=<fleet>"; exit 1; }
  destroy_fleet
  log "destroy-only done."
  exit 0
fi

# ── required args ─────────────────────────────────────────────────────────────
[ -n "$LABEL" ]    || { echo "set LABEL=<unique-run-id> (names the fleet metadata + coordinator RUN_ID)"; exit 1; }
[ -n "$MACHINES" ] || { echo "set MACHINES=<N> (number of worker machines)"; exit 1; }
case "$MACHINES" in (*[!0-9]*|'') echo "MACHINES must be a positive integer"; exit 1;; esac
[ "$MACHINES" -ge 1 ] || { echo "MACHINES must be >= 1"; exit 1; }

OUTDIR="$HERE/out/dist-$LABEL"
mkdir -p "$OUTDIR"

# ── auth: LiteLLM gateway key (never printed) — prefer env, else e2e/econ/.env.local (from run.sh) ──
if [ -z "${LITELLM_API_KEY:-}" ] && [ -f "$ECON_DIR/.env.local" ]; then
  LITELLM_API_KEY="$(grep -E '^LITELLM_API_KEY=' "$ECON_DIR/.env.local" | head -1 | sed 's/^LITELLM_API_KEY=//; s/^["'"'"']//; s/["'"'"']$//')"
fi
[ -n "${LITELLM_API_KEY:-}" ] || { echo "set LITELLM_API_KEY (or add it to e2e/econ/.env.local)"; exit 1; }
export LITELLM_API_KEY
echo "==> LITELLM_API_KEY: set (len ${#LITELLM_API_KEY})"

# EXA web-search key — web search OFF by default for a clean, baseline-comparable
# run (SWE-bench fixes are public → web search = answer-lookup). STRICTLY opt-in:
# only WEBSEARCH=1 enables it. An ambient EXA_API_KEY in the shell env is IGNORED
# (matches the single-machine runbook's `env -u EXA_API_KEY WEBSEARCH=0` invariant).
if [ "${WEBSEARCH:-0}" = "1" ]; then
  if [ -z "${EXA_API_KEY:-}" ] && [ -f "$ECON_DIR/.env.local" ]; then
    EXA_API_KEY="$(grep -E '^EXA_API_KEY=' "$ECON_DIR/.env.local" | head -1 | sed 's/^EXA_API_KEY=//; s/^["'"'"']//; s/["'"'"']$//')"
  fi
  export EXA_API_KEY="${EXA_API_KEY:-}"
else
  export EXA_API_KEY=""   # ignore any ambient key unless WEBSEARCH=1
fi
[ -n "$EXA_API_KEY" ] \
  && echo "==> EXA_API_KEY: set (len ${#EXA_API_KEY}) — web search ENABLED (NOT baseline-comparable)" \
  || echo "==> EXA_API_KEY: unset — web search disabled (clean, baseline-comparable run)"

# ── vendor the LOCAL econ build into the TOOLBOX build context (from run.sh) ───
# The fleet image bakes the arm toolbox, so the vendored glibc binary must be in
# econ/local-docker/context/vendor/ before the remote build (as run.sh does).
if [ "$ARM" = "econ" ]; then
  VENDOR="$ECON_DIR/local-docker/context/vendor"
  echo "==> vendoring local econ build from $ECON_REPO -> $VENDOR"
  mkdir -p "$VENDOR/dot-opencode"
  # GLIBC binary (NOT -musl): the SWE-bench instance images are Debian/glibc.
  BIN="$(find "$ECON_REPO/packages/opencode/dist" -type f -name opencode -path '*/opencode-linux-x64-baseline/bin/opencode' 2>/dev/null | head -1)"
  [ -n "$BIN" ] || { echo "no local linux-x64-baseline (glibc) econ build under $ECON_REPO/packages/opencode/dist (run: cd $ECON_REPO && bun install && bun run --cwd packages/opencode build)"; exit 1; }
  cp "$BIN" "$VENDOR/opencode"; chmod +x "$VENDOR/opencode"
  echo "    binary: $BIN -> vendor/opencode ($(du -h "$VENDOR/opencode" | cut -f1))"
  [ -f "$ECON_REPO/opencode.json" ] || { echo "no opencode.json at $ECON_REPO/opencode.json"; exit 1; }
  cp "$ECON_REPO/opencode.json" "$VENDOR/opencode.json"
  echo "    config: $ECON_REPO/opencode.json -> vendor/opencode.json"
  rm -rf "${VENDOR:?}/dot-opencode"; mkdir -p "$VENDOR/dot-opencode"
  if [ -d "$ECON_REPO/.opencode" ]; then
    cp -R "$ECON_REPO/.opencode/." "$VENDOR/dot-opencode/"
    echo "    plugins: $ECON_REPO/.opencode -> vendor/dot-opencode/"
    # Prune econ's GitHub project tools: .opencode/tool/github-*.ts import
    # @opencode-ai/plugin and are useless in an OFFLINE SWE-bench resolve. The
    # .dockerignore fix ships the dep so they COULD load; dropping them here means
    # opencode never even tries — leaner image + one fewer way to crash session
    # startup (a single unloadable tool aborts econ with exit=1, 0 turns).
    rm -f "$VENDOR"/dot-opencode/tool/github-*.ts
    echo "    pruned dot-opencode/tool/github-*.ts (unused offline; belt-and-suspenders w/ .dockerignore fix)"
  else
    echo "    plugins: none at $ECON_REPO/.opencode — vendor/dot-opencode/ left empty"
  fi
  CI_SRC="$ECON_REPO/packages/code-intelligence"
  if [ -d "$CI_SRC/src" ]; then
    CI_DST="$VENDOR/dot-opencode/vendor/code-intelligence"
    rm -rf "$CI_DST"; mkdir -p "$CI_DST"
    cp -R "$CI_SRC/src" "$CI_DST/src"
    cp "$CI_SRC/package.json" "$CI_DST/package.json" 2>/dev/null || true
    echo "    engine: $CI_SRC/{src,package.json} -> vendor/dot-opencode/vendor/code-intelligence/"
  else
    echo "    engine: none at $CI_SRC/src — in-container plugins fall back and stay disabled"
  fi
fi

# ── resolve the task set → INSTANCE_IDS (comma-separated) ─────────────────────
echo "==> resolving task set (suite=${SUITE:-full} tasks=${TASKS:+set} file=${TASKS_FILE:-none})"
INSTANCE_IDS="$("$PY_HOST" "$HERE/tools/suite.py" \
  ${SUITE:+--suite "$SUITE"} ${TASKS:+--tasks "$TASKS"} ${TASKS_FILE:+--file "$TASKS_FILE"} \
  --dataset "$DATASET" --split "$SPLIT")"
[ -n "$INSTANCE_IDS" ] || { echo "suite.py resolved 0 ids"; exit 1; }
NUM_IDS="$(printf '%s' "$INSTANCE_IDS" | tr ',' '\n' | grep -c .)"
echo "    $NUM_IDS instance(s); dataset=$DATASET split=$SPLIT"

# ── ensure app + coordinator volume (from run.sh) ─────────────────────────────
echo "==> ensuring app $APP exists"
if ! flyctl apps create "$APP" --org "$ORG" 2>/tmp/fly-dist-appcreate.err; then
  grep -qi 'already been taken\|already exists' /tmp/fly-dist-appcreate.err \
    && echo "    app $APP already exists — reusing" \
    || { cat /tmp/fly-dist-appcreate.err; exit 1; }
fi

echo "==> ensuring coordinator volume $COORD_VOL (${COORD_VOL_GB}GB, $REGION)"
# fly ALLOWS duplicate volume names, so only create when none exists. Match NAME.
if flyctl volumes list -a "$APP" 2>/dev/null | awk '{print $3}' | grep -qx "$COORD_VOL"; then
  echo "    volume $COORD_VOL already exists — reusing"
else
  flyctl volumes create "$COORD_VOL" -a "$APP" --region "$REGION" --size "$COORD_VOL_GB" --yes
fi

# ── build the fleet image on fly's remote builder (context = e2e) ─────────────
if [ -n "$IMAGE" ]; then
  IMG="$IMAGE"; echo "==> reusing image: $IMG"
else
  echo "==> building fleet image on fly remote builder (context=$E2E_DIR)"
  # Explicit --config: with a positional build CONTEXT arg flyctl looks for
  # fly.toml relative to that dir (e2e has none) and otherwise tries to rebuild
  # config from machines — which fails on a fresh, machine-less app.
  flyctl deploy --build-only --remote-only --push -a "$APP" \
    --config "$HERE/fly.toml" \
    --image-label "dist-$(date +%s)" \
    --dockerfile "$HERE/Dockerfile.dist" "$E2E_DIR" 2>&1 | tee "$OUTDIR/build.log"
  IMG="$(grep -oE 'registry\.fly\.io/[^ ]+' "$OUTDIR/build.log" | tail -1)"
  [ -n "$IMG" ] || { echo "could not determine built image ref"; exit 1; }
  echo "==> image: $IMG"
fi

# ── run_machine: `flyctl machine run` with a MANIFEST_UNKNOWN / 429 / capacity retry ──
# The just-pushed image manifest can lag behind the machine-create call
# (MANIFEST_UNKNOWN); the Machines API create is also rate-limited (429) and can
# hit transient capacity errors. Reuse the SAME already-pushed IMG on each retry.
run_machine() {                             # run_machine <logfile> <flyctl machine run args...>
  local logf="$1"; shift
  local tries=0 max="${RUN_RETRIES:-6}"
  while [ "$tries" -lt "$max" ]; do
    tries=$((tries + 1))
    if flyctl machine run "$@" >"$logf" 2>&1; then
      return 0
    fi
    if grep -qiE 'MANIFEST_UNKNOWN|429|rate limit|capacity|please try again' "$logf"; then
      log "  transient machine-create error (try $tries/$max): $(grep -ioE 'MANIFEST_UNKNOWN|429|rate limit|capacity' "$logf" | head -1) — retry in $((tries * 5))s (same image)"
      sleep $((tries * 5)); continue
    fi
    log "  machine run FAILED (non-transient) — tail:"; tail -6 "$logf" >&2
    return 1
  done
  log "  machine run gave up after $tries tries"; return 1
}

# ── create the COORDINATOR machine (1 volume, coordinator entrypoint) ──────────
echo "==> creating coordinator ($COORD_SIZE / ${COORD_MEM}MB, vol $COORD_VOL, $NUM_IDS tasks)"
run_machine "$OUTDIR/coord-run.log" "$IMG" \
  --app "$APP" --region "$REGION" \
  --vm-size "$COORD_SIZE" --vm-memory "$COORD_MEM" \
  --volume "$COORD_VOL:/data" --restart no \
  --entrypoint /work/distributed/coordinator/coordinator-entrypoint.sh \
  --metadata fleet="$LABEL" --metadata role=coordinator \
  -e RUN_ID="$LABEL" -e TASKS="$INSTANCE_IDS" \
  -e DATASET="$DATASET" -e SPLIT="$SPLIT" -e HOLD="$HOLD" \
  -e HEARTBEAT_TIMEOUT="$HEARTBEAT_TIMEOUT" \
  || { echo "coordinator create failed — see $OUTDIR/coord-run.log"; exit 1; }
cat "$OUTDIR/coord-run.log"
COORD_MID="$(grep -oE 'Machine ID: [0-9a-f]+' "$OUTDIR/coord-run.log" | head -1 | awk '{print $3}')"
[ -n "$COORD_MID" ] || { echo "could not scrape coordinator machine id"; exit 1; }
# 6PN internal address the workers dial (and the host polls via ssh, below).
COORD_URL="http://${COORD_MID}.vm.${APP}.internal:8080"
echo "==> coordinator: $COORD_MID  ($COORD_URL)"

echo "==> waiting for coordinator to reach 'started'"
for _ in $(seq 1 60); do
  ST="$(flyctl machine status "$COORD_MID" -a "$APP" 2>/dev/null | grep -ioE 'state *= *[a-z]+' | head -1)"
  printf '%s' "$ST" | grep -qi started && { echo "    $ST"; break; }
  sleep 5
done

# ── create N WORKER machines, PACED (burst 3 then sleep) ──────────────────────
# Machines API create is ~1 req/s (burst 3) per app → burst 3, sleep 3; retry
# 429/MANIFEST/capacity in run_machine. Workers take NO volume — DinD data-root
# lives on ephemeral rootfs (PLAN decision 2). NOTE: if flyctl in use lacks
# `--rootfs-size`, leave ROOTFS_GB unset (default rootfs) and, if the env-image
# unpack is IOPS-starved on the smoke, flip a worker to a volume instead.
echo "==> creating $MACHINES worker(s) (${MEM}MB/${CPUS}cpu, no volume, paced <=1/s)"
ROOTFS_FLAG=(); [ -n "$ROOTFS_GB" ] && ROOTFS_FLAG=(--rootfs-size "$ROOTFS_GB")
WORKERS_OK=0
for i in $(seq 1 "$MACHINES"); do
  if run_machine "$OUTDIR/worker-$i-run.log" "$IMG" \
      --app "$APP" --region "$REGION" \
      --vm-memory "$MEM" --vm-cpus "$CPUS" "${ROOTFS_FLAG[@]+"${ROOTFS_FLAG[@]}"}" \
      --restart no \
      --entrypoint /work/distributed/worker/worker-entrypoint.sh \
      --metadata fleet="$LABEL" --metadata role=worker \
      -e COORDINATOR_URL="$COORD_URL" -e ARM="$ARM" -e RUN_ID="$LABEL" \
      -e DATASET="$DATASET" -e SPLIT="$SPLIT" \
      -e PER_INSTANCE_TIMEOUT="$PER_INSTANCE_TIMEOUT" -e GRADE_WORKERS="$GRADE_WORKERS" \
      -e LITELLM_API_KEY="$LITELLM_API_KEY" -e EXA_API_KEY="$EXA_API_KEY"; then
    WID="$(grep -oE 'Machine ID: [0-9a-f]+' "$OUTDIR/worker-$i-run.log" | head -1 | awk '{print $3}')"
    WORKERS_OK=$((WORKERS_OK + 1))
    echo "    worker $i/$MACHINES: ${WID:-?}"
  else
    echo "    worker $i/$MACHINES: FAILED (see $OUTDIR/worker-$i-run.log) — continuing"
  fi
  # pacing: burst of 3, then a 3s cool-off; otherwise ~1/s.
  if [ $((i % 3)) -eq 0 ]; then sleep 3; else sleep 1; fi
done
echo "==> $WORKERS_OK/$MACHINES workers created"
[ "$WORKERS_OK" -ge 1 ] || { echo "no workers came up — tearing down"; destroy_fleet; exit 1; }

# ── poll the coordinator until the bundle is ready ────────────────────────────
# The host is NOT on 6PN, so reach the coordinator's HTTP endpoint by running
# curl INSIDE the coordinator via ssh (localhost:8080). We also tail its logs for
# the `"bundle_ready"` beacon (same shape run.sh watches on the single machine).
echo "==> streaming coordinator logs + polling /status (every 30s, up to ${MAXWAIT}s)"
flyctl logs -a "$APP" --machine "$COORD_MID" > "$OUTDIR/coord.log" 2>&1 &
LOGPID=$!
poll_status() {
  flyctl ssh console -a "$APP" --machine "$COORD_MID" -C "curl -s localhost:8080/status" 2>/dev/null \
    | grep -vE 'Connecting|Waiting|Connected|already'
}
WAITED=0; STOPPED=0
while [ "$WAITED" -lt "$MAXWAIT" ]; do
  if grep -q '"bundle_ready"' "$OUTDIR/coord.log" 2>/dev/null; then
    echo "==> bundle_ready after ${WAITED}s"; break
  fi
  S="$(poll_status || true)"
  if [ -n "$S" ]; then
    printf '%s' "$S" | "$PY_HOST" -c '
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    print("    /status:", raw[:200]); raise SystemExit
done = d.get("done", d.get("completed"))
total = d.get("total")
res = d.get("resolved")
print(f"    progress: done={done} total={total} resolved={res} raw={json.dumps(d)[:160]}")
' 2>/dev/null || echo "    /status: $S"
    STOPPED=0
  else
    # Tolerate transient blips: only conclude the coordinator is gone after TWO
    # consecutive empty status reads AND a non-running machine state.
    ST="$(flyctl machine status "$COORD_MID" -a "$APP" 2>/dev/null | grep -ioE 'state *= *[a-z]+' | head -1)"
    if [ -n "$ST" ] && ! printf '%s' "$ST" | grep -qiE 'started|created|starting'; then
      STOPPED=$((STOPPED + 1))
      [ "$STOPPED" -ge 2 ] && { echo "==> coordinator no longer running ($ST)"; break; }
    fi
  fi
  sleep 30; WAITED=$((WAITED + 30))
done
[ "$WAITED" -ge "$MAXWAIT" ] && echo "==> hit MAXWAIT ${MAXWAIT}s without bundle_ready"
kill "$LOGPID" 2>/dev/null || true

echo "==> coordinator result beacons:"
grep -oE '\{"ev":.*\}' "$OUTDIR/coord.log" | tee "$OUTDIR/beacons.jsonl" || true

# ── pull the one merged bundle off the coordinator volume (from run.sh) ───────
if grep -q '"bundle_ready"' "$OUTDIR/coord.log" 2>/dev/null; then
  echo "==> pulling /data/bundle.tgz via sftp"
  # `fly ssh sftp get` REFUSES to overwrite — clear a stale bundle first.
  rm -f "$OUTDIR/bundle.tgz"
  flyctl ssh sftp get /data/bundle.tgz "$OUTDIR/bundle.tgz" -a "$APP" --machine "$COORD_MID" 2>&1 | tail -3 || \
    echo "    sftp pull failed — headline numbers are still in $OUTDIR/coord.log / beacons.jsonl"
  [ -f "$OUTDIR/bundle.tgz" ] && { rm -rf "$OUTDIR/bundle"; mkdir -p "$OUTDIR/bundle"; tar xzf "$OUTDIR/bundle.tgz" -C "$OUTDIR/bundle" && echo "    extracted -> $OUTDIR/bundle/"; }
else
  echo "==> no bundle_ready beacon — fleet did not finish; see $OUTDIR/coord.log"
fi

# ── teardown: destroy the whole fleet by metadata; remove the coord volume ────
# Workers self-stopped on drain (restart:no → stopped, compute billing stops);
# the coordinator is destroyed now that the bundle is pulled.
destroy_fleet
echo "==> done. results in $OUTDIR/ (coord.log, beacons.jsonl, bundle/)"
