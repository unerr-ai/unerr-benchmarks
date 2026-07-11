# Runnable fly.io image for the CLAUDE preflight (zero cost, no token):
# Node + git + Claude Code CLI + unerr (offline Pro) + the Claude toolbox
# scripts + a seeded /testbed repo. Built by fly's REMOTE builder (no local
# Docker). Parallel to e2e/codex/fly-remote/Dockerfile; codex is untouched.
#
# Build context = e2e/claude (small, self-contained). The agent-specific scripts
# are reused verbatim from local-docker/context so there is one source of truth.
FROM node:24-bookworm-slim
ENV TOOLBOX=/opt/toolbox
ENV PATH=$TOOLBOX/bin:$PATH
ENV HOME=/work

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl build-essential python3 \
 && rm -rf /var/lib/apt/lists/*

# Toolbox prefix holds globally-installed claude + unerr (native deps compile here).
COPY fly-remote/unerr-ai-unerr-0.4.1.tgz /tmp/unerr.tgz
RUN mkdir -p "$TOOLBOX" \
 && npm config set prefix "$TOOLBOX" \
 && npm install -g @anthropic-ai/claude-code /tmp/unerr.tgz \
 && rm -f /tmp/unerr.tgz \
 && unerr --version \
 && claude --version

# Claude toolbox helpers (preflight.sh / lib.sh expect them under $TOOLBOX).
COPY local-docker/context/dev-entitlement.mjs $TOOLBOX/dev-entitlement.mjs
COPY local-docker/context/lib.sh              $TOOLBOX/lib.sh
COPY local-docker/context/mcp-healthcheck.mjs $TOOLBOX/mcp-healthcheck.mjs
COPY local-docker/context/preflight.sh        $TOOLBOX/preflight.sh
COPY local-docker/context/run-instance.sh     $TOOLBOX/run-instance.sh
RUN chmod +x $TOOLBOX/preflight.sh $TOOLBOX/run-instance.sh

# Seed a tiny git repo at /testbed so preflight's git ls-files / file_read have
# real input (mirrors Dockerfile.smoke). Separate from /work, so a /work volume
# never hides it.
RUN mkdir -p /testbed && cd /testbed && git init -q \
 && git config user.email t@t && git config user.name t \
 && printf 'def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n' > calc.py \
 && printf '# sample repo\nused only to smoke-test unerr inside the container\n' > README.md \
 && git add -A && git commit -qm init

COPY fly-remote/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# /work is the writable scratch + unerr home (entitlement cache, unerrd socket).
RUN mkdir -p /work
WORKDIR /work
ENTRYPOINT ["/app/entrypoint.sh"]
