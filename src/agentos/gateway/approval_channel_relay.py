"""Send a child session's approval prompt to the chat its parent talks to.

A subagent or coding harness that a Telegram / Discord / Slack conversation
spawned raises its approvals in its own session, which no chat is bound to:
the channel turn that delivers prompts with buttons is the parent's, and it is
waiting on the child. This approval listener closes that gap. When the queue
persists a request whose session is a child (``audience_session_keys`` has
ancestors), it finds the nearest ancestor delivered on a channel -- by its
recorded delivery route, or by a group/channel session key -- and sends the
usual prompt with Approve / Deny buttons there. Clicking them goes through
the same custody check as any channel approval, which admits that chat
because it is the parent's.

Nothing is sent for a session's own prompts (its turn already does that) or
when no ancestor is delivered on a channel.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from agentos.gateway.approval_custody import parse_channel_session_key
from agentos.gateway.session_index import SessionIndex, get_session_index
from agentos.session.keys import is_child_session_key

if TYPE_CHECKING:
    from agentos.application.approval_queue import PendingApproval

log = structlog.get_logger(__name__)

_SHARED_KINDS = frozenset({"group", "channel"})


@dataclass(frozen=True, slots=True)
class RelayTarget:
    session_key: str
    channel: str
    chat_id: str


def _is_child(session_key: str) -> bool:
    return is_child_session_key(session_key) or session_key.startswith("subagent:")


def relay_target(entry: Any, index: SessionIndex | None) -> RelayTarget | None:
    """The chat a child prompt should go to, or None."""
    audience = list(getattr(entry, "audience_session_keys", ()) or ())
    if len(audience) < 2 or not _is_child(audience[0]):
        return None
    for key in audience[1:]:
        route = index.route(key) if index is not None else None
        if route is not None and route.channel and route.to:
            return RelayTarget(session_key=key, channel=route.channel, chat_id=route.to)
        named = parse_channel_session_key(key)
        if named is not None and named.kind in _SHARED_KINDS:
            return RelayTarget(session_key=key, channel=named.channel, chat_id=named.peer)
        if not _is_child(key):
            break
    return None


def prompt_fields(entry: Any) -> dict[str, Any]:
    params = getattr(entry, "params", None)
    params = params if isinstance(params, dict) else {}
    command = params.get("command")
    if not command and isinstance(params.get("argv"), list):
        command = " ".join(str(part) for part in params["argv"])
    title = params.get("displayTitle") or params.get("toolName")
    return {
        "approval_id": getattr(entry, "approval_id", ""),
        "command": str(command or title or ""),
        "tool_name": str(params.get("toolName") or getattr(entry, "namespace", "tool")),
        "status": "approval_required",
    }


class ChannelApprovalRelay:
    """Approval listener that relays child prompts to the parent's chat."""

    def __init__(
        self,
        channel_manager_ref: Callable[[], Any],
        *,
        index_ref: Callable[[], SessionIndex | None] = get_session_index,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._channels = channel_manager_ref
        self._index = index_ref
        self._loop = loop
        self._tasks: set[asyncio.Future[Any]] = set()

    def on_requested(self, entry: PendingApproval) -> None:
        target = relay_target(entry, self._index())
        if target is None:
            return
        manager = self._channels()
        channel = manager.get(target.channel) if manager is not None else None
        if channel is None:
            log.info(
                "approval.relay_no_channel",
                approval_id=entry.approval_id,
                channel=target.channel,
            )
            return
        self._schedule(self._send(channel, target, prompt_fields(entry)))

    def on_resolved(self, entry: PendingApproval) -> None:
        return None

    async def _send(self, channel: Any, target: RelayTarget, pending: dict[str, Any]) -> None:
        from agentos.channels.types import IncomingMessage
        from agentos.gateway.channel_dispatch import _deliver_channel_approval_prompt

        inbound = IncomingMessage(sender_id="", channel_id=target.chat_id, content="")
        await _deliver_channel_approval_prompt(channel, inbound, pending)
        log.info(
            "approval.relayed_to_parent_chat",
            approval_id=pending.get("approval_id"),
            channel=target.channel,
            session_key=target.session_key,
        )

    def _schedule(self, coro: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task: asyncio.Future[Any] = asyncio.ensure_future(coro)
        elif self._loop is not None and self._loop.is_running():
            task = asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(coro, self._loop), loop=self._loop
            )
        else:
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


__all__ = ["ChannelApprovalRelay", "RelayTarget", "prompt_fields", "relay_target"]
