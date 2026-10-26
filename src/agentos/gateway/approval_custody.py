"""Who may answer an approval from a chat channel.

A paired channel sender may resolve an approval only when the request's
session is delivered on that channel account, in that chat. Pairing admits a
sender to the agent; it does not make them the reviewer of every prompt the
gateway raises -- a Telegram contact must not be able to approve a command
the operator's desktop session queued.

"Delivered here" is established, for the requesting session and -- when it
is a subagent or coding-agent child -- each ancestor in turn, by either:

* the session key naming the channel and the chat:
  ``agent:<id>:<channel>:<direct|group|channel>:<peer>`` (also with an
  account segment before the kind); a DM is matched on the sender, a group or
  channel on the chat; or
* the session's recorded delivery route (``last_channel`` / ``last_to``):
  the shared ``agent:<id>:main`` DM session carries no channel in its key, so
  its route is what says which chat it talks to.

A key that names a different channel or chat ends the walk with a refusal.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import structlog

from agentos.gateway.session_index import SessionIndex, get_session_index
from agentos.session.keys import is_child_session_key

log = structlog.get_logger(__name__)

_PEER_KINDS = frozenset({"direct", "dm", "group", "channel"})
_SHARED_KINDS = frozenset({"group", "channel"})


@dataclass(frozen=True, slots=True)
class CustodyDecision:
    allowed: bool
    #: ``session-key`` | ``delivery-route`` | ``no-session`` | ``other-channel``
    #: | ``other-chat`` | ``not-delivered-here``
    reason: str
    session_key: str = ""


@dataclass(frozen=True, slots=True)
class ChannelKey:
    channel: str
    kind: str
    peer: str


def parse_channel_session_key(session_key: str) -> ChannelKey | None:
    """The channel, peer kind and peer a channel session key names, if any."""
    key = session_key.strip()
    if key.startswith("subagent:"):
        key = key[len("subagent:") :]
    parts = key.split(":")
    if len(parts) < 5 or parts[0] != "agent":
        return None
    for index in range(3, len(parts) - 1):
        if parts[index] in _PEER_KINDS:
            return ChannelKey(channel=parts[2], kind=parts[index], peer=parts[index + 1])
    return None


def _candidate_keys(entry: Any) -> list[str]:
    params = getattr(entry, "params", None)
    params = params if isinstance(params, Mapping) else {}
    own = str(params.get("sessionKey") or params.get("session_key") or "").strip()
    audience: Iterable[str] = getattr(entry, "audience_session_keys", ()) or ()
    keys = [own] if own else []
    keys.extend(key for key in audience if key and key not in keys)
    return keys


def channel_custody(
    entry: Any,
    *,
    channel_name: str,
    sender_id: str,
    chat_id: str,
    index: SessionIndex | None = None,
) -> CustodyDecision:
    """Decide whether ``sender_id`` in ``chat_id`` on ``channel_name`` may resolve ``entry``.

    ``entry`` is a ``PendingApproval`` (anything with ``params`` and
    ``audience_session_keys``).
    """
    keys = _candidate_keys(entry)
    if not keys:
        return CustodyDecision(False, "no-session")
    lookup = index if index is not None else get_session_index()
    for key in keys:
        named = parse_channel_session_key(key)
        if named is not None:
            if named.channel != channel_name:
                return CustodyDecision(False, "other-channel", key)
            expected = chat_id if named.kind in _SHARED_KINDS else sender_id
            if named.peer == expected:
                return CustodyDecision(True, "session-key", key)
            return CustodyDecision(False, "other-chat", key)
        route = lookup.route(key) if lookup is not None else None
        if route is not None and route.channel:
            if route.channel == channel_name and route.to == chat_id:
                return CustodyDecision(True, "delivery-route", key)
            return CustodyDecision(False, "other-channel", key)
        if not is_child_session_key(key) and not key.startswith("subagent:"):
            # A top-level session that names no channel and was never
            # delivered on one (the web console, the desktop, cron) is not
            # this chat's to answer.
            break
    return CustodyDecision(False, "not-delivered-here", keys[0])


def log_custody_refusal(decision: CustodyDecision, *, channel: str, approval_id: str) -> None:
    log.warning(
        "approval.custody_refused",
        channel=channel,
        approval_id=approval_id,
        reason=decision.reason,
        session_key=decision.session_key,
    )


__all__ = [
    "ChannelKey",
    "CustodyDecision",
    "channel_custody",
    "log_custody_refusal",
    "parse_channel_session_key",
]
