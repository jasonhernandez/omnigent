# Omnigent host image + the OpenShell sandbox policy.
#
# The published host image satisfies OpenShell's *image* contract (non-root
# `sandbox` user, iproute2/nftables) but ships no /etc/openshell/policy.yaml,
# so OpenShell >= 0.0.116 refuses it: the supervisor needs a non-root process
# identity, and deny-by-default egress blocks the dial-back. Adding the policy
# is the whole delta — see policy.yaml.
ARG BASE_IMAGE=ghcr.io/omnigent-ai/omnigent-host:latest
FROM ${BASE_IMAGE}
COPY policy.yaml /etc/openshell/policy.yaml

# The base image keeps WORKDIR /root for the root-based providers, but OpenShell
# validates the workspace as the non-root sandbox user before starting it, and
# /root is mode 700. /sandbox is that user's home, created by the base image.
WORKDIR /sandbox

# `omnigent sandbox create` overlays freshly built wheels onto the image venv
# with pip, but /opt/venv is root-owned and OpenShell runs as `sandbox`. Hand
# the venv to that user so the overlay install succeeds.
RUN chown -R sandbox:sandbox /opt/venv

# OpenCode is not in the base image's harness set (it is an EXTRA_HARNESS_CLIS
# row). Pin ~1.18.0 like omnigent/onboarding/harness_install.py: opencode-ai's
# npm `latest` is a 0.0.0-beta pre-release that the runtime version gate
# (>=1.17.7,<1.19.0) then rejects.
RUN npm install -g --no-audit --no-fund opencode-ai@~1.18.0 \
 && npm cache clean --force \
 && su sandbox -s /bin/sh -c 'opencode --version'

