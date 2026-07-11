# Full-resolve SWE-bench runner for fly.io — CLAUDE arm.
#
# Runs the SAME local-docker pipeline (run-benchmark.py → swebench grading)
# INSIDE a fly machine via Docker-in-Docker. Fly machines are real Firecracker
# VMs on native x86_64, so the SWE-bench instance images run with NO QEMU
# emulation (unlike Apple-Silicon laptops) — fast and faithful.
#
# Parallel to e2e/codex/fly-remote/fullresolve/Dockerfile; codex is untouched.
# Build context = e2e/claude (so we COPY local-docker/*). Built by run.sh via
# fly's remote builder.

FROM python:3.12-slim-bookworm

# dockerd + CLI (the engine we drive in-VM), git + basics for the harness.
RUN apt-get update && apt-get install -y --no-install-recommends \
      docker.io git curl ca-certificates procps iproute2 jq \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /work

# Grading + dataset deps in a venv (swebench harness pulls a heavy tree).
RUN python -m venv /work/.venv \
 && /work/.venv/bin/pip install --no-cache-dir --upgrade pip \
 && /work/.venv/bin/pip install --no-cache-dir "swebench>=4.1" datasets

# Harness — everything run-benchmark.py touches must be co-located: it builds
# the instance image with `docker build -f /work/Dockerfile.instance /work`.
# local-docker/context must already contain the staged unerr-ai-unerr-*.tgz
# (build-toolbox/run.sh stages it) so the in-VM toolbox build finds it.
COPY local-docker/run-benchmark.py    /work/run-benchmark.py
COPY local-docker/Dockerfile.instance /work/Dockerfile.instance
COPY local-docker/Dockerfile.toolbox  /work/Dockerfile.toolbox
COPY local-docker/context             /work/context
COPY fly-remote/fullresolve/entrypoint.sh /work/entrypoint.sh
RUN chmod +x /work/entrypoint.sh

# run-benchmark.py builds each instance image with context = /work, which holds
# the 300MB+ swebench .venv. Dockerfile.instance COPYs nothing FROM the context
# (only `COPY --from=unerr-claude-toolbox`), so ignore the whole context —
# otherwise hundreds of MB stream to dockerd on every instance build.
RUN printf '*\n' > /work/.dockerignore

ENTRYPOINT ["/work/entrypoint.sh"]
