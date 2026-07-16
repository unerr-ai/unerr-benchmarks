# swebench-registry — shared pull-through Docker registry cache

A **shared, arm-agnostic** Docker pull-through cache for SWE-bench testbed images
(`docker.io/swebench/sweb.eval.x86_64.*`). One long-lived fly app, `swebench-registry`,
serves every distributed run — econ, claude open-weight, codex, and future arms — over
the fly.io private 6PN network. It is infrastructure, not part of any single arm's image
or runbook.

## Why

Each SWE-bench instance's testbed image is a multi-GB layer pulled fresh from Docker Hub
the first time a worker needs it. A cold `docker pull` runs ~7–8 minutes per worker
before any resolve work starts, and it's paid **again on every run**, by every worker,
because fly workers are volumeless/ephemeral (see `e2e/distributed/README.md` §0). On top
of that, Docker Hub anonymous pulls are rate-limited (100 pulls / 6h per IP) — a fleet of
N workers all pulling the same handful of images can trip that limit outright and stall.

**What the cache does — and does NOT — fix (measured, django-11815, iad/8cpu):**
a `docker pull` is **extraction-bound**, not download-bound — decompress+untar of the
image onto the worker's ephemeral rootfs is ~400 s of CPU/disk; the download itself is
~10 s for ~1 GB. So the pull-through cache, which only speeds the *download* slice,
produces **≈ zero per-pull wall-time savings**: cold-from-Hub `429.7 s` vs
warm-from-cache `429.3 s`. The cache's real, load-bearing value is the **rate limit**:
the first worker (of any arm, any run) pulls each image through the mirror once via a
single *authenticated* upstream fetch; every worker after — same run or weeks later —
gets it from the in-region Tigris cache with no Docker Hub round-trip, so a 25-worker
fleet never trips the 100-pull/6h cap. **If you actually want to cut pull wall-time, you
must pre-*extract* images (a snapshot volume of unpacked images) — blob caching cannot
touch the extraction cost.**

## What it is

The official CNCF **Distribution** (`registry:2`) image running in **proxy / pull-through**
mode in front of `https://registry-1.docker.io`, backed by a shared **Tigris (S3)** bucket
(`swebench-registry-cache`) for the blob cache — NOT a per-machine fly volume. Fly volumes
are single-attach (bound 1:1 to a machine), so two registry replicas on their own volumes
would cache independently and a pre-seed would warm only one; one shared bucket behind 2+
machines is a single unified cache (HA + parallel pull throughput). Storage + proxy config
live in [`config.yml`](./config.yml) (baked into the image via [`Dockerfile`](./Dockerfile));
Tigris creds arrive as `AWS_*` secrets from `fly storage create` and are mapped onto the
driver's `REGISTRY_STORAGE_S3_*` env by [`reg-entrypoint.sh`](./reg-entrypoint.sh). See
[`fly.toml`](./fly.toml) for the service/vm config.

Docker's registry-mirror mechanism (what workers point at this cache with) only proxies
`docker.io` — which is exactly where SWE-bench images live, so this one registry covers
the full testbed-image surface for every arm.

### Why pull-through, not an active mirror (skopeo/crane sync)

An *active* mirror (a script that `skopeo copy`s a known image list into a private
registry ahead of time) needs: an enumerated, kept-current image list; a per-arm/per-suite
maintenance job every time SWE-bench adds instances; and image-name rewrites in every
runner (`docker.io/swebench/...` → `our-registry/swebench/...`). A **pull-through cache**
needs none of that — it's a transparent proxy keyed on the upstream image name, so it
warms itself lazily on first pull, requires zero image-list maintenance, and callers keep
pulling the exact same `docker.io/...` names they always have (dockerd rewrites the fetch
to the mirror internally). Less to build, less to keep in sync, works for arms that don't
exist yet.

## Deploy

```bash
cd e2e/distributed/registry
./deploy.sh
```

Idempotent — safe to re-run against a live app (it ships a new release + reconciles
app/bucket/IP state; the `fly storage create` step no-ops once the bucket exists). Env
overrides: `FLY_ORG` (default `vamsee-k-933`), `REGION` (default `iad`), `MACHINES`
(default `2`), `BUCKET` (default `swebench-registry-cache`).

`deploy.sh` prints the endpoint at the end. It is:

```
http://swebench-registry.flycast:5000
```

(6PN `.internal` alternative: `http://swebench-registry.internal:5000` — works with zero
IP allocation, but resolves to whichever machine(s) of the app are currently up by machine
ID, so it's less stable across a redeploy than the flycast address. Prefer flycast.)

## How workers consume it

Set `SWEBENCH_REGISTRY_MIRROR=http://swebench-registry.flycast:5000` in the run env
(alongside the existing `MACHINES=... ARM=... LABEL=...` vars passed to
`e2e/distributed/run-distributed.sh`). The shared worker boot path
(`e2e/distributed/lib/boot.sh`'s `boot_dockerd`, which every worker's
`worker-entrypoint.sh` calls before any resolve work) injects it into dockerd's
`--registry-mirror` (+ `--insecure-registry`) flags — gated so an **unset
`SWEBENCH_REGISTRY_MIRROR` is a no-op** (dockerd boots exactly as before, straight to
docker.io, no behavior change for anyone not opting in). `run-distributed.sh` passes the
var through to every worker (arm-agnostic) when it's set in the launching shell.

## Sizing

Storage is a Tigris (S3) bucket — it grows on demand, so there's no volume to size or
extend. The cache is **content-addressed** (registry:2 stores blobs by digest), so
identical layers — shared base images, shared env layers across many SWE-bench instances
and across arms — dedupe in the bucket once, regardless of how many image tags reference
them, keeping real usage far below a naive "N images × 2GB" estimate. Tigris bills for what
is actually stored; nothing to pre-provision. For scale: the full SWE-bench **Verified**
set (500 images, ~1 GB each) is roughly **~525 GB** pre-dedup in the bucket after a full
warm.

## Docker Hub token (raises the rate limit)

Anonymous Docker Hub pulls are capped at 100/6h *per IP* — the registry's own IP, since
it's the one doing the upstream fetch on the fleet's behalf (workers never talk to Docker
Hub directly once the mirror is set). One token lifts that cap for the whole fleet:

```bash
fly secrets set REGISTRY_PROXY_USERNAME=<hub-username> REGISTRY_PROXY_PASSWORD=<hub-token> -a swebench-registry
```

(`REGISTRY_PROXY_USERNAME`/`PASSWORD` are left commented-out placeholders in
[`fly.toml`](./fly.toml) — set via `fly secrets`, never in the checked-in toml.)

## Smoke test

```bash
./smoke.sh
```

Must run from a machine already on the fleet's private 6PN network (the registry has no
public IP by design) — see the header comment in [`smoke.sh`](./smoke.sh) for exactly how
to reach it via `flyctl ssh console`. It checks `GET /v2/` returns 200, then pulls a tiny
public image's manifest (`library/hello-world`) through the mirror twice and compares
latency as a cache-hit signal.

## Refreshing the cache

The pull-through mechanism above warms itself *lazily* — an image lands in the cache the
first time any worker of any run needs it. `warm-cache.sh` warms it **proactively**, ahead
of a run, for a given task set:

```bash
cd e2e/distributed/registry
FLY_ORG=vamsee-k-933 ./warm-cache.sh --suite mini
FLY_ORG=vamsee-k-933 ./warm-cache.sh --suite verified
FLY_ORG=vamsee-k-933 ./warm-cache.sh --tasks "django__django-11790,django__django-11815"
FLY_ORG=vamsee-k-933 ./warm-cache.sh --file ids.txt
```

### Why warm-by-pull, not push (and why HTTP, not `docker pull`)

This registry is a **proxy** (see "What it is" above) — it rejects direct pushes
(`skopeo`/`docker push` → 405). The only way to populate its cache is to pull each image
*through* the mirror, exactly like a real worker does — but a pull-through registry
caches on **any blob GET**, not just a `docker pull`: on a miss it fetches the blob from
Docker Hub, writes it to Tigris, and streams it back to the client. So `warm-cache.sh`
doesn't run docker at all — it launches an ephemeral, dockerless "warmer" machine on the
fleet app (reusing its already-built dist image, no DinD / big rootfs needed) that runs a
small stdlib-only Python script: for each image it `GET`s the manifest (resolving a
multi-arch list/index to the amd64/linux entry if needed), then `GET`s every blob
(`config` + each layer) through `http://swebench-registry.flycast:5000`, discarding the
bytes, with `CONCURRENCY` images in flight at once — then destroys the machine. It's
one-shot: the warmer runs to completion and exits, never a long-lived service.

The earlier version of this script booted DinD and ran `docker pull` per image, which
downloads **and extracts** each ~1GB testbed image to overlay2 before discarding it —
~7-10 min/image, sequential. That version's cost was warmer-side extraction +
serialization. Fetching manifest+blobs over HTTP and throwing the bytes away skips the
extraction (and dockerd) entirely, and does `CONCURRENCY` images at once instead of one at
a time — the two changes that make this fast.

### Registry CPU is the warm bottleneck — give it dedicated cores first

With extraction gone, the throughput limit moves to the **registry**: on every blob miss
it fetches from Docker Hub over TLS and writes to Tigris via S3 multipart — both
CPU-hungry. On the steady-state `shared-cpu-1x` VM (a heavily-oversubscribed fractional
core) that throttles a warm to **~20 MB/s aggregate** (measured: registry load pinned at
~1.0 on its single shared core while a warmer pulled). Scaling the registry to **dedicated
CPU** for the duration of a full warm lifts that ~5×:

```bash
# BEFORE a full warm: give the registry real cores
fly scale vm performance-2x -a swebench-registry     # 2 dedicated vCPU / 4GB, ×2 machines
# ... run warm-cache.sh (see below) ...
# AFTER: scale back — steady-state blob-serving doesn't need dedicated CPU
fly scale vm shared-cpu-1x --vm-memory 2048 -a swebench-registry
```

With dedicated CPU a single warmer hit **~100 MB/s** (its own NIC ceiling) and the registry
cores still had headroom, so a full warm is fastest with **several warmers in parallel over
disjoint id subsets** — each `warm-cache.sh` invocation is independent and idempotent:

```bash
split -n l/4 ids.txt sub_          # 4 disjoint chunks
for s in sub_*; do
  nohup env FLY_ORG=vamsee-k-933 IMAGE=python:3.12-slim CONCURRENCY=20 BATCH=200 \
    ./warm-cache.sh --file "$s" > "warm-$s.log" 2>&1 &
done
```

4 parallel warmers sustained **~150 MB/s aggregate** (Tigris/Hub per-connection latency,
not registry CPU, is the residual limit) and warmed all 500 Verified images (~525 GB,
~1 GB each) in roughly one hour of wall time vs ~4 h sequential.

> **Run long warms detached (`nohup`), not under a supervising shell that may reap it.**
> Each warmer runs on fly *independently* of the host script, but the host script's EXIT
> trap destroys its warmer — so if a wrapper (a CI step, a Claude Code background task, a
> timed-out SSH) SIGTERMs the host script mid-warm, it takes the warmer down with it,
> leaving that subset partial. `nohup ... &` + `disown` (as above) decouples it.

### Verifying a warm

Don't use `GET /v2/_catalog` to verify a large cache — on a pull-through/S3 backend it
enumerates every repo by walking the Tigris bucket (`ListObjects`), which is fine for a
handful of repos but **times out once hundreds are cached**. Two reliable checks instead:

- **Per-warmer truth:** each warmer prints `[warm] done: <ok>/<n> ok, <failed> failed`
  to its log — the authoritative per-subset count (fly's `flyctl logs` batch-flushes and
  *truncates*, so grep the warmer's own summary line, not individual `ok` lines).
- **Spot-check by manifest GET** (fast, targeted — from a machine on the 6PN network).
  You **must** send the same `Accept` header a real `docker pull` sends:
  ```bash
  fly ssh console -a swebench-registry -C \
    "wget -qS --header='Accept: application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json' \
     -O /dev/null http://localhost:5000/v2/swebench/sweb.eval.x86_64.<iid>/manifests/latest"
  ```
  `HTTP/1.1 200` = cached (`<iid>` = the instance id with `__` → `_1776_`).

  **Gotcha — no `Accept` header ⇒ false misses.** A bare `wget`/`curl` with no
  `Accept` makes Distribution try to down-convert the manifest to legacy **schema1**
  "to support an old client"; that conversion **fails with `400 manifest invalid`
  ("operation unsupported")** for OCI-format / manifest-list images (the entire
  matplotlib block, sympy, later scikit/sphinx, …). Those 400s are a *client-Accept
  artifact, NOT a cache miss* — `docker pull` (which always sends the header above)
  fetches them fine. A full-500 verify that omits `Accept` will spuriously report
  ~50 "misses"; with the header it reports `499 ok, 0 miss`. If you see a
  `msg="rewriting manifest … in schema1 format"` line in the registry log right
  before a 400, that's this, not a warm gap.

### id resolution

Reuses [`../tools/suite.py`](../tools/suite.py) — same `--suite|--tasks|--file` selectors,
same precedence, as `run-distributed.sh`. `--suite mini` and `--tasks`/`--file` need only
the stdlib on the host running `warm-cache.sh`; `--suite verified`/`lite` need
`pip install datasets`. There is **no mini-50 suite** in `suite.py`: `verified` (500 ids)
⊇ mini-50 ⊇ `mini` (10 ids), so warming `--suite verified` once covers every smaller tier.

### Batching (Docker Hub rate limits)

Docker Hub's anonymous-pull limit is 100 manifest pulls / 6h / IP, and this registry is
**one IP** doing every upstream fetch on the fleet's behalf. `warm-cache.sh` chunks the
resolved id list into batches of `BATCH` (default **120**) and launches one warmer per
batch, waiting for each to finish before moving on. With a Docker Hub token set on the
proxy (see "Docker Hub token" above), that anonymous cap is gone entirely, so `SLEEP=0` is
fine for any suite size. For a full `--suite verified` (500 ids), don't run it as one
sequential invocation though — it's a single NIC-bound warmer and slow; use the **dedicated
CPU + parallel-warmer** recipe under "Registry CPU is the warm bottleneck" above (split the
ids, one detached warmer per chunk), which is what actually warmed the full set in ~1 h.

```bash
fly secrets set REGISTRY_PROXY_USERNAME=<hub-username> REGISTRY_PROXY_PASSWORD=<hub-token> -a swebench-registry
```

Without a token, fall back to a smaller `BATCH` (under the 100 ceiling) and a `SLEEP` that
keeps each batch's fetches inside its own 6h rate-limit window:

```bash
FLY_ORG=vamsee-k-933 BATCH=90 SLEEP=21660 ./warm-cache.sh --suite verified
```

### Env vars

| Var | Default | |
|---|---|---|
| `FLY_ORG` | *(required)* | fly org, e.g. `vamsee-k-933` |
| `MIRROR` | `http://swebench-registry.flycast:5000` | the pull-through endpoint the warmer fetches through |
| `FLEET_APP` | `swebench-agent-dist` | app the ephemeral warmer launches on (reuses its already-built dist image) |
| `IMAGE` | *(read from a live machine on `FLEET_APP`)* | override the dist image ref directly — needed if no fleet has ever run |
| `CONCURRENCY` | `16` | images fetched in parallel per warmer (thread-pool size); one warmer is NIC-bound ~100 MB/s, so scale total throughput with parallel warmers (see "Registry CPU is the warm bottleneck") rather than a huge `CONCURRENCY` |
| `HTTP_TIMEOUT` | `300` | per manifest/blob GET timeout, seconds (`PULL_TIMEOUT` accepted as a back-compat alias from the old docker-pull warmer) |
| `BATCH` | `120` | image refs per warmer machine / per `WARM_IMAGES_B64` env var |
| `SLEEP` | `0` | seconds to sleep between batches |
| `WARMER_VM_MEM` | `2048` | warmer machine memory, MB |
| `WARMER_VM_CPUS` | `2` | warmer CPUs |
| `WARMER_REGION` | `iad` | fly region for the warmer, co-located with the registry so fetches stay in-region |
| `KEEP_WARMER` | unset | `1` = leave each batch's warmer machine up (stopped) for inspection instead of destroying it |

Idempotent and cron-friendly: re-running only re-fetches what isn't already cached (a
cache hit is a cheap GET, not a re-fetch), so it's safe to schedule every few days as
SWE-bench's testbed images drift.

## Teardown

This is shared, persistent infra — don't tear it down as part of a single run's cleanup.
If it's genuinely no longer needed by any arm:

```bash
flyctl apps destroy swebench-registry
```

(destroys the app + machines. The **Tigris bucket persists** — `fly apps destroy` does not
delete it; remove it separately via the Tigris dashboard / `fly storage` if you truly want
the cached blobs gone. Leaving the bucket means a later redeploy warm-starts from the
existing cache instead of paying the cold-pull cost again.)
