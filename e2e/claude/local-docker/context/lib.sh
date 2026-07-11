# e2e/common shared shell helpers. Source AFTER the toolbox is on PATH.
# Used by both run-instance.sh (the benchmark driver) and preflight.sh.
TOOLBOX="${TOOLBOX:-/opt/toolbox}"

# Mint a Pro entitlement OFFLINE and export the vars that let the unerr binary
# run fully without `unerr login` or any cloud round-trip:
#   - UNERR_ENTITLEMENT_KID/_PUBKEY  -> trust the dev-signed cache = PLAN is Pro
#     (lifts the free-tier 1-repo cap; honored by the daemon's repo-cap check).
#   - UNERR_TOKEN                    -> satisfies the LOGIN-PRESENCE wall, a check
#     SEPARATE from the plan (src/cloud/login-gate.ts: hasHeadlessToken). Human
#     commands (`unerr pm start`, `unerr install codex`) refuse without it even
#     when the plan is Pro. Its mere presence = "allowed"; the server would only
#     validate it on the wire, which we never reach (telemetry/recall stay local).
unerr_offline_pro() {
  eval "$(node "$TOOLBOX/dev-entitlement.mjs" mint pro --fresh-hours 720 2>/dev/null)"
  export UNERR_ENTITLEMENT_KID UNERR_ENTITLEMENT_PUBKEY
  export UNERR_TOKEN="${UNERR_TOKEN:-unerr_sk_e2e_offline_benchmark}"
}

# Start unerrd. MUST run AFTER unerr_offline_pro: the daemon decides the repo
# cap in its OWN process, so it only honors Pro if it inherits the entitlement
# env at spawn. `unerr pm start` blocks until the socket is ready, then the
# daemon reparents to PID 1 and stays up for the whole run.
unerr_start_daemon() {
  unerr pm start
}

# True when unerrd's global socket exists (daemon is up).
unerr_daemon_up() {
  [ -S "$HOME/.unerr/unerrd.sock" ]
}
