# SINGLE-ARM econ full-resolve SWE-bench runner for fly.io.
#
# Runs the local-docker pipeline (run-benchmark.py → swebench grading →
# report.py) INSIDE a fly machine via Docker-in-Docker. Fly machines are real
# Firecracker VMs on native x86_64, so the SWE-bench instance images run with
# NO QEMU emulation (unlike Apple-Silicon laptops) — fast and faithful.
#
# Build context = e2e/econ/  (so we can COPY both local-docker/* — including the
# vendored musl econ binary under local-docker/context/vendor/ — and the parent
# econ-telemetry.py / econ-tier-cost.py / report.py in one image). Built by
# run.sh via fly's remote builder.

FROM python:3.12-slim-bookworm

# dockerd + CLI (the engine we drive in-VM), git + jq for the harness/beacons.
RUN apt-get update && apt-get install -y --no-install-recommends \
      docker.io git curl ca-certificates procps iproute2 jq \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /work

# Grading + dataset deps in a venv (the swebench harness pulls a heavy tree).
RUN python -m venv /work/.venv \
 && /work/.venv/bin/pip install --no-cache-dir --upgrade pip \
 && /work/.venv/bin/pip install --no-cache-dir "swebench>=4.1" datasets

# Harness — run-benchmark.py builds each instance image with context = its own
# dir (/work/local-docker), so keep the local-docker subtree intact there. The
# toolbox build context is /work/local-docker/context (holds the vendored binary).
COPY local-docker/run-benchmark.py    /work/local-docker/run-benchmark.py
COPY local-docker/Dockerfile.instance /work/local-docker/Dockerfile.instance
COPY local-docker/Dockerfile.toolbox  /work/local-docker/Dockerfile.toolbox
COPY local-docker/context             /work/local-docker/context
# Cost/telemetry helpers live one level up from run-benchmark.py (../ on laptop
# and here), so mirror that: put them at /work.
COPY econ-telemetry.py                /work/econ-telemetry.py
COPY econ-tier-cost.py                /work/econ-tier-cost.py
COPY report.py                        /work/report.py
# Shared dockerd-boot + toolbox-build lib, sourced by entrypoint.sh as
# /work/lib/boot.sh. run.sh vendors it into lib/ from e2e/distributed/lib/boot.sh
# (it lives outside this build context). Missing it → entrypoint boots no dockerd.
COPY lib/boot.sh                      /work/lib/boot.sh
COPY fly-remote/fullresolve/entrypoint.sh /work/entrypoint.sh
RUN chmod +x /work/entrypoint.sh /work/local-docker/context/run-instance.sh

# run-benchmark.py builds each instance image with context = /work/local-docker,
# which holds the vendored musl binary (tens of MB). Dockerfile.instance COPYs
# NOTHING from that context (only `COPY --from=unerr-econ-toolbox`), so ignore
# the whole context — else it streams to dockerd on every instance build. This
# .dockerignore is only read when local-docker is the build context; the toolbox
# build (context=local-docker/context) is unaffected and still sees vendor/.
RUN printf '*\n' > /work/local-docker/.dockerignore

ENTRYPOINT ["/work/entrypoint.sh"]
