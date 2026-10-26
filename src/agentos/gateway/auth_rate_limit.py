"""Failed-authentication rate limiting and the pre-auth socket budget.

The gateway admits a Control client on one bearer token. Without a limit on
failed attempts that token can be guessed at network speed from anywhere the
port is reachable (a LAN or tailnet bind). This module keeps two guards that
the HTTP middleware and the WebSocket handshake share:

* :class:`AuthRateLimiter` counts *wrong* credentials per
  ``{client, credential scope}``. ``max_failures`` failures inside
  ``window_seconds`` lock the key out for ``lockout_seconds``; a locked key is
  refused before its credential is even compared. A *missing* credential
  never consumes budget: a client that sends none is told to authenticate,
  and an operator whose console has not loaded its token yet must not lock
  themself out.

  A loopback peer (no ``Origin``) is never locked out when
  ``exempt_loopback`` is on (the default), so a stale local script cannot
  shut the operator out of their own machine; each of its failures is
  answered after a progressive delay instead (250 ms, doubling, capped at
  5 s). A browser attempt (one that carries ``Origin``) is keyed by that
  origin and is never exempt: a page in the operator's browser reaches the
  gateway from a loopback address too.

* :class:`PreauthBudget` caps the sockets one client address may hold open
  while it has not finished the handshake, so a peer cannot pin the
  gateway's pre-auth timers and memory with thousands of idle connections.

Both are in-memory and per process; they reset on restart.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agentos.gateway.access import is_loopback_address

#: The scope every gateway-token check counts against, HTTP and WS alike, so
#: an attacker cannot double the budget by alternating transports.
TOKEN_SCOPE = "gateway-token"

#: First progressive delay for a loopback peer, then doubled per failure.
LOOPBACK_DELAY_START_S = 0.25
#: Ceiling of the progressive delay.
LOOPBACK_DELAY_MAX_S = 5.0

#: Most sockets one client address may hold open before the handshake ends.
MAX_PREAUTH_SOCKETS_PER_IP = 128

#: Most keys the limiter tracks; the least recently used is dropped past it.
_MAX_TRACKED_KEYS = 10_000


@dataclass(frozen=True, slots=True)
class AuthRateDecision:
    """What to do with one authentication attempt.

    ``allowed`` is false while the key is locked out; ``retry_after_ms`` then
    says when to try again. ``delay_s`` is the pause to apply before
    answering a failure (loopback peers only).
    """

    allowed: bool
    retry_after_ms: int = 0
    delay_s: float = 0.0


_ALLOW = AuthRateDecision(allowed=True)


@dataclass(slots=True)
class _Entry:
    failures: list[float]
    locked_until: float = 0.0
    consecutive: int = 0


def _rate_limit_settings(config: Any) -> Any:
    auth = getattr(config, "auth", None)
    return getattr(auth, "rate_limit", None)


class AuthRateLimiter:
    """Per ``{client, scope}`` failed-credential counter with lockout.

    Reads ``config.auth.rate_limit`` on every call, so ``config.apply`` (or a
    test) changing it takes effect at once.
    """

    def __init__(
        self,
        config: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_tracked: int = _MAX_TRACKED_KEYS,
    ) -> None:
        self._config = config
        self._clock = clock
        self._max_tracked = max_tracked
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()

    # ── keys ─────────────────────────────────────────────────────────

    @staticmethod
    def key_for(client_ip: str | None, scope: str, *, origin: str | None = None) -> tuple[str, str]:
        if origin:
            return (f"origin:{origin.strip().lower()}", scope)
        return (f"ip:{(client_ip or 'unknown').strip().lower()}", scope)

    def _exempt(self, client_ip: str | None, origin: str | None) -> bool:
        settings = _rate_limit_settings(self._config)
        if settings is None or not bool(getattr(settings, "exempt_loopback", True)):
            return False
        return not origin and is_loopback_address(client_ip)

    def _enabled(self) -> bool:
        settings = _rate_limit_settings(self._config)
        return settings is not None and bool(getattr(settings, "enabled", True))

    def _settings(self) -> tuple[int, float, float]:
        settings = _rate_limit_settings(self._config)
        max_failures = max(1, int(getattr(settings, "max_failures", 10)))
        window = max(0.001, float(getattr(settings, "window_seconds", 60.0)))
        lockout = max(0.0, float(getattr(settings, "lockout_seconds", 300.0)))
        return max_failures, window, lockout

    # ── public API ───────────────────────────────────────────────────

    def check(
        self, client_ip: str | None, scope: str = TOKEN_SCOPE, *, origin: str | None = None
    ) -> AuthRateDecision:
        """Whether a presented credential may be evaluated at all."""
        if not self._enabled() or self._exempt(client_ip, origin):
            return _ALLOW
        entry = self._entries.get(self.key_for(client_ip, scope, origin=origin))
        if entry is None:
            return _ALLOW
        now = self._clock()
        if entry.locked_until > now:
            return AuthRateDecision(
                allowed=False, retry_after_ms=max(1, int((entry.locked_until - now) * 1000))
            )
        return _ALLOW

    def record_failure(
        self, client_ip: str | None, scope: str = TOKEN_SCOPE, *, origin: str | None = None
    ) -> AuthRateDecision:
        """Count one wrong credential and return the resulting decision."""
        if not self._enabled():
            return _ALLOW
        key = self.key_for(client_ip, scope, origin=origin)
        now = self._clock()
        max_failures, window, lockout = self._settings()
        entry = self._entries.get(key)
        if entry is None:
            entry = _Entry(failures=[])
            self._entries[key] = entry
        self._entries.move_to_end(key)
        entry.failures = [t for t in entry.failures if now - t < window]
        entry.failures.append(now)
        entry.consecutive += 1
        self._evict(now, window)

        if self._exempt(client_ip, origin):
            delay = min(
                LOOPBACK_DELAY_MAX_S,
                LOOPBACK_DELAY_START_S * (2 ** min(entry.consecutive - 1, 16)),
            )
            return AuthRateDecision(allowed=True, delay_s=delay)

        if len(entry.failures) >= max_failures and lockout > 0:
            entry.locked_until = now + lockout
            entry.failures.clear()
            return AuthRateDecision(allowed=False, retry_after_ms=int(lockout * 1000))
        return _ALLOW

    def record_success(
        self, client_ip: str | None, scope: str = TOKEN_SCOPE, *, origin: str | None = None
    ) -> None:
        """A correct credential clears the key's failure history."""
        if self._entries:
            self._entries.pop(self.key_for(client_ip, scope, origin=origin), None)

    def reset(self) -> None:
        self._entries.clear()

    # ── housekeeping ─────────────────────────────────────────────────

    def _evict(self, now: float, window: float) -> None:
        if len(self._entries) <= self._max_tracked:
            return
        stale = [
            key
            for key, entry in self._entries.items()
            if entry.locked_until <= now
            and (not entry.failures or now - entry.failures[-1] >= window)
        ]
        for key in stale:
            self._entries.pop(key, None)
        while len(self._entries) > self._max_tracked:
            self._entries.popitem(last=False)


class PreauthBudget:
    """Counts sockets per client address that have not finished the handshake."""

    def __init__(self, limit: int = MAX_PREAUTH_SOCKETS_PER_IP) -> None:
        self.limit = limit
        self._open: dict[str, int] = {}

    def try_acquire(self, client_ip: str | None) -> bool:
        key = (client_ip or "unknown").strip().lower()
        current = self._open.get(key, 0)
        if current >= self.limit:
            return False
        self._open[key] = current + 1
        return True

    def release(self, client_ip: str | None) -> None:
        key = (client_ip or "unknown").strip().lower()
        current = self._open.get(key, 0)
        if current <= 1:
            self._open.pop(key, None)
        else:
            self._open[key] = current - 1

    def pending(self, client_ip: str | None) -> int:
        return self._open.get((client_ip or "unknown").strip().lower(), 0)


@dataclass(slots=True)
class AuthGuard:
    """The two guards one gateway app shares between HTTP and WebSocket."""

    limiter: AuthRateLimiter
    preauth: PreauthBudget

    @classmethod
    def from_config(cls, config: Any) -> AuthGuard:
        return cls(limiter=AuthRateLimiter(config), preauth=PreauthBudget())


def retry_after_seconds(retry_after_ms: int) -> int:
    """``Retry-After`` header value (whole seconds, at least 1)."""
    return max(1, -(-int(retry_after_ms) // 1000))


__all__ = [
    "LOOPBACK_DELAY_MAX_S",
    "LOOPBACK_DELAY_START_S",
    "MAX_PREAUTH_SOCKETS_PER_IP",
    "TOKEN_SCOPE",
    "AuthGuard",
    "AuthRateDecision",
    "AuthRateLimiter",
    "PreauthBudget",
    "retry_after_seconds",
]
