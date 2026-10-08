import ast
from pathlib import Path

import pytest
from fastmcp import Client

from ha_recall.access import Access, Principal, test_principal
from ha_recall.compat import Compatibility
from ha_recall.models import Entity, Fact, Relation
from ha_recall.search import Embeddings, Search
from ha_recall.server import Settings, create_server
from ha_recall.store import MemoryError, Store


@pytest.fixture
def compatibility():
    store = Store(":memory:")
    access = Access(store, ("home", "technical"))
    store.authorizer = access.require
    result = Compatibility(store, Search(store, Embeddings()), access)
    yield result
    store.close()


def tool_signatures(root):
    result = {}
    for path in root:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr == "tool"
                for d in node.decorator_list
            ):
                result[node.name] = ast.dump(node.args, include_attributes=False)
    return result


async def test_all_upstream_tools_live_and_signature_compatible():
    expected = tool_signatures(Path("upstream/src/tools").glob("*.py"))
    actual = tool_signatures([Path("ha_recall/compat_tools.py")])
    assert len(expected) == 39
    assert actual == expected
    store = Store(":memory:")
    try:
        mcp = create_server(Settings(token="x" * 40), store=store)
        async with Client(mcp) as client:
            tools = {t.name for t in await client.list_tools()}
            assert expected.keys() <= tools
            assert len(tools) == 55
            entity = (
                await client.call_tool(
                    "create_entity", {"namespace": "home", "entity": {"name": "Entity first"}}
                )
            ).data
            note = (await client.call_tool("read_note", {"note_id": entity["id"]})).data
            assert note["title"] == "Entity first"
            note = (
                await client.call_tool(
                    "create_note",
                    {"project_id": "home", "title": "Legacy note", "body": "Optional description"},
                )
            ).data
            record = (
                await client.call_tool("get_record", {"namespace": "home", "record_id": note["id"]})
            ).data
            assert record["data"]["description"] == "Optional description"
    finally:
        store.close()


async def test_note_lifecycle_links_versions_and_health(compatibility):
    call = compatibility.call
    project = await call("create_workspace", name="Test collection")
    pid = project["id"]
    assert (await call("rename_workspace", project_id=pid, name="Renamed"))["name"] == "Renamed"
    folder = await call("create_folder", project_id=pid, name="Devices")
    assert (await call("rename_folder", folder_id=folder["id"], name="Lights"))["name"] == "Lights"
    bulb = await call(
        "create_note",
        project_id=pid,
        title="EGLO lamp",
        folder_id=folder["id"],
        body="---\ntype: device\ntags: [light, zigbee]\nreview_every: 1d\nreviewed: 2000-01-01\n---\nPairing fixed by reset.",
    )
    room = await call(
        "create_note", project_id=pid, title="Living room", body="The EGLO lamp is here."
    )
    assert (await call("unlinked_mentions", note_id=bulb["id"]))["mentions"][0]["id"] == room["id"]
    linked = await call("link_mention", note_id=bulb["id"], source_note_id=room["id"])
    assert "[[EGLO lamp]]" in linked["body"]
    assert (await call("read_note", note_id=bulb["id"]))["backlinks"][0]["id"] == room["id"]
    assert (await call("graph", project_id=pid))["links"]
    await call("save_version", note_id=bulb["id"], label="working")
    revisions = (await call("list_revisions", note_id=bulb["id"]))["revisions"]
    assert revisions[0]["label"] == "working"
    previous = await call("read_revision", note_id=bulb["id"], revision_id=revisions[0]["id"])
    await call("update_note", note_id=bulb["id"], title="EGLO light", body="Changed")
    assert "[[EGLO light]]" in (await call("read_note", note_id=room["id"]))["body"]
    restored = await call("restore_revision", note_id=bulb["id"], revision_id=revisions[0]["id"])
    assert restored["body"] == previous["body"]
    assert (
        await call(
            "update_note", note_id=bulb["id"], body="stale", base_updated_at=bulb["updated_at"]
        )
    )["error"] == "conflict"
    assert (await call("grep", text="pairing", project_id=pid))["results"][0]["id"] == bulb["id"]
    assert (await call("search", query="reset", project_id=pid))["results"][0]["id"] == bulb["id"]
    assert (await call("query_notes", project_id=pid, type="device", tags=["zigbee"]))["notes"][0][
        "id"
    ] == bulb["id"]
    assert (await call("list_tags", project_id=pid))["tags"][0]["count"] == 1
    assert (await call("workspace_health", project_id=pid))["counts"]["overdue"] == 1
    assert (await call("mark_reviewed", note_id=bulb["id"]))["review"]["overdue"] is False
    assert (await call("preview_diagram", source="graph TD\n A --> B"))["ok"]
    await call("link_notes", note_id=bulb["id"], target_title="Missing note")
    assert (await call("workspace_health", project_id=pid))["counts"]["broken_links"] == 1
    assert "candidates" in await call("suggest_links", note_id=bulb["id"])
    assert len((await call("list_tree", project_id=pid))["notes"]) == 2
    assert len((await call("list_projects"))["projects"]) == 3


async def test_folder_copy_move_trash_restore_and_purge(compatibility):
    call = compatibility.call
    project = (await call("create_workspace", name="Move test"))["id"]
    parent = await call("create_folder", project_id=project, name="parent")
    child = await call("create_folder", project_id=project, name="child", parent_id=parent["id"])
    note = await call(
        "create_note", project_id=project, title="note", body="text", folder_id=child["id"]
    )
    fact = compatibility.store.add_fact(
        project, Fact(entity_id=note["id"], predicate="test", value="value", source="test")
    )
    assert (await call("move", item_type="folder", item_id=parent["id"], parent_id=child["id"]))[
        "error"
    ] == "invalid_move"
    copied = await call("copy", item_type="folder", item_id=parent["id"])
    assert copied["name"] == "parent (copy)"
    assert len(compatibility.store.list(project, "fact")) == 2
    assert (await call("move", item_type="folder", item_id=parent["id"], project_id="technical"))[
        "project_id"
    ] == "technical"
    assert compatibility.store.get("technical", fact["id"])["data"]["entity_id"] == note["id"]
    assert (await call("delete", item_type="folder", item_id=parent["id"]))["ok"]
    assert (await call("read_note", note_id=note["id"]))["error"]
    assert (await call("list_trash"))["folders"]
    assert (await call("restore", item_type="folder", item_id=parent["id"]))["ok"]
    assert (await call("read_note", note_id=note["id"]))["body"] == "text"
    assert (await call("purge", item_type="note", item_id=note["id"]))["error"] == "not_in_trash"
    await call("delete", item_type="note", item_id=note["id"])
    assert (await call("purge", item_type="note", item_id=note["id"]))["ok"]
    assert (
        compatibility.db.execute("SELECT 1 FROM records WHERE id=?", (fact["id"],)).fetchone()
        is None
    )
    await call("delete_workspace", project_id=project)
    assert (await call("list_trash"))["workspaces"]
    await call("restore", item_type="workspace", item_id=project)
    assert len((await call("list_tree", project_id=project))["notes"]) == 1
    await call("delete_workspace", project_id=project)
    assert (await call("purge", item_type="workspace", item_id=project))["ok"]


async def test_local_token_grants_are_enforced_and_invites_revocable(compatibility):
    call = compatibility.call
    note = await call("create_note", project_id="home", title="Private fixture")
    invited = await call("share_workspace", project_id="home", upn="viewer@local", role="viewer")
    assert invited["status"] == "invited"
    revoked = await call("share_workspace", project_id="home", upn="revoked@local", role="editor")
    assert (
        await call("revoke_invitation", project_id="home", invitation_id=revoked["invitation_id"])
    )["ok"]
    token = test_principal.set(Principal("viewer", "viewer@local"))
    try:
        assert (await call("read_note", note_id=note["id"]))["title"] == "Private fixture"
        assert (await call("update_note", note_id=note["id"], body="denied"))[
            "error"
        ] == "forbidden"
        assert (await call("list_tree", project_id="technical"))["error"] == "not_found"
        assert (await call("create_note", project_id="home", title="denied"))[
            "error"
        ] == "forbidden"
    finally:
        test_principal.reset(token)

    assert (await call("set_member_role", project_id="home", user_id="viewer", role="editor"))["ok"]
    assert len((await call("list_members", project_id="home"))["members"]) == 2
    token = test_principal.set(Principal("viewer", "viewer@local"))
    try:
        assert (await call("update_note", note_id=note["id"], body="allowed"))["body"] == "allowed"
        assert (await call("share_workspace", project_id="home", upn="third", role="editor"))[
            "error"
        ] == "forbidden"
    finally:
        test_principal.reset(token)
    assert (await call("remove_member", project_id="home", user_id="viewer"))["ok"]
    await call("set_workspace_access", project_id="technical", org_access="viewer")
    token = test_principal.set(Principal("viewer", "viewer@local"))
    try:
        assert (await call("read_note", note_id=note["id"]))["error"] == "not_found"
        assert (await call("list_org_workspaces"))["workspaces"][0]["id"] == "technical"
    finally:
        test_principal.reset(token)


async def test_native_edits_keep_wikilinks_and_folder_integrity(compatibility):
    call, store = compatibility.call, compatibility.store
    target = await call("create_note", project_id="home", title="Lamp")
    source = store.create_entity("home", Entity(name="Room", description="[[Lamp]]"))
    assert len(store.context("home", source["id"])["relations"]) == 1

    self_link = store.create_entity("home", Entity(name="Self", description="[[Self]]"))
    renamed = store.update_entity(
        "home", self_link["id"], Entity(name="Renamed", description="[[Self]]"), 1
    )
    assert renamed == store.get("home", self_link["id"])
    assert renamed["data"]["description"] == "[[Renamed]]"
    store.update_entity("home", target["id"], Entity(name="Light"), 1)
    assert (await call("read_note", note_id=source["id"]))["body"] == "[[Light]]"
    folder = await call("create_folder", project_id="technical", name="Other collection")
    with pytest.raises(MemoryError):
        store.create_entity(
            "home", Entity(name="Invalid parent", document={"folder_id": folder["id"]})
        )
    store.forget("home", target["id"], 2)
    assert store.context("home", source["id"])["relations"] == []
    store.restore("home", target["id"], 2, 3)
    assert len(store.context("home", source["id"])["relations"]) == 1


async def test_moves_ignore_deleted_boundaries_but_reject_live_native_edges(compatibility):
    call, store = compatibility.call, compatibility.store
    a = await call("create_note", project_id="home", title="A")
    b = await call("create_note", project_id="home", title="B", body="[[A]]")
    edge = store.add_relation(
        "home", Relation(subject_id=a["id"], object_id=b["id"], predicate="uses", source="user")
    )
    assert (
        "cross_namespace_relation"
        in (await call("move", item_type="note", item_id=a["id"], project_id="technical"))["error"]
    )
    store.forget("home", edge["id"], 1)
    assert (await call("move", item_type="note", item_id=a["id"], project_id="technical"))[
        "project_id"
    ] == "technical"
    assert store.context("home", b["id"])["relations"] == []


async def test_purge_folder_includes_preexisting_nested_trash(compatibility):
    call = compatibility.call
    parent = await call("create_folder", project_id="home", name="parent")
    child = await call("create_folder", project_id="home", parent_id=parent["id"], name="child")
    note = await call("create_note", project_id="home", title="old trash", folder_id=child["id"])
    await call("delete", item_type="folder", item_id=child["id"])
    await call("delete", item_type="folder", item_id=parent["id"])
    assert (await call("purge", item_type="folder", item_id=parent["id"]))["ok"]
    for rid in (parent["id"], child["id"], note["id"]):
        assert (
            compatibility.db.execute("SELECT 1 FROM records WHERE id=?", (rid,)).fetchone() is None
        )


async def test_existing_v01_database_keeps_records_and_history(tmp_path):
    import json

    path = str(tmp_path / "v01.sqlite3")
    original = Store(path)
    entity = original.create_entity("home", Entity(name="Existing", external_id="stable"))
    old_data = dict(entity["data"])
    old_data.pop("document")
    with original.transaction():
        original.db.execute(
            "UPDATE records SET data=? WHERE id=?", (json.dumps(old_data), entity["id"])
        )
    history = original.history("home", entity["id"])
    original.close()
    upgraded = Store(path)
    try:
        api = create_server(Settings(token="test" * 10), store=upgraded)
        async with Client(api) as client:
            assert (await client.call_tool("read_note", {"note_id": entity["id"]})).data[
                "title"
            ] == "Existing"
            repeated = await client.call_tool(
                "create_entity",
                {"namespace": "home", "entity": {"name": "Existing", "external_id": "stable"}},
            )
            assert repeated.data["id"] == entity["id"]
        assert upgraded.history("home", entity["id"]) == history
    finally:
        upgraded.close()


async def test_folder_restore_does_not_duplicate_wikilink_relations(compatibility):
    call = compatibility.call
    folder = await call("create_folder", project_id="home", name="Linked notes")
    a = await call("create_note", project_id="home", title="A", folder_id=folder["id"])
    await call("create_note", project_id="home", title="B", body="[[A]]", folder_id=folder["id"])
    await call("delete", item_type="folder", item_id=folder["id"])
    assert (await call("restore", item_type="folder", item_id=folder["id"]))["ok"]
    assert len(compatibility.store.context("home", a["id"])["relations"]) == 1
