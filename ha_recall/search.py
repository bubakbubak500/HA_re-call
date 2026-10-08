"""Exact + FTS5 + optional HTTP embeddings, fused using reciprocal ranks."""

import asyncio
import hashlib
import math
import threading
from pathlib import Path

import httpx

from .models import is_current
from .store import MemoryError


class Embeddings:
    def __init__(self, url="", model="model2vec", api_key="", client=None):
        self.url = url
        self.model = model
        self.api_key = api_key
        self.client = client
        # A different endpoint with the same model label must invalidate the cache.
        self.cache_key = hashlib.sha256(f"{url}|{model}".encode()).hexdigest()

    async def encode(self, texts):
        if not self.url:
            raise ValueError("Embeddings are not configured")
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

        async def request(client):
            response = await client.post(
                self.url, json={"model": self.model, "input": texts}, headers=headers
            )
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda row: row["index"])
            if [row["index"] for row in data] != list(range(len(texts))):
                raise ValueError("Invalid embedding batch indexes")
            vectors = []
            for row in data:
                raw = row["embedding"]
                if not isinstance(raw, list) or not raw or len(raw) > 8192:
                    raise ValueError("Invalid embedding dimension")
                vector = [float(v) for v in raw]
                if not all(math.isfinite(v) for v in vector):
                    raise ValueError("Non-finite embedding")
                length = math.sqrt(sum(v * v for v in vector))
                if not length or not math.isfinite(length):
                    raise ValueError("Invalid embedding norm")
                vectors.append([v / length for v in vector])
            if len({len(v) for v in vectors}) > 1:
                raise ValueError("Inconsistent embedding dimensions")
            return vectors

        if self.client:
            return await request(self.client)
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            return await request(client)


class LocalEmbeddings:
    """Optional Model2Vec from an existing local directory; never downloads weights."""

    def __init__(self, directory):
        from model2vec import StaticModel

        path = Path(directory).resolve(strict=True)
        if not path.is_dir():
            raise ValueError("HA_RECALL_MODEL_PATH must be an existing local directory")
        digest = hashlib.sha256()
        for filename in ("config.json", "tokenizer.json", "model.safetensors"):
            with (path / filename).open("rb") as stream:
                digest.update(hashlib.file_digest(stream, "sha256").digest())
        self.cache_key = "model2vec:" + digest.hexdigest()
        self.url = "local-model2vec"
        self.lock = threading.Lock()
        self.model = StaticModel.from_pretrained(str(path), normalize=True)

    async def encode(self, texts):
        def run():
            with self.lock:
                result = self.model.encode(texts, show_progress_bar=False).tolist()
            if len(result) != len(texts) or not all(
                vector and all(math.isfinite(v) for v in vector) and any(vector)
                for vector in result
            ):
                raise ValueError("Invalid Model2Vec embeddings")
            return result

        return await asyncio.to_thread(run)


class Search:
    def __init__(self, store, embeddings):
        self.store, self.embeddings = store, embeddings
        self.index_lock = asyncio.Lock()

    async def reindex(self, namespace, limit=100):
        if not 1 <= limit <= 1000:
            raise MemoryError("invalid_limit")
        if not self.embeddings.url:
            return {"indexed": 0, "semantic": "disabled"}
        async with self.index_lock:
            candidates = self.store.embedding_candidates(
                namespace, self.embeddings.cache_key, limit
            )
            count = 0
            for start in range(0, len(candidates), 16):
                batch = candidates[start : start + 16]
                vectors = await self.embeddings.encode([self.store.text(r)[:24000] for r in batch])
                for record, vector in zip(batch, vectors, strict=True):
                    count += self.store.save_vector(record, self.embeddings.cache_key, vector)
            return {"indexed": count, "semantic": "ready"}

    async def search(self, namespace, query, limit=20, entity_type=None):
        lexical = self.store.lexical_search(namespace, query, 100)
        if not 1 <= limit <= 100:
            raise MemoryError("invalid_limit")
        semantic, state = [], "disabled"
        if self.embeddings.url:
            try:
                # Bounded self-healing; explicit reindex handles larger imports.
                await self.reindex(namespace, 32)
                qvec = (await self.embeddings.encode([query]))[0]
                for record, vector in self.store.vectors(namespace, self.embeddings.cache_key):
                    if len(vector) == len(qvec) and (
                        record["kind"] == "entity" or is_current(record["data"])
                    ):
                        score = sum(a * b for a, b in zip(vector, qvec, strict=True))
                        if score > 0:
                            semantic.append((score, record))
                semantic.sort(key=lambda pair: pair[0], reverse=True)
                state = "ready"
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                state = "unavailable"
        scores, records, reasons = {}, {}, {}
        for label, ranked in (("fulltext", lexical), ("semantic", [r for _, r in semantic[:100]])):
            for rank, record in enumerate(ranked):
                rid = record["id"]
                records[rid] = record
                scores[rid] = scores.get(rid, 0) + 1 / (60 + rank + 1)
                reasons.setdefault(rid, []).append(record.get("match", label))
                if record.get("match") == "exact":
                    scores[rid] += 1
        results = []
        for rid in sorted(scores, key=lambda key: (-scores[key], key)):
            record = records[rid]
            if entity_type:
                data = record["data"]
                ids = (
                    [record["id"]]
                    if record["kind"] == "entity"
                    else (
                        [data["entity_id"]]
                        if record["kind"] == "fact"
                        else [data["subject_id"], data["object_id"]]
                    )
                )
                if not any(
                    self.store.get(namespace, i)["data"]["entity_type"] == entity_type for i in ids
                ):
                    continue
            results.append({**record, "score": scores[rid], "matches": reasons[rid]})
            if len(results) == limit:
                break
        return {
            "results": results,
            "semantic": state,
            "hint": "Use entity_context to expand related entities and current facts.",
        }
