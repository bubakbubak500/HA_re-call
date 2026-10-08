"""Validate against isolated real HA Core and existing local Model2Vec weights."""

import argparse
import json
import os
import secrets
import subprocess
import time
from pathlib import Path
from uuid import uuid4


def docker(*args, env=None):
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def run(model, transport):
    root = Path(__file__).resolve().parents[1]
    suffix = uuid4().hex[:10]
    network = "recall-test-" + suffix
    ha, server = network + "-ha", network + "-server"
    config, data = network + "-config", network + "-data"
    exchange = root / ".test-tmp" / network
    exchange.mkdir(parents=True)
    env = {**os.environ, "HA_RECALL_TOKEN": secrets.token_urlsafe(32)}
    endpoint = "/sse" if transport == "sse" else "/mcp"
    try:
        docker("network", "create", network)
        docker(
            "run",
            "-d",
            "--name",
            ha,
            "--network",
            network,
            "--network-alias",
            "ha-core",
            "-v",
            f"{config}:/config",
            "-v",
            f"{root}:/work:ro",
            "-v",
            f"{exchange}:/exchange",
            *(["-v", f"{Path(model).resolve()}:/model:ro"] if model else ["-e", "FIXTURE_MODEL=1"]),
            "-e",
            "HA_RECALL_TOKEN",
            "-e",
            "RECALL_URL=http://recall-server:8004" + endpoint,
            "ha-recall-ha-test:local",
            env=env,
        )
        for _ in range(180):
            if (exchange / "ha-ready.json").exists():
                break
            if docker("inspect", "-f", "{{.State.Running}}", ha) != "true":
                raise RuntimeError("HA exited during bootstrap")
            time.sleep(1)
        else:
            raise RuntimeError("HA bootstrap timeout")
        credentials = json.loads((exchange / "ha-ready.json").read_text(encoding="utf-8"))
        env.update(
            HA_RECALL_HA_TOKEN=credentials["token"], HA_RECALL_EMBEDDING_KEY=credentials["token"]
        )
        docker(
            "run",
            "-d",
            "--name",
            server,
            "--network",
            network,
            "--network-alias",
            "recall-server",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--cap-drop",
            "ALL",
            "-v",
            f"{data}:/data",
            "-e",
            "HA_RECALL_TOKEN",
            "-e",
            "HA_RECALL_HA_TOKEN",
            "-e",
            "HA_RECALL_EMBEDDING_KEY",
            "-e",
            "HA_RECALL_TRANSPORT=" + transport,
            "-e",
            "HA_RECALL_HA_URL=http://ha-core:8123",
            "-e",
            "HA_RECALL_HA_INSTANCE=test",
            "-e",
            "HA_RECALL_SYNC_INTERVAL=60",
            "-e",
            "HA_RECALL_EMBEDDING_URL=http://ha-core:8123/api/ha_recall/embeddings",
            "ha-recall:local",
            env=env,
        )
        for _ in range(180):
            if (exchange / "result.json").exists():
                result = json.loads((exchange / "result.json").read_text())
                print(json.dumps(result))
                return
            if docker("inspect", "-f", "{{.State.Running}}", ha) != "true":
                raise RuntimeError("HA test failed")
            time.sleep(1)
        raise RuntimeError("HA validation timeout")
    except Exception:
        for name in (ha, server):
            logs = subprocess.run(
                ["docker", "logs", "--tail", "100", name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            text = logs.stdout + logs.stderr
            for key in ("HA_RECALL_TOKEN", "HA_RECALL_HA_TOKEN", "HA_RECALL_EMBEDDING_KEY"):
                if env.get(key):
                    text = text.replace(env[key], "[REDACTED]")
            print(text)
        raise
    finally:
        for name in (ha, server):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        for volume in (config, data):
            subprocess.run(["docker", "volume", "rm", volume], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)
        (exchange / "ha-ready.json").unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--model")
    source.add_argument(
        "--fixture-model",
        action="store_true",
        help="Test transport/bridge using deterministic vectors; no semantic quality claim",
    )
    parser.add_argument("--transport", choices=["http", "sse"], default="http")
    args = parser.parse_args()
    run(args.model, args.transport)
