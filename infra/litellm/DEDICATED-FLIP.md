# Serverless → dedicated-GPU flip + the tool-call fix

How the `econ-litellm` gateway routes a tier from Fireworks **serverless** to a
**dedicated Fireworks GPU deployment** for the duration of a benchmark run, and the
tool-call bug that the flip exposes plus its fix.

Two files carry the whole mechanism:

- `infra/litellm/econ-entrypoint.sh` — the runtime flip + the tool-call fix (this repo).
- `infra/litellm/config.yaml` — the committed serverless config + the belt-and-suspenders
  `allowed_openai_params` on the flippable tiers (this repo).
- `e2e/distributed/gpu-flip.sh` — the operator-side trigger that sets the
  fly secret (this repo).

The committed `config.yaml` is **always serverless** — the flip happens only at RUNTIME,
in the container's copy of `config.yaml`, never in the repo file. That keeps the OL-8.B2
model-registry drift test green (it only parses `model_name:` / `model:` / cost lines).

---

## 1. The flip: serverless → dedicated GPU

### What "flip" means

A serverless tier line in `config.yaml` looks like:

```yaml
  - model_name: minimax/minimax-m3
    litellm_params:
      model: fireworks_ai/accounts/fireworks/models/minimax-m3   # ← serverless base model
```

Flipping it points `litellm_params.model` at a **dedicated deployment path** instead:

```yaml
      model: fireworks_ai/accounts/vamsee-k-ra566yo1je2/deployments/<id>   # ← dedicated GPU
```

econ never sees this. It sends the *slug* (`minimax/minimax-m3`) via `ECON_TIER_BINDING`
(`model-registry.ts`); the gateway maps the slug → whichever `model:` is live. So the flip
is invisible to the agent.

### The trigger (operator side, sibling repo)

`e2e/distributed/gpu-flip.sh`:
1. The operator raises the dedicated Fireworks GPU(s) per tier and passes the deployment
   ids (the script does **not** launch GPUs itself).
2. For each tier it runs `fly secrets set <TIER>_DEPLOYMENT_PATH=<path> -a econ-litellm`.
3. At teardown it **unsets** the secret → the gateway reverts to serverless on next boot.

Secret name → tier map (exactly `ECON_TIER_BINDING`):

| secret | tier | serverless slug |
|---|---|---|
| `CONDUCTOR_DEPLOYMENT_PATH` | conductor | `minimax-m3` |
| `ORACLE_DEPLOYMENT_PATH` | oracle | `glm-5p2` |
| `REASONER_DEPLOYMENT_PATH` | reasoner | `deepseek-v4-pro` |
| `EXECUTOR_DEPLOYMENT_PATH` | executor | `gpt-oss-120b` |

(`CONDUCTOR_DEPLOYMENT_PATH` keeps its original single-tier back-compat with
`DEDICATED_CONDUCTOR=1` runs.)

### The rewrite (gateway side, this repo)

`econ-entrypoint.sh` is the container `ENTRYPOINT`, chained **in front of** the base
litellm image entrypoint. On boot, for each tier whose `<TIER>_DEPLOYMENT_PATH` secret is
set, `flip_tier()` sed-rewrites that tier's `model:` line in the runtime `config.yaml`:

```sh
sed -i -E "s|^([[:space:]]*)model:[[:space:]]*fireworks_ai/accounts/fireworks/models/${_slug}[[:space:]]*\$|\\1model: ${_path}\\n\\1drop_params: true|" "$CONFIG"
```

Anchored end-of-line match → only the serverless base-model line is touched, never the
`model_name:` slug or another tier. If no secret is set, it's a **no-op** and serverless
`config.yaml` is served verbatim. It then chains to `/app/docker/prod_entrypoint.sh` (NOT
`docker/entrypoint.sh`, which is migration-only and exits without serving) so prisma /
spend-logs DB setup still runs.

---

## 2. The tool-call fix (`drop_params: true`)

### Symptom

The terminal-bench econ arm scored **0 tokens / 0 resolved** with a dedicated conductor:
every opencode LLM call `400`'d with `litellm.UnsupportedParamsError` on `tool_choice`.

### Cause

A **dedicated** Fireworks deployment does not advertise its param support in a form
LiteLLM can resolve. With the gateway's deliberate global `litellm_settings.drop_params:
false`, LiteLLM **rejects** params it can't confirm the model supports — here
`tool_choice` and `reasoning_effort` — instead of forwarding or dropping them → `400`.

### Why `allowed_openai_params` alone did NOT fix it

The first attempt was `allowed_openai_params: ["tool_choice", "reasoning_effort"]` on the
flipped tiers in `config.yaml` (already on `minimax-m3`; added to `glm-5p2` +
`deepseek-v4-pro` + `gpt-oss-120b`). **LiteLLM only honors `allowed_openai_params` on the
`/chat/completions` surface — NOT on `/responses`.** opencode's default surface is
`/responses`, so it kept `400`ing on every call. That is exactly what produced the
0-token / 0-resolved terminal run.

### The fix

`flip_tier()` **also** injects a per-tier `drop_params: true` into the same
`litellm_params` block (the `\\1drop_params: true` in the sed above). `drop_params: true`
**does** take effect on `/responses` → LiteLLM silently drops the unsupported params
instead of `400`ing.

Belt and suspenders, per surface:

| surface | who fixes it | effect |
|---|---|---|
| `/responses` (opencode default) | per-tier `drop_params: true` (injected at flip) | drops unsupported params, no 400 |
| `/chat/completions` | `allowed_openai_params` (in config.yaml) | forwards them verbatim; `reasoning_effort` pins ride through untouched |

The global `litellm_settings.drop_params` stays **`false`** — only the flipped tiers get
`drop_params: true`, so non-flipped (serverless) tiers are unaffected and still surface
real param errors.

---

## 3. Verify

After a flip (both must return `200`):

```sh
# /responses + tool_choice  → 200 with tool_calls   (this is what used to 400)
curl -sS https://econ-litellm.fly.dev/v1/responses \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"minimax/minimax-m3","input":"...","tools":[...],"tool_choice":"required"}'

# /chat/completions + reasoning_effort → 200 with tool_calls
curl -sS https://econ-litellm.fly.dev/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"minimax/minimax-m3","messages":[...],"tools":[...],"reasoning_effort":"low"}'
```

Both verified live against the dedicated minimax-m3 deployment.

## 4. Revert to serverless

Unset the tier's secret (`gpu-flip.sh` teardown, or
`fly secrets unset <TIER>_DEPLOYMENT_PATH -a econ-litellm`) and the gateway restarts on
serverless `config.yaml`: no `model:` rewrite, no injected `drop_params`. Nothing in the
repo changes — the flip only ever lived in the running container.

---

## 5. Related

- this repo's CLAUDE.md → "gpu-flip.sh" in the benchmark runbooks section.
- `econ-entrypoint.sh` header comment (lines 21–32) is the in-code version of §2.
- Spend-logging depends on the `econ-litellm-db` Postgres being writable — if it flips
  read-only (90%-disk safety) cost reads `$0` even at `200`; extend the pg_data volume.
