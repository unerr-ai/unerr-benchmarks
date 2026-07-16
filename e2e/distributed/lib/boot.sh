# Shared Docker-in-Docker boot + toolbox-build library.
#
# Sourced by both the single-machine econ `entrypoint.sh`
# (e2e/econ/fly-remote/fullresolve/entrypoint.sh) and the distributed
# `worker-entrypoint.sh` (e2e/distributed/worker/worker-entrypoint.sh). One
# copy of the dockerd-boot + toolbox-build logic — the two flavours only
# differ in WHERE the docker data-root lives (a fly volume for the
# single-machine run, ephemeral rootfs for a volumeless distributed worker)
# and which toolbox image/context an arm needs, so both are parameters.
#
# Safe to `source`: defines functions only, runs nothing at source time.
# `set -uo pipefail` is the CALLER's responsibility, not this lib's.
#
# Expects `log()` and `emit()` helpers to already be defined by the sourcing
# script (same one-line-log / one-line-JSON-beacon convention as
# e2e/econ/fly-remote/fullresolve/entrypoint.sh); falls back to matching
# definitions here ONLY if the caller hasn't defined them, so sourcing this
# file never clobbers a caller's own log()/emit().
command -v log >/dev/null 2>&1 || log() { printf '[boot.sh] %s\n' "$*" >&2; }
command -v emit >/dev/null 2>&1 || emit() { printf '{"ev":%s}\n' "$1"; }

# ensure_overlay2_backing <data_root> <size_gb> <logdir>
#
# docker's overlay2 driver cannot use an overlayfs directory as its upperdir
# ("'overlay2' is not supported over overlayfs" / "not supported as upperdir").
# Fly worker machines run volumeless (PLAN.md decision 2), so their docker
# data-root lands on the machine's OVERLAYFS rootfs and dockerd dies on boot
# ("error initializing graphdriver: driver not supported", exit 11). When
# <data_root> sits on overlayfs, back it with a sparse loopback ext4 image
# (real ext4 -> overlay2 works, exactly like the single-machine ext4 volume).
# No-op when <data_root> is already a real fs (the ext4 fly volume on the
# single-machine run, or an already-mounted loopback), so this is safe for both
# callers and leaves the single-machine path byte-for-byte unchanged.
# Verified on fly (2026-07-10): overlayfs rootfs -> loopback ext4 -> dockerd
# overlay2 (Backing Filesystem: extfs) -> `docker run hello-world`, all green.
# Size caps docker's disk (sparse, grows on write) — DOCKER_FS_GB default 42
# leaves headroom under fly's 50 GB rootfs max; img path via DOCKER_FS_IMG.
ensure_overlay2_backing() {
  local data_root="$1" size_gb="$2" logdir="$3"
  local img="${DOCKER_FS_IMG:-/docker-dind.ext4}"
  mkdir -p "$data_root"
  # Only an overlayfs backing is the problem; a real fs reports a different type.
  [ "$(stat -f -c %T "$data_root" 2>/dev/null)" = "overlayfs" ] || return 0
  log "data-root on overlayfs -> backing with ${size_gb}G loopback ext4 ($img); overlay2 can't stack on overlayfs"
  if ! { truncate -s "${size_gb}G" "$img" \
         && mkfs.ext4 -qF -m 0 "$img" \
         && mount -o loop "$img" "$data_root"; } >"$logdir/docker-backing.log" 2>&1; then
    log "loopback ext4 backing FAILED — tail:"; tail -20 "$logdir/docker-backing.log" >&2
    emit '"fatal","stage":"docker-backing"'; exit 13
  fi
  log "data-root backed by ext4 loopback ($(stat -f -c %T "$data_root"))"
  emit '"docker_backing_ready"'
}

# boot_dockerd <data_root> <logdir>
#
# Starts dockerd with its data-root pinned to <data_root> (a fly volume path
# for a durable single-machine run, or a path on ephemeral rootfs for a
# volumeless worker), waits up to 120s for it to come up, and emits the
# `dockerd_up` beacon. Fatal on failure: emits `"fatal","stage":"dockerd"`
# (dockerd died on boot) or `"fatal","stage":"dockerd-timeout"` (never came
# up) and exits 11, matching the original inline behaviour byte-for-byte.
boot_dockerd() {
  local data_root="$1" logdir="$2"
  local dockerd_pid

  # overlay2 needs a non-overlay backing fs; provision a loopback ext4 one if
  # data-root is on overlayfs (volumeless worker). No-op on an ext4 fly volume
  # (single-machine run), so this call is inert there.
  ensure_overlay2_backing "$data_root" "${DOCKER_FS_GB:-42}" "$logdir"

  # Optional pull-through registry mirror — SHARED across every arm (econ, claude,
  # future arms all boot dockerd through this one function). When
  # SWEBENCH_REGISTRY_MIRROR is set, point dockerd at it so a fresh worker's
  # SWE-bench testbed-image pull (docker.io/swebench/sweb.eval.x86_64.<iid>) hits a
  # shared cache instead of Docker Hub directly. It's plain HTTP on the private
  # 6PN, so its bare host:port also goes into --insecure-registry. Unset (the
  # default) -> mirror_flags stays empty -> dockerd start is byte-for-byte unchanged.
  local -a mirror_flags=()
  if [ -n "${SWEBENCH_REGISTRY_MIRROR:-}" ]; then
    local mirror_hostport="${SWEBENCH_REGISTRY_MIRROR#*://}"
    mirror_flags=(--registry-mirror="$SWEBENCH_REGISTRY_MIRROR" --insecure-registry="$mirror_hostport")
    log "registry mirror: $SWEBENCH_REGISTRY_MIRROR"
  fi

  log "starting dockerd (data-root=$data_root)"
  dockerd --data-root="$data_root" --storage-driver=overlay2 \
          "${mirror_flags[@]+"${mirror_flags[@]}"}" \
          >"$logdir/dockerd.log" 2>&1 &
  dockerd_pid=$!

  for i in $(seq 1 60); do
    if docker info >/dev/null 2>&1; then break; fi
    if ! kill -0 "$dockerd_pid" 2>/dev/null; then
      log "dockerd died on boot — tail of dockerd.log:"; tail -40 "$logdir/dockerd.log" >&2
      emit '"fatal","stage":"dockerd"'; exit 11
    fi
    sleep 2
  done
  if ! docker info >/dev/null 2>&1; then
    log "dockerd not ready after 120s"; tail -40 "$logdir/dockerd.log" >&2
    emit '"fatal","stage":"dockerd-timeout"'; exit 11
  fi
  log "dockerd up: $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
  emit '"dockerd_up"'
}

# build_toolbox <dockerfile> <context> <image_tag> <logdir>
#
# Builds <image_tag> from <dockerfile> with build context <context> (the
# arm-specific toolbox image, e.g. econ-toolbox or unerr-claude-toolbox), logs to
# <logdir>/toolbox-build.log, and emits the `toolbox_built` beacon. Fatal on
# failure: emits `"fatal","stage":"toolbox"` and exits 12, matching the
# original inline behaviour byte-for-byte.
build_toolbox() {
  local dockerfile="$1" context="$2" image_tag="$3" logdir="$4"

  log "building $image_tag from $context"
  if docker build -f "$dockerfile" -t "$image_tag" \
       "$context" >"$logdir/toolbox-build.log" 2>&1; then
    log "toolbox: built"; emit '"toolbox_built"'
  else
    log "toolbox build FAILED — tail:"; tail -40 "$logdir/toolbox-build.log" >&2
    emit '"fatal","stage":"toolbox"'; exit 12
  fi
}
