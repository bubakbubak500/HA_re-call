"""Configure the MCP endpoint and local token; never put credentials in URLs."""

import asyncio
from urllib.parse import urlsplit

import httpx
import probatio
from homeassistant import config_entries
from homeassistant.components.mcp.coordinator import mcp_client
from homeassistant.helpers import selector
from mcp.shared.exceptions import McpError

from .const import DOMAIN


def schema(defaults=None):
    defaults = defaults or {}
    return probatio.Schema(
        {
            probatio.Required(
                "url", default=defaults.get("url", "http://local-ha-recall:8004/mcp")
            ): str,
            probatio.Required("token"): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            probatio.Optional(
                "expose_to_assist", default=defaults.get("expose_to_assist", False)
            ): bool,
            probatio.Optional(
                "embedding_bridge", default=defaults.get("embedding_bridge", True)
            ): bool,
        }
    )


async def validate(hass, data):
    parsed = urlsplit(data["url"])
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid_url")
    if len(data["token"]) < 32:
        raise ValueError("invalid_auth")

    async def token():
        return data["token"]

    async with asyncio.timeout(30):
        async with mcp_client(hass, data["url"], token) as (session, _):
            tools = await session.list_tools()
            if not {"search", "read_note", "create_entity", "search_memory"} <= {
                tool.name for tool in tools.tools
            }:
                raise ValueError("wrong_server")


class RecallConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                await validate(self.hass, user_input)
                # One integration avoids duplicate Assist tool names and ambiguous model bridges.
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="HA re:call", data=user_input)
            except ValueError as exc:
                errors["base"] = (
                    str(exc)
                    if str(exc) in ("invalid_url", "invalid_auth", "wrong_server")
                    else "cannot_connect"
                )
            except httpx.HTTPStatusError as exc:
                errors["base"] = (
                    "invalid_auth" if exc.response.status_code in (401, 403) else "cannot_connect"
                )
            except (TimeoutError, httpx.HTTPError, ExceptionGroup, McpError):
                errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="user", data_schema=schema(user_input), errors=errors)

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            try:
                await validate(self.hass, user_input)
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
            except (ValueError, TimeoutError, httpx.HTTPError, ExceptionGroup, McpError):
                errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema(entry.data), errors=errors
        )

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            try:
                await validate(self.hass, user_input)
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
            except (ValueError, TimeoutError, httpx.HTTPError, ExceptionGroup, McpError):
                errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema(entry.data), errors=errors
        )
