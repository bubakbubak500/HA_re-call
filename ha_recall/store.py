"""SQLite graph, FTS5, optimistic concurrency and append-only history.

Every mutation, FTS update and revision is committed in one transaction. Embeddings
are a disposable cache, guarded by revision so a slow response cannot overwrite a
newer edit. One connection is serialized across MCP worker threads.
"""

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .models import Entity, Fact, Relation, is_current, now, overlaps


class MemoryError(ValueError):
    pass


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.authorizer = None
        self.entity_changed = None
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise RuntimeError(f"Unsupported database version: {version}")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY, namespace TEXT NOT NULL, kind TEXT NOT NULL,
                data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS scope ON records(namespace,kind,status);
            CREATE UNIQUE INDEX IF NOT EXISTS identity ON records(
                namespace,json_extract(data,'$.external_id')
            ) WHERE kind='entity' AND json_extract(data,'$.external_id') IS NOT NULL;
            CREATE TABLE IF NOT EXISTS history (
                record_id TEXT NOT NULL REFERENCES records(id), revision INTEGER NOT NULL,
                snapshot TEXT NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
                at TEXT NOT NULL, PRIMARY KEY(record_id,revision)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
                id UNINDEXED, namespace UNINDEXED, text, tokenize='unicode61 remove_diacritics 2'
            );
            CREATE TABLE IF NOT EXISTS vectors (
                record_id TEXT PRIMARY KEY REFERENCES records(id), revision INTEGER NOT NULL,
                model TEXT NOT NULL, vector TEXT NOT NULL
            );
            PRAGMA user_version=1;
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            yield

    def _get(self, namespace, record_id, include_inactive=False):
        if self.authorizer:
            self.authorizer(namespace, include_deleted=include_inactive)
        row = self.db.execute(
            "SELECT * FROM records WHERE namespace=? AND id=?", (namespace, record_id)
        ).fetchone()
        if not row or (not include_inactive and row["status"] != "active"):
            raise MemoryError("record_not_found")
        result = dict(row)
        result["data"] = json.loads(result["data"])
        return result

    def get(self, namespace, record_id, include_inactive=False):
        with self.lock:
            return self._get(namespace, record_id, include_inactive)

    def list(self, namespace, kind=None, status="active", limit=100, offset=0):
        if not 1 <= limit <= 1000 or offset < 0:
            raise MemoryError("invalid_pagination")
        with self.lock:
            rows = self.db.execute(
                "SELECT id FROM records WHERE namespace=? AND status=? "
                "AND (? IS NULL OR kind=?) ORDER BY created_at,id LIMIT ? OFFSET ?",
                (namespace, status, kind, kind, limit, offset),
            ).fetchall()
            return [self._get(namespace, row["id"], True) for row in rows]

    @staticmethod
    def text(record):
        data = record["data"]
        keys = {
            "entity": ("name", "external_id", "aliases", "categories", "description"),
            "fact": ("predicate", "value"),
            "relation": ("predicate", "attributes"),
            "folder": ("name",),
        }
        return "\n".join(
            dump(data[k]) if isinstance(data.get(k), (list, dict)) else str(data[k])
            for k in keys[record["kind"]]
            if data.get(k)
        )

    def _save(self, namespace, kind, data, actor, action, record=None, status="active"):
        if self.authorizer:
            self.authorizer(namespace, write=True, include_deleted=True)
        if kind == "entity" and status == "active":
            data = Entity.model_validate(data).model_dump(mode="json")
            folder_id = (data.get("document") or {}).get("folder_id")
            if folder_id:
                folder = self._get(namespace, folder_id)
                if folder["kind"] != "folder":
                    raise MemoryError("invalid_folder")
        stamp = now()
        result = dict(
            id=record["id"] if record else str(uuid4()),
            namespace=namespace,
            kind=kind,
            data=data,
            status=status,
            revision=record["revision"] + 1 if record else 1,
            created_at=record["created_at"] if record else stamp,
            updated_at=stamp,
        )
        self.db.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET namespace=excluded.namespace,data=excluded.data,status=excluded.status,"
            "revision=excluded.revision,updated_at=excluded.updated_at",
            (
                result["id"],
                namespace,
                kind,
                dump(data),
                status,
                result["revision"],
                result["created_at"],
                stamp,
            ),
        )
        self.db.execute(
            "INSERT INTO history VALUES (?,?,?,?,?,?)",
            (result["id"], result["revision"], dump(result), action, actor, stamp),
        )
        self.db.execute("DELETE FROM search_index WHERE id=?", (result["id"],))
        self.db.execute("DELETE FROM vectors WHERE record_id=?", (result["id"],))
        if status == "active" and kind != "folder":
            self.db.execute(
                "INSERT INTO search_index VALUES (?,?,?)",
                (result["id"], namespace, self.text(result)),
            )
        if (
            kind == "entity"
            and self.entity_changed
            and action in ("create", "update", "restore", "delete")
        ):
            self.entity_changed(namespace, record, result)
            result = self._get(namespace, result["id"], True)
        return result

    def _entity(self, namespace, record_id):
        entity = self._get(namespace, record_id)
        if entity["kind"] != "entity":
            raise MemoryError("expected_entity")
        return entity

    @staticmethod
    def _revision(record, expected):
        if record["revision"] != expected:
            raise MemoryError("revision_conflict: read the current record before editing")

    def create_entity(self, namespace, entity: Entity, actor="local"):
        data = entity.model_dump(mode="json")
        with self.transaction():
            if entity.external_id:
                row = self.db.execute(
                    "SELECT id FROM records WHERE namespace=? AND kind='entity' "
                    "AND json_extract(data,'$.external_id')=?",
                    (namespace, entity.external_id),
                ).fetchone()
                if row:
                    existing = self._get(namespace, row["id"], True)
                    if (
                        existing["status"] == "active"
                        and Entity.model_validate(existing["data"]).model_dump(mode="json") == data
                    ):
                        return existing
                    raise MemoryError("identity_exists: use update_entity or restore_record")
            return self._save(namespace, "entity", data, actor, "create")

    def update_entity(self, namespace, record_id, entity: Entity, expected_revision, actor="local"):
        with self.transaction():
            record = self._entity(namespace, record_id)
            self._revision(record, expected_revision)
            data = entity.model_dump(mode="json")
            if "document" not in entity.model_fields_set:
                data["document"] = record["data"].get("document")
            try:
                return self._save(namespace, "entity", data, actor, "update", record)
            except sqlite3.IntegrityError as exc:
                raise MemoryError("identity_exists") from exc

    def _conflicts(self, namespace, data, exclude=None):
        rows = self.db.execute(
            "SELECT id FROM records WHERE namespace=? AND kind='fact' "
            "AND status='active' AND json_extract(data,'$.entity_id')=? "
            "AND json_extract(data,'$.predicate')=?",
            (namespace, data["entity_id"], data["predicate"]),
        ).fetchall()
        return [
            r
            for row in rows
            if (r := self._get(namespace, row["id"]))["id"] != exclude
            and (data["exclusive"] or r["data"]["exclusive"])
            and overlaps(data, r["data"])
        ]

    def add_fact(self, namespace, fact: Fact, actor="local"):
        data = fact.model_dump(mode="json")
        with self.transaction():
            self._entity(namespace, fact.entity_id)
            # Idempotence also applies to pending proposals.
            row = self.db.execute(
                "SELECT id FROM records WHERE namespace=? AND kind='fact' "
                "AND data=? AND status IN ('active','pending')",
                (namespace, dump(data)),
            ).fetchone()
            if row:
                return self._get(namespace, row["id"], True)
            conflicts = self._conflicts(namespace, data)
            result = self._save(
                namespace,
                "fact",
                data,
                actor,
                "propose" if conflicts else "create",
                status="pending" if conflicts else "active",
            )
            return {**result, "conflicts": [r["id"] for r in conflicts]}

    def resolve_fact(self, namespace, record_id, accept, expected_revision, actor="local"):
        with self.transaction():
            record = self._get(namespace, record_id, True)
            self._revision(record, expected_revision)
            if record["kind"] != "fact" or record["status"] != "pending":
                raise MemoryError("expected_pending_fact")
            if accept:
                self._entity(namespace, record["data"]["entity_id"])
                for old in self._conflicts(namespace, record["data"]):
                    self._save(
                        namespace, "fact", old["data"], actor, "supersede", old, "superseded"
                    )
            return self._save(
                namespace,
                "fact",
                record["data"],
                actor,
                "accept" if accept else "reject",
                record,
                "active" if accept else "rejected",
            )

    def add_relation(self, namespace, relation: Relation, actor="local"):
        data = relation.model_dump(mode="json")
        with self.transaction():
            self._entity(namespace, relation.subject_id)
            self._entity(namespace, relation.object_id)
            row = self.db.execute(
                "SELECT id FROM records WHERE namespace=? AND kind='relation' "
                "AND data=? AND status='active'",
                (namespace, dump(data)),
            ).fetchone()
            if row:
                return self._get(namespace, row["id"])
            return self._save(namespace, "relation", data, actor, "create")

    def forget(self, namespace, record_id, expected_revision, actor="local"):
        with self.transaction():
            record = self._get(namespace, record_id, True)
            if record["kind"] == "folder":
                raise MemoryError("use_delete_folder: folder deletion must include descendants")
            self._revision(record, expected_revision)
            if record["status"] == "deleted":
                return record
            if record["kind"] == "entity":
                rows = self.db.execute(
                    "SELECT id FROM records WHERE namespace=? "
                    "AND status IN ('active','pending') AND kind IN ('fact','relation') AND "
                    "(json_extract(data,'$.entity_id')=? OR json_extract(data,'$.subject_id')=? "
                    "OR json_extract(data,'$.object_id')=?)",
                    (namespace, record_id, record_id, record_id),
                ).fetchall()
                for row in rows:
                    child = self._get(namespace, row["id"], True)
                    self._save(
                        namespace,
                        child["kind"],
                        child["data"],
                        actor,
                        "parent_deleted",
                        child,
                        "deleted",
                    )
            return self._save(
                namespace, record["kind"], record["data"], actor, "delete", record, "deleted"
            )

    def history(self, namespace, record_id):
        with self.lock:
            self._get(namespace, record_id, True)
            return [
                {**dict(r), "snapshot": json.loads(r["snapshot"])}
                for r in self.db.execute(
                    "SELECT * FROM history WHERE record_id=? ORDER BY revision DESC", (record_id,)
                )
            ]

    def restore(self, namespace, record_id, revision, expected_revision, actor="local"):
        with self.transaction():
            current = self._get(namespace, record_id, True)
            if current["kind"] == "folder":
                raise MemoryError("use_restore_folder: folder restoration must include descendants")
            self._revision(current, expected_revision)
            row = self.db.execute(
                "SELECT snapshot FROM history WHERE record_id=? AND revision=?",
                (record_id, revision),
            ).fetchone()
            if not row:
                raise MemoryError("revision_not_found")
            previous = json.loads(row[0])
            if previous["status"] != "active":
                raise MemoryError("choose_an_active_revision")
            data = previous["data"]
            if current["kind"] == "fact":
                self._entity(namespace, data["entity_id"])
                status = "pending" if self._conflicts(namespace, data, record_id) else "active"
            else:
                status = "active"
            if current["kind"] == "relation":
                self._entity(namespace, data["subject_id"])
                self._entity(namespace, data["object_id"])
            try:
                return self._save(
                    namespace, current["kind"], data, actor, "restore", current, status
                )
            except sqlite3.IntegrityError as exc:
                raise MemoryError("identity_exists") from exc

    def context(self, namespace, entity_id):
        with self.lock:
            entity = self._entity(namespace, entity_id)
            rows = self.db.execute(
                "SELECT id FROM records WHERE namespace=? AND status='active' "
                "AND kind IN ('fact','relation') AND (json_extract(data,'$.entity_id')=? "
                "OR json_extract(data,'$.subject_id')=? OR json_extract(data,'$.object_id')=?)",
                (namespace, entity_id, entity_id, entity_id),
            ).fetchall()
            related = [r for row in rows if is_current((r := self._get(namespace, row[0]))["data"])]
            ids = {
                r["data"][key]
                for r in related
                if r["kind"] == "relation"
                for key in ("subject_id", "object_id")
            } - {entity_id}
            return {
                "entity": entity,
                "facts": [r for r in related if r["kind"] == "fact"],
                "relations": [r for r in related if r["kind"] == "relation"],
                "neighbors": [self._entity(namespace, i) for i in sorted(ids)],
            }

    def lexical_search(self, namespace, query, limit=20):
        if not query.strip() or len(query) > 2000 or not 1 <= limit <= 100:
            raise MemoryError("invalid_search")
        with self.lock:
            exact = self.db.execute(
                "SELECT id FROM records WHERE namespace=? AND kind='entity' "
                "AND status='active' AND (id=? OR json_extract(data,'$.external_id')=? "
                "OR lower(json_extract(data,'$.name'))=lower(?) OR EXISTS "
                "(SELECT 1 FROM json_each(json_extract(records.data,'$.aliases')) WHERE lower(value)=lower(?)))",
                (namespace, query, query, query, query),
            ).fetchall()
            tokens = re.findall(r"\w+", query, flags=re.UNICODE)
            fts = " OR ".join('"' + token + '"' for token in tokens)
            rows = (
                self.db.execute(
                    "SELECT id FROM search_index WHERE search_index MATCH ? "
                    "AND namespace=? ORDER BY bm25(search_index) LIMIT ?",
                    (fts, namespace, limit * 4),
                ).fetchall()
                if fts
                else []
            )
            result, seen = [], set()
            for row in [*exact, *rows]:
                if row[0] in seen:
                    continue
                seen.add(row[0])
                record = self._get(namespace, row[0])
                if record["kind"] != "entity" and not is_current(record["data"]):
                    continue
                result.append({**record, "match": "exact" if row in exact else "fulltext"})
            return result[:limit]

    def embedding_candidates(self, namespace, model, limit=100):
        with self.lock:
            rows = self.db.execute(
                "SELECT r.id FROM records r LEFT JOIN vectors v ON r.id=v.record_id "
                "WHERE r.namespace=? AND r.status='active' AND r.kind!='folder' "
                "AND (v.record_id IS NULL OR v.model!=? OR v.revision!=r.revision) LIMIT ?",
                (namespace, model, limit),
            ).fetchall()
            return [self._get(namespace, row[0]) for row in rows]

    def save_vector(self, record, model, vector):
        with self.transaction():
            try:
                current = self._get(record["namespace"], record["id"], True)
            except MemoryError as exc:
                if str(exc) == "record_not_found":
                    return False
                raise
            if current["revision"] != record["revision"] or current["status"] != "active":
                return False
            self.db.execute(
                "INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)",
                (record["id"], record["revision"], model, dump(vector)),
            )
            return True

    def vectors(self, namespace, model):
        with self.lock:
            return [
                (self._get(namespace, row[0]), json.loads(row[1]))
                for row in self.db.execute(
                    "SELECT r.id,v.vector FROM records r JOIN vectors v ON r.id=v.record_id "
                    "WHERE r.namespace=? AND r.status='active' AND v.model=? AND v.revision=r.revision",
                    (namespace, model),
                )
            ]

    def export(self, namespace):
        with self.lock:
            rows = self.db.execute(
                "SELECT id FROM records WHERE namespace=? ORDER BY id", (namespace,)
            ).fetchall()
            return {
                "schema_version": 1,
                "namespace": namespace,
                "records": [self._get(namespace, r[0], True) for r in rows],
                "history": [h for r in rows for h in self.history(namespace, r[0])],
            }

    def fingerprint(self, namespace):
        return hashlib.sha256(dump(self.export(namespace)).encode()).hexdigest()
