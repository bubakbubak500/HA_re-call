"""Use HA's maintained MCP transport, with a local bearer token instead of OAuth."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

import httpx
import probatio
from homeassistant.components.mcp.coordinator import mcp_client
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from mcp.shared.exceptions import McpError

from .const import DOMAIN, PROMPT

_LOGGER = logging.getLogger(__name__)


class RecallTool(llm.Tool):
    integration = DOMAIN

    def __init__(self, remote, coordinator):
        self.remote_name = remote.name
        self.name = f"{DOMAIN}__{remote.name}"
        self.title = getattr(remote, "title", None)
        self.description = remote.description
        # Preserve the MCP schema for consumers which accept JSON Schema directly.
        # probatio's round-trip currently serializes integer coercion as string.
        self.parameters_json_schema = remote.inputSchema
        self.parameters = probatio.from_openapi(remote.inputSchema)
        annotations = remote.annotations
        if hasattr(llm, "ToolAnnotations"):
            self.annotations = llm.ToolAnnotations(
                read_only=bool(annotations and annotations.readOnlyHint),
                destructive=annotations.destructiveHint
                if annotations and annotations.destructiveHint is not None
                else True,
                open_world=annotations.openWorldHint
                if annotations and annotations.openWorldHint is not None
                else True,
                idempotent=bool(annotations and annotations.idempotentHint),
            )
        self.coordinator = coordinator

    async def async_call(self, hass, tool_input, llm_context):
        try:
            async with asyncio.timeout(30):
                async with mcp_client(hass, self.coordinator.url, self.coordinator.token) as (
                    session,
                    _,
                ):
                    result = await session.call_tool(self.remote_name, tool_input.tool_args)
            # Compatibility tools return structured error dictionaries for old clients.
            data = result.structuredContent
            if data is None:
                data = {
                    "content": [entry.model_dump(exclude_none=True) for entry in result.content]
                }
            error = bool(result.isError or data.get("error"))
            if hasattr(llm, "ToolResult"):
                return llm.ToolResult(data=data, error=error)
            return {**data, **({"error": data.get("error") or "mcp_tool_failed"} if error else {})}
        except (TimeoutError, httpx.HTTPError, ExceptionGroup, McpError) as exc:
            raise HomeAssistantError(
                "HA re:call is unavailable; the operation may not have completed"
            ) from exc


class RecallCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        super().__init__(
            hass, _LOGGER, name=DOMAIN, config_entry=entry, update_interval=timedelta(minutes=5)
        )
        self.url = entry.data["url"]
        self.entry = entry

    async def token(self):
        return self.entry.data["token"]

    async def _async_update_data(self):
        try:
            async with asyncio.timeout(30):
                async with mcp_client(self.hass, self.url, self.token) as (session, _):
                    result = await session.list_tools()
            return [RecallTool(tool, self) for tool in result.tools]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise ConfigEntryAuthFailed("Invalid HA re:call token") from exc
            raise UpdateFailed("HA re:call HTTP error") from exc
        except (TimeoutError, httpx.HTTPError, ExceptionGroup, ValueError, McpError) as exc:
            raise UpdateFailed("Cannot load HA re:call tools") from exc


@dataclass(kw_only=True)
class RecallAPI(llm.API):
    coordinator: RecallCoordinator

    async def async_get_api_instance(self, llm_context):
        if not self.coordinator.last_update_success:
            raise HomeAssistantError("HA re:call is unavailable")
        return llm.APIInstance(self, PROMPT, llm_context, tools=self.coordinator.data or [])
