# Dated tasks and Jarvis daily briefings

Jarvis can use the existing entity API to store dated household tasks without
adding a second database or changing the 55 MCP tools. Scheduling and briefings
are implemented by the Jarvis client and Node-RED, not by the memory server.
This is an optional client document convention, not a new native task API.

## Storage contract

Tasks are `concept` entities in the `home` namespace. Each entity has a stable
`external_id` prefixed with `jarvis-task:`, the category `jarvis_task_v1`, and a
human-readable `name` containing its title. Its `description` is this JSON object:

```json
{
  "schema": "jarvis_task_v1",
  "due_date": "2026-10-12",
  "status": "open",
  "notes": "Optional details",
  "source": "user:studio"
}
```

The due date must be a canonical ISO calendar date (`YYYY-MM-DD`); it is interpreted
in `Europe/Prague`. Status is `open` or `done`. Voice creation uses `user:voice` as
the source. The entity's normal metadata supplies creation and modification times.
Updates replace the document atomically through `update_entity` with the record's
`expected_revision`, retaining history. A stale edit must be rejected, not retried
blindly over another user's changes. Existing trash and restore tools also apply.

Clients browse all active entities using `list_records` pagination, identify this
category and schema, then filter structured due dates and task status. They must
not use ranked semantic search to claim that a day's task list is complete.
Malformed task documents must produce an error rather than silently disappear.

A due date is **not** `valid_until`: overdue open tasks remain visible. Tomorrow's
list contains tomorrow's tasks; today's briefing includes today's and overdue open
tasks. Completed tasks remain in memory and can be reopened. Ordinary remembered
facts do not automatically become tasks or authorize device actions.

## Current Jarvis client behavior

Jarvis Studio exposes the schedule, latest briefing, source timestamps, and task
creation, editing, completion and reopening. Voice routes handle explicit daily
briefing requests and dated task commands. Ambiguous task titles require choosing
a unique task; the client does not complete a vaguely similar record. The first
version supports date-only, non-recurring tasks.

The Node-RED flow calls the HA action `jarvis_semantic.daily_briefing`:

| Field | Meaning |
| --- | --- |
| `scheduled: true` | Apply the Studio weekday/time settings, a 15-minute catch-up window and a persisted successful-run date. |
| `scheduled: false` | Prepare immediately; suitable for a manual button or an event. |
| `force_refresh: true` | Refresh public news even if the cache is still fresh. |
| `publish: true` | Publish an HA persistent notification when enabled in Studio. |

The initial schedule is Monday through Friday at 07:00 in `Europe/Prague`, including
public holidays that fall on those weekdays. Node-RED checks once a minute; times
and days can be changed in Studio without redeploying flows. A failed or partial
scheduled attempt is retried after five minutes within the catch-up window. An
event-driven flow should provide its own trigger filtering/cooldown and disable
the timed schedule if it is no longer wanted.

Briefings combine a deterministic local date, a date-verified Czech name-day API
response, recent iROZHLAS RSS headlines, and fresh reads of memory tasks and the native HA shopping list. News is
cached for up to three hours and never reused across midnight. Luna can only
select indices into fetched headlines; it cannot invent a headline or link.
Private tasks and shopping items are appended locally and are not sent to the news-selection model.
Source outages are reported explicitly; a memory outage is not an empty task list.

`sensor.jarvis_denni_prehled` exposes the last briefing and schedule; the
`jarvis_daily_briefing_ready` event reports completion. The initial scheduled
delivery is an HA notification. Speech is returned through the normal Jarvis
conversation pipeline on request; no automatic speaker target is assumed.

## Native HA shopping list

Undated shopping items live in the native Home Assistant Shopping List integration,
accessed through its `todo` entity and standard `todo` actions. This list is the
source of truth for Jarvis, Studio, HA dashboards and mobile clients. Shopping
items are not copied into the memory graph; no background synchronization is
needed. Dated reminders such as a trip to the store remain tasks in re:call.

Studio 2.3 includes a dedicated shopping view with add, complete and reopen
controls. It refreshes every five seconds while visible and preserves text being
typed. Completion targets the item's UID. A changed name or status causes a stale
Studio edit to be rejected when detected before writing. HA has no atomic revision
check for these actions; external clients can still race with an update. Writes
are read back before success is reported.

The client serializes its own mutations. Repeated additions of an existing open
name are idempotent after case, accent and whitespace normalization. Re-adding a
single completed match reopens that UID. Ambiguous spoken completion never
completes all matching names: choose the exact item in Studio instead. Multiple
shopping-list entities require explicit configuration support before they can be
used; the current client refuses an ambiguous target.

Supported Czech examples:

- `Přidej mléko na nákupní seznam` — add one shopping item.
- `Co mám koupit?` — read outstanding shopping items.
- `Koupil jsem mléko` — complete the exact matching open item.
- `Zapiš na zítra zajít nakoupit` — create a dated reminder in memory.

The daily briefing reads shopping items afresh even when public news is cached.
An unavailable shopping list is reported as unavailable, never as an empty list.
The sensor also exposes `shopping_count` and `shopping_available`.
