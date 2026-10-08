"""Run against real HA Core in an isolated container, never the user's HA."""

import asyncio
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
from homeassistant import bootstrap, loader
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import area_registry, device_registry, entity_registry, llm
from model2vec import StaticModel

from custom_components.ha_recall.llm import async_get_tools


async def run():
    hass = HomeAssistant("/config")
    loader.async_setup(hass)
    shared = Path("/exchange")
    token = os.environ["HA_RECALL_TOKEN"]
    url = os.environ["RECALL_URL"]
    try:
        result = await bootstrap.async_from_config_dict(
            {
                "homeassistant": {
                    "name": "Recall isolated test",
                    "latitude": 50,
                    "longitude": 14,
                    "elevation": 200,
                    "unit_system": "metric",
                    "time_zone": "Europe/Prague",
                },
                "http": {"server_host": "0.0.0.0", "server_port": 8123},
                "websocket_api": {},
                "config": {},
                "api": {},
                "llm": {},
                "mcp": {},
            },
            hass,
        )
        assert result is hass
        logging.basicConfig(level=logging.WARNING, force=True)
        await hass.async_start()
        user = await hass.auth.async_create_user("Recall test owner", group_ids=[GROUP_ID_ADMIN])
        refresh = await hass.auth.async_create_refresh_token(user, client_id="http://recall-test")
        ha_token = hass.auth.async_create_access_token(refresh)
        areas = area_registry.async_get(hass)
        area = areas.async_create("Test obývák")
        devices = device_registry.async_get(hass)
        settings = {"url": url, "token": token, "expose_to_assist": True, "embedding_bridge": True}
        # The real loaded StaticModel is used through the same interface as Jarvis.
        real_model = os.environ.get("FIXTURE_MODEL") != "1"
        if real_model:
            model = await hass.async_add_executor_job(StaticModel.from_pretrained, "/model")
        else:
            import numpy as np

            class FixtureModel:
                def encode(self, texts, **kwargs):
                    return np.array([[1.0, 0.0] for _ in texts])

            model = FixtureModel()
        hass.data["jarvis_semantic"] = {"test_jarvis": SimpleNamespace(model=model)}
        manifest_path = Path(hass.config.path("custom_components/jarvis_semantic"))
        manifest_path.mkdir(parents=True, exist_ok=True)
        (manifest_path / "model_manifest.json").write_text(
            '{"test_model":"existing-potion-multilingual-128M"}'
        )
        (shared / "ha-ready.json").write_text(json.dumps({"token": ha_token}), encoding="utf-8")
        async with httpx.AsyncClient(timeout=2) as client:
            for _ in range(150):
                try:
                    if (await client.get(url.rsplit("/", 1)[0] + "/health")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError("Recall server did not start")
        flow = await hass.config_entries.flow.async_init("ha_recall", context={"source": "user"})
        assert flow["type"] == "form"
        invalid = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {**settings, "url": "ftp://invalid"}
        )
        assert invalid["errors"]["base"] == "invalid_url"
        invalid = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {**settings, "token": "wrong" * 10}
        )
        assert invalid["type"] == "form" and invalid["errors"]
        configured = await hass.config_entries.flow.async_configure(flow["flow_id"], settings)
        assert configured["type"] == "create_entry", configured
        entry = configured["result"]
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        print(
            "ENTRY",
            entry.state,
            entry.reason,
            "APIS",
            [api.id for api in llm.async_get_apis(hass)],
            flush=True,
        )
        device = devices.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={("recall_test", "lamp")}, name="EGLO test"
        )
        devices.async_update_device(device.id, area_id=area.id)
        entities = entity_registry.async_get(hass)
        lamp = entities.async_get_or_create(
            "light",
            "recall_test",
            "lamp",
            config_entry=entry,
            device_id=device.id,
            original_name="EGLO test",
        )
        names = {tool.remote_name for tool in coordinator.data}
        assert len(names) == 55 and {"search", "create_note", "create_entity", "purge"} <= names
        context = llm.LLMContext(
            platform="jarvis_semantic",
            context=Context(user_id=user.id),
            language="cs",
            assistant="conversation",
            device_id=None,
        )
        contributed = async_get_tools(hass, context, llm.LLM_API_ASSIST)
        assert len(contributed.tools) == 55
        api = await llm.async_get_api(hass, f"ha_recall-{entry.entry_id}", context)
        assist = await llm.async_get_api(hass, llm.LLM_API_ASSIST, context)
        assert len([tool for tool in assist.tools if tool.name.startswith("ha_recall_")]) == 55

        async def call(name, arguments):
            result = await api.async_call_tool(
                llm.ToolInput(tool_name="ha_recall_" + name, tool_args=arguments)
            )
            assert not result.error, result
            return result.data

        note = await call(
            "create_note",
            {
                "project_id": "home",
                "title": "Závada párování lampy",
                "body": "Žárovku nešlo připojit k síti Zigbee. Pomohl její reset.",
            },
        )
        assert (await call("read_note", {"note_id": note["id"]}))["body"].startswith("Žárovku")
        matches = await call(
            "search_memory", {"namespace": "home", "query": "problémy s připojením osvětlení"}
        )
        assert matches["semantic"] == "ready", matches
        assert any(r["id"] == note["id"] for r in matches["results"])
        entity = await call(
            "create_entity",
            {"namespace": "home", "entity": {"name": "Native fixture", "entity_type": "device"}},
        )
        fact = await call(
            "add_fact",
            {
                "namespace": "home",
                "fact": {
                    "entity_id": entity["id"],
                    "predicate": "preference",
                    "value": "warm",
                    "source": "test",
                },
            },
        )
        proposal = await call(
            "add_fact",
            {
                "namespace": "home",
                "fact": {
                    "entity_id": entity["id"],
                    "predicate": "preference",
                    "value": "cold",
                    "source": "test",
                },
            },
        )
        assert fact["status"] == "active" and proposal["status"] == "pending"
        rejected = await call(
            "resolve_fact",
            {
                "namespace": "home",
                "fact_id": proposal["id"],
                "accept": False,
                "expected_revision": 1,
            },
        )
        assert rejected["status"] == "rejected"
        missing = await api.async_call_tool(
            llm.ToolInput(tool_name="ha_recall_read_note", tool_args={"note_id": "missing"})
        )
        assert missing.error
        hass.data["jarvis_semantic"] = {}
        degraded = await call(
            "search_memory", {"namespace": "home", "query": "Závada párování lampy"}
        )
        assert degraded["semantic"] == "unavailable" and degraded["results"][0]["id"] == note["id"]
        hass.data["jarvis_semantic"] = {"test_jarvis": SimpleNamespace(model=model)}
        async with httpx.AsyncClient() as client:
            endpoint = "http://127.0.0.1:8123/api/ha_recall/embeddings"
            assert (await client.post(endpoint, json={"input": ["private"]})).status_code == 401
            assert (
                await client.post(
                    endpoint, headers={"Authorization": "Bearer " + ha_token}, json={"input": []}
                )
            ).status_code == 400
            assert (
                await client.post(
                    endpoint, headers={"Authorization": "Bearer " + ha_token}, content=b"x" * 256001
                )
            ).status_code == 413
        # Wait for the server's real HA WebSocket registry sync and inspect its stable reference.
        for _ in range(280):
            matches = await call("search_memory", {"namespace": "home", "query": lamp.entity_id})
            if any(
                (r["data"].get("external_id") or "").startswith("ha:test:entity:")
                for r in matches["results"]
            ):
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError("HA registry sync did not import the entity")
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert async_get_tools(hass, context, llm.LLM_API_ASSIST) is None
        (shared / "result.json").write_text(
            json.dumps(
                {
                    "status": "passed",
                    "ha_version": "2026.10.0",
                    "tools": len(names),
                    "transport": url.rsplit("/", 1)[-1],
                    "real_model2vec": real_model,
                    "auth": True,
                    "registry_sync": True,
                    "assist_contribution": True,
                }
            ),
            encoding="utf-8",
        )
        print("PASS real HA Core: MCP tools, auth, Assist, Model2Vec bridge and registry sync")
    finally:
        await hass.async_stop()


asyncio.run(run())
