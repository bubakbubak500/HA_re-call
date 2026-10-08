"""Authenticated, bounded access to Jarvis's existing model. No second model loaded."""

import asyncio
import hashlib
import json
from pathlib import Path

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN


class EmbeddingView(HomeAssistantView):
    url = "/api/ha_recall/embeddings"
    name = "api:ha_recall:embeddings"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass
        self.lock = asyncio.Lock()

    def _encode(self, matcher, texts):
        model = matcher.model
        # max_length is part of the model identity. No intent-routing preprocessing.
        vectors = model.encode(
            texts, show_progress_bar=False, use_multiprocessing=False, max_length=512
        )
        manifest = Path(
            self.hass.config.path("custom_components/jarvis_semantic/model_manifest.json")
        )
        version = (
            hashlib.sha256(manifest.read_bytes()).hexdigest()
            if manifest.is_file()
            else f"loaded-{id(model)}"
        )
        return vectors.tolist(), f"jarvis-model2vec:{version}:raw-512-v1"

    async def post(self, request):
        enabled = any(
            getattr(value, "entry", None) and value.entry.data.get("embedding_bridge", False)
            for value in self.hass.data.get(DOMAIN, {}).values()
        )
        if not enabled:
            return web.json_response({"error": "embedding_bridge_disabled"}, status=503)
        if request.content_length is not None and request.content_length > 256000:
            return web.json_response({"error": "request_too_large"}, status=413)
        # Bound streamed/chunked requests too, before JSON parsing.
        raw = bytearray()
        async for chunk in request.content.iter_chunked(8192):
            raw.extend(chunk)
            if len(raw) > 256000:
                return web.json_response({"error": "request_too_large"}, status=413)
        try:
            body = json.loads(raw)
            texts = body["input"]
            if not isinstance(texts, list) or not 1 <= len(texts) <= 16:
                raise ValueError
            if not all(isinstance(text, str) and 0 < len(text) <= 24000 for text in texts):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "invalid_embedding_input"}, status=400)
        matchers = [
            matcher
            for matcher in self.hass.data.get("jarvis_semantic", {}).values()
            if hasattr(matcher, "model")
        ]
        if len(matchers) != 1:
            return web.json_response({"error": "jarvis_model_unavailable_or_ambiguous"}, status=503)
        if self.lock.locked():
            return web.json_response({"error": "embedding_busy"}, status=429)
        async with self.lock:
            try:
                vectors, model = await self.hass.async_add_executor_job(
                    self._encode, matchers[0], texts
                )
            except Exception:
                return web.json_response({"error": "embedding_failed"}, status=503)
        return web.json_response(
            {
                "object": "list",
                "model": model,
                "data": [
                    {"object": "embedding", "index": i, "embedding": vector}
                    for i, vector in enumerate(vectors)
                ],
            }
        )
