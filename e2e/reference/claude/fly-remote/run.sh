#!/usr/bin/env bash
# Build the Claude preflight image on fly's REMOTE builder (no local Docker) and
# run it on a machine that STAYS UP (we never destroy it) so the built image +
# installed claude/unerr + seeded repo can be reused for further setup testing.
#
# Preflight is zero cost: no CLAUDE_CODE_OAUTH_TOKEN, no `claude -p`.
#
# Usage:
#   ./run.sh                       # build + launch + stream preflight, keep machine
#   IMAGE=registry.fly.io/... ./run.sh   # skip build, reuse a prebuilt image
#   APP=swebench-agent-claude REGION=iad MEM=2048 CPUS=1 ./run.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CTX="$(cd "$HERE/.." && pwd)"          # build context = e2e/reference/claude (self-contained)
cd "$HERE"

APP="${APP:-swebench-agent-claude}"
ORG="${FLY_ORG:-your-fly-org}"                # fly org; override via FLY_ORG
REGION="${REGION:-iad}"
MEM="${MEM:-2048}"
CPUS="${CPUS:-1}"

# auth: prefer env, else read the saved fly token (same pattern as codex fly-remote)
if [ -z "${FLY_API_TOKEN:-}" ]; then
  FLY_API_TOKEN="$(node -e "const fs=require('fs');const y=fs.readFileSync(process.env.HOME+'/.fly/config.yml','utf8');const m=y.match(/access_token:\s*(\S+)/);process.stdout.write(m?m[1]:'')")"
fi
export FLY_API_TOKEN
[ -n "$FLY_API_TOKEN" ] || { echo "no fly token (run: flyctl auth login)"; exit 1; }

mkdir -p "$HERE/out"

echo "==> ensuring app $APP exists"
flyctl apps list 2>/dev/null | grep -q "^$APP" || flyctl apps create "$APP" --org "$ORG"

if [ -n "${IMAGE:-}" ]; then
  IMG="$IMAGE"; echo "==> reusing image: $IMG"
else
  echo "==> building image on fly remote builder (context: $CTX)"
  flyctl deploy --build-only --remote-only --push -a "$APP" \
    --image-label "preflight-$(date +%s)" \
    --dockerfile "$HERE/Dockerfile" "$CTX" 2>&1 | tee "$HERE/out/build.log"
  IMG="$(grep -oE 'registry\.fly\.io/[^ ]+' "$HERE/out/build.log" | tail -1)"
  [ -n "$IMG" ] || { echo "could not determine built image ref"; exit 1; }
  echo "==> image: $IMG"
fi

echo "==> launching PERSISTENT machine ($MEM MB / $CPUS cpu, region $REGION) — will NOT be destroyed"
# Drop --quiet so the "Machine ID:" line is printed (we scrape it). --restart no
# so a crash doesn't loop, but the entrypoint sleeps forever to keep it up.
MID="$(flyctl machine run "$IMG" \
  --app "$APP" --region "$REGION" --vm-memory "$MEM" --vm-cpus "$CPUS" \
  --restart no \
  2>&1 | tee "$HERE/out/launch.log" | grep -oiE 'Machine ID: [0-9a-f]+' | awk '{print $3}')"
[ -n "$MID" ] || { echo "machine launch failed — see $HERE/out/launch.log"; exit 1; }
echo "==> machine: $MID"

echo "==> streaming logs until preflight reports (Ctrl-C just detaches; machine keeps running)"
( flyctl logs -a "$APP" --machine "$MID" 2>/dev/null & LOGPID=$!
  # stop tailing once preflight printed its exit marker (machine then sleeps)
  for _ in $(seq 1 180); do
    grep -q 'PREFLIGHT_EXIT=' "$HERE/out/run.log" 2>/dev/null && break
    sleep 5
  done
  sleep 3; kill $LOGPID 2>/dev/null || true
) | tee "$HERE/out/run.log"

echo
echo "==> preflight result:"
grep -E '=== preflight summary|PREFLIGHT_EXIT=|\[PASS\]|\[FAIL\]' "$HERE/out/run.log" || true
echo
echo "==> machine $MID is LEFT RUNNING for reuse. Useful commands:"
echo "    re-run preflight:  flyctl ssh console -a $APP -C /opt/toolbox/preflight.sh"
echo "    shell in:          flyctl ssh console -a $APP"
echo "    stop (save \$):     flyctl machine stop $MID -a $APP"
echo "    start again:       flyctl machine start $MID -a $APP"
echo "    destroy when done: flyctl machine destroy $MID -a $APP --force"
