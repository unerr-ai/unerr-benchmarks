# --- econ override (2026-07-06), APPENDED to upstream fireworks_ai/cost_calculator.py ---
# Upstream's cost_per_token multiplies the FULL prompt_tokens by input_cost_per_token —
# usage.prompt_tokens_details.cached_tokens is never read, so response-cost / key-spend
# price a 100%-cached hop identically to a cold one (v3 probe: discount-amount 0.0 on a
# 6.5k-token fully-cached hop). The generic calculator already discounts cached tokens
# against cache_read_input_token_cost (registered per-deployment via model_info in
# config.yaml), so redefine cost_per_token as a delegate. Appending (not replacing the
# file) preserves get_base_model_for_pricing, which litellm/utils.py imports from here.
# The import is lazy to stay out of litellm's package-init import cycle. Delete once
# upstream's fireworks_ai calculator reads prompt_tokens_details itself.


def cost_per_token(model, usage):
    from litellm.litellm_core_utils.llm_cost_calc.utils import generic_cost_per_token

    return generic_cost_per_token(
        model=model, usage=usage, custom_llm_provider="fireworks_ai"
    )


cost_per_token.__econ_override__ = True  # the Dockerfile build-time assert hooks on this
