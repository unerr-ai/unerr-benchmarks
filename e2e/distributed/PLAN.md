# Distributed SWE-bench Runner — Implementation Plan

**Goal.** Turn a SWE-bench run from a single-machine serial loop (10 instances ≈ 1–2 h) into a
work-stealing fleet: `N` fly.io worker machines (16 GB / 8 CPU) pull instances from a shared queue,
resolve + grade each, report results back, and tear themselves down when the queue drains. One
coordinator machine holds the queue and aggregates. Wall-clock ≈ the longest single straggler once
`N` is wide enough (AI21's 200k-run result).

**Invocation shape (target):**
```bash
# N machines, explicit task list:
MACHINES=8 ARM=econ LABEL=dist-v1 TASKS="django__django-11790,django__django-11815,..." ./run-distributed.sh
# N machines, full Verified suite (no TASKS given → all 500):
MACHINES=25 ARM=econ LABEL=full-verified ./run-distributed.sh
# subset by name:
MACHINES=10 ARM=econ LABEL=mini SUITE=mini ./run-distributed.sh
```

---

## 1. Architecture (decided)

```
        host (laptop)                         fly app  unerr-bench-dist-<label>  (org vamsee-k-933)
  ┌──────────────────────┐             ┌───────────────────────────────────────────────────────┐
  │ run-distributed.sh   │  create     │  COORDINATOR  (shared-cpu-1x / 1GB, 1 small volume)    │
  │  build image         │────────────▶│  ┌─────────────────────────────────────────────────┐  │
  │  create coordinator  │             │  │ coordinator/server.py  (aiohttp + SQLite queue) │  │
  │  seed queue          │  poll       │  │  POST /claim /heartbeat /complete /fail         │  │
  │  create N workers ───┼────────────▶│  │  GET  /status /drain                            │  │
  │  poll /status        │◀── status ──│  │  reaper thread → requeue expired leases         │  │
  │  pull bundle         │◀── sftp ────│  │  on drain → aggregate → grade-merge → report.py │  │
  │  destroy fleet       │             │  └─────────────────────────────────────────────────┘  │
  └──────────────────────┘             │        ▲  http over 6PN: <coord_id>.vm.<app>.internal  │
                                       │        │                                                │
                                       │  WORKER×N  (shared-cpu-8x / 16GB, NO volume, restart:no) │
                                       │  ┌─────────────────────────────────────────────────┐   │
                                       │  │ worker/worker-entrypoint.sh                     │   │
                                       │  │  dockerd (data-root on ephemeral rootfs)        │   │
                                       │  │  build toolbox once  [shared lib-boot.sh]       │   │
                                       │  │  loop: claim → run-benchmark.py --ids <one>     │   │
                                       │  │        → swebench grade --instance_ids <one>    │   │
                                       │  │        → POST /complete {patch,report,meta}     │   │
                                       │  │  empty queue → exit 0 → machine stops           │   │
                                       │  └─────────────────────────────────────────────────┘   │
                                       └───────────────────────────────────────────────────────┘
```

### Decisions & why (grounded in the three research digests)

1. **Coordinator = one fly machine with SQLite, not a broker.** At 10–500 tasks of minutes each,
   Postgres/Redis/NATS buys nothing below ~100 jobs/s; the whole queue is one atomic
   `UPDATE … RETURNING` claim. Fly's own work-queue blueprint names this exact "on-demand workers +
   Machines API" shape as its Option 2. Coordinator is a SPOF → give it the system's **one** small
   volume so the queue DB + accumulated results survive a restart.

2. **Workers have NO volume.** Fly docs: stateless DinD one-shots should use ephemeral rootfs
   (`--rootfs-size 50`, fly's max), not volumes — volumes are 1:1, bill even when stopped, and add
   create/cleanup/placement lifecycle per machine. This deletes the recon's #1 complexity
   (per-worker volume naming/teardown). DinD data-root points at a path on the enlarged rootfs;
   workers `docker image prune` between instances. *overlayfs gotcha (found + fixed in smoke,
   2026-07-10):* fly's rootfs is itself **overlayfs**, and docker's `overlay2` driver cannot use an
   overlayfs dir as its upperdir → dockerd dies on boot (`driver not supported`, exit 11). The
   single-machine run dodged this because its data-root is an ext4 volume. Fix (in the shared
   `lib/boot.sh` `ensure_overlay2_backing`, guarded so the single-machine ext4-volume path is a
   no-op): back the data-root with a sparse **loopback ext4 image** on the rootfs (`truncate` →
   `mkfs.ext4` → `mount -o loop`) → real ext4 → overlay2 works. Verified on a real fly machine
   (loopback ext4 → dockerd overlay2 `Backing Filesystem: extfs` → `docker run hello-world` green).
   *Size cap:* fly hard-limits ephemeral rootfs to
   **50 GB** (`config.rootfs.size_gb`), enough for one-instance-at-a-time DinD with pruning; the
   launcher clamps `ROOTFS_GB` to 50. *Calibration risk:* ephemeral rootfs is capped ~2000 IOPS /
   8 MiB/s vs a volume's 8000 IOPS — if env-image unpack is too slow in the smoke test, flip workers
   to a volume (one flag). Documented, not silently assumed.

3. **Results flow back over HTTP, not SFTP-from-N.** Worker POSTs `{patch, report.json, meta}` to
   the coordinator on completion → coordinator is the single collection point. Host pulls **one**
   bundle from the coordinator at the end. No N-way SFTP, no cross-machine volume merge.

4. **Grade per-worker, per-instance.** The worker already built that instance's env image during
   resolve, so grading it in place re-pulls nothing. SWE-bench writes one independent
   `report.json` per `(run_id, model, instance_id)` → distributed grading needs zero harness
   changes, just `--instance_ids <one> --cache_level env --clean True --timeout 2700 --max_workers 6`
   (official sizing: ≤ 0.75·cores). Coordinator merges the per-instance reports by concat.

5. **Lease + heartbeat + bounded retry (SQS visibility-timeout model).**
   - Lease TTL **45 min** (> the ~30 min straggler ceiling, so a slow-but-alive instance is never
     double-dispatched).
   - Worker heartbeats every **30 s** during resolve; a reaper requeues rows whose
     `last_heartbeat` is stale (catches alive-but-hung faster than the full lease TTL).
   - `attempt_count` max **2** → then `status='dead'` (dead-letter, surfaced in the final report as
     errored, not retried forever).
   - Completion is idempotent on `(run_id, instance_id)` upsert → a lease-expiry double-run just
     overwrites the same row (at-least-once + idempotent = effectively-once).

6. **Teardown.** Workers exit 0 on empty queue; `restart:no` → they go to `stopped` (compute billing
   stops instantly). Launcher destroys stopped workers by metadata `fleet=<label>`; coordinator
   destroyed after the bundle is pulled. `bench-ctl distributed-destroy <label>` nukes the whole
   fleet by metadata as a safety net.

7. **Machine-create pacing.** Machines API create is rate-limited **1 req/s (burst 3) per app**.
   Launcher creates workers at ≤ 1/s (or 3-burst-then-sleep) and retries 429s. Org soft cap is
   ~50 machines — fine for N ≤ 40; email support before a full-500 fleet if we ever want > 50.

### Consolidation guarantees (no second version)

- The distributed layer **never reimplements resolve or grade.** Workers shell out to the existing
  per-arm `e2e/<arm>/local-docker/run-benchmark.py --ids <one>` and the swebench harness. `report.py`
  is reused verbatim for the final cost report.
- The DinD-boot + toolbox-build stanza (currently inline in `e2e/econ/fly-remote/fullresolve/entrypoint.sh`)
  is **extracted to a shared `e2e/distributed/lib/boot.sh`** and sourced by BOTH the existing
  single-machine `entrypoint.sh` AND the new `worker-entrypoint.sh`. One copy of the boot logic.
- Single-machine `run.sh`/`entrypoint.sh` stays as the smoke / 1-off path; distributed is the scale
  path. They share `boot.sh` + `run-benchmark.py` + `report.py`.
- **Arm-pluggable:** the coordinator/worker/launcher are arm-agnostic. `ARM=econ|claude|codex`
  selects which `run-benchmark.py` + toolbox image + auth env the worker uses. v1 validates on econ;
  claude/codex drop in without new orchestration.

---

## 2. Components & files

```
e2e/distributed/
├── PLAN.md                        # this file
├── README.md                      # how to run / monitor / teardown  [NEW]
├── run-distributed.sh             # host launcher: build, create fleet, poll, pull, teardown  [NEW]
├── fly.toml                       # app config (dist app; coordinator http_service on 6PN)  [NEW]
├── lib/
│   └── boot.sh                    # shared dockerd-DinD + toolbox-build (sourced by both entrypoints)  [NEW, extracted]
├── coordinator/
│   ├── server.py                  # aiohttp + SQLite queue: /claim /heartbeat /complete /fail /status /drain  [NEW]
│   ├── coordinator-entrypoint.sh  # boot server, seed queue, wait drain, aggregate+grade-merge+report, bundle, HOLD  [NEW]
│   └── schema.sql                 # tasks table DDL  [NEW]
├── worker/
│   ├── worker-entrypoint.sh       # source boot.sh → build toolbox → launch worker-loop  [NEW]
│   └── worker-loop.py             # claim/resolve/grade/report loop + heartbeat thread + graceful drain-exit  [NEW]
├── Dockerfile.dist                # image = per-arm toolbox context + distributed scripts + swebench venv  [NEW or param of existing]
└── tools/
    ├── suite.py                   # resolve SUITE/TASKS → instance-id list (full=Verified 500, mini, lite)  [NEW]
    └── merge-reports.py           # concat per-instance report.json → one grade summary  [NEW]

e2e/econ/fly-remote/fullresolve/entrypoint.sh   # EDIT: source ../../../distributed/lib/boot.sh instead of inline DinD/toolbox
e2e/econ/local-docker/run-benchmark.py          # KEEP (already supports --ids <one>)
e2e/econ/report.py                              # KEEP (reused by coordinator aggregation)
tools/bench-ctl.sh                              # EDIT: add distributed-status / distributed-pull / distributed-destroy
```

### Coordinator queue schema (`schema.sql`)
```sql
CREATE TABLE tasks (
  instance_id     TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending',   -- pending|leased|done|dead
  attempt_count   INTEGER NOT NULL DEFAULT 0,
  worker_id       TEXT,
  lease_until     INTEGER,                            -- epoch s
  last_heartbeat  INTEGER,
  resolved        INTEGER,                            -- 1/0 from grade, NULL until graded
  patch           TEXT,                               -- model_patch
  report_json     TEXT,                               -- per-instance swebench report
  meta_json       TEXT,                               -- cost/telemetry record
  completed_by    TEXT,
  completed_at    INTEGER
);
```

### HTTP protocol (worker ⇄ coordinator, all over 6PN)
| Endpoint | Body | Effect |
|---|---|---|
| `POST /claim` | `{worker_id}` | atomic claim of one `pending`/lease-expired row → `{instance_id}` or `{done:true}` |
| `POST /heartbeat` | `{instance_id, worker_id}` | bump `lease_until` + `last_heartbeat` |
| `POST /complete` | `{instance_id, worker_id, patch, report_json, meta_json, resolved}` | idempotent upsert → `status='done'` |
| `POST /fail` | `{instance_id, worker_id, error}` | `attempt_count++`; ≥2 → `status='dead'` else `pending` |
| `GET /status` | — | counts by status + per-instance progress (host poll) |
| `GET /drain` | — | `{drained:true}` when no `pending`/`leased` left |

### Claim query (the whole queue, one statement)
```sql
UPDATE tasks SET status='leased', worker_id=:w, attempt_count=attempt_count+1,
  lease_until=:now+2700, last_heartbeat=:now
WHERE instance_id = (
  SELECT instance_id FROM tasks
  WHERE status='pending' OR (status='leased' AND lease_until < :now)
  ORDER BY attempt_count, instance_id LIMIT 1)
RETURNING instance_id;
```

---

## 3. Build slices (parallelizable — one sub-agent each)

| # | Slice | Files | Depends on | Agent tier |
|---|---|---|---|---|
| A | **Shared boot lib** — extract DinD+toolbox from econ entrypoint into `lib/boot.sh`; edit econ entrypoint to source it (behaviour-identical) | `lib/boot.sh`, econ `entrypoint.sh` | — | worker |
| B | **Coordinator server** — `server.py` (aiohttp+SQLite, all endpoints, reaper), `schema.sql` | `coordinator/server.py`, `schema.sql` | — | opus (core logic) |
| C | **Worker loop** — `worker-loop.py` (claim/resolve/grade/report + heartbeat thread + drain-exit), `worker-entrypoint.sh` | `worker/*` | A (sources boot.sh) | opus |
| D | **Coordinator entrypoint + aggregation** — boot server, seed, wait-drain, merge reports, run report.py, bundle, HOLD; `merge-reports.py` | `coordinator/coordinator-entrypoint.sh`, `tools/merge-reports.py` | B | worker |
| E | **Launcher** — `run-distributed.sh` (build, create coord+workers paced, poll, pull, teardown), `fly.toml`, `Dockerfile.dist`, `tools/suite.py` | `run-distributed.sh`, `fly.toml`, `Dockerfile.dist`, `tools/suite.py` | B,C,D interfaces | opus |
| F | **bench-ctl + README** — distributed subcommands, docs | `tools/bench-ctl.sh`, `README.md` | E | worker |

Slices A and B have no deps → start first, in parallel. C depends on A; D on B; E on the B/C/D
interfaces (contract is fixed by this doc, so E can start against the spec in parallel and integrate).
F last. Main thread integrates each diff and owns the cross-component wiring (env var names, the
6PN URL contract, metadata keys).

---

## 4. Validation (task #11)

Smoke: `MACHINES=2 ARM=econ LABEL=dist-smoke TASKS="django__django-11880,django__django-11951,django__django-11790"`.
Assert: (a) both workers claim distinct instances (work-stealing, no double-grade), (b) a killed
worker's lease requeues and the survivor finishes it, (c) coordinator produces a merged grade +
cost report matching a known single-machine result for those ids, (d) workers self-stop on drain and
the launcher destroys the whole fleet (0 machines, ≤1 volume left then removed). Then a MACHINES=5
run over the Mini-10 to confirm wall-clock drops from ~1–2 h to ≈ the longest straggler.

## 5. Cost sanity (20k fly credits)
shared-cpu-8x/16 GB workers billed per-second only while `started`; a Mini-10 across 5 machines ≈
10–15 min of 5 machines ≈ negligible. Full Verified 500 across 25 machines ≈ longest-straggler wall
(~30–40 min) × 25 machines started ≈ a few machine-hours — well within budget. Stopped machines cost
only rootfs storage until destroyed; teardown destroys them immediately.
