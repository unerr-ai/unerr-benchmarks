#!/bin/sh
# Map Fly's Tigris secrets (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, set by
# `fly storage create`) onto the env keys the Distribution s3 driver reads
# (REGISTRY_STORAGE_S3_ACCESSKEY / _SECRETKEY — the REGISTRY_<YAML_PATH> override
# convention for storage.s3.accesskey/secretkey). Doing it here keeps creds out
# of config.yml and the image, and doesn't rely on the AWS SDK's env-chain
# fallback. Then hand off to the stock registry the same way registry:2 does
# (`registry serve <config>`); "$@" is the CMD (the config path).
set -e
export REGISTRY_STORAGE_S3_ACCESSKEY="${REGISTRY_STORAGE_S3_ACCESSKEY:-$AWS_ACCESS_KEY_ID}"
export REGISTRY_STORAGE_S3_SECRETKEY="${REGISTRY_STORAGE_S3_SECRETKEY:-$AWS_SECRET_ACCESS_KEY}"
if [ -z "$REGISTRY_STORAGE_S3_ACCESSKEY" ] || [ -z "$REGISTRY_STORAGE_S3_SECRETKEY" ]; then
  echo "reg-entrypoint: FATAL — no S3 creds (AWS_ACCESS_KEY_ID/SECRET or REGISTRY_STORAGE_S3_ACCESSKEY/SECRETKEY)." >&2
  echo "  Run 'fly storage create -a swebench-registry' (see deploy.sh) to provision the Tigris bucket + secrets." >&2
  exit 1
fi
exec registry serve "$@"
