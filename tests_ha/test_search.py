import httpx
import pytest

from ha_recall.models import Entity
from ha_recall.search import Embeddings, Search
from ha_recall.store import Store


@pytest.fixture
def store():
    instance = Store(":memory:")
    yield instance
    instance.close()


async def test_semantic_match_without_lexical_overlap_and_scope(store):
    target = store.create_entity("home", Entity(name="žárovka"))
    store.create_entity("private", Entity(name="secret"))
    calls = []

    def handler(request):
        import json

        texts = json.loads(request.content)["input"]
        calls.extend(texts)
        return httpx.Response(
            200, json={"data": [{"index": i, "embedding": [1, 0, 0]} for i, _ in enumerate(texts)]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        search = Search(store, Embeddings("http://test/embeddings", client=client))
        result = await search.search("home", "osvětlení")
    assert result["semantic"] == "ready"
    assert result["results"][0]["id"] == target["id"]
    assert result["results"][0]["matches"] == ["semantic"]
    assert not any("secret" in text for text in calls)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, json={"data": []}),
        httpx.Response(200, json={"data": [{"index": 0, "embedding": [0, 0]}]}),
    ],
)
async def test_embedding_failure_preserves_fts(store, response):
    target = store.create_entity("home", Entity(name="EGLO"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)) as client:
        search = Search(store, Embeddings("http://test/embeddings", client=client))
        result = await search.search("home", "EGLO")
    assert result["semantic"] == "unavailable"
    assert result["results"][0]["id"] == target["id"]


def test_stale_vector_rejected_and_model_change_invalidates(store):
    entity = store.create_entity("home", Entity(name="old"))
    assert store.save_vector(entity, "model-a", [1, 0])
    assert store.embedding_candidates("home", "model-a") == []
    assert len(store.embedding_candidates("home", "model-b")) == 1
    store.update_entity("home", entity["id"], Entity(name="new"), 1)
    assert not store.save_vector(entity, "model-a", [1, 0])
    assert store.vectors("home", "model-a") == []


async def test_remote_model_rotation_reindexes_before_returning_results(store):
    import json

    entity = store.create_entity("home", Entity(name="lamp"))
    model = "first"

    def handler(request):
        texts = json.loads(request.content)["input"]
        return httpx.Response(
            200,
            json={
                "model": model,
                "data": [
                    {"index": i, "embedding": [1, 0] if model == "first" else [0, 1]}
                    for i in range(len(texts))
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = Embeddings("http://test", client=client)
        search = Search(store, provider)
        assert (await search.search("home", "lighting"))["results"][0]["id"] == entity["id"]
        original_key = provider.cache_key
        model = "second"
        assert (await search.search("home", "lighting"))["results"][0]["id"] == entity["id"]
        assert provider.cache_key != original_key
        assert store.vectors("home", original_key) == []


async def test_concurrent_deletion_does_not_return_stale_lexical_hit(store):
    entity = store.create_entity("home", Entity(name="lamp"))

    class DeletingProvider:
        url = "fake"
        cache_key = "test"

        async def encode(self, texts):
            if store.get("home", entity["id"], True)["status"] == "active":
                store.forget("home", entity["id"], 1)
            return [[1, 0] for _ in texts]

    result = await Search(store, DeletingProvider()).search("home", "lamp")
    assert result["results"] == []
