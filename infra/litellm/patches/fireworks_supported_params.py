# econ override (2026-07-19): LiteLLM resolves a fireworks model's supported
# OpenAI params from the model STRING, so a dedicated-deployment path
# (`...#accounts/<acct>/deployments/<id>`) loses tool_choice/reasoning_effort
# that its serverless base model has (measured live on 1.91.0: 39 vs 43 params).
# With drop_params:false that 400s (UnsupportedParamsError); the old per-tier
# `drop_params: true` flip workaround stopped the 400s but silently STRIPPED
# tool_choice/reasoning_effort on /responses — tool-call behaviour then differed
# between serverless and dedicated. Fireworks accepts these params identically on
# both deployment forms, so the honest fix is capability resolution, not dropping.
#
# APPENDED to litellm/llms/fireworks_ai/chat/transformation.py by the Dockerfile
# (same append+assert pattern as fireworks_cost_calculator.py). Rebinding the
# config class covers BOTH surfaces: /chat/completions and the /responses bridge
# converge on this provider lookup inside get_optional_params.

_econ_orig_get_supported_openai_params = FireworksAIConfig.get_supported_openai_params


def _econ_get_supported_openai_params(self, model: str):
    # `base#deployments/...` (the only form gpu-flip.sh writes): resolve from the
    # base-model half so dedicated inherits the serverless capability set EXACTLY.
    base = model.split("#", 1)[0]
    params = list(_econ_orig_get_supported_openai_params(self, base))
    # Pure `/deployments/<id>` strings carry no base model to resolve from — force
    # the params every Fireworks deployment accepts rather than let them be dropped.
    for _p in ("tools", "tool_choice", "reasoning_effort"):
        if _p not in params:
            params.append(_p)
    return params


_econ_get_supported_openai_params.__econ_override__ = True
FireworksAIConfig.get_supported_openai_params = _econ_get_supported_openai_params
