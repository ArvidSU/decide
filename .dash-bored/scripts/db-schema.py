"""Is the local Decisions database at this checkout's schema version?

Usage: python3 .dash-bored/scripts/db-schema.py status

status  status JSON: compares the database's `PRAGMA user_version` with SCHEMA_VERSION in
        decisions/store.py. Read-only.

The server upgrades an older database when it starts, keeping its data (MIGRATIONS in
decisions/store.py), so the fix for an old one is a restart. The database is found the way
`decide serve` finds it: DECISIONS_DB, else `db` in DECISIONS_CONFIG / ./decisions.toml /
~/.config/decisions/config.toml, else ~/.local/share/decisions/decisions.db. Stdlib only; runs on
the macOS system python3 (3.9).
"""
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STORE = os.path.join(ROOT, "decisions", "store.py")
DEFAULT_DB = "~/.local/share/decisions/decisions.db"
HOME = os.path.expanduser("~")


def out(state, detail):
    print(json.dumps({"state": state, "detail": detail}))


def short(path):
    return path.replace(HOME, "~", 1) if path.startswith(HOME) else path


def db_path():
    if os.environ.get("DECISIONS_DB"):
        return os.path.expanduser(os.environ["DECISIONS_DB"])
    for candidate in (os.environ.get("DECISIONS_CONFIG"), "decisions.toml", "~/.config/decisions/config.toml"):
        if candidate and os.path.exists(os.path.expanduser(candidate)):
            with open(os.path.expanduser(candidate)) as fh:
                m = re.search(r'^\s*db\s*=\s*"([^"]+)"', fh.read(), re.M)
            if m:
                return os.path.expanduser(m.group(1))
            break  # like load_config: the first config file found wins
    return os.path.expanduser(DEFAULT_DB)


def expected():
    with open(STORE) as fh:
        m = re.search(r"^SCHEMA_VERSION = (\d+)$", fh.read(), re.M)
    if not m:
        sys.exit("SCHEMA_VERSION not found in decisions/store.py")
    return int(m.group(1))


def version(path):
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def counts(path):
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        return [conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("decisions", "events")]
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def status():
    path = db_path()
    if not os.path.exists(path):
        out("unknown", "No database at %s yet; the server creates it when it starts." % short(path))
        return
    want = expected()
    try:
        have = version(path)
    except sqlite3.Error as exc:
        out("error", "%s is unreadable: %s" % (short(path), exc))
        return
    if have > want:
        out("error", "%s is at schema version %d, written by newer code: this checkout only knows up to %d."
            % (short(path), have, want))
    elif have < want:
        out("warning", "%s is at schema version %s; this checkout is at %d. Restart server: it upgrades the "
            "database when it starts, keeping its data." % (short(path), have or "unrecorded", want))
    else:
        n = counts(path)
        out("healthy", "%s is at schema version %d%s."
            % (short(path), want, " · %d decisions, %d events" % tuple(n) if n else ""))


if __name__ == "__main__":
    if sys.argv[1:] != ["status"]:
        sys.exit("usage: db-schema.py status")
    status()
