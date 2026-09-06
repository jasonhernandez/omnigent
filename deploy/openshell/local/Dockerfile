# Omnigent host image + the OpenShell sandbox policy.
#
# The published host image satisfies OpenShell's *image* contract (non-root
# `sandbox` user, iproute2/nftables) but ships no /etc/openshell/policy.yaml,
# so OpenShell >= 0.0.116 refuses it: the supervisor needs a non-root process
# identity, and deny-by-default egress blocks the dial-back. Adding the policy
# is the whole delta — see policy.yaml.
ARG BASE_IMAGE=ghcr.io/omnigent-ai/omnigent-host:latest
FROM ${BASE_IMAGE}

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

# Self-starting loopback relay for the server-managed flow, where nothing
# local can start it: the managed launcher runs `omnigent host` over
# `bash -lc`, a login shell, so the profile snippet brings the relay up in the
# same held-open exec. See relay-profile.sh.
COPY proxy-relay.py /usr/local/lib/omnigent-proxy-relay.py
COPY relay-profile.sh /etc/profile.d/zz-omnigent-relay.sh
RUN chmod 0644 /etc/profile.d/zz-omnigent-relay.sh \
 && chmod 0755 /usr/local/lib/omnigent-proxy-relay.py

# Map injected credential placeholders onto the variables git/gh expect.
COPY credentials-profile.sh /etc/profile.d/zz-omnigent-credentials.sh
RUN chmod 0644 /etc/profile.d/zz-omnigent-credentials.sh

# GitHub CLI. The github provider profile allow-lists `gh` (and `git`) as
# credential-bearing binaries but NOT `curl`, so a REST-based PR flow is denied
# under OpenShell while `gh` is not — an OpenShell landing agent that opens a
# PR needs this binary present.
ARG GH_VERSION=2.63.2
RUN curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
      -o /tmp/gh.tgz \
 && tar -xzf /tmp/gh.tgz -C /tmp \
 && install -m 0755 "/tmp/gh_${GH_VERSION}_linux_amd64/bin/gh" /usr/local/bin/gh \
 && rm -rf /tmp/gh.tgz "/tmp/gh_${GH_VERSION}_linux_amd64" \
 && gh --version | head -1

# Copied LAST on purpose. The policy changes on nearly every iteration, and a
# COPY high in the file invalidates every layer beneath it — including the npm
# install, which then needs the network to rebuild and fails outright whenever
# this host's DNS is flaky.
COPY policy.yaml /etc/openshell/policy.yaml
