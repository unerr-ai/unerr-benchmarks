#!/usr/bin/env bash
# SINGLE-ARM econ full-resolve SWE-bench Verified Mini on a fly.io machine.
#
# Runs the local-docker pipeline (resolve via Docker-in-Docker + swebench grading
# + cost report) ON FLY — native x86_64, no QEMU. The whole job runs in-VM; this
# script only vendors the local econ binary, deploys, launches, streams the
# result beacons, pulls the bundle, and destroys the machine.
#
# econ is SINGLE ARM (unerr is compiled in) — it runs ONCE per instance. No
# on/off, no MODES, no unerr install/daemon/MCP.
#
# The vendored binary is the LOCAL glibc econ build (linux-x64-baseline), NOT npm
# — the latest econ code is uncommitted locally, so run.sh copies the binary
# straight out of the sibling econ-coding-agent repo before deploying. GLIBC (not
# musl) because the SWE-bench instance images it runs inside are Debian.
#
# Usage:
#   INSTANCES=1  ./run.sh    # SMOKE — 1 instance (do this first)
#   INSTANCES=10 ./run.sh    # Verified Mini-10
#   MEM=8192 CPUS=4 ./run.sh
#
# Prereqs: flyctl logged in (token auto-read from ~/.fly/config.yml);
#          econ built locally (cd <econ-coding-agent> && bun run --cwd packages/opencode build);
#          LITELLM_API_KEY exported or in ../../.env.local (e2e/econ/.env.local).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ECON_DIR="$(cd "$HERE/../.." && pwd)"                              # e2e/econ — the docker build context
ECON_REPO="${ECON_REPO:-$HERE/../../../../../econ-coding-agent}"    # sibling of this benchmark repo
cd "$HERE"

APP="${APP:-unerr-bench-econ-fullresolve}"
ORG="${FLY_ORG:-vamsee-k-933}"              # team space; override via FLY_ORG
REGION="${REGION:-iad}"
VOL="${VOL:-bench_data_econ}"
VOL_GB="${VOL_GB:-200}"                     # SWE-bench env images are tens of GB
INSTANCES="${INSTANCES:-1}"                 # 1 = smoke; 10 = Verified Mini-10
PARALLEL="${PARALLEL:-1}"                   # instances resolved concurrently in-VM
# A parallel resolve runs PARALLEL docker containers at once (each an econ instance).
# The work is I/O-bound — model calls are remote — but every container still holds a
# node/bun process + the test env in RAM, so scale the machine with PARALLEL unless
# MEM/CPUS are pinned explicitly. fly allows going bigger: e.g. MEM=32768 CPUS=16.
if [ "$PARALLEL" -gt 1 ]; then
  MEM="${MEM:-16384}"                       # ~2GB/instance headroom + DinD
  CPUS="${CPUS:-8}"
else
  MEM="${MEM:-8192}"                        # DinD needs room
  CPUS="${CPUS:-4}"
fi
IDS="${IDS:-}"                              # comma-sep instance_ids for a targeted re-run (overrides INSTANCES)
LABEL="${LABEL:-econ}"                      # output/grade label; use a distinct label for a re-run
RESUME="${RESUME:-0}"                       # 1 = keep prior /data/results (don't wipe other labels)
HOLD="${HOLD:-1800}"
MAXWAIT="${MAXWAIT:-5400}"

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

# ── auth: EXA web-search key (OPT-IN; never printed) — econ reads process.env.EXA_API_KEY
# for its websearch tool (packages/opencode/src/tool/mcp-websearch.ts). Web search is
# OFF by default: on SWE-bench the target fixes are public on GitHub, so an enabled web
# search is an answer-lookup / integrity risk and makes results NON-comparable to the
# saved Claude baseline. Enable deliberately with `WEBSEARCH=1 ./run.sh` (or by exporting
# EXA_API_KEY yourself); the key is then resolved from env → e2e/econ/.env.local → ~/.zshrc.
if [ "${WEBSEARCH:-0}" = "1" ] || [ -n "${EXA_API_KEY:-}" ]; then
  if [ -z "${EXA_API_KEY:-}" ] && [ -f "$ECON_DIR/.env.local" ]; then
    EXA_API_KEY="$(grep -E '^EXA_API_KEY=' "$ECON_DIR/.env.local" | head -1 | sed 's/^EXA_API_KEY=//; s/^["'"'"']//; s/["'"'"']$//')"
  fi
  if [ -z "${EXA_API_KEY:-}" ] && [ -f "$HOME/.zshrc" ]; then
    EXA_API_KEY="$(grep -E '(^|[[:space:]])EXA_API_KEY=' "$HOME/.zshrc" | head -1 | sed -E 's/.*EXA_API_KEY=//; s/^["'"'"']//; s/["'"'"']$//')"
  fi
fi
export EXA_API_KEY="${EXA_API_KEY:-}"
[ -n "$EXA_API_KEY" ] \
  && echo "==> EXA_API_KEY: set (len ${#EXA_API_KEY}) — web search ENABLED (NOT baseline-comparable)" \
  || echo "==> EXA_API_KEY: unset — web search disabled (clean, baseline-comparable run)"

mkdir -p "$HERE/out"

# ── vendor the LOCAL econ build into the TOOLBOX build context ────────────────
# Dockerfile.toolbox (build context = local-docker/context) COPYs vendor/opencode,
# vendor/opencode.json, vendor/dot-opencode/ — so vendor them there.
VENDOR="$ECON_DIR/local-docker/context/vendor"
echo "==> vendoring local econ build from $ECON_REPO -> $VENDOR"
mkdir -p "$VENDOR/dot-opencode"
# GLIBC binary (NOT -musl): the SWE-bench instance images are Debian/glibc, so a
# musl-linked binary fails to exec with ENOENT (its /lib/ld-musl loader is absent
# on Debian). `baseline` = older-CPU target for max portability across fly VMs.
BIN="$(find "$ECON_REPO/packages/opencode/dist" -type f -name opencode -path '*/opencode-linux-x64-baseline/bin/opencode' 2>/dev/null | head -1)"
[ -n "$BIN" ] || { echo "no local linux-x64-baseline (glibc) econ build under $ECON_REPO/packages/opencode/dist (run: cd $ECON_REPO && bun install && bun run --cwd packages/opencode build)"; exit 1; }
cp "$BIN" "$VENDOR/opencode"
chmod +x "$VENDOR/opencode"
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

# Co-locate the code-intelligence engine INSIDE the packaged .opencode tree so the
# runtime plugins (econ-recon-inject / econ-governance-gate / econ-checkpoint) can
# import it IN-CONTAINER. In the instance image the plugins ship at
# /testbed/.opencode/plugins/, so their old "../../packages/code-intelligence/..."
# resolve points at /testbed/packages/ — the graded target repo, NOT the econ
# checkout — and fails ("Cannot find module code-intelligence/src/index.ts"). The
# plugins now prefer "../vendor/code-intelligence/src/...", which lands here. The
# engine is self-contained at import time (bun:sqlite + node:* + relative imports;
# web-tree-sitter is an optional lazy upgrade), so src/ + package.json is enough —
# no node_modules. It MUST live under .opencode/ so run-instance.sh's diff exclude
# (':(exclude).opencode') keeps it OUT of the graded model_patch. Dockerfile.toolbox
# COPYs the whole vendor/dot-opencode/ tree, so it ships automatically (no Dockerfile
# edit needed).
CI_SRC="$ECON_REPO/packages/code-intelligence"
if [ -d "$CI_SRC/src" ]; then
  CI_DST="$VENDOR/dot-opencode/vendor/code-intelligence"
  rm -rf "$CI_DST"; mkdir -p "$CI_DST"
  cp -R "$CI_SRC/src" "$CI_DST/src"
  cp "$CI_SRC/package.json" "$CI_DST/package.json" 2>/dev/null || true
  echo "    engine: $CI_SRC/{src,package.json} -> vendor/dot-opencode/vendor/code-intelligence/"
else
  echo "    engine: none at $CI_SRC/src — in-container plugins will fall back to ../../packages and stay disabled"
fi

# ── ensure app + volume ───────────────────────────────────────────────────────
echo "==> ensuring app $APP exists"
if ! flyctl apps create "$APP" --org "$ORG" 2>/tmp/fly-econ-fr-appcreate.err; then
  grep -qi 'already been taken\|already exists' /tmp/fly-econ-fr-appcreate.err \
    && echo "    app $APP already exists — reusing" \
    || { cat /tmp/fly-econ-fr-appcreate.err; exit 1; }
fi

echo "==> ensuring volume $VOL (${VOL_GB}GB, $REGION)"
# fly ALLOWS duplicate volume names, so only create when none exists — else
# repeated runs would silently stack 200GB volumes. Match the NAME column.
if flyctl volumes list -a "$APP" 2>/dev/null | awk '{print $3}' | grep -qx "$VOL"; then
  echo "    volume $VOL already exists — reusing"
else
  flyctl volumes create "$VOL" -a "$APP" --region "$REGION" --size "$VOL_GB" --yes
fi

# ── build on fly's remote builder (no local Docker). Context = e2e/econ. ──────
if [ -n "${IMAGE:-}" ]; then
  IMG="$IMAGE"; echo "==> reusing image: $IMG"
else
  echo "==> building image on fly remote builder (context=$ECON_DIR)"
  # Explicit --config: with a positional build CONTEXT arg flyctl looks for
  # fly.toml relative to that dir (ECON_DIR has none) and otherwise tries to
  # rebuild config from machines — which fails on a fresh, machine-less app.
  flyctl deploy --build-only --remote-only --push -a "$APP" \
    --config "$HERE/fly.toml" \
    --image-label "fr-$(date +%s)" \
    --dockerfile "$HERE/Dockerfile" "$ECON_DIR" 2>&1 | tee "$HERE/out/build.log"
  IMG="$(grep -oE 'registry\.fly\.io/[^ ]+' "$HERE/out/build.log" | tail -1)"
  [ -n "$IMG" ] || { echo "could not determine built image ref"; exit 1; }
  echo "==> image: $IMG"
fi

# ── launch one-shot DinD machine ──────────────────────────────────────────────
echo "==> launching one-shot DinD machine ($MEM MB / $CPUS cpu, $REGION, vol $VOL, parallel=$PARALLEL)"
# NOT --quiet: it suppresses the "Machine ID:" line we scrape. Tee the full
# output so a launch error is visible (otherwise it gets eaten by the pipe).
flyctl machine run "$IMG" \
  --app "$APP" --region "$REGION" --vm-memory "$MEM" --vm-cpus "$CPUS" \
  --volume "$VOL:/data" --restart no \
  -e LITELLM_API_KEY="$LITELLM_API_KEY" \
  -e EXA_API_KEY="$EXA_API_KEY" \
  -e INSTANCES="$INSTANCES" -e HOLD="$HOLD" \
  -e IDS="$IDS" -e LABEL="$LABEL" -e RESUME="$RESUME" \
  -e PARALLEL="$PARALLEL" ${GRADE_WORKERS:+-e GRADE_WORKERS="$GRADE_WORKERS"} \
  2>&1 | tee "$HERE/out/machine-run.log"
MID="$(grep -oE 'Machine ID: [0-9a-f]+' "$HERE/out/machine-run.log" | head -1 | awk '{print $3}')"
[ -n "$MID" ] || { echo "machine launch failed — see out/machine-run.log"; exit 1; }
echo "==> machine: $MID — streaming logs until bundle_ready (or machine stops)"

# ── stream logs, poll for the bundle_ready beacon ─────────────────────────────
flyctl logs -a "$APP" --machine "$MID" > "$HERE/out/run.log" 2>&1 &
LOGPID=$!
WAITED=0; STOPPED=0
while [ "$WAITED" -lt "$MAXWAIT" ]; do
  if grep -q '"bundle_ready"' "$HERE/out/run.log" 2>/dev/null; then
    echo "==> bundle_ready after ${WAITED}s"; break
  fi
  # Tolerate transient status blips: only conclude the machine is gone after TWO
  # consecutive non-running reports (a single blip must NOT abort the run).
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

# ── pull the full bundle off the volume while the machine is still holding ────
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

# ── destroy the machine unless KEEP=1 (default KEEP=1) ────────────────────────
if [ "${KEEP:-1}" = "1" ]; then
  echo "==> KEEP=1 (default) — leaving machine $MID running (destroy with: flyctl machine destroy $MID -a $APP --force)"
else
  echo "==> destroying machine $MID"
  flyctl machine destroy "$MID" -a "$APP" --force 2>/dev/null || true
fi
echo "==> done. results in out/ (run.log, beacons.jsonl, bundle/)"
