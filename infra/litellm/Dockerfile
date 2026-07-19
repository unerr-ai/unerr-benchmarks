# econ LiteLLM gateway image (OL-8.A2), per docs.litellm.ai/docs/proxy/deploy
# "Use litellm as a base image". Pinned BY DIGEST (resolved from ghcr main-stable,
# 2026-07-06) per the docs' "pin a version, do not use :latest" warning; bump
# deliberately and re-run the OL-8.C3 tool-call smoke on every bump — the image
# moves fast and streaming regressions happen.
# 2026-07-06 bump rationale: the 2026-07-04 pin (1.90.3) dropped
# prompt_tokens_details from STREAMED usage for every fireworks model except
# the conductor's (nonstream carried it fine) — child slices on the other
# three tier models recorded 0 cache reads. Post-bump probe must confirm
# stream usage details for the reasoner model before trusting stream cache
# accounting.
FROM ghcr.io/berriai/litellm@sha256:078f96272f0c84da383e6a1f197478390b5204a86dcf00a181bcfe02a00e5dfa
WORKDIR /app
COPY config.yaml .
# Cached-token discount override (2026-07-06, see patches/ header comment):
# upstream's fireworks_ai cost calculator ignores prompt_tokens_details, so
# response-cost / key-spend never discount cache hits. APPEND a cost_per_token
# redefinition (later def wins; get_base_model_for_pricing survives — utils.py
# imports it from this module). The re-import assert makes a base-image layout
# change break the build loudly instead of silently skipping the patch.
COPY patches/fireworks_cost_calculator.py /tmp/fw_cost_override.py
RUN python3 -c "import litellm.llms.fireworks_ai.cost_calculator as m; f = open(m.__file__, 'a'); f.write('\n\n' + open('/tmp/fw_cost_override.py').read()); f.close(); print('patched', m.__file__)" \
 && python3 -c "import litellm.llms.fireworks_ai.cost_calculator as m; assert getattr(m.cost_per_token, '__econ_override__', False), 'econ fireworks cost override missing'; print('override active:', m.__file__)"
RUN chmod +x ./docker/entrypoint.sh
# econ conductor-flip wrapper: a no-op unless CONDUCTOR_DEPLOYMENT_PATH is set, in
# which case it rewrites the conductor's upstream to a dedicated Fireworks
# deployment (in the container's config copy only — the committed config.yaml and
# the OL-8.B2 drift test are untouched) before chaining the base entrypoint above.
COPY econ-entrypoint.sh /app/econ-entrypoint.sh
RUN chmod +x /app/econ-entrypoint.sh
ENTRYPOINT ["/app/econ-entrypoint.sh"]
EXPOSE 4000/tcp
# No --detailed_debug in prod (docs warn it slows responses).
CMD ["--port", "4000", "--config", "config.yaml"]
