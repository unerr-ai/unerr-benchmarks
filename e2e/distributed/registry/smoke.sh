#!/usr/bin/env bash
# Smoke test: does the deployed swebench-registry actually proxy AND cache?
#
# WHERE TO RUN THIS — the registry is private-only (6PN/flycast, no public
# IP), so this script only works from a machine already on the fleet's private
# network. Two ways:
#
#   1. From the registry app itself:
#        flyctl ssh console -a swebench-registry -C '/bin/sh -c "$(cat smoke.sh)"'
#      or copy this file in and run it after `flyctl ssh console -a swebench-registry`.
#
#   2. From any worker (or the coordinator) of a live distributed fleet — same
#      org/private network, so it reaches the flycast address too:
#        flyctl ssh console -a swebench-agent-dist -C '/bin/sh -c "$(cat smoke.sh)"'
#
# It will NOT work from your laptop / the fly remote builder / CI unless
# those are also attached to the same 6PN (e.g. via `flyctl wireguard` / a
# WireGuard peer) — a plain `curl` from outside the private network times out
# by design (that's the point of keeping this registry private-only).
#
# Usage: ENDPOINT=http://swebench-registry.flycast:5000 ./smoke.sh
set -euo pipefail

ENDPOINT="${ENDPOINT:-http://swebench-registry.flycast:5000}"
IMAGE="library/hello-world"     # tiny, public, on docker.io — safe to pull repeatedly
TAG="latest"
ACCEPT='application/vnd.docker.distribution.manifest.v2+json,application/vnd.oci.image.manifest.v1+json,application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json'

echo "==> smoke-testing $ENDPOINT"

# 1) base v2 API check — the distribution spec requires this to return 200
#    (with an empty JSON body) for a reachable, correctly-configured registry.
echo "==> [1/3] GET $ENDPOINT/v2/ (registry reachable)"
CODE="$(curl -s -o /dev/null -w '%{http_code}' "$ENDPOINT/v2/")"
if [ "$CODE" != "200" ]; then
  echo "    FAIL: expected 200, got $CODE — registry unreachable or misconfigured"
  exit 1
fi
echo "    OK: 200"

# 2) first manifest pull — proxied through to docker.io, populates the cache
#    (first-hit latency includes the real upstream round-trip).
echo "==> [2/3] GET $ENDPOINT/v2/$IMAGE/manifests/$TAG (first pull — proxies to docker.io, populates cache)"
T0=$(date +%s%N)
CODE="$(curl -s -o /tmp/swebench-registry-smoke-manifest1.json -w '%{http_code}' -H "Accept: $ACCEPT" \
  "$ENDPOINT/v2/$IMAGE/manifests/$TAG")"
T1=$(date +%s%N)
if [ "$CODE" != "200" ]; then
  echo "    FAIL: expected 200, got $CODE"
  cat /tmp/swebench-registry-smoke-manifest1.json
  exit 1
fi
FIRST_MS=$(( (T1 - T0) / 1000000 ))
echo "    OK: 200 in ${FIRST_MS}ms"

# 3) second manifest pull — should be served from the local cache (the mounted
#    registry_data volume), so materially faster than the first. This is the
#    cache-hit signal: a proxy that ISN'T caching would show similar latency
#    both times (every pull re-hits docker.io).
echo "==> [3/3] GET $ENDPOINT/v2/$IMAGE/manifests/$TAG (second pull — expect a cache hit)"
T0=$(date +%s%N)
CODE="$(curl -s -o /tmp/swebench-registry-smoke-manifest2.json -w '%{http_code}' -H "Accept: $ACCEPT" \
  "$ENDPOINT/v2/$IMAGE/manifests/$TAG")"
T1=$(date +%s%N)
if [ "$CODE" != "200" ]; then
  echo "    FAIL: expected 200, got $CODE"
  cat /tmp/swebench-registry-smoke-manifest2.json
  exit 1
fi
SECOND_MS=$(( (T1 - T0) / 1000000 ))
echo "    OK: 200 in ${SECOND_MS}ms"

echo ""
echo "==> summary: first pull ${FIRST_MS}ms, second pull ${SECOND_MS}ms"
if [ "$SECOND_MS" -lt "$FIRST_MS" ]; then
  echo "    second pull was faster — consistent with a cache hit (not a proof;"
  echo "    for a hard guarantee, diff the two manifest JSON files and check"
  echo "    /var/lib/registry growth on the registry machine)."
else
  echo "    WARN: second pull was NOT faster than the first — re-run once (docker.io"
  echo "    itself can be noisy) before concluding the proxy cache isn't hitting."
fi

diff -q /tmp/swebench-registry-smoke-manifest1.json /tmp/swebench-registry-smoke-manifest2.json >/dev/null \
  && echo "    manifests match byte-for-byte across both pulls (expected)." \
  || echo "    WARN: manifest bytes differ between pulls — unexpected for the same tag."
