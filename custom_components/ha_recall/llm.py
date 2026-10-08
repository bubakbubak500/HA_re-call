"""Optional contribution to Assist, which also makes memory available to Jarvis/Luna."""

from homeassistant.components.llm import LLMTools
from homeassistant.core import callback
from homeassistant.helpers.llm import LLM_API_ASSIST

from .const import DOMAIN, PROMPT


@callback
def async_get_tools(hass, llm_context, api_id):
    if api_id != LLM_API_ASSIST:
        return None
    tools = []
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if not hasattr(coordinator, "entry"):
            continue
        if (
            coordinator.entry.data.get("expose_to_assist", False)
            and coordinator.last_update_success
        ):
            tools.extend(coordinator.data or [])
    return LLMTools(tools=tools, prompt=PROMPT) if tools else None
