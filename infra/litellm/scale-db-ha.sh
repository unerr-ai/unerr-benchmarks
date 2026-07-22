#!/usr/bin/env bash
# Provision / verify the econ-litellm-db postgres-flex cluster as a 3-member HA
# cluster, every member performance-4x / 16GB in iad. IDEMPOTENT — safe to re-run:
# it clones up to TARGET_MEMBERS, resizes any member not already at the target
# guest, and prints the final topology + health. It never destroys a member.
#
# WHY this exists: econ-litellm-db shipped as a SINGLE-member cluster. A single
# member has no failover — any connection/disk hiccup on the primary takes the
# whole gateway down (and every benchmark fleet billing through it) with it. On
# 2026-07-22 a stale-connection outage on that lone primary silent-killed 17
# tasks of a rerun. HA (>=3 members, odd quorum) is the fix; the cluster is far
# cheaper than one full benchmark run. See README.md "Database: HA cluster".
#
# Adding a member = `fly machine clone <primary>` — the postgres-flex entrypoint
# makes the clone join via repmgr as a streaming standby (fresh basebackup, its
# own volume). Resizing the primary triggers a brief repmgr failover; run this
# while NO benchmark fleet is pointed at the gateway.
set -euo pipefail

APP="${DB_APP:-econ-litellm-db}"
REGION="${DB_REGION:-iad}"
TARGET_MEMBERS="${TARGET_MEMBERS:-3}"
VM_SIZE="${DB_VM_SIZE:-performance-4x}"
VM_MEMORY="${DB_VM_MEMORY:-16384}"   # MB

echo "==> econ-litellm-db HA: target=${TARGET_MEMBERS}x ${VM_SIZE}/${VM_MEMORY}MB in ${REGION} (app=${APP})"

members_json() { flyctl machines list -a "$APP" --json 2>/dev/null; }

primary_id() {
  members_json | python3 -c "
import json,sys
ms=json.load(sys.stdin)
# postgres-flex tags the primary via its role metadata; fall back to first member.
for m in ms:
    md=m.get('config',{}).get('metadata',{}) or {}
    if str(md.get('fly-managed-postgres-role') or md.get('role') or '').lower()=='primary':
        print(m['id']); break
else:
    print(ms[0]['id'] if ms else '')
"
}

member_count() { members_json | python3 -c "import json,sys;print(len(json.load(sys.stdin)))"; }

PRIMARY="$(primary_id)"
[ -n "$PRIMARY" ] || { echo "!! no members found on $APP — create the cluster first (fly postgres create)"; exit 1; }
echo "    primary=$PRIMARY"

# 1) Clone up to TARGET_MEMBERS (sequential, so each joins repmgr cleanly).
have="$(member_count)"
while [ "$have" -lt "$TARGET_MEMBERS" ]; do
  echo "==> members=$have/$TARGET_MEMBERS — cloning a standby ($VM_SIZE/$VM_MEMORY in $REGION)"
  flyctl machine clone "$PRIMARY" -a "$APP" --region "$REGION" \
    --vm-size "$VM_SIZE" --vm-memory "$VM_MEMORY"
  for _ in $(seq 1 20); do sleep 8; [ "$(member_count)" -gt "$have" ] && break; done
  have="$(member_count)"
done

# 2) Resize any member not already at the target guest (primary last — its resize
#    restarts it and repmgr promotes a standby; harmless with 2 standbys present).
# Portable to macOS bash 3.2 (no mapfile): iterate the newline list via read.
OFFSIZE="$(members_json | python3 -c "
import json,sys
tgt_cpu=int('${VM_SIZE}'.split('-')[1].rstrip('x')); tgt_mem=${VM_MEMORY}
prim='${PRIMARY}'
rows=[]
for m in json.load(sys.stdin):
    g=m.get('config',{}).get('guest',{})
    if g.get('cpus')!=tgt_cpu or g.get('memory_mb')!=tgt_mem:
        rows.append(m['id'])
# primary resized last
rows.sort(key=lambda i: i==prim)
print('\n'.join(rows))
")"
while IFS= read -r M; do
  [ -n "$M" ] || continue
  echo "==> resizing $M -> $VM_SIZE/$VM_MEMORY"
  flyctl machine update "$M" --vm-size "$VM_SIZE" --vm-memory "$VM_MEMORY" -a "$APP" -y
done <<EOF
$OFFSIZE
EOF

# 3) Report final topology + health.
echo "==> final cluster:"
members_json | python3 -c "
import json,sys
for m in json.load(sys.stdin):
    g=m.get('config',{}).get('guest',{})
    print(f\"    {m['id']}  {m['state']:8} {g.get('cpu_kind','?')}-{g.get('cpus','?')}x/{g.get('memory_mb','?')}MB\")
"
echo "==> health checks (want all passing = replication streaming):"
flyctl checks list -a "$APP" 2>&1 | grep -iE "passing|critical|warning" | head -12 || true
echo "==> done. If the gateway (econ-litellm) still errors 'PostgreSQL connection: Closed',"
echo "    restart it so it reopens fresh pools:  fly machine restart <id> -a econ-litellm"
