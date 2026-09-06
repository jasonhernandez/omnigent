# Omnigent on a local OpenShell gateway

A reproducible local loop: an OpenShell sandbox, running on the gateway's Docker
compute driver, registered as an Omnigent host. `./up.sh` brings the whole thing
up; this file explains the parts that are not obvious and why each exists.

The published host image does **not** work with a current OpenShell gateway as
shipped — the gaps are listed under [What the image needs](#what-the-image-needs).
Everything here was verified against gateway/SDK **0.0.116** on amd64 Linux with
the Docker driver.

## Run it

```bash
deploy/openshell/local/up.sh
```

It builds the patched image, starts a server the sandbox can reach, provisions
the sandbox, holds the relay open, and registers the host. Ctrl-C detaches.

Prerequisites: a running OpenShell gateway (`openshell status` reports
`Connected`), Docker, and a repo venv with the extra —
`uv sync --extra openshell`.

## Verify it by hand

1. `openshell status` → `Connected`.
2. Run `./up.sh`. It ends on
   `✓ Connected as '<name>' (<id>), 0 live runner(s). Listening for sessions`.
3. In a second terminal, confirm the server sees the sandbox as a live host:
   ```bash
   curl -s http://127.0.0.1:6868/v1/hosts | python3 -m json.tool | head -20
   ```
   Expect `"status": "online"` and a `configured_harnesses` map.
4. Confirm the agent CLIs really are in the sandbox:
   ```bash
   openshell sandbox exec -n "$(cat deploy/openshell/local/.state/run/sandbox_id)" \
     -- sh -lc 'id; omnigent --version; claude --version'
   ```
   Expect uid `sandbox`, an `omnigent` version, and a `claude` version.
5. Open `http://127.0.0.1:6868` and start a session against that host.

To drive a real agent turn the sandbox also needs model credentials — see
[Credentials](#credentials).

## What the image needs

`Dockerfile` layers three things onto `ghcr.io/omnigent-ai/omnigent-host:latest`.
Each was a hard failure, in this order:

| Symptom | Cause | Fix |
|---|---|---|
| `OCI USER is required because run_as_user is omitted`, container exits | The image runs as root and ships no `/etc/openshell/policy.yaml`; the supervisor refuses a root sandbox | `policy.yaml` sets `process.run_as_user: sandbox` |
| `workspace path component '/root' is not traversable` | Image keeps `WORKDIR /root` (mode 700) for the root-based providers | `WORKDIR /sandbox` |
| `omnigent: Permission denied` inside the sandbox | Landlock denied `/opt`, where the Omnigent venv lives | `/opt` in `read_only` |
| `pip install ... Permission denied: /opt/venv/...` | `omnigent sandbox create` overlays wheels into a root-owned venv | `chown -R sandbox:sandbox /opt/venv`, `/opt/venv` in `read_write` |

A policy the supervisor considers unsafe is **silently discarded** for the
restrictive default, which resurfaces as the identity error above rather than a
policy error. `protocol: tcp` with an IP-literal `host` is one such rejection —
hence `host.openshell.internal` plus `allowed_ips`.

## Why the relay exists

`proxy-relay.py` is the one piece that is a workaround rather than configuration.

OpenShell is deny-by-default and forces sandbox egress through its policy proxy;
direct TCP is refused. The Omnigent host reaches the server two ways, and only
one of them survives that:

- Its **HTTP** calls honour `https_proxy` and work.
- Its **WebSocket tunnel** (`/v1/hosts/<id>/tunnel`) is opened by `websockets`,
  which is pinned `<15` in `pyproject.toml` (>=15 hangs on macOS). Proxy support
  landed in 15, so the client dials directly and loops on
  `Connect call failed ('172.18.0.1', 6868)`.

The proxy itself is willing — an absolute-URI upgrade request through it answers
`101 Switching Protocols`. So the relay listens on loopback inside the sandbox
(exempt from both the proxy and the egress policy), rewrites each request line to
absolute-URI form, and forwards it to the proxy. The host then targets
`http://127.0.0.1:<port>` and is unaware of any of it.

Two ways to remove the relay later: lift the `websockets<15` cap and pass
`proxy=`, or run a gateway with transparent TCP (not in 0.0.116 — the upstream
example requires a gateway built from that branch).

OpenShell reaps an exec's process tree when the RPC returns, so the relay has to
be held open by a long-lived exec; `up.sh` backgrounds one for the session.

## Why the server binds `0.0.0.0`

The sandbox dials this machine, so a loopback-only server is unreachable. Omnigent
reads a non-loopback bind as "exposed" and auto-enables accounts (login) mode,
which answers the sandbox with 401 — so `up.sh` pins
`OMNIGENT_AUTH_ENABLED=0` / `OMNIGENT_LOCAL_SINGLE_USER=1`.

That serves an **unauthenticated** API on every interface. Fine on a laptop,
wrong on a shared network. `up.sh` also keeps its own `--port 6868` and data dir
so it never disturbs a default server on 6767; `policy.yaml` allows both ports.

## Credentials

A fresh sandbox has no model credentials. Name the variables to copy in before
running `up.sh`:

```bash
export CLAUDE_CODE_OAUTH_TOKEN=...   # from `claude setup-token` on this machine
export OMNIGENT_OPENSHELL_SANDBOX_ENV=CLAUDE_CODE_OAUTH_TOKEN
```

A listed variable that is not set fails the launch. The runner subprocess does
not inherit the sandbox's proxy variables, and `opencode serve` is launched with
a filtered environment that passes the proxy vars but **not** the CA ones — so
name both:

```
OMNIGENT_RUNNER_ENV_PASSTHROUGH=https_proxy,http_proxy,HTTPS_PROXY,HTTP_PROXY,NO_PROXY,no_proxy,NODE_EXTRA_CA_CERTS,SSL_CERT_FILE,CURL_CA_BUNDLE,REQUESTS_CA_BUNDLE
```

Without the CA half, OpenShell's proxy terminates TLS with an ephemeral CA that
the harness has no reason to trust, and opencode fails with `self signed
certificate in certificate chain`. OpenShell exports the trust bundle itself
(`/etc/openshell-tls/`), so only the forwarding is missing.

opencode picks its own default model over the merged model map when nothing
pins one, which can land on an image model that rejects chat messages
(`Input should be 'user': input.messages.0.role`). opencode-native adopts the
top-level `model` from the user's `~/.config/opencode/opencode.json` — note
that path specifically, not `~/.opencode/` — so pin one there and copy it into
the sandbox alongside the credentials:

```json
{ "model": "<provider>/<model>" }
```

Register the host under a **stable label** (`omnigent sandbox connect
--host-name <label>`). The hosts table is keyed on (owner, name), so a fixed
label reuses one row; without it the host is named after the sandbox's
container hostname and every rebuild mints a new identity, leaving the previous
one behind as an offline entry in the UI's host picker.

Harnesses that authenticate from **files** rather than env vars (claude-native
on a subscription, opencode, pi) need those files copied into the sandbox
instead — `~/.claude/.credentials.json`, `~/.local/share/opencode/auth.json`,
`~/.pi/agent/`. They are live credentials inside a container running agent
code, so delete the sandbox when you are done with it.

`policy.yaml` allows the provider hosts those harnesses use — Anthropic, Z.AI,
the Qwen token plan, and opencode's catalog. Egress is denied by default, so a
new provider needs a row; verify one with

```bash
openshell sandbox exec -n <sandbox> -- curl -s -o /dev/null -w '%{http_code}\n' https://<host>/
```

A blocked host returns `000`.

`pi` reports `needs-auth` until a pi provider is configured in Omnigent itself:
its own `~/.pi/agent/auth.json` is not enough, because pi-native routes through
the Omnigent provider config. A subscription entry says "use Pi's own native
auth", so no key enters the config:

```yaml
providers:
  pi:
    cli: pi
    default: [pi]
    kind: subscription
```

`default: [pi]` claims only the pi surface, so it does not collide with a
`default: true` provider serving the anthropic family. The sandbox needs the
same `providers:` block as the host — the launcher merges it in.

## Compute driver

The gateway's driver is an OpenShell-side choice; nothing here depends on it.
Auto-detection order is Kubernetes → Podman → Docker, and this loop was verified
on **Docker**. Pin it with `OPENSHELL_DRIVERS=docker` in
`${XDG_CONFIG_HOME:-~/.config}/openshell/gateway.env`, then
`systemctl --user restart openshell-gateway`.

BoxLite is **not** an OpenShell compute driver — the upstream proposals
(NVIDIA/OpenShell#421, #423, #424) were all closed unmerged. Omnigent talks to
BoxLite directly instead, via `--provider boxlite`, which needs no gateway.
MicroVMs under OpenShell mean its `vm` driver, still broken upstream
(NVIDIA/OpenShell#2940, open).
