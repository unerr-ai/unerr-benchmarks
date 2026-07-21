#!/usr/bin/env bash
# Build the `unerr-claude-toolbox` image from THIS unerr-cli checkout.
#
#   1. pnpm pack the local unerr  -> ships prebuilt dist/ + native postinstall
#   2. refresh the entitlement minter from the unerr checkout (source of truth)
#   3. docker build the relocatable toolbox carrier image (Claude Code + unerr)
#
# Unlike the codex builder, this tree is SELF-CONTAINED: context/ already holds
# lib.sh, run-instance.sh, preflight.sh and mcp-healthcheck.mjs (committed
# Claude variants), and context/ IS the docker build context. We stage three
# build-time artifacts into it: the packed unerr tarball, the latest
# dev-entitlement.mjs, and a vendored python-build-standalone tarball (the
# self-contained python3 tools/harbor_agents.py's ClaudeUnerrAgent.install()
# uploads into terminal-bench task containers that ship no python3 at all —
# see e2e/distributed/HARNESS_UNIVERSAL.md §2/§13 "environment-footprint
# principle"). Nothing in e2e/reference/codex or e2e/common is touched.
#
# Re-run whenever you change unerr source and want it in the benchmark.
#
# --vendor-only / VENDOR_ONLY=1: fetch ONLY the vendored python-build-standalone
# tarball, skipping steps 1-3 and the docker build — see that flag below.

set -euo pipefail
# SWE-bench instance images are linux/amd64 only. On Apple Silicon (arm64) the
# toolbox must be built amd64 too, or its node/native binaries can't exec when
# grafted onto the x86_64 instance image (runs under emulation). Force it.
export DOCKER_DEFAULT_PLATFORM="${DOCKER_DEFAULT_PLATFORM:-linux/amd64}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CTX="$HERE/context"

# --vendor-only / VENDOR_ONLY=1: fetch ONLY the build-context artifacts this script
# vendors (currently just the python-build-standalone tarball below) and skip the
# unerr rebuild/repack + docker build entirely. Exists so a MISSING vendored
# artifact (e.g. run-distributed.sh's preflight gate failing) can be fixed with a
# pure fetch — running the full script instead would REBUILD the vendored unerr
# tgz from whatever the sibling unerr-cli checkout currently holds, silently
# changing which unerr build the benchmark measures. Full-run behaviour (flag
# absent) is unchanged.
VENDOR_ONLY="${VENDOR_ONLY:-0}"
for _arg in "$@"; do
  [ "$_arg" = "--vendor-only" ] && VENDOR_ONLY=1
done

mkdir -p "$CTX"

if [ "$VENDOR_ONLY" = "1" ]; then
  echo "==> VENDOR_ONLY=1 (--vendor-only): vendoring build-context artifacts only — skipping unerr rebuild + docker build"
else
  UNERR_REPO="${UNERR_REPO:-$(cd "$HERE/../../../../../unerr-cli" 2>/dev/null && pwd || true)}"

  echo "==> unerr repo: ${UNERR_REPO:-<unset>}"
  [ -n "${UNERR_REPO:-}" ] && [ -f "$UNERR_REPO/package.json" ] || { echo "set UNERR_REPO to your unerr-cli checkout"; exit 1; }

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
fi

# Vendor a self-contained CPython (python-build-standalone, linux-x86_64,
# "install_only" build) for tools/harbor_agents.py's ClaudeUnerrAgent to
# upload into terminal-bench task containers that ship no python3/python at
# all (~30% of TB2.1 base images) — never `apt-get install python3` inside
# the task's own environment (see HARNESS_UNIVERSAL.md's environment-
# footprint principle). Fetched once and reused across builds — delete the
# tarball under context/ to force a re-fetch at a newer
# PY_STANDALONE_RELEASE/PY_STANDALONE_PYVER.
PY_STANDALONE_RELEASE="${PY_STANDALONE_RELEASE:-20241016}"
PY_STANDALONE_PYVER="${PY_STANDALONE_PYVER:-3.12.7}"
PY_STANDALONE_TGZ="cpython-${PY_STANDALONE_PYVER}+${PY_STANDALONE_RELEASE}-x86_64-unknown-linux-gnu-install_only.tar.gz"
if ls "$CTX"/cpython-*-x86_64-unknown-linux-gnu-install_only.tar.gz >/dev/null 2>&1; then
  echo "==> python-build-standalone already vendored: $(basename "$(ls "$CTX"/cpython-*-x86_64-unknown-linux-gnu-install_only.tar.gz | head -1)")"
else
  echo "==> fetching python-build-standalone ($PY_STANDALONE_TGZ)"
  curl -fsSL "https://github.com/astral-sh/python-build-standalone/releases/download/${PY_STANDALONE_RELEASE}/${PY_STANDALONE_TGZ}" \
    -o "$CTX/$PY_STANDALONE_TGZ"
fi

if [ "$VENDOR_ONLY" = "1" ]; then
  echo "==> vendor-only: done — context/ has the artifacts it needs; skipping docker build"
  exit 0
fi

echo "==> docker build unerr-claude-toolbox"
docker build -f "$HERE/Dockerfile.toolbox" -t unerr-claude-toolbox "$CTX"

echo "==> done. toolbox image: unerr-claude-toolbox"
docker run --rm --entrypoint /opt/toolbox/node/bin/node unerr-claude-toolbox \
  /opt/toolbox/bin/unerr --version 2>/dev/null || true
docker run --rm --entrypoint /opt/toolbox/node/bin/node unerr-claude-toolbox \
  /opt/toolbox/bin/claude --version 2>/dev/null || true
