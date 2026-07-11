# Full-resolve SWE-bench runner for fly.io.
#
# Runs the SAME local-docker pipeline (run-benchmark.py → swebench grading →
# report-runs.py) INSIDE a fly machine via Docker-in-Docker. Fly machines are
# real Firecracker VMs on native x86_64, so the SWE-bench instance images run
# with NO QEMU emulation (unlike Apple-Silicon laptops) — fast and faithful.
#
# Build context = e2e/codex/  (so we can COPY both local-docker/* and the
# parent report-runs.py in one image). Built by run.sh via fly's remote builder.

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
# the instance image with `docker build -f <its dir>/Dockerfile.instance <its dir>`.
COPY local-docker/run-benchmark.py    /work/run-benchmark.py
COPY local-docker/Dockerfile.instance /work/Dockerfile.instance
COPY local-docker/Dockerfile.toolbox  /work/Dockerfile.toolbox
COPY local-docker/context             /work/context
COPY report-runs.py                   /work/report-runs.py
COPY fly-remote/fullresolve/entrypoint.sh /work/entrypoint.sh
RUN chmod +x /work/entrypoint.sh

# run-benchmark.py builds each instance image with context = its own dir (/work),
# which now holds the 338MB swebench .venv. Dockerfile.instance COPYs nothing FROM
# the context (only `COPY --from=unerr-codex-toolbox`), so ignore the whole context
# — otherwise 400MB+ is streamed to dockerd on every one of the 50 instance builds.
RUN printf '*\n' > /work/.dockerignore

ENTRYPOINT ["/work/entrypoint.sh"]
