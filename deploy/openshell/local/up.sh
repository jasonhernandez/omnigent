#!/usr/bin/env bash
# Bring up the local Omnigent-on-OpenShell loop, end to end.
#
# Builds the policy-patched host image, starts an Omnigent server the sandbox
# can reach, provisions a sandbox through OpenShell, holds the loopback relay
# open, and registers the sandbox as a host. See README.md for the why.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "${here}/../../.." && pwd)"

IMAGE="${OMNIGENT_OPENSHELL_HOST_IMAGE:-omnigent-host-openshell:local}"
PORT="${OMNIGENT_TEST_PORT:-6868}"
DATA_DIR="${OMNIGENT_DATA_DIR:-${here}/.state}"
SANDBOX_NAME="${SANDBOX_NAME:-omni-os}"
OMNIGENT_BIN="${OMNIGENT_BIN:-${repo}/.venv/bin/omnigent}"
STATE="${DATA_DIR}/run"

mkdir -p "${STATE}"

step() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }

step "Checking the OpenShell gateway"
openshell status

step "Building ${IMAGE}"
docker build --platform linux/amd64 -t "${IMAGE}" "${here}"

step "Starting the Omnigent server on 0.0.0.0:${PORT}"
# The sandbox dials the host, so the server cannot stay on loopback. A
# non-loopback bind would otherwise auto-enable accounts (login) mode and the
# sandbox would get 401s, so single-user is pinned explicitly. Local dev only —
# this serves an unauthenticated API on every interface.
if curl -sf -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  echo "  → already listening on ${PORT}"
else
  OMNIGENT_DATA_DIR="${DATA_DIR}" OMNIGENT_AUTH_ENABLED=0 OMNIGENT_LOCAL_SINGLE_USER=1 \
    nohup "${OMNIGENT_BIN}" server --host 0.0.0.0 --port "${PORT}" \
    > "${STATE}/server.log" 2>&1 &
  for _ in $(seq 1 40); do
    curl -sf -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && break
    sleep 2
  done
  curl -sf -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null \
    || { echo "server did not come up; see ${STATE}/server.log" >&2; exit 1; }
  echo "  → up (log: ${STATE}/server.log)"
fi

step "Provisioning the sandbox"
# --server is the URL the *sandbox* uses. host.openshell.internal is how a
# sandbox addresses this machine; the relay below re-points it at loopback.
OMNIGENT_OPENSHELL_HOST_IMAGE="${IMAGE}" OMNIGENT_DATA_DIR="${DATA_DIR}" \
  "${OMNIGENT_BIN}" sandbox create --provider openshell --name "${SANDBOX_NAME}" \
  --server "http://host.openshell.internal:${PORT}" | tee "${STATE}/create.log"

sandbox_id="$(grep -oE '^Sandbox   [^ ]+' "${STATE}/create.log" | awk '{print $2}' | tail -1)"
[ -n "${sandbox_id}" ] || { echo "could not read the sandbox name from create output" >&2; exit 1; }
echo "${sandbox_id}" > "${STATE}/sandbox_id"

step "Starting the loopback relay in ${sandbox_id}"
# OpenShell reaps an exec's process tree when the RPC returns, so the relay is
# held open by this backgrounded exec for as long as the loop runs.
openshell sandbox exec -n "${sandbox_id}" -- sh -c "cat > /sandbox/proxy-relay.py" \
  < "${here}/proxy-relay.py"
OMNIGENT_RELAY_PORT="${PORT}" \
  nohup openshell sandbox exec -n "${sandbox_id}" \
    --env "OMNIGENT_RELAY_PORT=${PORT}" --env "OMNIGENT_RELAY_TARGET_PORT=${PORT}" \
    -- python3 /sandbox/proxy-relay.py > "${STATE}/relay.log" 2>&1 &
echo $! > "${STATE}/relay.pid"
sleep 6
cat "${STATE}/relay.log" || true

step "Registering the sandbox as a host"
echo "Ctrl-C detaches (and stops the in-sandbox host)."
OMNIGENT_OPENSHELL_HOST_IMAGE="${IMAGE}" OMNIGENT_DATA_DIR="${DATA_DIR}" \
  exec "${OMNIGENT_BIN}" sandbox connect --provider openshell \
    --sandbox-id "${sandbox_id}" --server "http://127.0.0.1:${PORT}"
