# Working on decisions

## Greenfield: no backwards compatibility

This project has no users to protect yet. Change the schema, API, event names, CLI flags and payloads freely, and do it cleanly:

- No migrations, backfills or upgrade paths for existing databases. Change `SCHEMA` in `decisions/store.py`. If an old database breaks, delete it.
- No fallbacks for data written by older versions, no deprecated aliases, no "legacy" defaults. Rename things when a better name appears, and update every caller.
- Don't spend effort on, or write notes about, how a change affects existing deployments, running agents or previously installed skills.

When a change makes something obsolete, delete it rather than keeping it around.

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
