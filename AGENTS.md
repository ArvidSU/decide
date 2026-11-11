# Working on decisions

## Data survives upgrades

The dogfood server holds real data. Upgrading the code must keep every space, goal, decision, token and event. Never fix a problem by deleting or resetting a database.

- Every change to `SCHEMA` in `decisions/store.py` comes with a migration in the same commit. The database records its schema version in `PRAGMA user_version`. On startup, `Store` runs each migration the database hasn't had yet, in order, each in its own transaction. `SCHEMA` always describes the latest version, so a new database and a migrated one end up identical. If this runner doesn't exist yet when you change the schema, building it is part of your change.
- A migration carries the data over: backfill new columns, move rows when a table is reshaped. Once committed, a migration is never edited. Fix a mistake with a new one.
- The event log is never rewritten, by migrations either: that would break the hash chain. Code that reads events must keep understanding every event type and payload shape ever written. When you rename or reshape an event, old events keep the old form, and readers handle both.
- Test each migration: build a database at the previous version with rows in the affected tables, open it with `Store`, and check that the data is there, the schema equals a fresh database's, and the event chain still verifies.
- Deploying upgrades the database in place, after backing it up. A deploy never needs a reset.

The API, CLI flags and GUI can still change freely, because the server, `decide`, the skill and the resumer are deployed together. Don't keep deprecated aliases or fallbacks for old clients. Rename things when a better name appears, and update every caller. When a change makes something obsolete, delete it rather than keeping it around.

## Keep in sync

A change to behaviour usually touches several of these. Update all that apply in the same change:

- `decisions/store.py` (data and events), `decisions/server.py` (REST), `decisions/cli.py` (`decide`, resumer)
- `decisions/static/` (GUI: vanilla JS + CSS, no build step)
- `decisions/skill/SKILL.md`: what agents are told to do
- `README.md`: concepts, API table, config
- `tests/test_decisions.py`
- `.dash-bored/scripts/decisions-api.py`: the project dashboard

## Conventions

- Zero dependencies: Python ≥ 3.11 stdlib, SQLite, vanilla JS.
- Every mutation appends to the hash-chained event log. Never update or delete events.
- Run the tests with `python3 -m unittest discover tests`.
- Always commit your changes.
