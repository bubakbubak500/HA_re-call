"""Local token identities and collection grants; no SSO or external identity service."""

from contextvars import ContextVar
from dataclasses import dataclass

from fastmcp.server.dependencies import get_access_token

from .models import now
from .store import MemoryError


@dataclass(frozen=True)
class Principal:
    id: str = "local"
    upn: str = "local"


test_principal = ContextVar("recall_test_principal", default=None)


def principal():
    if override := test_principal.get():
        return override
    token = get_access_token()
    # In-process clients are trusted application code, not an unauthenticated HTTP path.
    if token is None:
        return Principal()
    return Principal(token.subject or token.client_id, token.claims.get("upn", "local"))


class Access:
    def __init__(self, store, namespaces):
        self.store = store
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,name TEXT NOT NULL,owner_id TEXT NOT NULL,
                org_access TEXT NOT NULL DEFAULT 'none',deleted INTEGER NOT NULL DEFAULT 0,
                personal INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS principals (id TEXT PRIMARY KEY,upn TEXT UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS memberships (
                project_id TEXT NOT NULL REFERENCES projects(id),user_id TEXT NOT NULL,
                role TEXT NOT NULL,PRIMARY KEY(project_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS invitations (
                id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),
                upn TEXT NOT NULL,role TEXT NOT NULL,UNIQUE(project_id,upn)
            );
            CREATE TABLE IF NOT EXISTS compatibility_events (
                id INTEGER PRIMARY KEY,project_id TEXT NOT NULL,actor TEXT NOT NULL,
                action TEXT NOT NULL,detail TEXT NOT NULL,at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deletion_batches (
                root_id TEXT NOT NULL,record_id TEXT NOT NULL,revision INTEGER NOT NULL,
                PRIMARY KEY(root_id,record_id)
            );
            CREATE TABLE IF NOT EXISTS revision_labels (
                record_id TEXT NOT NULL,revision INTEGER NOT NULL,label TEXT,
                PRIMARY KEY(record_id,revision)
            );
        """)
        with store.transaction():
            store.db.execute("INSERT OR IGNORE INTO principals VALUES ('local','local')")
            for namespace in namespaces:
                store.db.execute(
                    "INSERT OR IGNORE INTO projects VALUES (?,?,'local','none',0,1,?)",
                    (namespace, namespace, now()),
                )
                store.db.execute(
                    "INSERT OR IGNORE INTO memberships VALUES (?,'local','owner')", (namespace,)
                )

    def provision(self):
        user = principal()
        db = self.store.db
        existing = db.execute(
            "SELECT id,upn FROM principals WHERE id=? OR upn=?", (user.id, user.upn.casefold())
        ).fetchall()
        if any(row["id"] != user.id or row["upn"] != user.upn.casefold() for row in existing):
            raise MemoryError("identity_conflict: preserve stable identity IDs and UPNs")
        db.execute("INSERT OR IGNORE INTO principals VALUES (?,?)", (user.id, user.upn.casefold()))
        if user.id != "local":
            for invite in db.execute(
                "SELECT * FROM invitations WHERE upn=?", (user.upn.casefold(),)
            ).fetchall():
                db.execute(
                    "INSERT OR REPLACE INTO memberships VALUES (?,?,?)",
                    (invite["project_id"], user.id, invite["role"]),
                )
                db.execute("DELETE FROM invitations WHERE id=?", (invite["id"],))
        return user

    def role(self, namespace, include_deleted=False):
        user = principal()
        row = self.store.db.execute(
            "SELECT p.deleted,p.org_access,m.role FROM projects p "
            "LEFT JOIN memberships m ON p.id=m.project_id AND m.user_id=? WHERE p.id=?",
            (user.id, namespace),
        ).fetchone()
        if not row or (row["deleted"] and not include_deleted):
            return None
        return row["role"] or ("viewer" if row["org_access"] == "viewer" else None)

    def require(self, namespace, write=False, owner=False, include_deleted=False):
        with self.store.lock:
            role = self.role(namespace, include_deleted)
            if role is None:
                raise MemoryError("not_found")
            if (write and role not in ("owner", "editor")) or (owner and role != "owner"):
                raise MemoryError("forbidden")
        return namespace

    def projects(self, include_deleted=False):
        with self.store.lock:
            return [
                r[0]
                for r in self.store.db.execute("SELECT id FROM projects ORDER BY id")
                if self.role(r[0], include_deleted)
            ]

    def find(self, record_id, write=False, include_inactive=False, kind=None):
        row = self.store.db.execute(
            "SELECT namespace FROM records WHERE id=?", (record_id,)
        ).fetchone()
        if not row:
            raise MemoryError("not_found")
        namespace = self.require(row[0], write=write, include_deleted=include_inactive)
        record = self.store._get(namespace, record_id, include_inactive)
        if kind and record["kind"] != kind:
            raise MemoryError("invalid_item_type")
        return record
