# Start the Omnigent loopback relay inside an OpenShell sandbox.
#
# The server-managed flow provisions the sandbox itself and starts
# `omnigent host` over `bash -lc`, so there is no seam for a launcher to bring
# the relay up first — but `bash -lc` is a LOGIN shell, so this file runs, and
# the host's exec is held open for its lifetime, which keeps the relay alive
# with it. Idempotent: every `bash -lc` sources this, and only the first one
# with nothing already listening starts a relay.
case $- in *i*) return 0 ;; esac
[ -n "${https_proxy:-${HTTPS_PROXY:-}}" ] || return 0

# Opt-in, because this file is sourced by EVERY `bash -lc` — including the
# short-lived execs a CLI launch makes. OpenShell reaps an exec's process tree
# when the RPC returns, so a relay started by one of those dies with it, and in
# the meantime it holds the port against the long-lived relay the CLI launcher
# is trying to start. The server-managed flow, which has no launcher to start
# one, sets this in the sandbox environment instead.
[ -n "${OMNIGENT_RELAY_AUTOSTART:-}" ] || return 0

_omnigent_relay_port="${OMNIGENT_RELAY_PORT:-6767}"

# POSIX only. This file is sourced by every login shell the launchers use, and
# `sh -lc` is dash on Debian: bash's /dev/tcp is unavailable there and the test
# would fail open, starting a duplicate relay on every exec. `ss` is always
# present — iproute2 is part of OpenShell's own image contract.
_omnigent_relay_listening() {
  ss -ltn 2>/dev/null | grep -q "127\.0\.0\.1:${_omnigent_relay_port}[[:space:]]"
}

if ! _omnigent_relay_listening; then
  OMNIGENT_RELAY_PORT="${_omnigent_relay_port}" \
  OMNIGENT_RELAY_TARGET_HOST="${OMNIGENT_RELAY_TARGET_HOST:-host.openshell.internal}" \
  OMNIGENT_RELAY_TARGET_PORT="${OMNIGENT_RELAY_TARGET_PORT:-16767}" \
    nohup python3 /usr/local/lib/omnigent-proxy-relay.py \
      >>/tmp/omnigent-relay.log 2>&1 &
  # Give the listener a moment so the host's first dial does not race it.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    _omnigent_relay_listening && break
    sleep 0.3
  done
fi
unset _omnigent_relay_port
unset -f _omnigent_relay_listening 2>/dev/null || true
