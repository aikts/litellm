from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from typing import Final

import aiohttp
from aiohttp import web
import httpx
import pytest
import pytest_asyncio

from litellm.llms.custom_httpx.aiohttp_transport import LiteLLMAiohttpTransport


@pytest_asyncio.fixture(loop_scope="function")
async def delayed_peer() -> AsyncIterator[tuple[str, asyncio.Event, asyncio.Event]]:
    occupied: Final = asyncio.Event()
    delayed_started: Final = asyncio.Event()

    async def handler(request: web.Request) -> web.StreamResponse:
        if request.path == "/hold":
            occupied.set()
        if request.path == "/delayed":
            delayed_started.set()
        await asyncio.sleep(float(request.query.get("delay", "0")))
        if request.path != "/stream":
            return web.Response(body=b"ok")
        response: Final = web.StreamResponse()
        await response.prepare(request)
        for _ in range(8):
            await response.write(b"chunk\n")
            await asyncio.sleep(0.05)
        await response.write_eof()
        return response

    app: Final = web.Application()
    app.router.add_get("/{path:.*}", handler)
    runner: Final = web.AppRunner(app)
    await runner.setup()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        site: Final = web.SockSite(runner, listener)
        await site.start()
        try:
            yield f"http://127.0.0.1:{listener.getsockname()[1]}", occupied, delayed_started
        finally:
            await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "queue_delay,header_delay,pool_timeout,request_sent",
    [(0.3, 0.2, 2.0, True), (0.7, 0.0, 2.0, False), (0.3, 0.0, 0.1, False)],
)
async def test_pool_wait_and_headers_share_read_budget(
    delayed_peer: tuple[str, asyncio.Event, asyncio.Event],
    queue_delay: float,
    header_delay: float,
    pool_timeout: float,
    request_sent: bool,
) -> None:
    base_url, occupied, delayed_started = delayed_peer
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=1)) as session:
        transport: Final = LiteLLMAiohttpTransport(session, owns_session=False)
        holder: Final = asyncio.ensure_future(session.get(f"{base_url}/hold?delay={queue_delay}"))
        await asyncio.wait_for(occupied.wait(), timeout=2)
        request: Final = httpx.Request(
            "GET",
            f"{base_url}/delayed?delay={header_delay}",
            extensions={"timeout": {"read": 0.4, "connect": 2.0, "pool": pool_timeout}},
        )
        try:
            with pytest.raises(httpx.TimeoutException):
                await transport.handle_async_request(request)
        finally:
            held_response: Final = await holder
            held_response.release()
        async with httpx.AsyncClient(transport=transport, timeout=0.4) as client:
            recovered: Final = await client.get(f"{base_url}/healthy")
            assert recovered.content == b"ok"
            assert delayed_started.is_set() is request_sent


@pytest.mark.asyncio
@pytest.mark.parametrize("read_timeout", [None, 0.2])
@pytest.mark.parametrize("stream", [False, True])
async def test_live_body_outlasts_header_budget(
    delayed_peer: tuple[str, asyncio.Event, asyncio.Event], read_timeout: float | None, stream: bool
) -> None:
    base_url, _, _ = delayed_peer
    async with aiohttp.ClientSession() as session:
        transport: Final = LiteLLMAiohttpTransport(session, owns_session=False)
        async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(read_timeout)) as client:
            request: Final = client.build_request("GET", f"{base_url}/stream")
            response: Final = await client.send(request, stream=stream)
            try:
                assert await response.aread() == b"chunk\n" * 8
            finally:
                await response.aclose()
