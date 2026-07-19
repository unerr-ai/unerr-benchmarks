# econ-litellm gateway (LiteLLM on Fly.io)

The shared LiteLLM proxy every benchmark arm bills through — "cost" in this repo
always means REAL spend read from this gateway's DB. Moved here from
`econ-coding-agent/infra/litellm` on 2026-07-19; this directory is now the only
home of the gateway infra (the econ repo no longer carries it).

- App: `econ-litellm` (org `vamsee-k-933`, region `iad`) — dumb-pipe proxy to
  Fireworks serverless (per-tier dedicated-GPU flips via `DEDICATED-FLIP.md`).
- DB: `econ-litellm-db` (fly postgres-flex; LiteLLM tables live in the
  `econ_litellm` database — spend logs, keys).

## Files

| file | what |
|---|---|
| `fly.toml` | app config — machine size/count rationale lives in comments here |
| `Dockerfile` | pinned `berriai/litellm` base + fireworks cost-calculator patch |
| `config.yaml` | committed SERVERLESS model list; mirrors econ's `ECON_COST_MATRIX` rates |
| `econ-entrypoint.sh` | runtime per-tier dedicated-deployment flip (no-op without secrets) |
| `DEDICATED-FLIP.md` | serverless→dedicated flip mechanism + the /responses tool-call fix |
| `probe.sh` | go/no-go smoke: completion + cached-token usage passthrough per model |
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
