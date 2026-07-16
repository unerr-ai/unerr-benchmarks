#!/usr/bin/env bash
# Deploy (or re-deploy) the shared SWE-bench pull-through registry cache.
#
# Idempotent — every step below is safe to re-run against a live app: app
# create / bucket-secret check / IP allocate all no-op when already present, and
# `flyctl deploy` just ships a new release of the same long-lived machines.
# This is arm-agnostic infra: run it once, every distributed run (econ,
# claude open-weight, codex, future arms) shares the result.
#
# Usage:
#   cd e2e/distributed/registry
#   ./deploy.sh
#   FLY_ORG=my-org REGION=lax MACHINES=2 ./deploy.sh
#
# Requires: flyctl logged in (same auth `run-distributed.sh` uses).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

APP="swebench-registry"
BUCKET="${BUCKET:-swebench-registry-cache}"   # shared Tigris (S3) blob cache
ORG="${FLY_ORG:-vamsee-k-933}"
REGION="${REGION:-iad}"
MACHINES="${MACHINES:-2}"   # HA + parallel pull throughput; ONE shared bucket

echo "==> swebench-registry deploy: app=$APP org=$ORG region=$REGION bucket=$BUCKET machines=$MACHINES"

# ── app ─────────────────────────────────────────────────────────────────────
echo "==> ensuring app $APP exists"
if ! flyctl apps create "$APP" --org "$ORG" 2>/tmp/swebench-registry-appcreate.err; then
  grep -qi 'already been taken\|already exists' /tmp/swebench-registry-appcreate.err \
    && echo "    app $APP already exists — reusing" \
    || { cat /tmp/swebench-registry-appcreate.err; exit 1; }
fi

# ── shared blob cache = Tigris (S3) bucket ─────────────────────────────────
# ONE bucket shared by every replica (vs single-attach fly volumes, which cache
# independently → a pre-seed warms only one replica). `fly storage create`
# provisions the bucket AND sets the AWS_* secrets reg-entrypoint.sh maps onto
# the s3 driver. Guarded by the secret presence: re-running `fly storage create`
# would mint a SECOND bucket, so skip once AWS_ACCESS_KEY_ID is set.
echo "==> ensuring Tigris bucket '$BUCKET' + AWS_* secrets on $APP"
if ! flyctl storage create -n "$BUCKET" -o "$ORG" -a "$APP" -y 2>/tmp/swebench-registry-storage.err; then
  grep -qi 'already exists\|already been' /tmp/swebench-registry-storage.err \
    && echo "    bucket '$BUCKET' already exists — reusing (AWS_* secrets already on $APP)" \
    || { cat /tmp/swebench-registry-storage.err; exit 1; }
fi

# ── private flycast address ────────────────────────────────────────────────
# `flyctl ips allocate-v6 --private` (verified against `flyctl ips allocate-v6
# --help` + fly.io/docs/networking/flycast/) allocates the app-wide private
# IPv6 that Fly Proxy answers at `<app>.flycast` — a STABLE address that
# survives redeploys/machine-id changes, unlike `<machine_id>.vm.<app>.
# internal` which needs the current machine id, or `<app>.internal` which
# resolves to whichever machine(s) happen to be up. Flycast needs zero
# allocation on a per-worker-consumer side (workers just reach it over 6PN,
# same private network as the fleet app) but the *app being cached* does need
# this one-time allocation, which is what this step does.
echo "==> allocating private flycast address (idempotent — errors on repeat are expected/ignored)"
if ! flyctl ips allocate-v6 --private -a "$APP" --org "$ORG" 2>/tmp/swebench-registry-ipalloc.err; then
  grep -qi 'already\|exist' /tmp/swebench-registry-ipalloc.err \
    && echo "    private flycast address already allocated — reusing" \
    || { echo "    WARN: flycast allocation failed for an unexpected reason:"; cat /tmp/swebench-registry-ipalloc.err; \
         echo "    continuing — verify manually with: flyctl ips list -a $APP"; }
fi

# Safety net Fly's own Flycast docs call out explicitly: a PUBLIC IP on this
# app would expose the registry (and the Docker Hub token, once set via
# secrets) to the internet. This app should never carry one — nothing above
# requests one — but list them for a human to check; deliberately NOT
# auto-releasing here (guessing the wrong IP to release is worse than a
# manual step).
echo "==> current IPs on $APP (verify no public/v4/dedicated-v6 entries — should be private-v6 only):"
flyctl ips list -a "$APP" || true

# ── deploy ──────────────────────────────────────────────────────────────────
echo "==> deploying $APP (registry:2 + Tigris-S3 config, pull-through/proxy mode)"
flyctl deploy -a "$APP" --ha=false
echo "==> scaling to $MACHINES machine(s) in $REGION (HA + parallel pull throughput; shared Tigris bucket)"
flyctl scale count "$MACHINES" -a "$APP" --region "$REGION" --yes

# ── done ────────────────────────────────────────────────────────────────────
FLYCAST_URL="http://swebench-registry.flycast:5000"
INTERNAL_URL="http://swebench-registry.internal:5000"

echo ""
echo "==> swebench-registry deployed."
echo "    Workers: set SWEBENCH_REGISTRY_MIRROR=$FLYCAST_URL in the run env"
echo "    (6PN .internal alternative, same private network, no allocation"
echo "    needed but less stable across redeploys: $INTERNAL_URL)"
echo ""
echo "    Docker Hub token (raises the 100-pull/6h anonymous limit on the ONE"
echo "    upstream fetch per image — worth doing before a big/parallel run):"
echo "      fly secrets set REGISTRY_PROXY_USERNAME=<hub-username> REGISTRY_PROXY_PASSWORD=<hub-token> -a $APP"
echo ""
echo "    Smoke test (run from inside the 6PN — see smoke.sh header): ./smoke.sh"
