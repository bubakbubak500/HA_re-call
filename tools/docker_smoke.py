"""Exercise a built image through MCP, including auth, writes and restart persistence."""

import argparse
import asyncio
import json
import os
import secrets
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx
from fastmcp import Client
from fastmcp.client.transports import SSETransport, StreamableHttpTransport


def docker(*args, env=None):
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


async def healthy(base):
    async with httpx.AsyncClient(timeout=2) as client:
        for _ in range(50):
            try:
                response = await client.get(base + "/health")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    raise RuntimeError("Container did not become healthy")


async def run(image, addon=False):
    for transport in ("http", "sse"):
        suffix = uuid4().hex[:12]
        name, volume = f"ha-recall-smoke-{suffix}", f"ha-recall-smoke-data-{suffix}"
        token = secrets.token_urlsafe(32)
        viewer_token = secrets.token_urlsafe(32)
        identities = [{"id": "viewer", "upn": "viewer@local", "token": viewer_token}]
        env = {**os.environ, "HA_RECALL_TOKEN": token, "HA_RECALL_TRANSPORT": transport}
        options = Path(".test-tmp") / f"options-{suffix}.json"
        options.parent.mkdir(parents=True, exist_ok=True)
        options.write_text(
            json.dumps({"token": token, "transport": transport, "identities": identities}),
            encoding="utf-8",
        )
        identities_file = options.with_name(f"identities-{suffix}.json")
        identities_file.write_text(json.dumps(identities), encoding="utf-8")
        args = [
            "run",
            "-d",
            "--name",
            name,
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "-p",
            "127.0.0.1::8004",
            "-v",
            f"{volume}:/data",
        ]
        if addon:
            args += [
                "--mount",
                f"type=bind,source={options.resolve()},target=/data/options.json,readonly",
            ]
        else:
            args += [
                "-e",
                "HA_RECALL_TOKEN",
                "-e",
                "HA_RECALL_TRANSPORT",
                "-e",
                "HA_RECALL_IDENTITIES_FILE=/run/identities.json",
                "--mount",
                f"type=bind,source={identities_file.resolve()},target=/run/identities.json,readonly",
            ]
        try:
            docker(*args, image, env=env)
            address = docker("port", name, "8004/tcp").splitlines()[0]
            base = "http://" + address
            await healthy(base)
            endpoint = base + ("/sse" if transport == "sse" else "/mcp")
            async with httpx.AsyncClient() as client:
                assert (await client.get(endpoint)).status_code == 401
            cls = SSETransport if transport == "sse" else StreamableHttpTransport
            async with Client(cls(endpoint, auth=token), timeout=10) as client:
                record = (
                    await client.call_tool(
                        "create_entity",
                        {"namespace": "home", "entity": {"name": "Container persistence test"}},
                    )
                ).data
                await client.call_tool(
                    "share_workspace",
                    {"project_id": "home", "upn": "viewer@local", "role": "viewer"},
                )
            async with Client(cls(endpoint, auth=viewer_token), timeout=10) as viewer:
                assert (await viewer.call_tool("read_note", {"note_id": record["id"]})).data[
                    "title"
                ] == "Container persistence test"
                assert (
                    await viewer.call_tool("create_note", {"project_id": "home", "title": "denied"})
                ).data["error"] == "forbidden"
                denied = await viewer.call_tool(
                    "create_entity",
                    {"namespace": "home", "entity": {"name": "denied"}},
                    raise_on_error=False,
                )
                assert denied.is_error
                assert (await viewer.call_tool("list_tree", {"project_id": "technical"})).data[
                    "error"
                ] == "not_found"
            docker("restart", name)
            # Docker Desktop may allocate a different ephemeral host port on restart.
            address = docker("port", name, "8004/tcp").splitlines()[0]
            base = "http://" + address
            endpoint = base + ("/sse" if transport == "sse" else "/mcp")
            await healthy(base)
            async with Client(cls(endpoint, auth=token), timeout=10) as client:
                restored = (
                    await client.call_tool(
                        "get_record", {"namespace": "home", "record_id": record["id"]}
                    )
                ).data
                assert restored == record
                await client.call_tool("remove_member", {"project_id": "home", "user_id": "viewer"})
            async with Client(cls(endpoint, auth=viewer_token), timeout=10) as viewer:
                assert (await viewer.call_tool("read_note", {"note_id": record["id"]})).data[
                    "error"
                ] == "not_found"
            print(
                f"PASS {image}: {transport}, auth, identity grants/revocation, writable volume, restart persistence"
            )
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            subprocess.run(["docker", "volume", "rm", volume], capture_output=True)
            options.unlink(missing_ok=True)
            identities_file.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--addon", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.image, args.addon))
