# HA re:call 1.0

Local structured memory for Home Assistant and Jarvis. One FastMCP process with
SQLite/WAL/FTS5, entities, atomic facts, relationships, revision history, and
hybrid search. No web frontend, PostgreSQL, SSO, or separate worker.

This independent repository is based on [panuhen/recall](https://github.com/panuhen/recall)
and retains its Git history and MIT license. The `upstream-base` tag marks the
original commit `4cb572e`. GitHub repository names cannot contain a colon, so the
repository is named `HA_re-call` and the product is **HA re:call**. Unmodified
original sources are preserved in `upstream/` and are excluded from the runtime.

## Features and compatibility

All **40 original MCP tools, including ping**, remain available with their original
names, arguments, and defaults. Fifteen entity tools bring the total to **55 tools**.
The original interface has not been reduced.

- A note is an entity's optional Markdown description in the same database.
  `create_note` and `create_entity` can read each other's records. Wiki links
  become graph relationships.
- Folders, moves, copies, revisions, restoration, trash, link checks, tags,
  conventions, and review schedules are supported. Moving a record between
  collections preserves its history.
- A workspace is a collection. Sharing tools manage local token identities with
  owner/editor/viewer roles. Invitations wait for a matching local UPN; they do
  not send email or require a team service. `org_access=viewer` grants read access
  to every configured token identity on this instance.
- Web URL fields are `null`. `preview_diagram` checks Mermaid structure and returns
  text; it does not render an image without a frontend.
- Stable HA registry identities, aliases, categories, sourced facts with validity
  intervals, and relationships with attributes. Live states and device control
  remain in Home Assistant.
- Exact identity/alias matching, accent-insensitive full-text search, and Model2Vec.
  `entity_context` expands an entity into current facts and neighboring entities.
- Changes carry a revision, UTC timestamp, actor, and history. `expected_revision`
  protects entity edits against concurrent overwrites; the original `update_note`
  supports `base_updated_at`.
- Deletion is reversible. Only an explicit `purge` permanently removes records
  and their revisions.

Conflicts are detected for the same entity and predicate over overlapping validity
intervals. New values remain `pending` until `resolve_fact` accepts or rejects them.
Independent observations use `exclusive=false`. The calling LLM must recognize
contradictions expressed with different predicates. Accepting a proposal supersedes
the entire overlapping fact; it does not split its validity interval.
`list_records` is an administrative view that includes facts outside their current
validity interval. Search and context respect validity dates.

## Run locally

Requires Python 3.12+ and uv:

```powershell
uv sync --locked
./tools/run-local.ps1
```

The script generates a random token in the ignored `data/mcp.token` file. The server
listens at `127.0.0.1:8004/mcp`; clients send `Authorization: Bearer <token>`.
`/health` reports database availability and registry synchronization status without
returning memory contents. Press `Ctrl+C` to stop; data remains in
`data/memory.sqlite3`.

To use existing local Model2Vec weights:

```powershell
uv sync --locked --extra local-model
$env:HA_RECALL_MODEL_PATH = 'C:/path/to/existing/model'
./tools/run-local.ps1
```

The directory must contain `config.json`, `tokenizer.json`, and `model.safetensors`.
Weights are never downloaded automatically. Alternatively, set
`HA_RECALL_EMBEDDING_URL` to a complete OpenAI-compatible embedding endpoint and
`HA_RECALL_EMBEDDING_KEY` to its token. A local model and an HTTP endpoint cannot
be enabled simultaneously.

Without embeddings, exact and full-text search remain available
(`semantic: disabled`). An endpoint failure preserves full-text results and reports
`unavailable`. A model identity change invalidates cached vectors. Each search
indexes at most 32 missing records; `reindex_memory` processes larger imports in
batches. Vectors are stored in SQLite and similarity is computed linearly for
household-sized collections.

## Local identities and privacy

The primary `HA_RECALL_TOKEN` must contain at least 32 characters and represents
identity `local`. This identity owns the default `home,technical` collections.
`HA_RECALL_NAMESPACES=home,technical,private` adds another personal collection.
Other tokens cannot access these collections until their owner grants access with
`share_workspace`.

In standalone mode, set `HA_RECALL_IDENTITIES_FILE` to a private JSON file:

```json
[{"id":"jarvis","upn":"jarvis@local","token":"REPLACE_WITH_A_RANDOM_TOKEN_OF_AT_LEAST_32_CHARACTERS"}]
```

Keep IDs and UPNs stable; rotate the token rather than changing the identity.
Restart after editing the file. The add-on exposes the same setting as `identities`.
A shared token does not distinguish individual speakers. Jarvis uses the permissions
of the token configured in the integration; share only the intended collections
with a voice assistant. Text is sent only to the embedding endpoint you configure.

## Install in Home Assistant

Requires **HA Core 2026.9.4 or a compatible newer version**, HA OS/Supervised for
the add-on, and an existing `jarvis_semantic` installation to share its Model2Vec.
Without the Jarvis model, use full-text search or a different embedding endpoint.

Build the installation packages:

```sh
uv run --locked python tools/package_addon.py
```

1. Extract `dist/ha_recall-addon.zip` into `/addons/`, producing
   `/addons/ha_recall/config.json`. Refresh local add-ons, build HA re:call,
   configure a random `token`, select transport `http`, and start it.
2. Extract `dist/ha_recall-integration.zip` into HA's `/config/`, producing
   `/config/custom_components/ha_recall/manifest.json`. Restart HA Core.
3. Open Settings → Devices & services → Add integration → **HA re:call**.
   Enter the add-on's internal URL, `http://local-ha-recall:8004/mcp`, and the
   same token. If the hostname differs, use the one shown in the add-on's
   information. For the `sse` transport, use `/sse`.
4. Enable Assist/Jarvis access if desired. The integration contributes all 55 tools
   with the `ha_recall__` prefix to the Assist API and also registers a separate
   **HA re:call** LLM API. Jarvis/Luna clients using Assist can discover these tools.
   Assist access is disabled by default.
5. To share the already loaded Model2Vec, enable the embedding bridge. Set the
   add-on's `embedding_url` to
   `http://<HA-address>:8123/api/ha_recall/embeddings` and `embedding_key` to a
   long-lived **Home Assistant token**, not the MCP token from step 1.
6. For automatic registry synchronization, set `ha_url` to
   `http://<HA-address>:8123`, `ha_token` to an HA token authorized to read registries,
   `ha_instance` to a stable household identifier, `ha_namespace` to `home`, and
   `sync_interval` to a value such as 300 seconds. Restart the add-on after changes.

The bridge calls the loaded `DecisionLayer.model.encode` directly, without intent
preprocessing or loading a second copy of the weights. It requires HA authentication
and bounds request size and concurrency. Model failures return HTTP 503 while memory
continues serving full-text results. The integration uses HA Core's HTTP/SSE MCP
client with a bearer token. No OAuth server or authentication bypass is required.

The add-on has no web ingress or Supervisor API access. Its `/data` directory belongs
in add-on backups. By default, its port is not published outside HA's internal network.

## Registry synchronization and stable identity

Periodic synchronization runs in the server process. It uses only the WebSocket
commands `config/area_registry/list`, `config/device_registry/list`, and
`config/entity_registry/list`, never `get_states` or service calls.
`/health.registry_sync` reports `ready`, `unavailable`, `pending`, or `disabled`,
plus the last successful synchronization time. Failures do not stop memory;
synchronization retries at the next interval.

Registry UUIDs survive `entity_id` renames; current and previous HA IDs remain
aliases. User descriptions and categories are preserved. Import updates its own
room relationships while preserving manual ones. It does not restore user-deleted
records. References missing from a later snapshot are retained because absence
need not mean permanent removal. Each snapshot is imported atomically. HA tokens
are not stored in the memory database.

One-time import or offline snapshot import:

```powershell
$env:HA_RECALL_HA_TOKEN = '<HA token>'
uv run --locked ha-recall-import --instance house --ha-url http://homeassistant.local:8123
uv run --locked ha-recall-import --instance house --snapshot registries.json
```

Snapshots contain `areas`, `devices`, and `entities` in HA registry API format.
Keep `--instance` stable across imports for the same household.

## Standalone container, backup, and upgrades

```sh
cp .env.example .env
# Set a random HA_RECALL_TOKEN and any local endpoints.
docker compose up --build -d
```

Compose binds to loopback by default; configure an explicit bind address for access
from another device. Use a TLS proxy across untrusted networks. The container runs
as UID 10001 with a read-only root filesystem and the `memory` data volume.
An optional `WITH_LOCAL_MODEL=1` build includes Model2Vec; mount existing weights
separately as read-only. Compose or `uv run --env-file .env ha-recall` loads `.env`;
the Python module itself does not.

```sh
uv run --locked python tools/backup.py --database data/memory.sqlite3 --output backup.sqlite3
```

The SQLite backup API includes committed WAL writes. To restore, stop the server
and restore the database into an empty data directory. Do not copy only the main
database file while the server is running. Backups include notes, entities, history,
trash, and local permissions. Back up configuration tokens separately.
`export_memory` exports readable records and history for one collection; it does
not replace a complete database backup.

Local version 0.1 databases open directly without losing records; compatibility
tables are created automatically. Back up before upgrading. Importing an original
remote PostgreSQL database is not automatic and is unnecessary for a new household
installation.

## Verification

For the optional Jarvis client convention for dated tasks, revision-safe updates,
and Node-RED daily briefings, see [Dated tasks and daily briefings](docs/jarvis-tasks.md).
This uses the existing MCP entity tools; the memory server itself does not schedule jobs.

```sh
uv sync --locked
uv run --locked pytest
uv run --locked ruff check ha_recall tests_ha tools addon/run.py custom_components tests_ha_runtime
uv run --locked python tools/package_addon.py
docker build -t ha-recall:local .
uv run --locked python tools/docker_smoke.py --image ha-recall:local
docker build -f tests_ha_runtime/Dockerfile -t ha-recall-ha-test:local .
uv run --locked python tools/ha_runtime_check.py --model /path/to/existing/model
uv run --locked python tools/ha_runtime_check.py --model /path/to/existing/model --transport sse
```

The last two commands start a temporary HA Core 2026.10.0 and backend on an isolated
Docker network. They verify configuration flows, incorrect tokens, the actual Assist
API, all 55 tools, Czech semantic search through shared Model2Vec, registry imports,
and integration unload. The test removes its own containers, volumes, network,
and token afterward; it does not use your production HA. CI's `--fixture-model`
option substitutes deterministic vectors for the weights. It verifies integration,
not semantic quality.

Regression tests also cover fact conflicts, temporal validity, collection isolation,
original tool signatures, notes and entities sharing one graph, history, trash,
moves, model caching, concurrent edits, and backups. Container tests cover both
transports, permissions, and restart persistence.
