"""Original re:call operations backed by the same SQLite entity graph.

Notes are entity descriptions, not a second knowledge store. Collections, folders,
local principal grants and audit events are small compatibility projections.
"""

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from pydantic import ValidationError

from . import guide, markdown
from .access import principal
from .mermaid import validate_mermaid
from .models import Entity, Relation, is_current, now
from .store import MemoryError, dump


class Compatibility:
    def __init__(self, store, search, access):
        self.store, self.search, self.access = store, search, access
        self.db = store.db
        store.entity_changed = self.entity_changed

    def entity_changed(self, namespace, before, after):
        if (
            before
            and after["status"] == "active"
            and before["data"]["name"] != after["data"]["name"]
        ):
            self._rename_links(namespace, before["data"]["name"], after["data"]["name"])
        self._rebuild_links(namespace)

    def _rename_links(self, namespace, old_title, new_title):
        if set(new_title) & markdown.UNSAFE_WIKILINK_TITLE_CHARS:
            return
        for source in self._records(namespace):
            rewritten, count = markdown.rewrite_wikilink_target(
                source["data"]["description"], old_title, new_title
            )
            if count:
                self.store._save(
                    namespace,
                    "entity",
                    {**source["data"], "description": rewritten},
                    principal().id,
                    "rename_link",
                    source,
                )

    async def call(self, operation, **arguments):
        try:
            with self.store.transaction():
                self.access.provision()
            if operation in ("search", "suggest_links"):
                return await getattr(self, "_" + operation)(**arguments)
            with self.store.transaction():
                result = getattr(self, "_" + operation)(**arguments)
            if operation in ("create_note", "update_note") and "error" not in result:
                try:
                    result["link_candidates"] = (await self._suggest_links(result["id"]))[
                        "candidates"
                    ]
                except MemoryError:
                    # The write already committed; an optional suggestion must not report it failed.
                    result["link_candidates"] = []
            return result
        except (MemoryError, ValidationError) as exc:
            return {"error": str(exc) if isinstance(exc, MemoryError) else "invalid_arguments"}
        except sqlite3.IntegrityError:
            return {"error": "conflict"}

    def _event(self, project, action, detail):
        self.db.execute(
            "INSERT INTO compatibility_events(project_id,actor,action,detail,at) VALUES (?,?,?,?,?)",
            (project, principal().id, action, dump(detail), now()),
        )

    def _project(self, project_id, include_deleted=False):
        self.access.require(project_id, include_deleted=include_deleted)
        row = self.db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return {
            "id": row["id"],
            "name": row["name"],
            "slug": row["id"],
            "owner_id": row["owner_id"],
            "role": self.access.role(project_id, include_deleted),
            "is_personal": bool(row["personal"]),
            "org_access": row["org_access"],
            "updated_at": row["updated_at"],
            "url": None,
            "member_count": self.db.execute(
                "SELECT COUNT(*) FROM memberships WHERE project_id=?", (project_id,)
            ).fetchone()[0],
        }

    def _list_projects(self):
        return {"projects": [self._project(p) for p in self.access.projects()]}

    def _create_workspace(self, name):
        if not name.strip() or len(name) > 256:
            raise MemoryError("name_required")
        namespace = markdown.slugify(name)[:48] or "workspace"
        if not namespace[0].isalpha() or not namespace[0].isascii():
            namespace = "workspace-" + uuid4().hex[:8]
        while self.db.execute(
            "SELECT 1 FROM projects WHERE id=? UNION SELECT 1 FROM records WHERE namespace=?",
            (namespace, namespace),
        ).fetchone():
            namespace = namespace[:48] + "-" + uuid4().hex[:8]
        self.db.execute(
            "INSERT INTO projects VALUES (?,?,?,'none',0,0,?)",
            (namespace, name.strip(), principal().id, now()),
        )
        self.db.execute(
            "INSERT INTO memberships VALUES (?,?, 'owner')", (namespace, principal().id)
        )
        self._event(namespace, "create_workspace", {"name": name})
        return self._project(namespace)

    def _rename_workspace(self, project_id, name):
        self.access.require(project_id, owner=True)
        if not name.strip() or len(name) > 256:
            raise MemoryError("name_required")
        self.db.execute(
            "UPDATE projects SET name=?,updated_at=? WHERE id=?", (name.strip(), now(), project_id)
        )
        self._event(project_id, "rename_workspace", {"name": name})
        return self._project(project_id)

    def _set_workspace_access(self, project_id, org_access):
        self.access.require(project_id, owner=True)
        if org_access not in ("none", "viewer"):
            raise MemoryError("invalid_access")
        self.db.execute(
            "UPDATE projects SET org_access=?,updated_at=? WHERE id=?",
            (org_access, now(), project_id),
        )
        self._event(project_id, "set_workspace_access", {"org_access": org_access})
        return self._project(project_id)

    def _list_org_workspaces(self):
        projects = [
            self._project(p)
            for p in self.access.projects()
            if not self.db.execute(
                "SELECT 1 FROM memberships WHERE project_id=? AND user_id=?", (p, principal().id)
            ).fetchone()
        ]
        return {"workspaces": projects}

    def _list_members(self, project_id):
        self.access.require(project_id, include_deleted=True)
        members = [
            {"user_id": r["id"], "upn": r["upn"], "display_name": r["upn"], "role": r["role"]}
            for r in self.db.execute(
                "SELECT u.*,m.role FROM memberships m JOIN principals u ON m.user_id=u.id WHERE m.project_id=?",
                (project_id,),
            )
        ]
        invitations = [
            dict(r)
            for r in self.db.execute("SELECT * FROM invitations WHERE project_id=?", (project_id,))
        ]
        return {
            "members": members,
            "invitations": invitations,
            "your_role": self.access.role(project_id, True),
        }

    def _share_workspace(self, project_id, upn, role="viewer"):
        self.access.require(project_id, owner=True)
        if role not in ("viewer", "editor") or not upn.strip() or len(upn) > 256:
            raise MemoryError("invalid_member")
        upn = upn.strip().casefold()
        user = self.db.execute("SELECT id FROM principals WHERE upn=?", (upn,)).fetchone()
        if user:
            if self.db.execute(
                "SELECT 1 FROM memberships WHERE project_id=? AND user_id=? AND role='owner'",
                (project_id, user[0]),
            ).fetchone():
                raise MemoryError("cannot_change_owner")
            self.db.execute(
                "INSERT OR REPLACE INTO memberships VALUES (?,?,?)", (project_id, user[0], role)
            )
            result = {"ok": True, "status": "member", "user_id": user[0]}
        else:
            iid = str(uuid4())
            self.db.execute(
                "INSERT INTO invitations VALUES (?,?,?,?) ON CONFLICT(project_id,upn) DO UPDATE SET role=excluded.role",
                (iid, project_id, upn, role),
            )
            invitation = self.db.execute(
                "SELECT id FROM invitations WHERE project_id=? AND upn=?", (project_id, upn)
            ).fetchone()
            result = {"ok": True, "status": "invited", "invitation_id": invitation[0]}
        self._event(project_id, "share_workspace", {"upn": upn, "role": role})
        return result

    def _set_member_role(self, project_id, user_id, role):
        self.access.require(project_id, owner=True)
        if role not in ("viewer", "editor"):
            raise MemoryError("invalid_role")
        row = self.db.execute(
            "SELECT role FROM memberships WHERE project_id=? AND user_id=?", (project_id, user_id)
        ).fetchone()
        if not row or row[0] == "owner":
            raise MemoryError("cannot_change_owner" if row else "not_found")
        self.db.execute(
            "UPDATE memberships SET role=? WHERE project_id=? AND user_id=?",
            (role, project_id, user_id),
        )
        self._event(project_id, "set_member_role", {"user_id": user_id, "role": role})
        return {"ok": True}

    def _remove_member(self, project_id, user_id):
        self.access.require(project_id, owner=user_id != principal().id)
        row = self.db.execute(
            "SELECT role FROM memberships WHERE project_id=? AND user_id=?", (project_id, user_id)
        ).fetchone()
        if not row or row[0] == "owner":
            raise MemoryError("cannot_remove")
        self.db.execute(
            "DELETE FROM memberships WHERE project_id=? AND user_id=?", (project_id, user_id)
        )
        self._event(project_id, "remove_member", {"user_id": user_id})
        return {"ok": True}

    def _revoke_invitation(self, project_id, invitation_id):
        self.access.require(project_id, owner=True)
        if not self.db.execute(
            "DELETE FROM invitations WHERE project_id=? AND id=?", (project_id, invitation_id)
        ).rowcount:
            raise MemoryError("not_found")
        self._event(project_id, "revoke_invitation", {"id": invitation_id})
        return {"ok": True}

    def _records(self, project, kind="entity", status="active"):
        return [
            self.store._get(project, r[0], True)
            for r in self.db.execute(
                "SELECT id FROM records WHERE namespace=? AND kind=? AND status=? ORDER BY updated_at DESC,id",
                (project, kind, status),
            )
        ]

    def _note(self, record):
        data = record["data"]
        doc = data.get("document") or {}
        metadata = markdown.project_metadata(markdown.parse_frontmatter(data["description"])[0])
        first = self.db.execute(
            "SELECT actor FROM history WHERE record_id=? ORDER BY revision LIMIT 1", (record["id"],)
        ).fetchone()
        last = self.db.execute(
            "SELECT actor FROM history WHERE record_id=? ORDER BY revision DESC LIMIT 1",
            (record["id"],),
        ).fetchone()
        return {
            "id": record["id"],
            "project_id": record["namespace"],
            "folder_id": doc.get("folder_id"),
            "title": data["name"],
            "slug": markdown.slugify(data["name"]),
            "body": data["description"],
            **metadata,
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "created_by": first[0] if first else "local",
            "updated_by": last[0] if last else "local",
            "created_via": "mcp",
            "updated_via": "mcp",
            "url": None,
            "entity_type": data["entity_type"],
            "external_id": data.get("external_id"),
            "revision": record["revision"],
        }

    def _folder(self, record):
        return {"id": record["id"], "project_id": record["namespace"], **record["data"]}

    def _validate_folder(self, namespace, folder_id):
        if folder_id:
            folder = self.access.find(folder_id, kind="folder")
            if folder["namespace"] != namespace:
                raise MemoryError("invalid_folder")

    def _read_note(self, note_id):
        record = self.access.find(note_id, kind="entity")
        note = self._note(record)
        links = self._wikilinks(record["namespace"])
        backlinks = [
            self._note(self.access.find(source, kind="entity"))
            for source, target in links
            if target == note_id
        ]
        return {
            **note,
            "backlinks": backlinks,
            "can_edit": self.access.role(record["namespace"]) in ("owner", "editor"),
            **self._note_health(note),
        }

    def _create_note(self, project_id, title, body="", folder_id=None):
        self.access.require(project_id, write=True)
        self._validate_folder(project_id, folder_id)
        entity = Entity(name=title, description=body, document={"folder_id": folder_id})
        record = self.store._save(
            project_id, "entity", entity.model_dump(mode="json"), principal().id, "create_note"
        )
        self._rebuild_links(project_id)
        return {**self._read_note(record["id"]), "link_candidates": []}

    def _update_note(self, note_id, title=None, body=None, base_updated_at=None):
        record = self.access.find(note_id, write=True, kind="entity")
        if base_updated_at and base_updated_at != record["updated_at"]:
            return {"error": "conflict", "note": self._note(record)}
        data = dict(record["data"])
        old_title = data["name"]
        if title is not None:
            data["name"] = title
        if body is not None:
            data["description"] = body
        data = Entity.model_validate(data).model_dump(mode="json")
        self.store._save(record["namespace"], "entity", data, principal().id, "update_note", record)
        if (
            old_title != data["name"]
            and not set(data["name"]) & markdown.UNSAFE_WIKILINK_TITLE_CHARS
        ):
            for source in self._records(record["namespace"]):
                rewritten, count = markdown.rewrite_wikilink_target(
                    source["data"]["description"], old_title, data["name"]
                )
                if count:
                    changed = {**source["data"], "description": rewritten}
                    self.store._save(
                        source["namespace"],
                        "entity",
                        changed,
                        principal().id,
                        "rename_link",
                        source,
                    )
        self._rebuild_links(record["namespace"])
        return {**self._read_note(note_id), "link_candidates": []}

    def _wikilinks(self, project_id):
        by_title = {}
        records = self._records(project_id)
        for record in records:
            by_title.setdefault(record["data"]["name"].casefold(), []).append(record["id"])
        return {
            (record["id"], ids[0])
            for record in records
            for title in markdown.extract_wikilinks(record["data"]["description"])
            if len(ids := by_title.get(title.casefold(), [])) == 1
        }

    def _rebuild_links(self, project_id):
        wanted = self._wikilinks(project_id)
        existing = [
            r
            for r in self._records(project_id, "relation")
            if r["data"]["source"] == "recall:wikilink"
        ]
        current = {(r["data"]["subject_id"], r["data"]["object_id"]) for r in existing}
        seen = set()
        for record in existing:
            pair = (record["data"]["subject_id"], record["data"]["object_id"])
            if pair not in wanted or pair in seen:
                self.store._save(
                    project_id,
                    "relation",
                    record["data"],
                    principal().id,
                    "unlink",
                    record,
                    "deleted",
                )
            seen.add(pair)
        for subject, target in wanted - current:
            relation = Relation(
                subject_id=subject,
                object_id=target,
                predicate="references",
                source="recall:wikilink",
            )
            self.store._save(
                project_id, "relation", relation.model_dump(mode="json"), principal().id, "link"
            )

    def _link_notes(self, note_id, target_title):
        record = self.access.find(note_id, write=True, kind="entity")
        if not target_title.strip() or set(target_title) & markdown.UNSAFE_WIKILINK_TITLE_CHARS:
            raise MemoryError("unlinkable_title")
        body = record["data"]["description"]
        if target_title.casefold() in [t.casefold() for t in markdown.extract_wikilinks(body)]:
            return {**self._note(record), "already_linked": True}
        return self._update_note(
            note_id, body=body + ("\n\n" if body else "") + f"[[{target_title.strip()}]]\n"
        )

    def _unlinked_mentions(self, note_id, limit=20):
        target = self.access.find(note_id, kind="entity")
        note = self._note(target)
        matcher = markdown.MentionMatcher(markdown.mention_terms(note["title"], note["metadata"]))
        links = self._wikilinks(target["namespace"])
        matches = []
        for record in self._records(target["namespace"]):
            if record["id"] == note_id or (record["id"], note_id) in links:
                continue
            if mention := matcher.first(record["data"]["description"]):
                matches.append(
                    {
                        "id": record["id"],
                        "title": record["data"]["name"],
                        "url": None,
                        "term": mention.term,
                        "match": mention.text,
                        "snippet": markdown.mention_snippet(
                            record["data"]["description"], mention.start, mention.end
                        ),
                    }
                )
        return {
            "mentions": matches[: max(1, min(limit, 50))],
            "can_edit": self.access.role(target["namespace"]) in ("owner", "editor"),
        }

    def _link_mention(self, note_id, source_note_id):
        target = self.access.find(note_id, kind="entity")
        source = self.access.find(source_note_id, write=True, kind="entity")
        if target["namespace"] != source["namespace"]:
            raise MemoryError("not_found")
        if (source_note_id, note_id) in self._wikilinks(target["namespace"]):
            raise MemoryError("already_linked")
        note = self._note(target)
        if set(note["title"]) & markdown.UNSAFE_WIKILINK_TITLE_CHARS:
            raise MemoryError("unlinkable_title")
        body = source["data"]["description"]
        match = markdown.MentionMatcher(
            markdown.mention_terms(note["title"], note["metadata"])
        ).first(body)
        if not match:
            raise MemoryError("no_mention")
        body = (
            body[: match.start]
            + markdown.mention_link_text(note["title"], match, body)
            + body[match.end :]
        )
        return self._update_note(source_note_id, body=body, base_updated_at=source["updated_at"])

    def _list_tree(self, project_id):
        self.access.require(project_id)
        return {
            "guide": self._guide(project_id),
            "folders": [self._folder(r) for r in self._records(project_id, "folder")],
            "notes": [self._note(r) for r in self._records(project_id)],
        }

    def _create_folder(self, project_id, name, parent_id=None):
        self.access.require(project_id, write=True)
        self._validate_folder(project_id, parent_id)
        if not name.strip() or len(name) > 256:
            raise MemoryError("name_required")
        record = self.store._save(
            project_id,
            "folder",
            {"name": name.strip(), "parent_id": parent_id},
            principal().id,
            "create_folder",
        )
        return self._folder(record)

    def _rename_folder(self, folder_id, name):
        folder = self.access.find(folder_id, write=True, kind="folder")
        if not name.strip() or len(name) > 256:
            raise MemoryError("name_required")
        return self._folder(
            self.store._save(
                folder["namespace"],
                "folder",
                {**folder["data"], "name": name.strip()},
                principal().id,
                "rename_folder",
                folder,
            )
        )

    def _descendants(self, record):
        if record["kind"] != "folder":
            return [record]
        result = [record]
        seen = {record["id"]}
        for parent in result:
            if parent["kind"] != "folder":
                continue
            for candidate in [
                *self._records(record["namespace"], "folder"),
                *self._records(record["namespace"]),
            ]:
                key = (
                    candidate["data"].get("parent_id")
                    if candidate["kind"] == "folder"
                    else (candidate["data"].get("document") or {}).get("folder_id")
                )
                if key == parent["id"] and candidate["id"] not in seen:
                    seen.add(candidate["id"])
                    result.append(candidate)
        return result

    def _move(self, item_type, item_id, project_id=None, folder_id=None, parent_id=None):
        if item_type not in ("note", "folder"):
            raise MemoryError("invalid_item_type")
        record = self.access.find(
            item_id, write=True, kind="entity" if item_type == "note" else "folder"
        )
        target = project_id or record["namespace"]
        self.access.require(target, write=True)
        parent = folder_id if item_type == "note" else parent_id
        self._validate_folder(target, parent)
        records = self._descendants(record)
        ids = {r["id"] for r in records}
        if parent in ids:
            raise MemoryError("invalid_move")
        # A cross-collection move cannot split graph edges; require the whole
        # connected subgraph to move together, or unlink the boundary first.
        dependent = []
        if target != record["namespace"]:
            for row in self.db.execute(
                "SELECT id FROM records WHERE namespace=? AND kind IN ('fact','relation')",
                (record["namespace"],),
            ):
                child = self.store._get(record["namespace"], row[0], True)
                data = child["data"]
                if child["kind"] == "fact" and data["entity_id"] in ids:
                    dependent.append(child)
                if child["kind"] == "relation" and (
                    data["subject_id"] in ids or data["object_id"] in ids
                ):
                    if data["subject_id"] not in ids or data["object_id"] not in ids:
                        if child["status"] not in ("active", "pending"):
                            continue
                        if data.get("source") == "recall:wikilink":
                            self.store._save(
                                record["namespace"],
                                "relation",
                                data,
                                principal().id,
                                "unlink",
                                child,
                                "deleted",
                            )
                            continue
                        raise MemoryError(
                            "cross_namespace_relation: unlink boundary relations before moving"
                        )
                    dependent.append(child)
        for entry in [*records, *dependent]:
            data = dict(entry["data"])
            if entry["id"] == item_id:
                if item_type == "note":
                    data["document"] = {**(data.get("document") or {}), "folder_id": parent}
                else:
                    data["parent_id"] = parent
            self.store._save(
                target, entry["kind"], data, principal().id, "move", entry, entry["status"]
            )
        self._rebuild_links(record["namespace"])
        if target != record["namespace"]:
            self._rebuild_links(target)
        moved = self.access.find(item_id)
        return self._note(moved) if item_type == "note" else self._folder(moved)

    def _copy(self, item_type, item_id):
        if item_type not in ("note", "folder"):
            raise MemoryError("invalid_item_type")
        record = self.access.find(
            item_id, write=True, kind="entity" if item_type == "note" else "folder"
        )
        mapping = {}
        for entry in self._descendants(record):
            data = dict(entry["data"])
            if entry["kind"] == "entity":
                data["external_id"] = None  # A copy cannot impersonate the same HA device.
                data["document"] = dict(data.get("document") or {})
                parent = data["document"].get("folder_id")
                data["document"]["folder_id"] = mapping.get(parent, parent)
            else:
                data["parent_id"] = mapping.get(data.get("parent_id"), data.get("parent_id"))
            if entry["id"] == item_id:
                data["name"] += " (copy)"
            copied = self.store._save(
                entry["namespace"], entry["kind"], data, principal().id, "copy"
            )
            mapping[entry["id"]] = copied["id"]
        # Facts and internal relations belong to the copied entities too.
        for kind in ("fact", "relation"):
            for entry in self._records(record["namespace"], kind):
                data = dict(entry["data"])
                keys = ("entity_id",) if kind == "fact" else ("subject_id", "object_id")
                if all(data[k] in mapping for k in keys):
                    data.update({k: mapping[data[k]] for k in keys})
                    self.store._save(entry["namespace"], kind, data, principal().id, "copy")
        self._rebuild_links(record["namespace"])
        copied = self.access.find(mapping[item_id])
        return self._note(copied) if item_type == "note" else self._folder(copied)

    def _delete_batch(self, root, records):
        ids = {r["id"] for r in records}
        if records:
            namespace = records[0]["namespace"]
            for row in self.db.execute(
                "SELECT id FROM records WHERE namespace=? AND kind IN ('fact','relation') AND status IN ('active','pending')",
                (namespace,),
            ):
                child = self.store._get(namespace, row[0], True)
                if any(
                    child["data"].get(k) in ids for k in ("entity_id", "subject_id", "object_id")
                ):
                    records.append(child)
        self.db.execute("DELETE FROM deletion_batches WHERE root_id=?", (root,))
        for record in {r["id"]: r for r in records}.values():
            self.db.execute(
                "INSERT INTO deletion_batches VALUES (?,?,?)",
                (root, record["id"], record["revision"]),
            )
            self.store._save(
                record["namespace"],
                record["kind"],
                record["data"],
                principal().id,
                "delete_batch",
                record,
                "deleted",
            )

    def _delete(self, item_type, item_id):
        if item_type not in ("note", "folder"):
            raise MemoryError("invalid_item_type")
        record = self.access.find(
            item_id, write=True, kind="entity" if item_type == "note" else "folder"
        )
        self._delete_batch(item_id, self._descendants(record))
        self._rebuild_links(record["namespace"])
        return {"ok": True}

    def _delete_workspace(self, project_id):
        self.access.require(project_id, owner=True)
        if self._project(project_id)["is_personal"]:
            raise MemoryError("cannot_delete_personal")
        records = [
            self.store._get(project_id, r[0], True)
            for r in self.db.execute(
                "SELECT id FROM records WHERE namespace=? AND status IN ('active','pending')",
                (project_id,),
            )
        ]
        self._delete_batch(project_id, records)
        self.db.execute(
            "UPDATE projects SET deleted=1,updated_at=? WHERE id=?", (now(), project_id)
        )
        self._event(project_id, "delete_workspace", {})
        return {"ok": True}

    def _list_trash(self):
        result = {"workspaces": [], "folders": [], "notes": []}
        for project in self.access.projects(include_deleted=True):
            p = self._project(project, True)
            if self.db.execute("SELECT deleted FROM projects WHERE id=?", (project,)).fetchone()[0]:
                if p["role"] == "owner":
                    result["workspaces"].append(p)
                continue
            if p["role"] not in ("editor", "owner"):
                continue
            for kind, key in (("folder", "folders"), ("entity", "notes")):
                result[key].extend(
                    self._folder(r) if kind == "folder" else self._note(r)
                    for r in self._records(project, kind, "deleted")
                )
        return result

    def _trash_root(self, item_type, item_id):
        if item_type == "workspace":
            self.access.require(item_id, owner=True, include_deleted=True)
            if not self.db.execute(
                "SELECT deleted FROM projects WHERE id=?", (item_id,)
            ).fetchone()[0]:
                raise MemoryError("not_in_trash")
            return item_id
        if item_type not in ("note", "folder"):
            raise MemoryError("invalid_item_type")
        record = self.access.find(
            item_id,
            write=True,
            include_inactive=True,
            kind="entity" if item_type == "note" else "folder",
        )
        if record["status"] != "deleted":
            raise MemoryError("not_in_trash")
        return record["namespace"]

    def _restore(self, item_type, item_id):
        project = self._trash_root(item_type, item_id)
        if item_type != "workspace":
            self.access.require(project, write=True)  # restore the project first
        else:
            self.db.execute(
                "UPDATE projects SET deleted=0,updated_at=? WHERE id=?", (now(), project)
            )
        rows = self.db.execute(
            "SELECT record_id,revision FROM deletion_batches WHERE root_id=?", (item_id,)
        ).fetchall()
        if not rows and item_type != "workspace":
            row = self.db.execute(
                "SELECT revision FROM history WHERE record_id=? AND json_extract(snapshot,'$.status')='active' ORDER BY revision DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            rows = [(item_id, row[0])] if row else []
        entries = []
        for rid, rev in rows:
            current = self.store._get(project, rid, True)
            if current["status"] != "deleted":
                continue
            previous = json.loads(
                self.db.execute(
                    "SELECT snapshot FROM history WHERE record_id=? AND revision=?", (rid, rev)
                ).fetchone()[0]
            )
            entries.append((current, previous))
        restoring = {r[0]["id"] for r in entries}
        for current, previous in sorted(
            entries,
            key=lambda pair: {"folder": 0, "entity": 1, "fact": 2, "relation": 3}[pair[0]["kind"]],
        ):
            data, status = previous["data"], previous["status"]
            parent = (
                data.get("parent_id")
                if current["kind"] == "folder"
                else (data.get("document") or {}).get("folder_id")
            )
            if parent and parent not in restoring:
                self._validate_folder(project, parent)
            if current["kind"] == "fact":
                self.store._entity(project, data["entity_id"])
                if status == "active" and self.store._conflicts(project, data, current["id"]):
                    status = "pending"
            if current["kind"] == "relation":
                self.store._entity(project, data["subject_id"])
                self.store._entity(project, data["object_id"])
            self.store._save(
                project, current["kind"], data, principal().id, "restore_batch", current, status
            )
        self._rebuild_links(project)
        self._event(project, "restore", {"root": item_id})
        return {"ok": True}

    def _purge(self, item_type, item_id):
        project = self._trash_root(item_type, item_id)
        if item_type == "workspace":
            ids = {
                r[0]
                for r in self.db.execute("SELECT id FROM records WHERE namespace=?", (project,))
            }
        else:
            ids = {item_id} | {
                r[0]
                for r in self.db.execute(
                    "SELECT record_id FROM deletion_batches WHERE root_id=?", (item_id,)
                )
            }
            # Never erase a child separately restored after the original delete.
            ids = {rid for rid in ids if self.store._get(project, rid, True)["status"] == "deleted"}
            # Include older trash inside the subtree, not only the most recent delete batch.
            while True:
                descendants = {
                    r["id"]
                    for kind in ("folder", "entity")
                    for r in self._records(project, kind, "deleted")
                    if (
                        r["data"].get("parent_id")
                        if kind == "folder"
                        else (r["data"].get("document") or {}).get("folder_id")
                    )
                    in ids
                }
                if descendants <= ids:
                    break
                ids.update(descendants)
        for row in self.db.execute(
            "SELECT id FROM records WHERE namespace=? AND kind IN ('fact','relation')", (project,)
        ):
            child = self.store._get(project, row[0], True)
            if any(child["data"].get(k) in ids for k in ("entity_id", "subject_id", "object_id")):
                ids.add(child["id"])
        for rid in ids:
            for table, field in (
                ("vectors", "record_id"),
                ("history", "record_id"),
                ("search_index", "id"),
                ("revision_labels", "record_id"),
                ("deletion_batches", "record_id"),
            ):
                self.db.execute(f"DELETE FROM {table} WHERE {field}=?", (rid,))
            self.db.execute("DELETE FROM records WHERE id=?", (rid,))
        self.db.execute("DELETE FROM deletion_batches WHERE root_id=?", (item_id,))
        if item_type == "workspace":
            self.db.execute("DELETE FROM invitations WHERE project_id=?", (project,))
            self.db.execute("DELETE FROM memberships WHERE project_id=?", (project,))
            self.db.execute("DELETE FROM projects WHERE id=?", (project,))
        self._event(project, "purge", {"item_type": item_type, "id": item_id, "count": len(ids)})
        return {"ok": True}

    def _save_version(self, note_id, label=None):
        record = self.access.find(note_id, write=True, kind="entity")
        saved = self.store._save(
            record["namespace"], "entity", record["data"], principal().id, "save_version", record
        )
        self.db.execute(
            "INSERT INTO revision_labels VALUES (?,?,?)", (note_id, saved["revision"], label)
        )
        return {"ok": True, **self._list_revisions(note_id)}

    def _list_revisions(self, note_id):
        record = self.access.find(note_id, kind="entity")
        result = []
        for revision in self.store.history(record["namespace"], note_id):
            label = self.db.execute(
                "SELECT label FROM revision_labels WHERE record_id=? AND revision=?",
                (note_id, revision["revision"]),
            ).fetchone()
            result.append(
                {
                    "id": str(revision["revision"]),
                    "note_id": note_id,
                    "trigger": revision["action"],
                    "label": label[0] if label else None,
                    "author": revision["actor"],
                    "created_at": revision["at"],
                }
            )
        return {"revisions": result}

    def _read_revision(self, note_id, revision_id):
        record = self.access.find(note_id, kind="entity")
        revision = next(
            (
                r
                for r in self.store.history(record["namespace"], note_id)
                if str(r["revision"]) == revision_id
            ),
            None,
        )
        if not revision:
            raise MemoryError("not_found")
        return {
            "id": revision_id,
            "note_id": note_id,
            "body": revision["snapshot"]["data"]["description"],
            "title": revision["snapshot"]["data"]["name"],
            "created_at": revision["at"],
            "created_by": revision["actor"],
        }

    def _restore_revision(self, note_id, revision_id):
        self.access.find(note_id, write=True, kind="entity")
        previous = self._read_revision(note_id, revision_id)
        return self._update_note(note_id, title=previous["title"], body=previous["body"])

    def _query_notes(
        self, project_id=None, type=None, tags=None, status=None, updated_by=None, limit=50
    ):
        projects = [self.access.require(project_id)] if project_id else self.access.projects()
        notes = [self._note(r) for p in projects for r in self._records(p)]
        notes = [
            n
            for n in notes
            if (not type or n["type"] == type)
            and (not tags or set(tags) <= set(n["tags"]))
            and (not status or n["status"] == status)
            and (not updated_by or updated_by.casefold() in n["updated_by"].casefold())
        ]
        return {
            "notes": sorted(notes, key=lambda n: n["updated_at"], reverse=True)[
                : max(1, min(limit, 200))
            ]
        }

    def _grep(self, text, project_id=None, limit=20):
        if len(text.strip()) < 2:
            return {"error": "text_too_short", "results": []}
        projects = [self.access.require(project_id)] if project_id else self.access.projects()
        result = []
        for project in projects:
            for record in self._records(project):
                note = self._note(record)
                title_match = text.casefold() in note["title"].casefold()
                if title_match or text.casefold() in note["body"].casefold():
                    excerpt, count = markdown.grep_excerpt(note["body"], text)
                    result.append(
                        {
                            **note,
                            "excerpt": excerpt,
                            "match_count": count,
                            "title_match": title_match,
                        }
                    )
        return {
            "results": sorted(result, key=lambda r: r["updated_at"], reverse=True)[
                : max(1, min(limit, 50))
            ]
        }

    async def _search(self, query, project_id=None, limit=20):
        if not query.strip():
            return {"results": [], "semantic": False}
        projects = [self.access.require(project_id)] if project_id else self.access.projects()
        results, semantic = {}, False
        for project in projects:
            found = await self.search.search(project, query, max(1, min(limit, 50)))
            with self.store.lock:
                self.access.require(project)  # a grant may have changed while embedding
                semantic |= found["semantic"] == "ready"
                for record in found["results"]:
                    rid = (
                        record["id"]
                        if record["kind"] == "entity"
                        else record["data"].get("entity_id", record["data"].get("subject_id"))
                    )
                    if not rid:
                        continue
                    current = self.access.find(rid, kind="entity")
                    note = self._note(current)
                    score = record["score"]
                    if rid not in results or score > results[rid]["score"]:
                        results[rid] = {
                            **note,
                            "score": score,
                            "snippet": self.store.text(record)[:400],
                        }
        return {
            "results": sorted(results.values(), key=lambda r: r["score"], reverse=True)[
                : max(1, min(limit, 50))
            ],
            "semantic": semantic,
        }

    async def _suggest_links(self, note_id, limit=8):
        with self.store.lock:
            record = self.access.find(note_id, kind="entity")
            namespace = record["namespace"]
            query = self.store.text(record)[:1800]
            excluded = {note_id} | {
                target for source, target in self._wikilinks(namespace) if source == note_id
            }
        results = await self._search(query, namespace, 50)
        return {
            "candidates": [r for r in results["results"] if r["id"] not in excluded][
                : max(1, min(limit, 25))
            ]
        }

    def _list_tags(self, project_id):
        self.access.require(project_id)
        counts = {}
        for record in self._records(project_id):
            for tag in self._note(record)["tags"]:
                counts[tag] = counts.get(tag, 0) + 1
        return {
            "tags": [
                {"tag": tag, "count": count}
                for tag, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            ]
        }

    def _graph(self, project_id=None):
        projects = [self.access.require(project_id)] if project_id else self.access.projects()
        nodes, links = [], []
        for project in projects:
            notes = self._records(project)
            nodes.extend(
                {
                    "id": r["id"],
                    "title": r["data"]["name"],
                    "type": r["data"]["entity_type"],
                    "project_id": project,
                }
                for r in notes
            )
            links.extend(
                {
                    "source": r["data"]["subject_id"],
                    "target": r["data"]["object_id"],
                    "predicate": r["data"]["predicate"],
                }
                for r in self._records(project, "relation")
                if is_current(r["data"])
            )
            if project_id is None:
                nodes.append(
                    {"id": project, "title": self._project(project)["name"], "type": "workspace"}
                )
                links.extend(
                    {"source": project, "target": r["id"], "predicate": "contains"} for r in notes
                )
        return {"nodes": nodes, "links": links}

    def _guide(self, project_id):
        for record in self._records(project_id):
            note = self._note(record)
            if note["type"] == "guide":
                conventions, problems = guide.parse_conventions(note["metadata"])
                return {
                    "id": note["id"],
                    "title": note["title"],
                    "url": None,
                    "body": note["body"],
                    "conventions": asdict(conventions),
                    "problems": problems,
                }
        return None

    def _note_health(self, note):
        entry = self._guide(note["project_id"])
        hints = guide.check_review_every(note["metadata"])
        if entry and not entry["problems"]:
            hints += guide.check_note(
                guide.Conventions(**entry["conventions"]), note["type"], note["tags"]
            )
        return {
            "guide_id": entry["id"] if entry else None,
            "convention_hints": hints,
            "review": guide.review_state(
                note["metadata"], datetime.fromisoformat(note["created_at"]).date(), date.today()
            ),
        }

    def _workspace_health(self, project_id):
        self.access.require(project_id)
        keys = ("overdue", "not_edited", "old_drafts", "orphans", "broken_links", "owner_left")
        result = {key: [] for key in keys}
        records = self._records(project_id)
        links = self._wikilinks(project_id)
        connected = {i for edge in links for i in edge}
        titles = {r["data"]["name"].casefold() for r in records}
        members = self._list_members(project_id)["members"]
        for record in records:
            note = self._note(record)
            review = self._note_health(note)["review"]
            if review and review.get("overdue"):
                result["overdue"].append({**note, "review": review})
            edited = datetime.fromisoformat(note["updated_at"])
            if edited < datetime.now(UTC) - timedelta(days=180):
                result["not_edited"].append(note)
                if note["status"] == "draft":
                    result["old_drafts"].append(note)
            if note["id"] not in connected:
                result["orphans"].append(note)
            broken = [
                t for t in markdown.extract_wikilinks(note["body"]) if t.casefold() not in titles
            ]
            if broken:
                result["broken_links"].append({**note, "targets": broken})
            owner = note["metadata"].get("owner")
            if owner and not guide.owner_matches(str(owner), members):
                result["owner_left"].append(note)
        return {
            **{k: v[:100] for k, v in result.items()},
            "counts": {k: len(v) for k, v in result.items()},
            "stale_after_months": 6,
        }

    def _mark_reviewed(self, note_id):
        record = self.access.find(note_id, write=True, kind="entity")
        body = markdown.set_frontmatter_value(
            record["data"]["description"], "reviewed", date.today().isoformat()
        )
        if body is None:
            raise MemoryError("frontmatter_invalid")
        updated = self._update_note(note_id, body=body, base_updated_at=record["updated_at"])
        return {
            "id": note_id,
            "title": updated["title"],
            "reviewed": updated["metadata"].get("reviewed"),
            "review": updated["review"],
            "updated_at": updated["updated_at"],
            "url": None,
        }

    def _preview_diagram(self, source):
        return validate_mermaid(source)
