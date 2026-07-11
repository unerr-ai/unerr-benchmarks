#!/usr/bin/env bash
# Build the `unerr-codex-toolbox` image from THIS unerr-cli checkout.
#
#   1. pnpm pack the local unerr  -> ships prebuilt dist/ + native postinstall
#   2. copy the entitlement minter + driver into the build context
#   3. docker build the relocatable toolbox carrier image
#
# Re-run whenever you change unerr source and want it in the benchmark.

set -euo pipefail
# SWE-bench instance images are linux/amd64 only. On Apple Silicon (arm64) the
# toolbox must be built amd64 too, or its node/native binaries can't exec when
# grafted onto the x86_64 instance image (runs under emulation). Force it.
export DOCKER_DEFAULT_PLATFORM="${DOCKER_DEFAULT_PLATFORM:-linux/amd64}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CTX="$HERE/context"
COMMON="$(cd "$HERE/../../common" && pwd)"
UNERR_REPO="${UNERR_REPO:-$(cd "$HERE/../../../../unerr-cli" && pwd)}"

echo "==> unerr repo: $UNERR_REPO"
[ -f "$UNERR_REPO/package.json" ] || { echo "set UNERR_REPO to your unerr-cli checkout"; exit 1; }

mkdir -p "$CTX"
rm -f "$CTX"/unerr-ai-unerr-*.tgz

echo "==> building + packing unerr (PROD build — same binary as published npm)"
# UNERR_PROD_BUILD=1 makes esbuild strip the __UNERR_DEV_BUILD__ blocks
# (dev.json / dev-mode), so the benchmarked binary is bit-for-bit the published
# one. Offline Pro still works: it rides the UNERR_ENTITLEMENT_KID/_PUBKEY env
# override (entitlement-keys.ts) + UNERR_TOKEN login hatch, NEITHER of which is
# stripped by the prod flag. So the benchmark runs the real shipping binary at
# the full Pro tier, with no login and no cloud.
( cd "$UNERR_REPO" && pnpm install --frozen-lockfile=false && UNERR_PROD_BUILD=1 pnpm run build && pnpm pack --pack-destination "$CTX" )

# The pack's `files` whitelist excludes scripts/, but package.json postinstall runs
# `node scripts/build-contracts.mjs --optional`. Without the file shipped, npm install
# hard-fails (MODULE_NOT_FOUND) instead of the intended clean no-op (the script
# self-exits 0 when the vendor submodule is absent, as it is for a packed consumer).
# Inject the self-contained (node-builtins-only) script so the postinstall behaves.
TGZ="$(ls "$CTX"/unerr-ai-unerr-*.tgz | head -1)"
echo "==> injecting scripts/build-contracts.mjs into $(basename "$TGZ") (postinstall no-op fix)"
_inj="$(mktemp -d)"
tar xzf "$TGZ" -C "$_inj"
mkdir -p "$_inj/package/scripts"
cp "$UNERR_REPO/scripts/build-contracts.mjs" "$_inj/package/scripts/build-contracts.mjs"
( cd "$_inj" && tar czf "$TGZ" package )
rm -rf "$_inj"

echo "==> staging minter + driver + preflight into build context"
cp "$UNERR_REPO/scripts/dev-entitlement.mjs" "$CTX/dev-entitlement.mjs"
cp "$COMMON/mcp-healthcheck.mjs"              "$CTX/mcp-healthcheck.mjs"
cp "$COMMON/lib.sh"                           "$CTX/lib.sh"
cp "$HERE/run-instance.sh"                    "$CTX/run-instance.sh"
cp "$COMMON/preflight.sh"                     "$CTX/preflight.sh"

echo "==> docker build unerr-codex-toolbox"
docker build -f "$HERE/Dockerfile.toolbox" -t unerr-codex-toolbox "$CTX"

echo "==> done. toolbox image: unerr-codex-toolbox"
docker run --rm --entrypoint /opt/toolbox/node/bin/node unerr-codex-toolbox \
  /opt/toolbox/bin/unerr --version 2>/dev/null || true
