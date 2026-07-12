#!/usr/bin/env bash
# Full end-to-end SWE-bench Verified Mini-50 A/B on a fly.io machine.
#
# Runs the local-docker pipeline (resolve via Docker-in-Docker + swebench grading
# + cost report) ON FLY — native x86_64, no QEMU. The whole job runs in-VM; this
# script only deploys, launches, streams the result beacons, pulls the bundle,
# and destroys the machine.
#
# Usage:
#   ./run.sh                                  # full: mini + codex, mode on, all 50
#   INSTANCES=1 MODELS=gpt-5.4-mini ./run.sh  # SMOKE: 1 instance, 1 model (do this first)
#   MODES=both ./run.sh                        # internal A/B (also run bare codex)
#   MEM=16384 CPUS=8 ./run.sh
#
# Prereqs: flyctl logged in (token auto-read from ~/.fly/config.yml);
#          OPENAI_API_KEY exported or in ../../../../unerr-web-service/.env.local.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CODEX_DIR="$(cd "$HERE/../.." && pwd)"     # e2e/reference/codex — the docker build context
cd "$HERE"

APP="${APP:-swebench-agent-codex-fullresolve}"
ORG="${FLY_ORG:-your-fly-org}"                # fly org; override via FLY_ORG
REGION="${REGION:-iad}"
VOL="${VOL:-bench_data}"
VOL_GB="${VOL_GB:-200}"                      # SWE-bench env images are tens of GB
MEM="${MEM:-16384}"
CPUS="${CPUS:-8}"
MODELS="${MODELS:-gpt-5.4-mini gpt-5.3-codex}"
MODES="${MODES:-on}"
INSTANCES="${INSTANCES:-0}"                  # 0 = all 50 Mini
HOLD="${HOLD:-1800}"

# auth: prefer env, else the saved fly token
if [ -z "${FLY_API_TOKEN:-}" ]; then
  FLY_API_TOKEN="$(node -e "const fs=require('fs');const y=fs.readFileSync(process.env.HOME+'/.fly/config.yml','utf8');const m=y.match(/access_token:\s*(\S+)/);process.stdout.write(m?m[1]:'')")"
fi
export FLY_API_TOKEN
[ -n "$FLY_API_TOKEN" ] || { echo "no fly token (run: flyctl auth login)"; exit 1; }

# openai key (never printed) — prefer env, else the web-service .env.local.
# Repo root is $HERE/../../../../.. ; the web service is its sibling.
if [ -z "${OPENAI_API_KEY:-}" ]; then
  for ENVF in \
      "$HERE/../../../../../../unerr-web-service/.env.local" \
      "$HOME/IdeaProjects/unerr-web-service/.env.local"; do
    [ -f "$ENVF" ] || continue
    OPENAI_API_KEY="$(grep -E '^OPENAI_API_KEY=' "$ENVF" | head -1 | sed 's/^OPENAI_API_KEY=//; s/^["'"'"']//; s/["'"'"']$//')"
    [ -n "$OPENAI_API_KEY" ] && break
  done
fi
[ -n "${OPENAI_API_KEY:-}" ] || { echo "set OPENAI_API_KEY (or add it to unerr-web-service/.env.local)"; exit 1; }
export OPENAI_API_KEY

mkdir -p "$HERE/out"

echo "==> ensuring app $APP exists"
# Idempotent: create, tolerate the "Name has already been taken" race/re-run.
if ! flyctl apps create "$APP" --org "$ORG" 2>/tmp/fly-appcreate.err; then
  grep -qi 'already been taken\|already exists' /tmp/fly-appcreate.err \
    && echo "    app $APP already exists — reusing" \
    || { cat /tmp/fly-appcreate.err; exit 1; }
fi

echo "==> ensuring volume $VOL (${VOL_GB}GB, $REGION)"
# fly ALLOWS duplicate volume names, so only create when none exists — else
# repeated runs would silently stack 200GB volumes. Match the NAME column.
if flyctl volumes list -a "$APP" 2>/dev/null | awk '{print $3}' | grep -qx "$VOL"; then
  echo "    volume $VOL already exists — reusing"
else
  flyctl volumes create "$VOL" -a "$APP" --region "$REGION" --size "$VOL_GB" --yes
fi

# Build on fly's remote builder (no local Docker). Context = e2e/reference/codex.
if [ -n "${IMAGE:-}" ]; then
  IMG="$IMAGE"; echo "==> reusing image: $IMG"
else
  echo "==> building image on fly remote builder (context=$CODEX_DIR)"
  # Pass --config explicitly: with a positional build CONTEXT arg, flyctl looks
  # for fly.toml relative to that dir (CODEX_DIR has none) and otherwise tries to
  # rebuild config "from any machines" — which fails on a fresh, machine-less app
  # ("could not create a fly.toml from any machines"). An explicit --config gives
  # it the app config directly so --build-only never needs the machines.
  flyctl deploy --build-only --remote-only --push -a "$APP" \
    --config "$HERE/fly.toml" \
    --image-label "run-$(date +%s)" \
    --dockerfile "$HERE/Dockerfile" "$CODEX_DIR" 2>&1 | tee "$HERE/out/build.log"
  IMG="$(grep -oE 'registry\.fly\.io/[^ ]+' "$HERE/out/build.log" | tail -1)"
  [ -n "$IMG" ] || { echo "could not determine built image ref"; exit 1; }
  echo "==> image: $IMG"
fi

echo "==> launching one-shot DinD machine ($MEM MB / $CPUS cpu, $REGION, vol $VOL)"
# NOT --quiet: it suppresses the "Machine ID:" line we scrape. Tee the full
# output so a launch error is visible (otherwise it gets eaten by the pipe).
flyctl machine run "$IMG" \
  --app "$APP" --region "$REGION" --vm-memory "$MEM" --vm-cpus "$CPUS" \
  --volume "$VOL:/data" --restart no \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  -e MODELS="$MODELS" -e MODES="$MODES" -e INSTANCES="$INSTANCES" -e HOLD="$HOLD" \
  2>&1 | tee "$HERE/out/machine-run.log"
MID="$(grep -oE 'Machine ID: [0-9a-f]+' "$HERE/out/machine-run.log" | head -1 | awk '{print $3}')"
[ -n "$MID" ] || { echo "machine launch failed — see out/machine-run.log"; exit 1; }
echo "==> machine: $MID — streaming logs until bundle_ready (or machine stops)"

# Stream logs straight to run.log (no tee/pipe subshell — that buffered oddly),
# then poll run.log for the bundle_ready beacon. Watch out/run.log live in another
# terminal: `tail -f out/run.log`.
flyctl logs -a "$APP" --machine "$MID" > "$HERE/out/run.log" 2>&1 &
LOGPID=$!
MAXWAIT="${MAXWAIT:-5400}"; WAITED=0; STOPPED=0
while [ "$WAITED" -lt "$MAXWAIT" ]; do
  if grep -q '"bundle_ready"' "$HERE/out/run.log" 2>/dev/null; then
    echo "==> bundle_ready after ${WAITED}s"; break
  fi
  # Tolerate transient status failures: only conclude the machine is gone after
  # TWO consecutive non-running reports (a single blip returning "" must NOT abort
  # the run — that was the bug that destroyed healthy machines mid-resolve).
  ST="$(flyctl machine status "$MID" -a "$APP" 2>/dev/null | grep -ioE 'state *= *[a-z]+' | head -1)"
  if [ -n "$ST" ] && ! printf '%s' "$ST" | grep -qiE 'started|created|starting'; then
    STOPPED=$((STOPPED + 1))
    [ "$STOPPED" -ge 2 ] && { echo "==> machine no longer running ($ST)"; break; }
  else
    STOPPED=0
  fi
  sleep 15; WAITED=$((WAITED + 15))
done
[ "$WAITED" -ge "$MAXWAIT" ] && echo "==> hit MAXWAIT ${MAXWAIT}s without bundle_ready"
kill "$LOGPID" 2>/dev/null || true

echo "==> result beacons:"
grep -oE '\{"ev":.*\}' "$HERE/out/run.log" | tee "$HERE/out/beacons.jsonl" || true

# Pull the full bundle off the volume while the machine is still holding.
if grep -q '"bundle_ready"' "$HERE/out/run.log" 2>/dev/null; then
  echo "==> pulling /data/bundle.tgz via sftp"
  flyctl ssh sftp get /data/bundle.tgz "$HERE/out/bundle.tgz" -a "$APP" --machine "$MID" 2>&1 | tail -3 || \
    echo "    sftp pull failed — headline numbers are still in out/run.log / out/beacons.jsonl"
  [ -f "$HERE/out/bundle.tgz" ] && { rm -rf "$HERE/out/bundle"; mkdir -p "$HERE/out/bundle"; tar xzf "$HERE/out/bundle.tgz" -C "$HERE/out/bundle" && echo "    extracted -> out/bundle/"; }
else
  echo "==> no bundle_ready beacon — job did not finish; see out/run.log"
fi

echo "==> destroying machine $MID"
flyctl machine destroy "$MID" -a "$APP" --force 2>/dev/null || true
echo "==> done. results in out/ (run.log, beacons.jsonl, bundle/)"
