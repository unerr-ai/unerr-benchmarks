#!/usr/bin/env bash
# econ SMOKE runner on a fly.io machine.
#
# Runs the EXACT same smoke as e2e/econ/smoke.sh but ON a fly.io machine — to
# prove econ + LiteLLM-gateway auth + telemetry + the SQLite per-tier reader
# all work in the fly environment before a real SWE-bench mini run. This is a
# proof-of-life smoke: no Docker-in-Docker, no SWE-bench images — just one
# tiny task run through econ's headless CLI.
#
# The vendored binary is the LOCAL econ build (linux-x64-baseline-musl), NOT
# npm — the latest econ code is uncommitted locally, so run.sh copies the
# binary straight out of the sibling econ-coding-agent repo's dist/ before
# deploying.
#
# Usage:
#   bash run.sh                 # vendor local binary, build, launch, stream, destroy
#   KEEP=1 bash run.sh          # leave the machine running after the smoke finishes
#   MEM=4096 CPUS=4 bash run.sh
#
# Prereqs: flyctl logged in (token auto-read from ~/.fly/config.yml);
#          econ built locally (cd ../../../../econ-coding-agent && bun run --cwd packages/opencode build);
#          LITELLM_API_KEY exported or in ../.env.local (e2e/econ/.env.local).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ECON_DIR="$(cd "$HERE/.." && pwd)"                              # e2e/econ — the docker build context
ECON_REPO="${ECON_REPO:-$HERE/../../../../econ-coding-agent}"    # sibling of this benchmark repo
cd "$HERE"

APP="${APP:-unerr-bench-econ}"
ORG="${FLY_ORG:-vamsee-k-933}"    # team space; override via FLY_ORG
REGION="${REGION:-iad}"
MEM="${MEM:-2048}"                # smoke is light — no DinD, no SWE-bench images
CPUS="${CPUS:-2}"
HOLD="${HOLD:-60}"
MAXWAIT="${MAXWAIT:-900}"

# ── auth: fly token — prefer env, else the saved token ────────────────────────
if [ -z "${FLY_API_TOKEN:-}" ]; then
  FLY_API_TOKEN="$(node -e "const fs=require('fs');const y=fs.readFileSync(process.env.HOME+'/.fly/config.yml','utf8');const m=y.match(/access_token:\s*(\S+)/);process.stdout.write(m?m[1]:'')")"
fi
export FLY_API_TOKEN
[ -n "$FLY_API_TOKEN" ] || { echo "no fly token (run: flyctl auth login)"; exit 1; }

# ── auth: LiteLLM gateway key (never printed) — prefer env, else e2e/econ/.env.local ──
if [ -z "${LITELLM_API_KEY:-}" ] && [ -f "$ECON_DIR/.env.local" ]; then
  LITELLM_API_KEY="$(grep -E '^LITELLM_API_KEY=' "$ECON_DIR/.env.local" | head -1 | sed 's/^LITELLM_API_KEY=//; s/^["'"'"']//; s/["'"'"']$//')"
fi
[ -n "${LITELLM_API_KEY:-}" ] || { echo "set LITELLM_API_KEY (or add it to e2e/econ/.env.local)"; exit 1; }
export LITELLM_API_KEY
echo "==> LITELLM_API_KEY: set (len ${#LITELLM_API_KEY})"

mkdir -p "$HERE/out"

# ── vendor the LOCAL econ build (linux-x64-baseline-musl) — not npm ───────────
echo "==> vendoring local econ build from $ECON_REPO"
mkdir -p "$HERE/vendor/dot-opencode"
BIN="$(find "$ECON_REPO/packages/opencode/dist" -type f -name opencode -path '*linux-x64-baseline-musl*' 2>/dev/null | head -1)"
[ -n "$BIN" ] || { echo "no local linux-x64-baseline-musl econ build under $ECON_REPO/packages/opencode/dist (run: cd $ECON_REPO && bun install && bun run --cwd packages/opencode build)"; exit 1; }
cp "$BIN" "$HERE/vendor/opencode"
chmod +x "$HERE/vendor/opencode"
# the vendored binary is linux — not runnable on mac, so just confirm it landed.
echo "    binary: $BIN -> vendor/opencode ($(du -h "$HERE/vendor/opencode" | cut -f1))"

[ -f "$ECON_REPO/opencode.json" ] || { echo "no opencode.json at $ECON_REPO/opencode.json"; exit 1; }
cp "$ECON_REPO/opencode.json" "$HERE/vendor/opencode.json"
echo "    config: $ECON_REPO/opencode.json -> vendor/opencode.json"

rm -rf "${HERE:?}/vendor/dot-opencode"; mkdir -p "$HERE/vendor/dot-opencode"
if [ -d "$ECON_REPO/.opencode" ]; then
  cp -R "$ECON_REPO/.opencode/." "$HERE/vendor/dot-opencode/"
  echo "    plugins: $ECON_REPO/.opencode -> vendor/dot-opencode/"
else
  echo "    plugins: none at $ECON_REPO/.opencode — vendor/dot-opencode/ left empty"
fi

# Co-locate the code-intelligence engine INSIDE the packaged .opencode tree so the
# runtime plugins (econ-recon-inject / econ-governance-gate / econ-checkpoint) can
# import it at runtime. In-container the plugins ship at <repo>/.opencode/plugins/,
# where the old "../../packages/code-intelligence/..." resolve points outside the
# econ checkout and fails; the plugins now prefer "../vendor/code-intelligence/src/
# ...", which lands here. Engine is self-contained at import time (bun:sqlite +
# node:* + relative imports), so src/ + package.json suffices — no node_modules.
CI_SRC="$ECON_REPO/packages/code-intelligence"
if [ -d "$CI_SRC/src" ]; then
  CI_DST="$HERE/vendor/dot-opencode/vendor/code-intelligence"
  rm -rf "$CI_DST"; mkdir -p "$CI_DST"
  cp -R "$CI_SRC/src" "$CI_DST/src"
  cp "$CI_SRC/package.json" "$CI_DST/package.json" 2>/dev/null || true
  echo "    engine: $CI_SRC/{src,package.json} -> vendor/dot-opencode/vendor/code-intelligence/"
else
  echo "    engine: none at $CI_SRC/src — plugins fall back to ../../packages and stay disabled in-container"
fi

# ── ensure app exists ──────────────────────────────────────────────────────────
echo "==> ensuring app $APP exists"
if ! flyctl apps create "$APP" --org "$ORG" 2>/tmp/fly-econ-appcreate.err; then
  grep -qi 'already been taken\|already exists' /tmp/fly-econ-appcreate.err \
    && echo "    app $APP already exists — reusing" \
    || { cat /tmp/fly-econ-appcreate.err; exit 1; }
fi

# ── build on fly's remote builder (no local Docker). Context = e2e/econ. ──────
if [ -n "${IMAGE:-}" ]; then
  IMG="$IMAGE"; echo "==> reusing image: $IMG"
else
  echo "==> building image on fly remote builder (context=$ECON_DIR)"
  flyctl deploy --build-only --remote-only --push -a "$APP" \
    --config "$HERE/fly.toml" \
    --dockerfile "$HERE/Dockerfile" \
    --image-label "smoke-$(date +%s)" \
    "$ECON_DIR" 2>&1 | tee "$HERE/out/build.log"
  IMG="$(grep -oE 'registry\.fly\.io/[^ ]+' "$HERE/out/build.log" | tail -1)"
  [ -n "$IMG" ] || { echo "could not determine built image ref"; exit 1; }
  echo "==> image: $IMG"
fi

# ── launch one-shot machine (no ports, no volume) ──────────────────────────────
echo "==> launching one-shot smoke machine ($MEM MB / $CPUS cpu, $REGION)"
# NOT --quiet: it suppresses the "Machine ID:" line we scrape. Tee the full
# output so a launch error is visible (otherwise it gets eaten by the pipe).
flyctl machine run "$IMG" \
  --app "$APP" --region "$REGION" --vm-memory "$MEM" --vm-cpus "$CPUS" \
  --restart no \
  -e LITELLM_API_KEY="$LITELLM_API_KEY" \
  2>&1 | tee "$HERE/out/machine-run.log"
MID="$(grep -oE 'Machine ID: [0-9a-f]+' "$HERE/out/machine-run.log" | head -1 | awk '{print $3}')"
[ -n "$MID" ] || { echo "machine launch failed — see out/machine-run.log"; exit 1; }
echo "==> machine: $MID — streaming logs until RESULT: (or machine stops)"

# ── stream logs, poll for the smoke's verdict beacon ───────────────────────────
flyctl logs -a "$APP" --machine "$MID" > "$HERE/out/run.log" 2>&1 &
LOGPID=$!
WAITED=0; STOPPED=0
while [ "$WAITED" -lt "$MAXWAIT" ]; do
  if grep -q 'RESULT:' "$HERE/out/run.log" 2>/dev/null; then
    echo "==> RESULT: beacon seen after ${WAITED}s"; break
  fi
  # Tolerate transient status blips: only conclude the machine is gone after
  # TWO consecutive non-running reports (a single blip must NOT abort the run).
  ST="$(flyctl machine status "$MID" -a "$APP" 2>/dev/null | grep -ioE 'state *= *[a-z]+' | head -1)"
  if [ -n "$ST" ] && ! printf '%s' "$ST" | grep -qiE 'started|created|starting'; then
    STOPPED=$((STOPPED + 1))
    [ "$STOPPED" -ge 2 ] && { echo "==> machine no longer running ($ST)"; break; }
  else
    STOPPED=0
  fi
  sleep 10; WAITED=$((WAITED + 10))
done
[ "$WAITED" -ge "$MAXWAIT" ] && echo "==> WARN: hit MAXWAIT ${MAXWAIT}s without a RESULT: beacon"
kill "$LOGPID" 2>/dev/null || true

echo "==> smoke verdict:"
grep -A2 'SMOKE VERDICT' "$HERE/out/run.log" || echo "    (no SMOKE VERDICT block found — see out/run.log)"
grep 'RESULT:' "$HERE/out/run.log" || true

# ── destroy the machine unless KEEP=1 ──────────────────────────────────────────
if [ "${KEEP:-0}" = "1" ]; then
  echo "==> KEEP=1 — leaving machine $MID running"
else
  echo "==> destroying machine $MID"
  flyctl machine destroy "$MID" -a "$APP" --force 2>/dev/null || true
fi
echo "==> done. results in out/ (build.log, machine-run.log, run.log)"
