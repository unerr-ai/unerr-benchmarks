# econ SMOKE runner for fly.io — proves econ + LiteLLM-gateway auth + telemetry +
# the SQLite per-tier reader work in the fly environment before a real
# SWE-bench mini run. Mirrors econ's own proven image shape
# (packages/opencode/Dockerfile: Alpine + the musl binary).
#
# The binary is the LOCAL econ build (linux-x64-baseline-musl) — NOT npm,
# since the latest econ code is uncommitted locally. run.sh vendors it into
# fly-remote/vendor/ before this build runs.
#
# Build context = e2e/econ (see run.sh: `flyctl deploy ... "$ECON_DIR"`), so
# every COPY below is relative to e2e/econ.
FROM alpine

RUN apk add --no-cache libgcc libstdc++ ripgrep git python3 bash coreutils ca-certificates

COPY fly-remote/vendor/opencode /usr/local/bin/opencode
RUN chmod +x /usr/local/bin/opencode && /usr/local/bin/opencode --version

COPY econ-telemetry.py econ-tier-cost.py report.py smoke.sh /opt/econ/
COPY fly-remote/vendor/opencode.json /opt/econ/opencode.json
# run.sh always creates fly-remote/vendor/dot-opencode/ (even empty) so this
# COPY never fails even when the econ repo has no .opencode/ checked out.
COPY fly-remote/vendor/dot-opencode/ /opt/econ/.opencode/

ENV ECON_BIN=/usr/local/bin/opencode \
    ECON_CONFIG=/opt/econ/opencode.json \
    ECON_PLUGINS=/opt/econ/.opencode \
    KEEP=0

ENTRYPOINT ["bash", "/opt/econ/smoke.sh"]
