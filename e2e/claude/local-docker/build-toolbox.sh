#!/usr/bin/env bash
# Build the `unerr-claude-toolbox` image from THIS unerr-cli checkout.
#
#   1. pnpm pack the local unerr  -> ships prebuilt dist/ + native postinstall
#   2. refresh the entitlement minter from the unerr checkout (source of truth)
#   3. docker build the relocatable toolbox carrier image (Claude Code + unerr)
#
# Unlike the codex builder, this tree is SELF-CONTAINED: context/ already holds
# lib.sh, run-instance.sh, preflight.sh and mcp-healthcheck.mjs (committed
# Claude variants), and context/ IS the docker build context. We only stage two
# build-time artifacts into it: the packed unerr tarball and the latest
# dev-entitlement.mjs. Nothing in e2e/codex or e2e/common is touched.
#
# Re-run whenever you change unerr source and want it in the benchmark.

set -euo pipefail
# SWE-bench instance images are linux/amd64 only. On Apple Silicon (arm64) the
# toolbox must be built amd64 too, or its node/native binaries can't exec when
# grafted onto the x86_64 instance image (runs under emulation). Force it.
export DOCKER_DEFAULT_PLATFORM="${DOCKER_DEFAULT_PLATFORM:-linux/amd64}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CTX="$HERE/context"
UNERR_REPO="${UNERR_REPO:-$(cd "$HERE/../../../../unerr-cli" 2>/dev/null && pwd || true)}"

echo "==> unerr repo: ${UNERR_REPO:-<unset>}"
[ -n "${UNERR_REPO:-}" ] && [ -f "$UNERR_REPO/package.json" ] || { echo "set UNERR_REPO to your unerr-cli checkout"; exit 1; }

mkdir -p "$CTX"
rm -f "$CTX"/unerr-ai-unerr-*.tgz

echo "==> building + packing unerr (DEV build — all __UNERR_DEV_BUILD__ features ON)"
# UNERR_PROD_BUILD=0 KEEPS the __UNERR_DEV_BUILD__ blocks in the bundle, so the
# benchmarked binary exercises the FULL feature set (not the trimmed published
# binary). This is intentional for the A/B: we want to measure unerr with every
# feature active. Offline Pro is unaffected either way — it rides the
# UNERR_ENTITLEMENT_KID/_PUBKEY env override + UNERR_TOKEN login hatch. Override
# with UNERR_PROD_BUILD=1 to benchmark the bit-for-bit published binary instead.
( cd "$UNERR_REPO" && pnpm install --frozen-lockfile=false && UNERR_PROD_BUILD="${UNERR_PROD_BUILD:-0}" pnpm run build && pnpm pack --pack-destination "$CTX" )

# The pack's `files` whitelist excludes scripts/, but package.json postinstall runs
# `node scripts/build-contracts.mjs --optional`. Without the file shipped, npm install
# hard-fails (MODULE_NOT_FOUND) instead of the intended clean no-op. Inject the
# self-contained (node-builtins-only) script so the postinstall behaves.
TGZ="$(ls "$CTX"/unerr-ai-unerr-*.tgz | head -1)"
echo "==> injecting scripts/build-contracts.mjs into $(basename "$TGZ") (postinstall no-op fix)"
_inj="$(mktemp -d)"
tar xzf "$TGZ" -C "$_inj"
mkdir -p "$_inj/package/scripts"
cp "$UNERR_REPO/scripts/build-contracts.mjs" "$_inj/package/scripts/build-contracts.mjs"
( cd "$_inj" && tar czf "$TGZ" package )
rm -rf "$_inj"

echo "==> refreshing dev-entitlement.mjs from the unerr checkout (source of truth)"
cp "$UNERR_REPO/scripts/dev-entitlement.mjs" "$CTX/dev-entitlement.mjs"

echo "==> docker build unerr-claude-toolbox"
docker build -f "$HERE/Dockerfile.toolbox" -t unerr-claude-toolbox "$CTX"

echo "==> done. toolbox image: unerr-claude-toolbox"
docker run --rm --entrypoint /opt/toolbox/node/bin/node unerr-claude-toolbox \
  /opt/toolbox/bin/unerr --version 2>/dev/null || true
docker run --rm --entrypoint /opt/toolbox/node/bin/node unerr-claude-toolbox \
  /opt/toolbox/bin/claude --version 2>/dev/null || true
