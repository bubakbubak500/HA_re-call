import asyncio
import socket
import threading

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import SSETransport, StreamableHttpTransport

from ha_recall.server import LocalToken, Settings, create_server
from ha_recall.store import Store

TOKEN = "test-only-token-" + "a" * 32


def test_fail_closed():
    with pytest.raises(ValueError, match="TOKEN"):
        create_server(Settings(token=""))


async def test_token_verification():
    verifier = LocalToken(TOKEN)
    assert await verifier.verify_token("wrong") is None
    assert (await verifier.verify_token(TOKEN)).subject == "local"


@pytest.mark.parametrize("transport", ["http", "sse"])
async def test_real_mcp_transport_auth_and_memory_roundtrip(transport):
    store = Store(":memory:")
    mcp = create_server(Settings(token=TOKEN), store=store)
    path = "/sse" if transport == "sse" else "/mcp"
    app = mcp.http_app(path=path, transport=transport)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient() as client:
            assert (await client.get(base + "/health")).status_code == 200
            assert (await client.get(base + path)).status_code == 401
            assert (
                await client.get(base + path, headers={"Authorization": "Bearer wrong"})
            ).status_code == 401
            if transport == "sse":
                assert (
                    await client.post(base + "/messages/?session_id=unknown", json={})
                ).status_code == 401
        cls = SSETransport if transport == "sse" else StreamableHttpTransport
        async with Client(cls(base + path, auth=TOKEN), timeout=10) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert {"create_entity", "search_memory", "forget_record", "resolve_fact"} <= names
            result = await client.call_tool(
                "create_entity",
                {"namespace": "home", "entity": {"name": "EGLO", "external_id": "light.eglo"}},
            )
            entity = result.data
            result = await client.call_tool(
                "search_memory", {"namespace": "home", "query": "light.eglo"}
            )
            assert result.data["results"][0]["id"] == entity["id"]
            denied = await client.call_tool(
                "get_record",
                {"namespace": "private", "record_id": entity["id"]},
                raise_on_error=False,
            )
            assert denied.is_error
            fact = await client.call_tool(
                "add_fact",
                {
                    "namespace": "home",
                    "fact": {
                        "entity_id": entity["id"],
                        "predicate": "issue",
                        "value": "pairing reset",
                        "source": "test",
                    },
                },
            )
            assert fact.data["status"] == "active"
            await client.call_tool(
                "forget_record",
                {"namespace": "home", "record_id": entity["id"], "expected_revision": 1},
            )
            result = await client.call_tool(
                "search_memory", {"namespace": "home", "query": "pairing"}
            )
            assert result.data["results"] == []
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 10)
        sock.close()
        store.close()
        assert not thread.is_alive()
