"""The pre-auth message cap is enforced by the transport, before the frame is read.

A real uvicorn server with the gateway's protocol class and app on a
loopback port: an oversized pre-auth message (plain, fragmented or
compressed) is refused with 1009 and never reaches the handshake, and the
limit rises to ``max_payload`` for the very first message after hello-ok.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import pytest
import uvicorn
import websockets

from agentos.gateway.app import create_gateway_app
from agentos.gateway.config import GatewayConfig
from agentos.gateway.protocol import MAX_PAYLOAD_BYTES, MAX_PREAUTH_PAYLOAD_BYTES
from agentos.gateway.ws_transport import GatewayWebSocketProtocol


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.asynccontextmanager
async def _gateway() -> AsyncIterator[str]:
    port = _free_port()
    config = uvicorn.Config(
        create_gateway_app(GatewayConfig()),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
        ws=GatewayWebSocketProtocol,
        ws_max_size=MAX_PAYLOAD_BYTES,
    )
    server = uvicorn.Server(config)
    setattr(server, "capture_signals", contextlib.nullcontext)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"ws://127.0.0.1:{port}/ws"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


def _connect(pad: str = "", **params: Any) -> str:
    return json.dumps(
        {
            "type": "req",
            "id": "c1",
            "method": "connect",
            "params": {"minProtocol": 3, "maxProtocol": 3, "pad": pad, **params},
        }
    )


@contextlib.asynccontextmanager
async def _client(url: str, **kwargs: Any) -> AsyncIterator[Any]:
    # Not ``async with websockets.connect``: when the server drops the
    # connection in the middle of a large send, the client library's own
    # close path can trip over the already-closed transport.
    ws = await websockets.connect(url, **kwargs)
    try:
        yield ws
    finally:
        with contextlib.suppress(Exception):
            await ws.close()


async def _refused(ws: Any) -> tuple[list[Any], int | None]:
    frames: list[Any] = []
    try:
        while True:
            frames.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=5)))
    except websockets.ConnectionClosed as exc:
        return frames, exc.rcvd.code if exc.rcvd is not None else None


@pytest.mark.parametrize("compression", [None, "deflate"])
async def test_oversized_preauth_message_is_refused_before_the_handshake(
    compression: str | None,
) -> None:
    async with _gateway() as url:
        async with _client(url, compression=compression, max_size=None) as ws:
            await ws.recv()  # challenge
            # 1 MiB of one character: compresses to a few KiB with deflate.
            with contextlib.suppress(websockets.ConnectionClosed):
                await ws.send(_connect("x" * (1 << 20)))
            frames, code = await _refused(ws)
    assert code == 1009
    # Refused by the transport: the handshake never answered it.
    assert frames == []


async def test_fragmented_preauth_message_is_refused_on_its_running_size() -> None:
    async with _gateway() as url:
        async with _client(url, compression=None) as ws:
            await ws.recv()
            chunk = "y" * (MAX_PREAUTH_PAYLOAD_BYTES // 2)
            with contextlib.suppress(websockets.ConnectionClosed):
                await ws.send([chunk, chunk, chunk, chunk])
            frames, code = await _refused(ws)
    assert code == 1009
    assert frames == []


async def test_preauth_message_under_the_cap_is_handled() -> None:
    async with _gateway() as url:
        async with _client(url) as ws:
            await ws.recv()
            await ws.send(_connect("z" * (MAX_PREAUTH_PAYLOAD_BYTES - 500)))
            assert json.loads(await ws.recv())["type"] == "hello-ok"


async def test_limit_rises_for_the_first_message_after_hello() -> None:
    async with _gateway() as url:
        async with _client(url, max_size=None) as ws:
            await ws.recv()
            await ws.send(_connect())
            hello = json.loads(await ws.recv())
            assert hello["type"] == "hello-ok"
            # The reader was already waiting for this message while the
            # handshake was answered; it still gets the post-auth limit.
            for size in (1 << 20, 3 << 20):
                big = json.dumps(
                    {
                        "type": "req",
                        "id": f"r{size}",
                        "method": "health",
                        "params": {"pad": "p" * size},
                    }
                )
                await ws.send(big)
                while True:
                    frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    if frame.get("type") == "res":
                        break
                assert frame["id"] == f"r{size}"
                assert frame["ok"] is True
