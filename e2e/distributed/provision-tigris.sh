#!/usr/bin/env bash
# provision-tigris.sh — ONE-TIME setup of the Tigris results-archive bucket for
# the distributed benchmark fleets. Creates a bucket + Tigris keypair, attaches
# it to BOTH fleet apps (econ + claude) so their coordinators can upload at
# end-of-run (see tools/tigris_archive.py), and saves the keypair locally for
# the host lookup tool (tools/tigris-archive.sh).
#
#   !! Billable infra (a Tigris bucket) AND it prints S3 secret keys once. Run
#      it YOURSELF, not from an agent. Re-running is safe-ish but makes a new
#      bucket if --bucket differs. Pass --yes to skip the confirm prompt.
#
# Usage:
#   FLY_ORG=<your-team-org> ./provision-tigris.sh [--bucket NAME] [--apps a,b] [--yes]
#
# Defaults: bucket = swebench-dist-archive; apps = swebench-agent-dist,swebench-agent-dist-claude
#
# After it runs, enable archiving on any run with:
#   ARCHIVE_TIGRIS=1 TIGRIS_BUCKET=<bucket> ... ./run-distributed.sh
#   ARCHIVE_TIGRIS=1 TIGRIS_BUCKET=<bucket> ./bench.sh run econ:verified claude:pro ...
# and look runs up later (no live fleet needed) with:
#   ./tools/tigris-archive.sh list          ./tools/tigris-archive.sh overview <label>
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENDPOINT="https://fly.storage.tigris.dev"
ENVFILE="$HERE/.env.tigris"          # gitignored — read by tools/tigris-archive.sh

BUCKET="swebench-dist-archive"
APPS="swebench-agent-dist,swebench-agent-dist-claude"
YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --bucket) BUCKET="${2:?--bucket needs a value}"; shift 2 ;;
    --apps)   APPS="${2:?--apps needs a value}"; shift 2 ;;
    --yes)    YES=1; shift ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "ERROR: unknown arg '$1'" >&2; exit 2 ;;
  esac
done

ORG="${FLY_ORG:-}"
[ -n "$ORG" ] || { echo "ERROR: set FLY_ORG=<your-team-org> (a personal org is not allowed)" >&2; exit 2; }
command -v flyctl >/dev/null || { echo "ERROR: flyctl not on PATH (brew install flyctl; flyctl auth login)" >&2; exit 2; }
IFS=',' read -r a1 a2 _ <<<"$APPS"
[ -n "$a1" ] || { echo "ERROR: --apps must name at least one fly app" >&2; exit 2; }

echo "About to provision Tigris archive:"
echo "  bucket : $BUCKET   (org: $ORG)"
echo "  attach : $a1${a2:+, $a2}"
echo "  creds  : saved to $ENVFILE (gitignored) for the host lookup tool"
echo "  NOTE   : this creates a billable Tigris bucket and prints S3 keys once."
if [ "$YES" != 1 ]; then
  printf "Proceed? [y/N] "; read -r ans; case "$ans" in y|Y|yes|YES) : ;; *) echo "aborted."; exit 1 ;; esac
fi

# 1. Create the bucket on app #1 — flyctl sets AWS_* secrets on that app AND
#    prints them once. Capture the output so we can mirror to app #2 + the host.
TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
echo "==> flyctl storage create -n $BUCKET -o $ORG -a $a1"
# NOTE: after setting the AWS_* secrets + printing the keys, `flyctl storage
# create` tries to DEPLOY $a1 to apply them. Our fleet apps are launched via
# `flyctl machine run` and have NO Fly Launch release, so that deploy step errors
# ("current release not found for app …") and the command returns NON-ZERO even
# though the bucket + secrets were created and the keys were printed. So we do
# NOT abort on a non-zero exit here — we decide by whether the keys can actually
# be extracted below (grab). A genuine failure (bucket exists, no perms) prints
# no keys, and the `[ -z "$AK" ]` guard then exits 3 with a manual template.
flyctl storage create -n "$BUCKET" -o "$ORG" -a "$a1" -y 2>&1 | tee "$TMP" || \
  echo "note: 'flyctl storage create' returned non-zero (usually just the post-create deploy step on a machine-run app) — continuing; the key-extraction guard below decides success." >&2

# 2. Extract the printed keypair (tolerant to `KEY=val`, `KEY: val`, quoted).
grab() { grep -iE "$1" "$TMP" | head -1 | sed -E 's/.*'"$1"'[":= ]+//; s/[",]*$//' | tr -d '"'\'' '; }
AK="$(grab 'AWS_ACCESS_KEY_ID')"
SK="$(grab 'AWS_SECRET_ACCESS_KEY')"

if [ -z "$AK" ] || [ -z "$SK" ]; then
  echo "" >&2
  echo "WARN: could not auto-extract the AWS keypair from flyctl's output (format may have changed)." >&2
  echo "      The keys ARE set on app '$a1' and were printed above. To finish manually:" >&2
  echo "        1) copy the AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY into $ENVFILE (see the template written below)" >&2
  echo "        2) attach app '$a2': flyctl secrets set AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... TIGRIS_BUCKET=$BUCKET -a $a2" >&2
  cat >"$ENVFILE" <<EOF
# Tigris archive creds for tools/tigris-archive.sh — FILL THESE IN (from the flyctl output above).
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_ENDPOINT_URL_S3=$ENDPOINT
TIGRIS_BUCKET=$BUCKET
EOF
  chmod 600 "$ENVFILE"
  echo "      wrote a template to $ENVFILE" >&2
  exit 3
fi

# 3. Save creds locally for the host lookup tool (gitignored; 0600; not echoed).
umask 077
cat >"$ENVFILE" <<EOF
# Tigris archive creds for tools/tigris-archive.sh — auto-written by provision-tigris.sh. DO NOT COMMIT.
AWS_ACCESS_KEY_ID=$AK
AWS_SECRET_ACCESS_KEY=$SK
AWS_ENDPOINT_URL_S3=$ENDPOINT
TIGRIS_BUCKET=$BUCKET
EOF
chmod 600 "$ENVFILE"
echo "==> wrote $ENVFILE (AWS_ACCESS_KEY_ID len=${#AK}, secret len=${#SK}) — gitignored, chmod 600"

# 4. Mirror the same keypair + bucket onto the other fleet app(s) so BOTH arms
#    archive to the ONE shared bucket. (Tigris keys are account-scoped.)
# `--stage` sets the secrets WITHOUT triggering a deploy — required for these
# machine-run apps (no Fly Launch release; a plain `secrets set` would error the
# same way storage create's deploy step did). Staged secrets are injected into
# the next machine `flyctl machine run` creates, which is exactly how
# run-distributed.sh launches each coordinator — so staging is the right state.
set_app_secrets() {  # <app>
  local app="$1"
  echo "==> attaching bucket to app '$app' (flyctl secrets set --stage, values not echoed)"
  flyctl secrets set --stage \
    AWS_ACCESS_KEY_ID="$AK" AWS_SECRET_ACCESS_KEY="$SK" \
    AWS_ENDPOINT_URL_S3="$ENDPOINT" TIGRIS_BUCKET="$BUCKET" \
    -a "$app" >/dev/null 2>&1 && echo "    ok: $app" || echo "    WARN: could not set secrets on $app (create it first, or set manually)" >&2
}
# app #1 already has AWS_* from storage create; just add TIGRIS_BUCKET/endpoint.
flyctl secrets set --stage AWS_ENDPOINT_URL_S3="$ENDPOINT" TIGRIS_BUCKET="$BUCKET" -a "$a1" >/dev/null 2>&1 \
  && echo "==> set TIGRIS_BUCKET on $a1" || echo "WARN: could not set TIGRIS_BUCKET on $a1" >&2
[ -n "$a2" ] && set_app_secrets "$a2"

echo ""
echo "Done. Archive a run with:"
echo "  ARCHIVE_TIGRIS=1 TIGRIS_BUCKET=$BUCKET MACHINES=2 ARM=econ SUITE=smoke ./run-distributed.sh"
echo "  ARCHIVE_TIGRIS=1 TIGRIS_BUCKET=$BUCKET ./bench.sh run econ:verified claude:pro --suite smoke"
echo "Look runs up later (no live fleet):"
echo "  ./tools/tigris-archive.sh list        ./tools/tigris-archive.sh overview <label>"
