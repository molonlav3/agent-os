"""Resolve ``[gateway] bind`` / ``--bind-mode`` into the addresses to listen on.

==========  ===========================================================
mode        listens on
==========  ===========================================================
loopback    ``127.0.0.1``
lan         ``0.0.0.0`` -- every interface; the auth guard requires a token
tailnet     the Tailscale IPv4 (``tailscale ip -4``) **and** ``127.0.0.1``
custom      the configured ``host`` (what ``--listen``/``--bind`` set)
==========  ===========================================================

``tailnet`` never falls back to a wildcard: when Tailscale is not installed,
not logged in, or reports no IPv4, startup fails with the reason instead of
quietly exposing the gateway on every interface.
"""

from __future__ import annotations

import ipaddress
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from agentos.gateway.access import is_loopback_bind

BIND_MODES: tuple[str, ...] = ("loopback", "lan", "tailnet", "custom")

LOOPBACK_HOST = "127.0.0.1"
LAN_HOST = "0.0.0.0"

#: ``runner(argv) -> (returncode, stdout, stderr)``; injectable for tests.
CommandRunner = Callable[[Sequence[str]], tuple[int, str, str]]

_TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")


class BindResolutionError(ValueError):
    """The requested bind mode cannot be satisfied on this machine."""


@dataclass(frozen=True, slots=True)
class BindResolution:
    mode: str
    host: str
    #: Further addresses served by the same app (``tailnet``'s loopback).
    extra_hosts: tuple[str, ...] = ()

    @property
    def hosts(self) -> tuple[str, ...]:
        return (self.host, *self.extra_hosts)


def _default_runner(argv: Sequence[str]) -> tuple[int, str, str]:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        list(argv),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def tailscale_ipv4(runner: CommandRunner | None = None) -> str:
    """Return this machine's tailnet IPv4, or raise :class:`BindResolutionError`."""
    if runner is None:
        if shutil.which("tailscale") is None:
            raise BindResolutionError(
                "bind mode 'tailnet' needs the tailscale CLI on PATH; install Tailscale "
                "and log in, or use --bind-mode lan / loopback"
            )
        runner = _default_runner
    try:
        code, stdout, stderr = runner(["tailscale", "ip", "-4"])
    except (OSError, subprocess.SubprocessError) as exc:
        raise BindResolutionError(f"bind mode 'tailnet': `tailscale ip -4` failed: {exc}") from exc
    if code != 0:
        detail = (stderr or stdout).strip() or f"exit status {code}"
        raise BindResolutionError(
            f"bind mode 'tailnet': `tailscale ip -4` failed ({detail}); is Tailscale "
            "running and logged in?"
        )
    for line in stdout.splitlines():
        candidate = line.strip()
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv4Address) and address in _TAILNET_V4:
            return candidate
    raise BindResolutionError(
        "bind mode 'tailnet': `tailscale ip -4` reported no tailnet IPv4 address"
    )


def normalize_bind_mode(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    if cleaned not in BIND_MODES:
        raise BindResolutionError(
            f"unknown bind mode {value!r}; expected one of: {', '.join(BIND_MODES)}"
        )
    return cleaned


def resolve_bind(
    mode: str | None,
    *,
    host: str | None,
    tailscale_runner: CommandRunner | None = None,
) -> BindResolution:
    """Turn a bind mode plus the configured ``host`` into listen addresses.

    ``mode=None`` is the legacy path: ``host`` decides and the mode is
    reported as ``loopback`` or ``custom`` accordingly.
    """
    normalized = normalize_bind_mode(mode)
    configured = (host or "").strip() or LOOPBACK_HOST
    if normalized is None:
        return BindResolution("loopback" if is_loopback_bind(configured) else "custom", configured)
    if normalized == "loopback":
        return BindResolution("loopback", LOOPBACK_HOST)
    if normalized == "lan":
        return BindResolution("lan", LAN_HOST)
    if normalized == "tailnet":
        return BindResolution(
            "tailnet", tailscale_ipv4(tailscale_runner), extra_hosts=(LOOPBACK_HOST,)
        )
    return BindResolution("custom", configured)


__all__ = [
    "BIND_MODES",
    "BindResolution",
    "BindResolutionError",
    "CommandRunner",
    "normalize_bind_mode",
    "resolve_bind",
    "tailscale_ipv4",
]
