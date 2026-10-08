"""Diagnostics contain status and counts, never credentials, URLs or memory text."""


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = entry.runtime_data
    return {
        "available": coordinator.last_update_success,
        "tools": len(coordinator.data or []),
        "expose_to_assist": entry.data.get("expose_to_assist", False),
        "embedding_bridge": entry.data.get("embedding_bridge", False),
    }
