#!/usr/bin/env bash
# Run the SWE-bench Lite localization A/B on a fly.io machine (codex ±unerr).
# No local Docker: fly's remote builder builds the image. Results stream to
# ./out/loc-results.jsonl (scraped from machine stdout), then the machine is destroyed.
#
# Usage:
#   ./run.sh                       # default slice (requests:6,flask:3,pytest:6)
#   SELECT=requests:2 LIMIT=2 ./run.sh    # tiny smoke
#   APP=swebench-agent-codex REGION=iad ./run.sh
#
# Prereqs: flyctl logged in (token in ~/.fly/config.yml is auto-read);
#          OPENAI_API_KEY exported (or in ../../../unerr-web-service/.env.local).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
E2E_DIR="$(cd "$HERE/../../.." && pwd)"
cd "$HERE"

APP="${APP:-swebench-agent-codex}"
ORG="${FLY_ORG:-your-fly-org}"                # fly org; override via FLY_ORG
REGION="${REGION:-iad}"
SELECT="${SELECT:-requests:6,flask:3,pytest:6}"
LIMIT="${LIMIT:-}"
ARMS="${ARMS:-baseline,unerr}"
MEM="${MEM:-4096}"
CPUS="${CPUS:-2}"

# auth: prefer env, else read the saved fly token
if [ -z "${FLY_API_TOKEN:-}" ]; then
  FLY_API_TOKEN="$(node -e "const fs=require('fs');const y=fs.readFileSync(process.env.HOME+'/.fly/config.yml','utf8');const m=y.match(/access_token:\s*(\S+)/);process.stdout.write(m?m[1]:'')")"
fi
export FLY_API_TOKEN
[ -n "$FLY_API_TOKEN" ] || { echo "no fly token (run: flyctl auth login)"; exit 1; }

# openai key
if [ -z "${OPENAI_API_KEY:-}" ]; then
  OPENAI_API_KEY="$(grep -E '^OPENAI_API_KEY=' "$HERE/../../../unerr-web-service/.env.local" 2>/dev/null | cut -d= -f2- | tr -d '\n')"
fi
[ -n "$OPENAI_API_KEY" ] || { echo "set OPENAI_API_KEY"; exit 1; }

mkdir -p "$HERE/out"

echo "==> ensuring app $APP exists"
flyctl apps list 2>/dev/null | grep -q "^$APP" || flyctl apps create "$APP" --org "$ORG"

# reuse a prebuilt image with IMAGE=registry.fly.io/...  to skip the build
if [ -n "${IMAGE:-}" ]; then
  IMG="$IMAGE"; echo "==> reusing image: $IMG"
else
  echo "==> building image on fly remote builder (no local Docker)"
  flyctl deploy --build-only --remote-only --push -a "$APP" --image-label "run-$(date +%s)" --dockerfile "$HERE/Dockerfile" "$E2E_DIR" 2>&1 | tee "$HERE/out/build.log"
  IMG="$(grep -oE 'registry\.fly\.io/[^ ]+' "$HERE/out/build.log" | tail -1)"
  [ -n "$IMG" ] || { echo "could not determine built image ref"; exit 1; }
  echo "==> image: $IMG"
fi

echo "==> launching one-shot machine ($MEM MB / $CPUS cpu, region $REGION)"
MID="$(flyctl machine run "$IMG" \
  --app "$APP" --region "$REGION" --vm-memory "$MEM" --vm-cpus "$CPUS" \
  --restart no --quiet \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" -e SELECT="$SELECT" -e ARMS="$ARMS" ${LIMIT:+-e LIMIT="$LIMIT"} \
  2>&1 | grep -oE 'Machine ID: [0-9a-f]+' | awk '{print $3}')"
[ -n "$MID" ] || { echo "machine launch failed — see above"; exit 1; }
echo "==> machine: $MID — streaming logs (Ctrl-C just detaches; machine keeps running)"

# stream logs to file until the machine stops
( flyctl logs -a "$APP" --machine "$MID" 2>/dev/null & LOGPID=$!
  while flyctl machine status "$MID" -a "$APP" 2>/dev/null | grep -qE 'state *= *(started|created|starting)'; do sleep 10; done
  sleep 5; kill $LOGPID 2>/dev/null || true
) | tee "$HERE/out/run.log"

echo "==> collecting results"
grep -oE '\{"ev":"result".*\}' "$HERE/out/run.log" | sed 's/^{"ev":"result",/{/' > "$HERE/out/loc-results.jsonl" || true
echo "    rows: $(wc -l < "$HERE/out/loc-results.jsonl" 2>/dev/null || echo 0) -> out/loc-results.jsonl"

echo "==> destroying machine $MID"
flyctl machine destroy "$MID" -a "$APP" --force 2>/dev/null || true
echo "==> done."
