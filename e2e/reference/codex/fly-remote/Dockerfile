# Runnable fly.io image: Node + git + Codex CLI + unerr (offline Pro) + the
# localization A/B harness. Built by fly's REMOTE builder (no local Docker).
FROM node:24-bookworm-slim
ENV TOOLBOX=/opt/toolbox
ENV PATH=$TOOLBOX/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl build-essential python3 \
 && rm -rf /var/lib/apt/lists/*

# Toolbox prefix holds globally-installed codex + unerr (native deps compile here).
COPY codex/fly-remote/unerr-ai-unerr-0.3.4.tgz /tmp/unerr.tgz
RUN mkdir -p "$TOOLBOX" \
 && npm config set prefix "$TOOLBOX" \
 && npm install -g @openai/codex /tmp/unerr.tgz \
 && rm -f /tmp/unerr.tgz \
 && unerr --version

# toolbox helpers (lib.sh expects $TOOLBOX/dev-entitlement.mjs)
COPY common/dev-entitlement.mjs $TOOLBOX/dev-entitlement.mjs
COPY common/lib.sh              $TOOLBOX/lib.sh

# harness + dataset
WORKDIR /app
COPY codex/fly-remote/loc-runner.mjs /app/loc-runner.mjs
COPY codex/fly-remote/swebl.ndjson   /app/swebl.ndjson
COPY codex/fly-remote/entrypoint.sh  /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# /work is the writable scratch (repos, codex homes, results); a fly volume can mount here.
RUN mkdir -p /work
ENV HOME=/work
ENTRYPOINT ["/app/entrypoint.sh"]
