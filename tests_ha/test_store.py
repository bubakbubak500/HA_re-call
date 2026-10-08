import concurrent.futures

import pytest

from ha_recall.models import Entity, Fact, Relation
from ha_recall.store import MemoryError, Store


@pytest.fixture
def store(tmp_path):
    instance = Store(str(tmp_path / "memory.db"))
    yield instance
    instance.close()


@pytest.fixture
def bulb(store):
    return store.create_entity(
        "home",
        Entity(
            name="EGLO žárovka",
            entity_type="device",
            external_id="light.eglo_living_room",
            aliases=["lampa"],
        ),
    )


def test_identity_and_cross_namespace(store, bulb):
    assert store.create_entity("home", Entity(**bulb["data"])) == bulb
    with pytest.raises(MemoryError, match="identity_exists"):
        store.create_entity("home", Entity(name="other", external_id=bulb["data"]["external_id"]))
    with pytest.raises(MemoryError, match="not_found"):
        store.get("private", bulb["id"])
    private = store.create_entity("private", Entity(name="private"))
    with pytest.raises(MemoryError, match="not_found"):
        store.add_relation(
            "home",
            Relation(
                subject_id=bulb["id"], object_id=private["id"], predicate="near", source="user"
            ),
        )


def test_conflict_and_history(store, bulb):
    fact = Fact(entity_id=bulb["id"], predicate="evening_temperature", value="2700K", source="user")
    old = store.add_fact("home", fact)
    new = store.add_fact("home", fact.model_copy(update={"value": "3000K"}))
    assert new["status"] == "pending" and new["conflicts"] == [old["id"]]
    assert store.context("home", bulb["id"])["facts"][0]["id"] == old["id"]
    accepted = store.resolve_fact("home", new["id"], True, 1)
    assert accepted["status"] == "active"
    assert store.get("home", old["id"], True)["status"] == "superseded"
    assert [r["action"] for r in store.history("home", new["id"])] == ["accept", "propose"]
    restored = store.restore("home", old["id"], 1, 2)
    assert restored["status"] == "pending"
    assert len(store.context("home", bulb["id"])["facts"]) == 1


def test_time_and_observations(store, bulb):
    old = Fact(
        entity_id=bulb["id"],
        predicate="color",
        value="red",
        source="user",
        valid_until="2001-01-01T00:00:00Z",
    )
    store.add_fact("home", old)
    current = old.model_copy(update={"value": "green", "valid_until": None})
    # Overlap in historical time still needs explicit resolution.
    assert store.add_fact("home", current)["status"] == "pending"
    assert store.context("home", bulb["id"])["facts"] == []
    for value in ("pairing reset", "warm white preferred"):
        assert (
            store.add_fact(
                "home",
                Fact(
                    entity_id=bulb["id"],
                    predicate="observation",
                    value=value,
                    source="user",
                    exclusive=False,
                ),
            )["status"]
            == "active"
        )
    assert len(store.context("home", bulb["id"])["facts"]) == 2
    assert store.lexical_search("home", "red") == []


def test_soft_delete_restore_and_persistence(store, bulb):
    fact = store.add_fact(
        "home", Fact(entity_id=bulb["id"], predicate="issue", value="pairing reset", source="user")
    )
    deleted = store.forget("home", bulb["id"], 1)
    assert deleted["status"] == "deleted"
    assert store.lexical_search("home", "pairing") == []
    with pytest.raises(MemoryError):
        store.restore("home", fact["id"], 1, 2)
    restored = store.restore("home", bulb["id"], 1, 2)
    assert restored["revision"] == 3
    assert store.context("home", bulb["id"])["facts"] == []
    store.restore("home", fact["id"], 1, 2)
    assert len(store.context("home", bulb["id"])["facts"]) == 1


def test_optimistic_concurrency_and_atomic_rollback(store, bulb):
    def edit(name):
        try:
            return store.update_entity("home", bulb["id"], Entity(name=name), 1)["data"]["name"]
        except MemoryError:
            return "conflict"

    with concurrent.futures.ThreadPoolExecutor() as pool:
        results = list(pool.map(edit, ["first", "second"]))
    assert results.count("conflict") == 1
    assert len(store.history("home", bulb["id"])) == 2
    before = store.fingerprint("home")
    with pytest.raises(RuntimeError), store.transaction():
        store._save("home", "entity", Entity(name="rollback").model_dump(), "test", "create")
        raise RuntimeError("abort")
    assert before == store.fingerprint("home")


def test_fts_and_graph(store, bulb):
    room = store.create_entity("home", Entity(name="Obývák", entity_type="area"))
    store.add_relation(
        "home",
        Relation(
            subject_id=bulb["id"], predicate="located_in", object_id=room["id"], source="registry"
        ),
    )
    for query in ("light.eglo_living_room", "lampa", "zarovka"):
        assert store.lexical_search("home", query)[0]["id"] == bulb["id"]
    assert store.context("home", room["id"])["neighbors"][0]["id"] == bulb["id"]
    assert store.lexical_search("private", "EGLO") == []
    assert store.lexical_search("home", '" OR * --') == []


def test_disk_restart(tmp_path):
    path = str(tmp_path / "persistent.db")
    first = Store(path)
    entity = first.create_entity("home", Entity(name="Persistent"))
    first.close()
    second = Store(path)
    assert second.get("home", entity["id"])["data"]["name"] == "Persistent"
    assert len(second.history("home", entity["id"])) == 1
    second.close()
