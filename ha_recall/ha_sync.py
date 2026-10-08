"""Read-only HA registry snapshot import. Never requests or stores live states."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from websockets.asyncio.client import connect

from .models import Entity, Relation
from .store import MemoryError, Store, dump


class Area(BaseModel):
    model_config = ConfigDict(extra="ignore")
    area_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=256)
    aliases: list[str] = Field(default_factory=list)


class Device(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1)
    name: str | None = None
    name_by_user: str | None = None
    area_id: str | None = None


class HAEntity(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    name: str | None = None
    original_name: str | None = None
    device_id: str | None = None
    area_id: str | None = None
    aliases: list[str] = Field(default_factory=list)


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    areas: list[Area]
    devices: list[Device]
    entities: list[HAEntity]


async def fetch_snapshot(url, token):
    if not url.startswith(("http://", "https://")) or not token:
        raise ValueError("An HA http(s) URL and HA_RECALL_HA_TOKEN are required")
    endpoint = (
        url.replace("https://", "wss://", 1).replace("http://", "ws://", 1).rstrip("/")
        + "/api/websocket"
    )
    async with asyncio.timeout(45), connect(endpoint, max_size=16 * 1024 * 1024) as ws:
        if json.loads(await ws.recv()).get("type") != "auth_required":
            raise ValueError("Unexpected HA greeting")
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            raise ValueError("HA authentication failed")
        result = {}
        for index, (key, command) in enumerate(
            (
                ("areas", "config/area_registry/list"),
                ("devices", "config/device_registry/list"),
                ("entities", "config/entity_registry/list"),
            ),
            1,
        ):
            await ws.send(json.dumps({"id": index, "type": command}))
            reply = json.loads(await ws.recv())
            if reply.get("id") != index or not reply.get("success"):
                raise ValueError(f"Registry read failed: {key}")
            result[key] = reply["result"]
        return Snapshot.model_validate(result)


def import_snapshot(store, namespace, instance, snapshot: Snapshot):
    if not instance or ":" in instance or len(instance) > 64:
        raise ValueError("Use a stable HA instance label, 1-64 characters, without ':'")
    source = f"ha-registry:{instance}"
    references, desired = {}, []
    # Validate the complete plan before writing anything; only explicit fields
    # enter memory. Unknown state/attribute fields from exports are ignored.
    for area in snapshot.areas:
        desired.append(
            (
                f"area:{area.area_id}",
                Entity(
                    name=area.name,
                    entity_type="area",
                    external_id=f"ha:{instance}:area:{area.area_id}",
                    aliases=area.aliases,
                ),
            )
        )
    for device in snapshot.devices:
        desired.append(
            (
                f"device:{device.id}",
                Entity(
                    name=device.name_by_user or device.name or device.id,
                    entity_type="device",
                    external_id=f"ha:{instance}:device:{device.id}",
                ),
            )
        )
    for entity in snapshot.entities:
        desired.append(
            (
                f"entity:{entity.id}",
                Entity(
                    name=entity.name or entity.original_name or entity.entity_id,
                    entity_type="ha_entity",
                    external_id=f"ha:{instance}:entity:{entity.id}",
                    aliases=sorted(set([entity.entity_id, *entity.aliases])),
                ),
            )
        )
    if len({key for key, _ in desired}) != len(desired):
        raise ValueError("Duplicate registry identifiers")
    changed = skipped = 0
    with store.transaction():
        for key, entity in desired:
            row = store.db.execute(
                "SELECT id FROM records WHERE namespace=? AND kind='entity' "
                "AND json_extract(data,'$.external_id')=?",
                (namespace, entity.external_id),
            ).fetchone()
            old = store._get(namespace, row[0], True) if row else None
            if old and old["status"] != "active":
                skipped += 1  # A user deletion wins over a later registry import.
                continue
            data = entity.model_dump(mode="json")
            if old:
                data["description"] = old["data"]["description"]
                data["categories"] = old["data"]["categories"]
                data["aliases"] = sorted(set(data["aliases"] + old["data"]["aliases"]))
            data = Entity.model_validate(data).model_dump(mode="json")
            if not old or data != old["data"]:
                old = store._save(namespace, "entity", data, source, "ha_sync", old)
                changed += 1
            references[key] = old["id"]
        edges = []
        for device in snapshot.devices:
            if device.area_id:
                edges.append((f"device:{device.id}", "located_in", f"area:{device.area_id}"))
        for entity in snapshot.entities:
            if entity.device_id:
                edges.append((f"entity:{entity.id}", "belongs_to", f"device:{entity.device_id}"))
            area_id = entity.area_id or next(
                (d.area_id for d in snapshot.devices if d.id == entity.device_id), None
            )
            if area_id:
                edges.append((f"entity:{entity.id}", "located_in", f"area:{area_id}"))
        wanted = set()
        for subject, predicate, obj in edges:
            if subject not in references or obj not in references:
                continue
            relation = Relation(
                subject_id=references[subject],
                predicate=predicate,
                object_id=references[obj],
                source=source,
            )
            data = relation.model_dump(mode="json")
            wanted.add(dump(data))
        existing = store.db.execute(
            "SELECT id,data FROM records WHERE namespace=? AND kind='relation' "
            "AND status='active' AND json_extract(data,'$.source')=?",
            (namespace, source),
        ).fetchall()
        current = {r["data"] for r in existing}
        for row in existing:
            if row["data"] not in wanted:
                old = store._get(namespace, row["id"])
                store._save(namespace, "relation", old["data"], source, "ha_sync", old, "deleted")
                changed += 1
        for data in sorted(wanted - current):
            store._save(namespace, "relation", json.loads(data), source, "ha_sync")
            changed += 1
    return {
        "references": len(references),
        "changed": changed,
        "skipped_deleted": skipped,
        "live_states_imported": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=os.getenv("HA_RECALL_DB", "data/memory.sqlite3"))
    parser.add_argument("--namespace", default="home")
    parser.add_argument(
        "--instance", required=True, help="Stable instance label; keep it across imports"
    )
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--snapshot", type=Path)
    inputs.add_argument("--ha-url")
    args = parser.parse_args()
    if args.snapshot:
        snapshot = Snapshot.model_validate_json(args.snapshot.read_text(encoding="utf-8"))
    else:
        snapshot = asyncio.run(fetch_snapshot(args.ha_url, os.getenv("HA_RECALL_HA_TOKEN", "")))
    store = Store(args.database)
    try:
        print(json.dumps(import_snapshot(store, args.namespace, args.instance, snapshot)))
    except MemoryError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        store.close()


if __name__ == "__main__":
    main()
