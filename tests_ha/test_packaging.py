import json
import zipfile

import pytest

from ha_recall.models import Entity
from ha_recall.store import Store
from tools.backup import backup
from tools.package_addon import build


def test_addon_contains_only_required_sources(tmp_path):
    archive = build(tmp_path / "addon.zip")
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert {
            "ha_recall/config.json",
            "ha_recall/Dockerfile",
            "ha_recall/run.py",
            "ha_recall/ha_recall/server.py",
            "ha_recall/requirements-ha.txt",
        } <= set(names)
        assert not any(
            "upstream/" in name or ".token" in name or ".sqlite" in name or ".env" in name
            for name in names
        )
        assert json.loads(bundle.read("ha_recall/config.json"))["options"]["token"] == ""


def test_backup_live_wal_and_no_overwrite(tmp_path):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    store = Store(str(source))
    try:
        entity = store.create_entity("home", Entity(name="Before backup"))
        backup(source, destination)
        with pytest.raises(FileExistsError):
            backup(source, destination)
        restored = Store(str(destination))
        try:
            assert restored.get("home", entity["id"]) == entity
            assert restored.history("home", entity["id"]) == store.history("home", entity["id"])
        finally:
            restored.close()
    finally:
        store.close()
