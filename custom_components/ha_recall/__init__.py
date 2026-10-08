"""Local-token MCP memory and a bridge to the already loaded Jarvis Model2Vec."""

from homeassistant.helpers import llm as llm_helper

from .client import RecallAPI, RecallCoordinator
from .const import DOMAIN
from .embeddings import EmbeddingView


async def async_setup_entry(hass, entry):
    coordinator = RecallCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    state = hass.data.setdefault(DOMAIN, {})
    if "embedding_view" not in state:
        state["embedding_view"] = EmbeddingView(hass)
        hass.http.register_view(state["embedding_view"])
    state[entry.entry_id] = coordinator
    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.async_add_listener(lambda: None))
    entry.async_on_unload(
        llm_helper.async_register_api(
            hass,
            RecallAPI(
                hass=hass,
                id=f"{DOMAIN}-{entry.entry_id}",
                name=entry.title,
                coordinator=coordinator,
            ),
        )
    )
    return True


async def async_unload_entry(hass, entry):
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True
