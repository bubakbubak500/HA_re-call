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
        env = {**os.environ, "HA_RECALL_TOKEN": token, "HA_RECALL_TRANSPORT": transport}
        options = Path(".test-tmp") / f"options-{suffix}.json"
        options.parent.mkdir(parents=True, exist_ok=True)
        options.write_text(json.dumps({"token": token, "transport": transport}), encoding="utf-8")
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
            args += ["-e", "HA_RECALL_TOKEN", "-e", "HA_RECALL_TRANSPORT"]
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
            print(f"PASS {image}: {transport}, auth, writable volume, restart persistence")
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            subprocess.run(["docker", "volume", "rm", volume], capture_output=True)
            options.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--addon", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.image, args.addon))
