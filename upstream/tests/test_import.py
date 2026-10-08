"""Folder-tree import: the inverse of the zip export.

Covers the untrusted-input parsing in src/importer.py (paths, limits, titles,
the single-root unwrap) and the database + REST side (`data.import_tree`,
`POST /api/projects/{id}/import`): structure, links, permissions, and an
export → import round trip.
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest
from starlette.requests import Request

from src import config, data, importer


def _zip(files: dict[str, str | None]) -> bytes:
    """A zip from {path: body}; a None body adds a directory entry."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, body in files.items():
            if body is None:
                z.writestr(path.rstrip("/") + "/", "")
            else:
                z.writestr(path, body)
    return buf.getvalue()


def _paths(tree: importer.ImportTree) -> set[str]:
    return {"/".join([*n.folder, n.title]) for n in tree.notes}


# ── Parsing (pure) ──────────────────────────────────────────


def test_zip_rebuilds_folders_and_notes():
    tree = importer.parse_zip(_zip({
        "Inbox.md": "hello",
        "Projects/Alpha.md": "# A",
        "Projects/Archive/Old plan.md": "old",
        "Empty/": None,
    }))
    assert _paths(tree) == {"Inbox", "Projects/Alpha", "Projects/Archive/Old plan"}
    assert tree.folders == {("Projects",), ("Projects", "Archive"), ("Empty",)}
    assert tree.skipped == 0


def test_title_prefers_frontmatter_then_filename_not_h1():
    tree = importer.parse_zip(_zip({
        "a.md": "---\ntitle: Real Title\n---\nbody",
        "Plain name.md": "# A different heading\nbody",
    }))
    assert {n.title for n in tree.notes} == {"Real Title", "Plain name"}


def test_unsafe_and_unsupported_files_are_skipped_clutter_is_not_counted():
    tree = importer.parse_zip(_zip({
        "../escape.md": "x",
        "/abs.md": "x",
        "C:/drive.md": "x",
        "photo.png": "binary",
        ".obsidian/workspace.json": "{}",
        "__MACOSX/._a.md": "x",
        "Notes/.DS_Store": "x",
        "ok.md": "fine",
    }))
    assert _paths(tree) == {"ok"}
    assert tree.skipped == 4  # 3 unsafe paths + the png; clutter ignored
    assert tree.folders == set()


def test_mixed_content_zip_imports_only_notes():
    # A realistic export from elsewhere: notes next to attachments and docs.
    tree = importer.parse_zip(_zip({
        "Readme.MD": "upper-case extension",
        "Guide.markdown": "long extension",
        "log.txt": "plain text",
        "Projects/": None,
        "Projects/Plan.md": "plan",
        "Projects/spec.pdf": "%PDF-1.7",
        "Projects/budget.xlsx": "PK..",
        "Projects/notes.docx": "PK..",
        "Attachments/": None,
        "Attachments/photo.png": "\x89PNG",
        "Attachments/diagram.svg": "<svg/>",
        "Data/export.csv": "a,b",
        "Empty/": None,
    }))
    assert _paths(tree) == {"Readme", "Guide", "log", "Projects/Plan"}
    assert tree.skipped == 6  # pdf, xlsx, docx, png, svg, csv
    # Projects keeps its note; Attachments/Data held only skipped files and
    # would arrive empty, so they're left out; Empty was empty in the zip.
    assert tree.folders == {("Projects",), ("Empty",)}


def test_mixed_content_items_form_drops_attachment_only_folders():
    tree = importer.parse_items([
        {"path": "Attachments", "body": None},
        {"path": "Attachments/photo.png", "skip": True},  # browser didn't read it
        {"path": "Notes/huge.md", "skip": True},  # over the per-file cap
        {"path": "Notes/a.md", "body": "a"},
    ])
    assert _paths(tree) == {"Notes/a"}
    assert tree.folders == {("Notes",)}
    assert tree.skipped == 2


def test_single_root_named_like_the_zip_is_unwrapped():
    tree = importer.parse_zip(_zip({"Notes/a.md": "a", "Notes/sub/b.md": "b"}), root_name="Notes")
    assert _paths(tree) == {"a", "sub/b"}
    assert tree.folders == {("sub",)}


def test_single_root_with_a_different_name_is_real_structure():
    # A recall export of a workspace whose only content is one folder.
    tree = importer.parse_zip(_zip({"Projects/a.md": "a"}), root_name="Personal")
    assert _paths(tree) == {"Projects/a"}
    assert tree.folders == {("Projects",)}


def test_rejects_non_zip_and_limits(monkeypatch):
    with pytest.raises(importer.ImportRejected):
        importer.parse_zip(b"not a zip at all")
    monkeypatch.setattr(importer, "MAX_FILES", 2)
    with pytest.raises(importer.ImportRejected):
        importer.parse_zip(_zip({"a.md": "a", "b.md": "b", "c.md": "c"}))


def test_declared_size_is_checked_before_decompressing(monkeypatch):
    # A highly compressible "bomb": tiny zip, large declared size.
    monkeypatch.setattr(importer, "MAX_TOTAL_BYTES", 1000)
    with pytest.raises(importer.ImportRejected):
        importer.parse_zip(_zip({"big.md": "x" * 5000}))


def test_oversized_single_file_is_skipped(monkeypatch):
    monkeypatch.setattr(importer, "MAX_FILE_BYTES", 10)
    tree = importer.parse_zip(_zip({"big.md": "x" * 50, "small.md": "ok"}))
    assert _paths(tree) == {"small"}
    assert tree.skipped == 1


def test_items_form_keeps_directories_and_bodies():
    tree = importer.parse_items([
        {"path": "Sub/note.md", "body": "hi"},
        {"path": "Empty", "body": None},
        {"path": "../bad.md", "body": "x"},
        "garbage",
    ])
    assert _paths(tree) == {"Sub/note"}
    assert tree.folders == {("Sub",), ("Empty",)}
    assert tree.skipped == 1


# ── REST + database ─────────────────────────────────────────


def _request(project_id: str, user: data.User, body: bytes, ctype: str,
             **query: str) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    qs = "&".join(f"{k}={v}" for k, v in query.items()).encode()
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/projects/{project_id}/import",
            "path_params": {"project_id": project_id},
            "query_string": qs,
            "headers": [
                (b"x-user-id", user.external_id.encode()),
                (b"x-user-upn", user.upn.encode()),
                (b"x-bff-secret", config.BFF_SHARED_SECRET.encode()),
                (b"content-type", ctype.encode()),
                (b"content-length", str(len(body)).encode()),
            ],
        },
        receive,
    )


async def _import(project, user, body, ctype="application/zip", **query):
    from src import server

    resp = await server.api_import_tree(_request(project.id, user, body, ctype, **query))
    return resp.status_code, json.loads(resp.body)


async def test_import_zip_into_named_folder_with_links(make_user, make_project):
    owner = await make_user()
    proj = await make_project(owner)
    status, out = await _import(proj, owner, _zip({
        # B links to A, which comes later in the zip: still resolves.
        "Projects/B.md": "see [[A]]",
        "Projects/Deep/A.md": "target",
        "Top.md": "top",
    }), name="Imported")
    assert status == 201, out
    assert out["notes"] == 3 and out["folders"] == 3 and out["skipped"] == 0

    tree = await data.get_export_tree(proj.id, out["folder_id"])
    assert {i["path"] for i in tree["items"]} == {
        "Projects/B.md", "Projects/Deep/A.md", "Top.md",
    }
    pool = await data.get_pool()
    unresolved = await pool.fetchval(
        "SELECT count(*) FROM note_links l JOIN notes n ON n.id = l.source_note_id "
        "WHERE n.project_id = $1::uuid AND l.target_note_id IS NULL", proj.id,
    )
    assert unresolved == 0


async def test_export_import_round_trip(make_user, make_project):
    owner = await make_user()
    src_proj = await make_project(owner, "Source")
    f = await data.create_folder(src_proj.id, None, "Projects", owner.id)
    sub = await data.create_folder(src_proj.id, f.id, "Archive", owner.id)
    await data.create_note(src_proj.id, "Inbox", "---\ntags: [x]\n---\nhi", owner.id)
    await data.create_note(src_proj.id, "Alpha", "links [[Inbox]]", owner.id, f.id)
    await data.create_note(src_proj.id, "Old plan", "old", owner.id, sub.id)
    exported = await data.get_export_tree(src_proj.id, None)
    zipped = _zip({i["path"]: i["body"] for i in exported["items"]})

    dst = await make_project(owner, "Destination")
    status, out = await _import(dst, owner, zipped)  # straight into the root
    assert status == 201, out
    again = await data.get_export_tree(dst.id, None)
    assert sorted((i["path"], i["body"]) for i in again["items"]) == sorted(
        (i["path"], i["body"]) for i in exported["items"]
    )
    inbox = [n for n in await data.list_notes(dst.id) if n["title"] == "Inbox"]
    assert inbox and inbox[0]["tags"] == ["x"]


async def test_import_json_items_into_folder(make_user, make_project):
    owner = await make_user()
    proj = await make_project(owner)
    target = await data.create_folder(proj.id, None, "Target", owner.id)
    body = json.dumps({"items": [
        {"path": "a.md", "body": "a"}, {"path": "Sub/b.md", "body": "b"},
    ]}).encode()
    status, out = await _import(proj, owner, body, "application/json",
                                folder_id=target.id, name="Dropped")
    assert status == 201, out
    tree = await data.get_export_tree(proj.id, target.id)
    assert {i["path"] for i in tree["items"]} == {"Dropped/a.md", "Dropped/Sub/b.md"}


async def test_viewer_cannot_import(make_user, make_project, add_member):
    owner, viewer = await make_user(), await make_user()
    proj = await make_project(owner)
    await add_member(proj, viewer, "viewer")
    status, _ = await _import(proj, viewer, _zip({"a.md": "a"}))
    assert status == 403


async def test_bad_target_empty_and_oversized_uploads(make_user, make_project, monkeypatch):
    owner = await make_user()
    proj = await make_project(owner)
    other = await make_project(owner, "Other")
    foreign = await data.create_folder(other.id, None, "Elsewhere", owner.id)

    status, _ = await _import(proj, owner, _zip({"a.md": "a"}), folder_id=foreign.id)
    assert status == 400  # folder from another workspace
    status, _ = await _import(proj, owner, _zip({"a.md": "a"}), folder_id="not-a-uuid")
    assert status == 400
    status, out = await _import(proj, owner, _zip({"photo.png": "x"}))
    assert status == 400 and "Nothing to import" in out["error"]
    monkeypatch.setattr(importer, "MAX_UPLOAD_BYTES", 10)
    status, _ = await _import(proj, owner, _zip({"a.md": "a" * 100}))
    assert status == 413
    # Nothing was created by any of the refused imports.
    assert await data.get_export_tree(proj.id, None) == {"name": "Workspace", "items": []}
