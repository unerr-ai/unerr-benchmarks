#!/usr/bin/env bash
# Full end-to-end SWE-bench A/B for CLAUDE on a fly.io machine (Docker-in-Docker).
#
# Resolve (DinD) + swebench grading run IN-VM on native x86_64 (no QEMU). This
# host script only deploys, launches, streams the result beacons, pulls the
# bundle, and (by default) LEAVES the machine + volume up for reuse.
#
# Usage:
#   INSTANCES=1 ./run.sh                  # SMOKE: 1 instance, both arms (do first)
#   ./run.sh                              # default smoke (INSTANCES=1, MODES=both)
#   INSTANCES=5 ./run.sh                  # pilot
#   INSTANCES=0 ./run.sh                  # full Mini-50
#   MODES=on ./run.sh                     # only the unerr arm
#   KEEP=0 ./run.sh                       # destroy the machine after pulling (volume persists)
#
# Auth (no API key): mint a subscription token ONCE, then either export it or
# let auth-bootstrap write it:
#   (cd ../../local-docker && ./auth-bootstrap.sh)     # writes ../../local-docker/.env.local
#   export CLAUDE_CODE_OAUTH_TOKEN=...                 # or just export it
#
# Prereqs: flyctl logged in (token auto-read from ~/.fly/config.yml).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$(cd "$HERE/../.." && pwd)"      # e2e/reference/claude — the docker build context
cd "$HERE"

APP="${APP:-swebench-agent-claude-fullresolve}"
ORG="${FLY_ORG:-your-fly-org}"                # fly org; override via FLY_ORG
REGION="${REGION:-iad}"
VOL="${VOL:-bench_data}"
VOL_GB="${VOL_GB:-100}"
MEM="${MEM:-8192}"
CPUS="${CPUS:-4}"
MODES="${MODES:-both}"
INSTANCES="${INSTANCES:-1}"                   # 1 = smoke
HOLD="${HOLD:-1800}"
KEEP="${KEEP:-1}"                             # 1 = leave machine running for reuse

# fly auth: prefer env, else the saved fly token
if [ -z "${FLY_API_TOKEN:-}" ]; then
  FLY_API_TOKEN="$(node -e "const fs=require('fs');const y=fs.readFileSync(process.env.HOME+'/.fly/config.yml','utf8');const m=y.match(/access_token:\s*(\S+)/);process.stdout.write(m?m[1]:'')")"
fi
export FLY_API_TOKEN
[ -n "$FLY_API_TOKEN" ] || { echo "no fly token (run: flyctl auth login)"; exit 1; }

# subscription token (never printed): env, else local-docker/.env.local
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  ENVF="$CLAUDE_DIR/local-docker/.env.local"
  [ -f "$ENVF" ] && CLAUDE_CODE_OAUTH_TOKEN="$(grep -E '^CLAUDE_CODE_OAUTH_TOKEN=' "$ENVF" | head -1 | sed 's/^CLAUDE_CODE_OAUTH_TOKEN=//; s/^["'"'"']//; s/["'"'"']$//')"
fi
[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || {
  echo "set CLAUDE_CODE_OAUTH_TOKEN — mint it once with:"
  echo "  (cd $CLAUDE_DIR/local-docker && ./auth-bootstrap.sh)   # writes .env.local"
  echo "  # or: export CLAUDE_CODE_OAUTH_TOKEN=\$(claude setup-token)"
  exit 1
}
export CLAUDE_CODE_OAUTH_TOKEN

mkdir -p "$HERE/out"

echo "==> ensuring app $APP exists"
if ! flyctl apps create "$APP" --org "$ORG" 2>/tmp/fly-appcreate.err; then
  grep -qi 'already been taken\|already exists' /tmp/fly-appcreate.err \
    && echo "    app $APP already exists — reusing" \
    || { cat /tmp/fly-appcreate.err; exit 1; }
fi

echo "==> ensuring volume $VOL (${VOL_GB}GB, $REGION)"
if flyctl volumes list -a "$APP" 2>/dev/null | awk '{print $3}' | grep -qx "$VOL"; then
  echo "    volume $VOL already exists — reusing"
else
  flyctl volumes create "$VOL" -a "$APP" --region "$REGION" --size "$VOL_GB" --yes
fi

# Build on fly's remote builder (no local Docker). Context = e2e/reference/claude.
if [ -n "${IMAGE:-}" ]; then
  IMG="$IMAGE"; echo "==> reusing image: $IMG"
else
  echo "==> building image on fly remote builder (context=$CLAUDE_DIR)"
  flyctl deploy --build-only --remote-only --push -a "$APP" \
    --config "$HERE/fly.toml" \
    --image-label "run-$(date +%s)" \
    --dockerfile "$HERE/Dockerfile" "$CLAUDE_DIR" 2>&1 | tee "$HERE/out/build.log"
  IMG="$(grep -oE 'registry\.fly\.io/[^ ]+' "$HERE/out/build.log" | tail -1)"
  [ -n "$IMG" ] || { echo "could not determine built image ref"; exit 1; }
  echo "==> image: $IMG"
fi

echo "==> launching DinD machine ($MEM MB / $CPUS cpu, $REGION, vol $VOL, instances=$INSTANCES modes=$MODES)"
flyctl machine run "$IMG" \
  --app "$APP" --region "$REGION" --vm-memory "$MEM" --vm-cpus "$CPUS" \
  --volume "$VOL:/data" --restart no \
  -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  -e MODES="$MODES" -e INSTANCES="$INSTANCES" -e HOLD="$HOLD" \
  -e DEBUG_MCP_PROBE="${DEBUG_MCP_PROBE:-0}" -e PROBE_INTERVAL="${PROBE_INTERVAL:-25}" \
  2>&1 | tee "$HERE/out/machine-run.log"
MID="$(grep -oE 'Machine ID: [0-9a-f]+' "$HERE/out/machine-run.log" | head -1 | awk '{print $3}')"
[ -n "$MID" ] || { echo "machine launch failed — see out/machine-run.log"; exit 1; }
echo "==> machine: $MID — streaming logs until bundle_ready"

flyctl logs -a "$APP" --machine "$MID" > "$HERE/out/run.log" 2>&1 &
LOGPID=$!
MAXWAIT="${MAXWAIT:-5400}"; WAITED=0; STOPPED=0
while [ "$WAITED" -lt "$MAXWAIT" ]; do
  if grep -q '"bundle_ready"' "$HERE/out/run.log" 2>/dev/null; then
    echo "==> bundle_ready after ${WAITED}s"; break
  fi
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

if grep -q '"bundle_ready"' "$HERE/out/run.log" 2>/dev/null; then
  echo "==> pulling /data/bundle.tgz via sftp"
  # `fly ssh sftp get` REFUSES to overwrite — a stale bundle.tgz from a prior run
  # makes the pull silently fail and leaves last run's data in place. Clear first.
  rm -f "$HERE/out/bundle.tgz"
  flyctl ssh sftp get /data/bundle.tgz "$HERE/out/bundle.tgz" -a "$APP" --machine "$MID" 2>&1 | tail -3 || \
    echo "    sftp pull failed — headline numbers are still in out/run.log / out/beacons.jsonl"
  [ -f "$HERE/out/bundle.tgz" ] && { rm -rf "$HERE/out/bundle"; mkdir -p "$HERE/out/bundle"; tar xzf "$HERE/out/bundle.tgz" -C "$HERE/out/bundle" && echo "    extracted -> out/bundle/"; }
else
  echo "==> no bundle_ready beacon — job did not finish; see out/run.log"
fi

if [ "$KEEP" = "1" ]; then
  echo "==> KEEP=1 — leaving machine $MID + volume $VOL up for reuse."
  echo "    logs:    flyctl logs -a $APP --machine $MID"
  echo "    shell:   flyctl ssh console -a $APP --machine $MID"
  echo "    stop \$:  flyctl machine stop $MID -a $APP   (volume + data persist)"
  echo "    destroy: flyctl machine destroy $MID -a $APP --force"
else
  echo "==> destroying machine $MID (volume $VOL persists)"
  flyctl machine destroy "$MID" -a "$APP" --force 2>/dev/null || true
fi
echo "==> done. results in out/ (run.log, beacons.jsonl, bundle/)"
