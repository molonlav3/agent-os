"""Idempotency keys for side-effecting RPC methods.

A client that loses its connection in the middle of ``chat.send`` cannot tell
whether the message went through. With an ``idempotency_key`` it can simply
send again: the same key within :data:`~agentos.gateway.protocol.DEDUPE_TTL_MS`
returns the first answer instead of starting a second turn, and the same key
with a different body is refused as ``INVALID_REQUEST`` /
``details.code: "REQUEST_CONFLICT"`` rather than silently answered with the
other request's result. Concurrent duplicates share the first call.

The cache is per gateway process. ``chat.send`` / ``sessions.send`` also
record the key on the task they start, so a retry after a restart finds that
turn instead of running it again (see ``rpc_sessions``).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agentos.gateway.protocol import DEDUPE_MAX_ENTRIES, DEDUPE_TTL_MS, ERROR_INVALID_REQUEST
from agentos.gateway.rpc import RpcHandlerError

#: Request fields that are not part of the request's meaning.
_KEY_FIELDS = frozenset({"idempotency_key", "idempotencyKey"})
_MAX_KEY_LENGTH = 256


def idempotency_key(params: Any) -> str | None:
    """The request's ``idempotency_key`` (either spelling), validated, or ``None``."""
    if not isinstance(params, dict):
        return None
    raw = params.get("idempotency_key", params.get("idempotencyKey"))
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("params.idempotency_key must be a non-empty string")
    key = raw.strip()
    if len(key) > _MAX_KEY_LENGTH:
        raise ValueError(f"params.idempotency_key is longer than {_MAX_KEY_LENGTH} characters")
    return key


def request_fingerprint(params: Any) -> str:
    """A stable digest of what the request asks for (the key itself excluded)."""
    body = (
        {key: value for key, value in params.items() if key not in _KEY_FIELDS}
        if isinstance(params, dict)
        else params
    )
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def request_conflict(key: str) -> RpcHandlerError:
    return RpcHandlerError(
        ERROR_INVALID_REQUEST,
        "This idempotency_key was already used for a different request.",
        details={"code": "REQUEST_CONFLICT", "idempotency_key": key},
    )


@dataclass
class _Entry:
    fingerprint: str
    expires_at: float
    done: asyncio.Future[dict[str, Any]]
    #: True while the side effect the answer names is still live (a running
    #: turn): such an entry outlives the TTL and the size cap.
    pinned: Callable[[dict[str, Any]], bool] | None = None
    result: dict[str, Any] | None = field(default=None)


class IdempotencyCache:
    """Answers by ``(method, idempotency_key)`` for a TTL, bounded in size."""

    def __init__(
        self,
        *,
        ttl_ms: int = DEDUPE_TTL_MS,
        max_entries: int = DEDUPE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_ms / 1000.0
        self._max_entries = max(1, int(max_entries))
        self._clock = clock
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def _is_pinned(self, entry: _Entry) -> bool:
        if not entry.done.done():
            return True
        if entry.pinned is None or entry.result is None:
            return False
        try:
            return bool(entry.pinned(entry.result))
        except Exception:
            return False

    def _prune(self) -> None:
        now = self._clock()
        for cache_key in [k for k, e in self._entries.items() if e.expires_at <= now]:
            entry = self._entries[cache_key]
            if not self._is_pinned(entry):
                del self._entries[cache_key]
        if len(self._entries) <= self._max_entries:
            return
        for cache_key in list(self._entries):
            if len(self._entries) <= self._max_entries:
                break
            if not self._is_pinned(self._entries[cache_key]):
                del self._entries[cache_key]

    async def run(
        self,
        method: str,
        key: str,
        fingerprint: str,
        call: Callable[[], Awaitable[dict[str, Any]]],
        *,
        pinned: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        """Run ``call`` once per ``(method, key)``; replay its answer afterwards."""
        self._prune()
        cache_key = (method, key)
        entry = self._entries.get(cache_key)
        if entry is not None:
            if entry.fingerprint != fingerprint:
                raise request_conflict(key)
            result = await asyncio.shield(entry.done)
            return copy.deepcopy(result)

        loop = asyncio.get_running_loop()
        entry = _Entry(
            fingerprint=fingerprint,
            expires_at=self._clock() + self._ttl_s,
            done=loop.create_future(),
            pinned=pinned,
        )
        self._entries[cache_key] = entry
        self._prune()
        try:
            result = await call()
        except BaseException as exc:
            # A failed call is not remembered: the retry runs it again.
            self._entries.pop(cache_key, None)
            if not entry.done.done():
                entry.done.set_exception(exc)
                # Waiters re-raise it; nobody else will retrieve it.
                entry.done.exception()
            raise
        stored = copy.deepcopy(result)
        entry.result = stored
        entry.done.set_result(stored)
        return result


_cache = IdempotencyCache()


def get_idempotency_cache() -> IdempotencyCache:
    return _cache


async def run_idempotent(
    method: str,
    params: Any,
    call: Callable[[], Awaitable[dict[str, Any]]],
    *,
    pinned: Callable[[dict[str, Any]], bool] | None = None,
    cache: IdempotencyCache | None = None,
) -> dict[str, Any]:
    """``call()``, deduplicated by the request's ``idempotency_key`` when it has one."""
    key = idempotency_key(params)
    if key is None:
        return await call()
    return await (cache or _cache).run(
        method, key, request_fingerprint(params), call, pinned=pinned
    )
