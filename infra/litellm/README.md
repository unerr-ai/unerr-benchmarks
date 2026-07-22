# econ-litellm gateway (LiteLLM on Fly.io)

The shared LiteLLM proxy every benchmark arm bills through — "cost" in this repo
always means REAL spend read from this gateway's DB. Moved here from
`econ-coding-agent/infra/litellm` on 2026-07-19; this directory is now the only
home of the gateway infra (the econ repo no longer carries it).

- App: `econ-litellm` (org `vamsee-k-933`, region `iad`) — dumb-pipe proxy to
  Fireworks serverless (per-tier dedicated-GPU flips via `DEDICATED-FLIP.md`).
- DB: `econ-litellm-db` (fly postgres-flex; LiteLLM tables live in the
  `econ_litellm` database — spend logs, keys). **3-member HA cluster** — see below.

## Database: HA cluster

`econ-litellm-db` is a **3-member postgres-flex HA cluster** in `iad`, every
member **`performance-4x` / 16GB / 100GB disk** (1 primary + 2 streaming
standbys; odd quorum → clean repmgr auto-failover). It is on the every-turn hot
path for **every** benchmark arm, so it must not be a single point of failure.

Provision or re-verify the topology (idempotent — clones up to 3, resizes to the
target guest, never destroys a member):

```sh
cd infra/litellm
./scale-db-ha.sh                    # TARGET_MEMBERS=3 DB_VM_SIZE=performance-4x DB_VM_MEMORY=16384
```

Adding a member is `fly machine clone <primary> -a econ-litellm-db --region iad`
— the postgres-flex entrypoint joins the clone via repmgr as a fresh-basebackup
standby. Run the script (esp. a primary resize, which triggers a brief failover)
while **no benchmark fleet is pointed at the gateway**.

> History: shipped as a **single-member** cluster with no failover. On 2026-07-22
> a stale-connection outage on the lone primary took the whole gateway down and
> silent-killed 17 tasks of a rerun (see the connection-closed note below). Scaled
> to 3×`performance-4x` HA that day — cheaper than one full benchmark run.

## Files

| file | what |
|---|---|
| `fly.toml` | app config — machine size/count rationale lives in comments here |
| `Dockerfile` | pinned `berriai/litellm` base + fireworks cost-calculator patch |
| `config.yaml` | committed SERVERLESS model list; mirrors econ's `ECON_COST_MATRIX` rates |
| `econ-entrypoint.sh` | runtime per-tier dedicated-deployment flip (no-op without secrets) |
| `DEDICATED-FLIP.md` | serverless→dedicated flip mechanism + the /responses tool-call fix |
| `probe.sh` | go/no-go smoke: completion + cached-token usage passthrough per model |
| `scale-db-ha.sh` | idempotent: ensure `econ-litellm-db` is a 3-member HA cluster at the target guest |
| `patches/` | fireworks cost-calculator override (cache-read discounts) |
| `.env.local` | gitignored operator secrets (see `.env.example`) |

## Deploy

```sh
cd infra/litellm
fly deploy -a econ-litellm          # image rebuild + rolling replace
LITELLM_API_KEY=$LITELLM_MASTER_KEY ./probe.sh   # post-deploy smoke
```

Runtime secrets (`fly secrets list -a econ-litellm`): `LITELLM_MASTER_KEY`,
`FIREWORKS_API_KEY`, `DATABASE_URL`, plus optional `<TIER>_DEPLOYMENT_PATH`
flips owned by `e2e/distributed/gpu-flip.sh` — never set those by hand.

## Ops notes

- Never scale to zero — the gateway is on econ's every-turn hot path.
- Spend logging requires `econ-litellm-db` to be writable: at 90% disk,
  postgres-flex flips the cluster read-only and cost reads $0 while requests
  still 200. Fix = extend the pg_data volume
  (`fly volumes extend <vol> -a econ-litellm-db -s <GB>`), then verify
  `SHOW default_transaction_read_only` is `off` (cluster-wide AND for the
  `econ_litellm` database — it has had a per-database override before).
- **Gateway down but DB healthy — stale connection pools.** Symptom: callers get
  HTTP `000`/timeout, `fly status -a econ-litellm` shows machines `started` but
  health checks `critical`, and the app logs spam
  `Error in PostgreSQL connection: Error { kind: Closed }` **while
  `econ-litellm-db` itself is 3/3 healthy**. Fly's proxy then returns `PR01 no
  known healthy instances` for every request. Cause = the gateway is holding dead
  pools to a DB that restarted/failed-over under it and never reconnected. Fix =
  **restart the gateway machines** so they reopen fresh pools:
  `fly machine restart <id> -a econ-litellm` (both). This is distinct from the
  disk-full read-only case above (there the DB is unwritable; here the DB is fine
  and only the gateway's connections are stale). The 3-member HA cluster reduces
  how often the DB restarts under the gateway in the first place.
