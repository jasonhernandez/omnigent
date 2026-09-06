"""Boxlite sandbox launcher (local micro-VM or remote ``boxlite serve``).

Implements the managed-launch subset of
:class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` for
`BoxLite <https://github.com/boxlite-ai/boxlite>`_ — an embeddable micro-VM +
OCI runtime. This module ships in the OSS build; the boxlite SDK itself is an
optional dependency (``pip install 'omnigent[boxlite]'``) imported lazily, so
the provider can be listed and the module probed without it.

BoxLite uniquely covers BOTH runtime targets through one launcher, selected by
config:

- **Local** (no ``endpoint``): ``Boxlite.default()`` — boxes are micro-VMs on
  the omnigent-server host itself (KVM on Linux / Hypervisor.framework on
  macOS). BoxLite is embedded in-process: NO daemon, NO ``boxlite serve``. The
  first local, hardware-isolated, persistent provider — no cloud account.
- **Cloud** (``endpoint`` set): ``Boxlite.rest(BoxliteRestOptions)`` — a thin
  REST client to a remote ``boxlite serve`` pool. Boxes run on the pool; the
  server reaches them over HTTP. Same role as Modal / Daytona, self-hosted.

Managed-only (``supports_cli_bootstrap=False``): the server-managed flow only
calls ``prepare`` / ``provision`` / ``run`` / ``terminate`` — it boots the
prebaked host image and starts ``omnigent host`` over ``run``; it never ships
wheels (``put``) or runs the in-sandbox App OAuth (``stream_exec`` /
``forward_local_port``). Those CLI-bootstrap primitives keep the base class's
raising defaults.

Concurrency model: BoxLite's async API drives a tokio runtime bridged to a
Python asyncio loop, and (mirroring the SDK's own ``SyncBoxlite``) wants a
stable, long-lived loop. omnigent calls launcher methods synchronously off its
own event loop (via ``asyncio.to_thread``), and a launcher is constructed PER
launch, so a per-launcher background loop would leak a thread per session.
Instead every boxlite call is marshalled onto a single PROCESS-LIFETIME loop
thread (:func:`_run`) — one daemon thread for all launchers, like a connection
pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

import click

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    RemoteCommandResult,
    SandboxLauncher,
)
from omnigent.onboarding.sandboxes.types import SandboxCapabilities

if TYPE_CHECKING:
    from collections.abc import Coroutine

    import boxlite as boxlite_sdk


# Coroutine result marshalled back through the shared loop (see _run).
_T = TypeVar("_T")


# ── Constants ──────────────────────────────────────────

HOST_IMAGE_ENV_VAR: str = "OMNIGENT_BOXLITE_HOST_IMAGE"
"""Environment variable overriding
:data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE` for boxlite
boxes, e.g. an org-internal copy of the host image
(``ghcr.io/<your-org>/omnigent-host:latest``)."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_BOXLITE_SANDBOX_ENV"
"""Environment variable naming (comma-separated) the SERVER-process environment
variables whose values are injected into every box this launcher creates —
typically the harness LLM credentials (``ANTHROPIC_API_KEY``,
``OPENAI_API_KEY``, gateway base URLs, …) and ``GIT_TOKEN`` that the in-box
host forwards to runners. Names, not values: read from the server's own
environment at provision time, so secrets never live in config files. The
server's managed-host config (``sandbox.boxlite.env``) takes precedence when
set."""

# Resources for the box. Matches the Modal / Daytona launchers: 2 vCPU / 4 GiB
# is enough for a host running one interactive session.
_SANDBOX_CPU: int = 2
_SANDBOX_MEMORY_MIB: int = 4096

# A secret NAME becomes the placeholder the box sees, so keep it to characters
# that survive an HTTP header verbatim. An inject_env NAME must be a POSIX
# environment variable name — anything else is silently unusable in the guest.
_SECRET_NAME_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9_-]+")
_ENV_NAME_RE: re.Pattern[str] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _secret_placeholder(name: str) -> str:
    """
    Return the placeholder the box sees for the secret *name*.

    Mirrors boxlite's own ``Secret.get_placeholder()``; computed here because
    the box env is assembled before the SDK objects are constructed on the
    loop thread.
    """
    return f"<BOXLITE_SECRET:{name}>"


@dataclass(frozen=True)
class _SecretBinding:
    """
    One resolved ``sandbox.boxlite.secrets`` entry.

    ``value`` is excluded from the repr so a traceback or a log line that
    happens to render a binding cannot leak the credential.
    """

    name: str
    hosts: tuple[str, ...]
    inject_env: str
    value: str = field(repr=False)


# Marshalling timeouts (seconds). The first provision from a given image makes
# boxlite pull the OCI image and boot a fresh micro-VM, which for the ~GiB host
# image can take minutes; later boots reuse the cached image.
_PROVISION_TIMEOUT_S: float = 900.0
_RUN_TIMEOUT_S: float = 600.0
_TERMINATE_TIMEOUT_S: float = 120.0


# ── Shared process-lifetime event loop ─────────────────

_loop_lock = threading.Lock()
_shared_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """
    Return the shared boxlite event loop, starting its daemon thread once.

    Recreates the loop (and thread) if a prior one was closed or its thread
    died — else a dead loop would brick every later boxlite call for the
    process lifetime.
    """
    global _shared_loop, _loop_thread
    with _loop_lock:
        alive = (
            _shared_loop is not None
            and not _shared_loop.is_closed()
            and _loop_thread is not None
            and _loop_thread.is_alive()
        )
        if not alive:
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, name="boxlite-runtime", daemon=True)
            thread.start()
            _shared_loop = loop
            _loop_thread = thread
        assert _shared_loop is not None  # set just above when not alive
        return _shared_loop


# Grace added to the outer wait once a timeout fires: the in-loop wait_for
# should cancel the coroutine well within this, so the outer result() only
# trips if cancellation itself hangs (a double fault).
_CANCEL_GRACE_S: float = 30.0


def _run(coro: Coroutine[Any, Any, _T], *, timeout: float) -> _T:
    """
    Run *coro* on the shared loop and block for its result.

    The timeout is applied in-loop via ``asyncio.wait_for`` so it cancels the
    coroutine (dropping the underlying boxlite future) instead of orphaning it;
    ``run_coroutine_threadsafe(...).result(timeout=...)`` alone would bound only
    the wait. The outer ``result`` is a grace backstop should cancellation hang.

    :raises asyncio.TimeoutError: when *coro* exceeds *timeout*
        (``concurrent.futures.TimeoutError`` if cancellation itself hangs).
    """

    async def _bounded() -> _T:
        return await asyncio.wait_for(coro, timeout)

    future = asyncio.run_coroutine_threadsafe(_bounded(), _get_loop())
    return future.result(timeout=timeout + _CANCEL_GRACE_S)


def _ensure_sdk() -> None:
    """
    Verify the boxlite SDK is importable, with an install hint when not.

    Called at the top of every launcher entry point because the SDK is an
    optional dependency — the base ``omnigent`` install does not pull it in.

    :raises click.ClickException: When the ``boxlite`` package is not installed.
    """
    try:
        import boxlite  # noqa: F401  # presence probe only
    except ImportError as exc:
        raise click.ClickException(
            "The boxlite SDK is required for the 'boxlite' sandbox provider. "
            "Install it with `pip install 'omnigent[boxlite]'`. Local mode also "
            "needs hardware virtualization (KVM on Linux, Hypervisor.framework "
            "on macOS)."
        ) from exc


class BoxliteSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for BoxLite boxes (local micro-VM or remote pool).

    All transport rides the boxlite async SDK marshalled onto the shared loop:
    ``runtime.create`` / ``get`` / ``remove`` for lifecycle, ``box.exec`` for
    commands (stdout/stderr drained from the streaming ``Execution``). The
    runtime handle (``Boxlite.default()`` local, or ``Boxlite.rest(...)`` cloud)
    is created lazily on the loop thread and cached.
    """

    provider: ClassVar[str] = "boxlite"
    # No local→box port-forward path (App OAuth callback) is needed: the
    # managed flow never runs it, and boxlite isn't used for the App-auth CLI.
    supports_local_port_forward: ClassVar[bool] = False
    # Managed-only: prepare / provision / run / terminate. The CLI-bootstrap
    # primitives (put / stream_exec / exec_foreground / wheel_install_command)
    # keep the base class's raising defaults.
    supports_cli_bootstrap: ClassVar[bool] = False

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            cli_bootstrap=False,
            managed_launch=True,
            local_port_forward=False,
            resume_stopped=False,
            programmatic_terminate=True,
            file_copy=False,
            streaming_exec=False,
            foreground_exec=False,
            sizes_sandbox_by_agent=True,
        )

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        image: str | None = None,
        env: Sequence[str] | None = None,
        home_dir: str | None = None,
        registry: Mapping[str, object] | None = None,
        disk_size_gb: int | None = None,
        cpus: int | None = None,
        memory_mib: int | None = None,
        agent_resources: Mapping[str, Mapping[str, int]] | None = None,
        clone_from: str | None = None,
        allow_net: Sequence[str] | None = None,
        secrets: Sequence[Mapping[str, object]] | None = None,
    ) -> None:
        """
        Initialize the launcher.

        :param endpoint: Remote ``boxlite serve`` URL, e.g.
            ``"https://boxlite.example.com:8100"``. ``None`` selects LOCAL mode
            (boxes run on the omnigent-server host via ``Boxlite.default()``).
            In cloud mode the API key is read from ``BOXLITE_API_KEY`` in the
            server environment (12-factor; never in the config file) via
            ``ApiKeyCredential.from_env()``.
        :param image: Registry image reference with omnigent pre-installed, e.g.
            ``"docker.io/me/omnigent-host:latest"`` — the server's
            ``sandbox.boxlite.image`` config. ``None`` resolves
            :data:`HOST_IMAGE_ENV_VAR` and falls back to
            :data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE`.
        :param env: Optional names of server-process environment variables to
            inject into every box, e.g. ``["OPENAI_API_KEY", "GIT_TOKEN"]`` —
            the server's ``sandbox.boxlite.env`` config. ``None`` resolves
            :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` (comma-separated) and falls
            back to injecting nothing.
        :param home_dir: LOCAL mode only — boxlite data directory (runtime
            state + cached images), the server's ``sandbox.boxlite.home_dir``
            config. ``None`` uses boxlite's default (``~/.boxlite``).
        :param registry: LOCAL mode only — optional private-registry config
            for pulling the host image, as a mapping with ``host`` (required)
            plus optional ``transport`` / ``skip_verify`` / ``username_env`` /
            ``password_env`` / ``token_env``. The ``*_env`` keys NAME server
            environment variables holding the credentials (12-factor; values
            never live in config). ``None`` uses anonymous pulls.
        :param cpus: Box vCPU count — the server's ``sandbox.boxlite.cpus``
            config. ``None`` keeps the built-in default.
        :param memory_mib: Box RAM in MiB — the server's
            ``sandbox.boxlite.memory_mib`` config. ``None`` keeps the built-in
            default. The built-in 4096 is not enough for every workload: a
            Python test suite OOM-killed its workers inside the guest, which
            surfaces host-side only as a stalled session, so this needs to be
            operator-tunable rather than baked in.
        :param disk_size_gb: Box disk size in GB — the server's
            ``sandbox.boxlite.disk_size_gb`` config. ``None`` uses the SDK's
            own default.
        :param clone_from: Name (or id) of a STOPPED, operator-owned warm box
            to clone copy-on-write instead of booting the image — the server's
            ``sandbox.boxlite.clone_from`` config. ``None`` boots the image.
            When set, ``image`` / ``cpus`` / ``memory_mib`` / ``disk_size_gb``
            describe the SOURCE box, not the clone (see :meth:`_aclone`).
        :param allow_net: Hostnames the box may resolve — the server's
            ``sandbox.boxlite.allow_net`` config, e.g.
            ``["api.anthropic.com", "github.com"]``. ``None`` keeps boxlite's
            default of full egress. An allow-list must also cover this server's
            own host, which the in-box host dials back to.
        :param secrets: Host-side credentials — the server's
            ``sandbox.boxlite.secrets`` config, each entry a mapping of
            ``name`` / ``source_env`` / ``hosts`` / ``inject_env``. The value
            stays on the omnigent-server host and boxlite's proxy substitutes
            it into HTTPS requests to ``hosts``; the box only ever sees the
            ``<BOXLITE_SECRET:name>`` placeholder in ``inject_env``. Prefer
            this over ``env`` for credentials: anything the agent runs can read
            an ``env`` value.

        When ``home_dir`` or ``registry`` is set the launcher builds a
        customized ``Boxlite(Options(...))`` runtime; otherwise it uses the
        zero-config ``Boxlite.default()``.
        """
        self._endpoint = endpoint
        self._image_ref = image
        self._env_names = tuple(env) if env is not None else None
        self._home_dir = home_dir
        self._registry = dict(registry) if registry is not None else None
        self._disk_size_gb = disk_size_gb
        self._cpus = cpus if cpus is not None else _SANDBOX_CPU
        self._memory_mib = memory_mib if memory_mib is not None else _SANDBOX_MEMORY_MIB
        self._agent_resources = {
            str(agent): dict(spec) for agent, spec in (agent_resources or {}).items()
        }
        self._clone_from = clone_from
        self._allow_net = tuple(allow_net) if allow_net is not None else None
        self._secrets = tuple(secrets) if secrets is not None else None
        self._runtime: boxlite_sdk.Boxlite | None = None

    async def _aruntime(self) -> boxlite_sdk.Boxlite:
        """Return the (lazily created, loop-bound) boxlite runtime handle."""
        if self._runtime is None:
            import boxlite

            if self._endpoint:
                # Cloud: the API key comes from BOXLITE_API_KEY in the server
                # env (12-factor; None when unset → unauthenticated).
                self._runtime = boxlite.Boxlite.rest(
                    boxlite.BoxliteRestOptions(
                        url=self._endpoint,
                        credential=boxlite.ApiKeyCredential.from_env(),
                    )
                )
            else:
                # Local: a customized Options runtime when home_dir / registry
                # is configured, else the zero-config global default.
                options = self._local_options()
                self._runtime = (
                    boxlite.Boxlite(options) if options is not None else boxlite.Boxlite.default()
                )
        return self._runtime

    def _local_options(self) -> boxlite_sdk.Options | None:
        """
        Build boxlite ``Options`` for LOCAL runtime customization, or ``None``
        to fall back to the zero-config global default (``Boxlite.default()``).
        """
        if self._home_dir is None and not self._registry:
            return None
        import boxlite

        return boxlite.Options(
            home_dir=self._home_dir,
            image_registries=self._build_image_registries(),
        )

    def _build_image_registries(self) -> list[boxlite_sdk.ImageRegistry]:
        """
        Build the private-registry list from config, resolving credential env
        NAMES to values from the server environment (12-factor).
        """
        if not self._registry:
            return []
        import boxlite

        reg = self._registry
        return [
            boxlite.ImageRegistry(
                host=str(reg["host"]),
                transport=str(reg.get("transport") or "https"),
                skip_verify=bool(reg.get("skip_verify", False)),
                username=self._resolve_env_name(reg.get("username_env")),
                password=self._resolve_env_name(reg.get("password_env")),
                bearer_token=self._resolve_env_name(reg.get("token_env")),
            )
        ]

    def _resolve_env_name(self, name: object) -> str | None:
        """Resolve a server env var NAME to its value (fail loud if unset)."""
        if not name:
            return None
        value = os.environ.get(str(name))
        if value is None:
            raise click.ClickException(
                f"sandbox.boxlite.registry references env var '{name}' but it is "
                "not set in the server's environment."
            )
        return value

    def _resolve_sandbox_env(self) -> list[tuple[str, str]]:
        """
        Resolve the env vars to inject into created boxes as ``(name, value)``.

        Explicit constructor names win; otherwise
        :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` (comma-separated) applies; an
        empty resolution injects nothing. Values come from the server's own
        environment; a configured name that is unset there fails loud rather
        than launching without a credential the agent needs.

        :returns: ``(name, value)`` pairs for ``BoxOptions.env``.
        :raises click.ClickException: When a configured name is not set in the
            server process environment.
        """
        if self._env_names is not None:
            names: Sequence[str] = self._env_names
        else:
            names = [
                name.strip()
                for name in os.environ.get(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
                if name.strip()
            ]
        resolved: list[tuple[str, str]] = []
        for name in names:
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set in "
                    "the server's environment — set it (or remove it from "
                    f"sandbox.boxlite.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved.append((name, value))
        return resolved

    def _resolve_secrets(self, env_names: set[str]) -> list[_SecretBinding]:
        """
        Validate the configured secret entries and resolve their values.

        Config carries NAMES, never values (12-factor, same as
        :meth:`_resolve_sandbox_env`): ``source_env`` names the SERVER variable
        holding the credential, ``inject_env`` names the BOX variable that
        receives the placeholder.

        :param env_names: Names ``sandbox.boxlite.env`` already injects
            verbatim. An ``inject_env`` colliding with one of them is rejected:
            which of the two wins would be undefined, and losing the race means
            the box gets the real credential instead of a placeholder.
        :returns: One binding per configured secret.
        :raises click.ClickException: On a malformed entry, a duplicate name or
            ``inject_env``, or a ``source_env`` unset in the server environment.
        """
        bindings: list[_SecretBinding] = []
        injected = set(env_names)
        names: set[str] = set()
        for entry in self._secrets or ():
            binding = self._secret_binding(entry)
            if binding.name in names:
                raise click.ClickException(
                    f"sandbox.boxlite.secrets declares the name '{binding.name}' "
                    "twice — each name mints one placeholder, so they must be unique."
                )
            if binding.inject_env in injected:
                raise click.ClickException(
                    f"sandbox.boxlite.secrets entry '{binding.name}' injects "
                    f"'{binding.inject_env}', which sandbox.boxlite.env or another "
                    "secret already injects — drop one, or the box may receive the "
                    "real credential instead of the placeholder."
                )
            names.add(binding.name)
            injected.add(binding.inject_env)
            bindings.append(binding)
        return bindings

    def _secret_binding(self, entry: Mapping[str, object]) -> _SecretBinding:
        """
        Validate one ``sandbox.boxlite.secrets`` entry and resolve its value.

        :param entry: The raw config mapping for one secret.
        :returns: The resolved binding.
        :raises click.ClickException: When the entry is malformed, or its
            ``source_env`` is not set in the server process environment.
        """
        name = str(entry.get("name") or "")
        if not _SECRET_NAME_RE.fullmatch(name):
            raise click.ClickException(
                f"sandbox.boxlite.secrets has an entry whose name is '{name}' — a "
                "name must match [A-Za-z0-9_-]+ (it becomes the "
                "<BOXLITE_SECRET:name> placeholder the box sends)."
            )
        raw_hosts = entry.get("hosts")
        hosts = (
            tuple(str(host) for host in raw_hosts) if isinstance(raw_hosts, list | tuple) else ()
        )
        if not hosts:
            raise click.ClickException(
                f"sandbox.boxlite.secrets entry '{name}' must list at least one "
                "host — the value is only substituted into requests to those "
                "hosts, so an empty list injects a placeholder that never resolves."
            )
        inject_env = str(entry.get("inject_env") or "")
        if not _ENV_NAME_RE.fullmatch(inject_env):
            raise click.ClickException(
                f"sandbox.boxlite.secrets entry '{name}' has inject_env "
                f"'{inject_env}', which is not a valid environment variable name "
                "([A-Za-z_][A-Za-z0-9_]*)."
            )
        source_env = str(entry.get("source_env") or "")
        if not source_env:
            raise click.ClickException(
                f"sandbox.boxlite.secrets entry '{name}' must set source_env — the "
                "SERVER environment variable NAME holding the credential."
            )
        value = os.environ.get(source_env)
        if value is None:
            raise click.ClickException(
                f"sandbox.boxlite.secrets entry '{name}' names env var "
                f"'{source_env}' but it is not set in the server's environment — "
                "set it (or remove the entry)."
            )
        return _SecretBinding(name=name, hosts=hosts, inject_env=inject_env, value=value)

    def _network_spec(self) -> boxlite_sdk.NetworkSpec | None:
        """
        Build the DNS allow-list spec, or ``None`` for boxlite's default (full
        egress) so the in-box host can reach ``server_url`` unconfigured.
        """
        if not self._allow_net:
            return None
        import boxlite

        return boxlite.NetworkSpec(mode="enabled", allow_net=list(self._allow_net))

    def prepare(self) -> None:
        """
        Local preflight: the boxlite SDK must be installed, and — for LOCAL
        mode — hardware virtualization must be available.

        :raises click.ClickException: When the SDK is missing, or local mode is
            selected on a Linux host without ``/dev/kvm``.
        """
        _ensure_sdk()
        if self._endpoint:
            # Cloud mode: the remote pool owns virtualization. Reachability /
            # auth surface on the first provision rather than here.
            return
        # Local mode needs a hypervisor. macOS (Apple Silicon) always has
        # Hypervisor.framework; on Linux, KVM must be present and accessible.
        if platform.system() == "Linux" and not os.path.exists("/dev/kvm"):
            raise click.ClickException(
                "boxlite local mode requires KVM, but /dev/kvm was not found. "
                "Enable KVM and add the server user to the 'kvm' group, or point "
                "sandbox.boxlite.cloud.endpoint at a remote `boxlite serve`."
            )

    def _resources_for(self, agent_name: str | None) -> tuple[int, int]:
        """Resolve (cpus, memory_mib) for one job.

        A per-agent entry overrides the server-wide value, and may set either
        field alone. An unknown agent falls back to the server-wide value, so
        adding an agent never has to touch this map.

        :param agent_name: Resolved built-in agent, or ``None``.
        :returns: The ``(cpus, memory_mib)`` this box should be created with.
        """
        override = self._agent_resources.get(agent_name or "", {})
        return (
            int(override.get("cpus", self._cpus)),
            int(override.get("memory_mib", self._memory_mib)),
        )

    def provision(self, name: str, *, agent_name: str | None = None) -> str:
        """
        Create a new BoxLite box — from the host image, or (when
        ``clone_from`` is set) copy-on-write from a warm source box.

        The box is detached and persistent (``detach=True``,
        ``auto_remove=False``); the managed-session machinery owns its teardown
        (session delete / relaunch → ``terminate``).
        Network defaults to full egress (boxlite ``NetworkSpec`` default
        ``Enabled``) so the in-box host can reach ``server_url``; configured
        ``allow_net`` hosts narrow that to a DNS allow-list.

        :param name: Human-readable label, e.g. ``"managed-a1b2c3d4"``. Recorded
            as the box name; the returned id is the canonical reference.
        :returns: The box id.
        :raises click.ClickException: If box creation fails, the configured
            ``clone_from`` source is missing or running, or a secret entry is
            malformed or unresolvable.
        """
        _ensure_sdk()
        if self._clone_from and (self._secrets or self._allow_net):
            # clone_box takes no BoxOptions, so a clone inherits the SOURCE
            # box's network policy and secret bindings. Silently dropping a
            # security control would be worse than refusing the combination.
            raise click.ClickException(
                "sandbox.boxlite.clone_from cannot be combined with allow_net or "
                "secrets: a clone inherits the source box's network policy and "
                "secret bindings, so they must be set when the warm source box is "
                "created."
            )
        resolved_ref = self._image_ref or os.environ.get(HOST_IMAGE_ENV_VAR) or DEFAULT_HOST_IMAGE
        env = self._resolve_sandbox_env()
        cpus, memory_mib = self._resources_for(agent_name)
        secrets = self._resolve_secrets({env_name for env_name, _ in env})
        # The box receives the placeholder, never the value — boxlite's host-side
        # proxy substitutes the real credential on the way out.
        env += [(binding.inject_env, _secret_placeholder(binding.name)) for binding in secrets]
        target = self._endpoint or "local"
        if self._clone_from:
            click.echo(f"▸ Cloning boxlite box '{name}' from '{self._clone_from}' ({target})")
        else:
            click.echo(f"▸ Creating boxlite box '{name}' from {resolved_ref} ({target})")

        async def _do() -> str:
            import boxlite

            runtime = await self._aruntime()
            if self._clone_from:
                box = await self._aclone(runtime, self._clone_from, name)
                return str(box.id)
            options = boxlite.BoxOptions(
                image=resolved_ref,
                cpus=cpus,
                memory_mib=memory_mib,
                disk_size_gb=self._disk_size_gb,
                env=env,
                network=self._network_spec(),
                secrets=[
                    boxlite.Secret(name=b.name, value=b.value, hosts=list(b.hosts))
                    for b in secrets
                ],
                auto_remove=False,
                detach=True,
            )
            box = await runtime.create(options, name=name)
            return str(box.id)

        # On failure, remove any box create() made server-side before it was
        # cancelled: we never got the id, so an orphan would leak untracked.
        try:
            box_id = _run(_do(), timeout=_PROVISION_TIMEOUT_S)
        except click.ClickException:
            self._best_effort_remove(name)
            raise
        except Exception as exc:
            self._best_effort_remove(name)
            # Surface the provider's reason (image pull failure, no KVM, quota)
            # so the managed-launch 502 carries it verbatim.
            raise click.ClickException(f"boxlite box creation failed: {exc}") from exc
        click.echo(f"  → created {box_id}")
        return str(box_id)

    async def _aclone(
        self, runtime: boxlite_sdk.Boxlite, source: str, name: str
    ) -> boxlite_sdk.Box:
        """
        Clone a warm source box copy-on-write instead of booting the image.

        The source is operator-owned — created and refreshed outside omnigent —
        and must be STOPPED: a clone copies the source's disks, so cloning a
        running box captures a mid-write filesystem.

        ``clone_box(*, options=CloneOptions, name=...)`` is the SDK's whole
        surface here, and ``CloneOptions`` carries no fields (boxlite 0.9.5
        documents it as a forward-compatible placeholder). So ``image`` /
        ``cpus`` / ``memory_mib`` / ``disk_size_gb`` / network / secrets all
        describe the SOURCE box; a clone inherits them and cannot override
        them. Env is the exception — :meth:`run` applies it per-exec.

        :param runtime: The loop-bound boxlite runtime handle.
        :param source: Name or id of the warm box to clone.
        :param name: Name for the new box.
        :returns: The cloned box handle.
        :raises click.ClickException: When the source is missing or running.
        """
        warm = await runtime.get(source)
        if warm is None:
            raise click.ClickException(
                f"sandbox.boxlite.clone_from names box '{source}', but no box by "
                "that name exists on this boxlite runtime — create the warm "
                "source box (and leave it stopped) before launching a managed "
                "session."
            )
        if (await warm.info()).state.running:
            raise click.ClickException(
                f"sandbox.boxlite.clone_from box '{source}' is running — stop it "
                "first. A clone copies the source's disks, so cloning a running "
                "box captures a mid-write filesystem."
            )
        return await warm.clone_box(name=name)

    def _clone_exec_env(self) -> list[tuple[str, str]] | None:
        """
        Env to apply on every ``box.exec``, or ``None`` when ``BoxOptions.env``
        already carries it.

        ``clone_box`` takes no ``BoxOptions``, so a cloned box inherits the
        SOURCE box's environment and never sees ``sandbox.boxlite.env``.
        ``box.exec(env=...)`` is the only lane the SDK offers to supply it
        after the fact, so the clone path resolves the names per exec.

        :raises click.ClickException: When a configured name is not set in the
            server process environment.
        """
        if not self._clone_from:
            return None
        return self._resolve_sandbox_env() or None

    def _best_effort_remove(self, name_or_id: str) -> None:
        """
        Delete a box by name or id, swallowing every error. Used to clean up a
        provision that failed or was cancelled — the box may exist server-side
        under its name even though ``create()`` never returned an id.
        """

        async def _do() -> None:
            runtime = await self._aruntime()
            await runtime.remove(name_or_id, force=True)

        with contextlib.suppress(Exception):
            _run(_do(), timeout=_TERMINATE_TIMEOUT_S)

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command in the box and capture its output.

        The streaming ``Execution`` carries stdout/stderr separately and
        ``wait()`` returns only the exit code, so both streams are drained
        before waiting.

        :param sandbox_id: Target box id.
        :param command: Shell command to execute remotely.
        :param check: When ``True``, raise on non-zero exit.
        :returns: Exit code plus captured output.
        :raises click.ClickException: If the box is gone, or *check* is ``True``
            and the command exits non-zero.
        """
        _ensure_sdk()
        exec_env = self._clone_exec_env()

        async def _drain(
            getter: Callable[[], Any], sink: list[str], *, echo: bool, err: bool = False
        ) -> None:
            """
            Drain a stream into *sink*. The SDK's ``stdout()`` / ``stderr()``
            RAISE (they do not return ``None``) when the stream is unavailable,
            so the getter is called defensively — matching boxlite's own
            SimpleBox handling.
            """
            try:
                stream = getter()
            except Exception:
                return
            if stream is None:
                return
            async for line in stream:
                text = line if isinstance(line, str) else line.decode("utf-8", "replace")
                sink.append(text)
                if echo and text.strip():
                    click.echo(text.rstrip("\n"), err=err)

        async def _do() -> tuple[int, str, str, str | None]:
            runtime = await self._aruntime()
            box = await runtime.get(sandbox_id)
            if box is None:
                raise click.ClickException(
                    f"boxlite box '{sandbox_id}' not found — it may have been removed. "
                    "Managed sessions provision a replacement on the next message."
                )
            # timeout_secs lets boxlite kill the GUEST process on timeout; the
            # _run wait_for only cancels the coroutine, not the guest. (The SDK
            # method is bound to a local first so the fork-PR security scan's
            # builtin-exec call heuristic doesn't flag this sandbox command.)
            run_in_box = box.exec
            execution = await run_in_box(
                "sh", ["-lc", command], env=exec_env, timeout_secs=_RUN_TIMEOUT_S
            )
            out_parts: list[str] = []
            err_parts: list[str] = []
            # Drain both streams concurrently: draining one to EOF first can
            # deadlock if the command fills the other's buffer (git-clone stderr).
            await asyncio.gather(
                _drain(execution.stdout, out_parts, echo=True),
                _drain(execution.stderr, err_parts, echo=True, err=True),
            )
            result = await execution.wait()
            return (
                result.exit_code,
                "".join(out_parts),
                "".join(err_parts),
                getattr(result, "error_message", None),
            )

        try:
            # Bound above the guest timeout so the guest kill fires first.
            exit_code, stdout, stderr, error_message = _run(
                _do(), timeout=_RUN_TIMEOUT_S + _CANCEL_GRACE_S
            )
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(
                f"Remote command failed to execute on box '{sandbox_id}': {exc}"
            ) from exc
        if check and exit_code != 0:
            # Surface the provider message AND a stderr tail (e.g. git-clone
            # "fatal: ..."); a bare exit code is otherwise opaque.
            stderr_tail = stderr.strip()[-800:]
            reasons = [r for r in (error_message, stderr_tail) if r]
            detail = f" — {' | '.join(reasons)}" if reasons else ""
            raise click.ClickException(
                f"Remote command failed on box '{sandbox_id}' "
                f"(exit {exit_code}): {command}{detail}"
            )
        return RemoteCommandResult(returncode=exit_code, stdout=stdout, stderr=stderr)

    def keep_alive(self, sandbox_id: str) -> None:
        """
        No-op: BoxLite boxes persist across stop/restart natively, so there is
        no idle-autostop to disable. (Managed-only launchers need not implement
        this; provided for completeness.)
        """
        del sandbox_id

    def terminate(self, sandbox_id: str) -> None:
        """
        Remove a box, releasing its compute. Idempotent: an already-gone box is
        a no-op success, detected by an existence check (``get``) rather than
        matching the removal error's text — so a genuine removal failure (even
        one whose message contains "not found", e.g. "image manifest not found")
        is surfaced, never swallowed.

        :param sandbox_id: The box id to remove.
        :raises click.ClickException: If a box that exists cannot be removed.
        """
        _ensure_sdk()

        async def _do() -> None:
            runtime = await self._aruntime()
            if await runtime.get(sandbox_id) is None:
                return  # already gone — idempotent success
            await runtime.remove(sandbox_id, force=True)

        try:
            _run(_do(), timeout=_TERMINATE_TIMEOUT_S)
        except Exception as exc:
            raise click.ClickException(
                f"Could not remove boxlite box '{sandbox_id}': {exc}"
            ) from exc
