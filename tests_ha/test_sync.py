import pytest

from ha_recall.ha_sync import Snapshot, import_snapshot
from ha_recall.models import Entity
from ha_recall.store import Store


def snapshot(area="living", entity_id="light.eglo"):
    return Snapshot.model_validate(
        {
            "areas": [
                {"area_id": "living", "name": "Obývák"},
                {"area_id": "office", "name": "Pracovna"},
            ],
            "devices": [{"id": "device-1", "name": "EGLO", "area_id": area}],
            "entities": [
                {
                    "id": "registry-1",
                    "entity_id": entity_id,
                    "device_id": "device-1",
                    "state": "on",
                    "attributes": {"brightness": 123},
                }
            ],
        }
    )


def test_sync_idempotence_rename_move_and_no_live_states():
    store = Store(":memory:")
    try:
        first = import_snapshot(store, "home", "house", snapshot())
        assert first["references"] == 4
        before = store.fingerprint("home")
        assert import_snapshot(store, "home", "house", snapshot())["changed"] == 0
        assert before == store.fingerprint("home")
        original = store.lexical_search("home", "light.eglo")[0]
        import_snapshot(store, "home", "house", snapshot("office", "light.renamed"))
        renamed = store.lexical_search("home", "light.renamed")[0]
        assert original["id"] == renamed["id"]
        assert "state" not in renamed["data"] and "attributes" not in renamed["data"]
        assert "light.eglo" in renamed["data"]["aliases"]
        context = store.context("home", renamed["id"])
        assert any(r["data"]["name"] == "Pracovna" for r in context["neighbors"])
        assert not any(r["data"]["name"] == "Obývák" for r in context["neighbors"])
        store.forget("home", renamed["id"], renamed["revision"])
        assert import_snapshot(store, "home", "house", snapshot())["skipped_deleted"] == 1
    finally:
        store.close()


def test_sync_rolls_back_entire_snapshot_on_failure():
    store = Store(":memory:")
    try:
        existing = store.create_entity(
            "home", Entity(name="old", external_id="ha:house:device:device-1")
        )
        # Corruption/invalid old metadata fails validation at the import boundary
        # before any accepted import can partially commit.
        before = store.fingerprint("home")
        invalid = snapshot()
        invalid.areas.append(invalid.areas[0])
        with pytest.raises(ValueError, match="Duplicate"):
            import_snapshot(store, "home", "house", invalid)
        assert store.fingerprint("home") == before
        assert store.get("home", existing["id"])["revision"] == 1
    finally:
        store.close()
