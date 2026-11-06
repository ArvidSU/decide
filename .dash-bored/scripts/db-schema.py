"""Does the local Decisions database match this checkout's schema, and delete it when it doesn't.

Usage: python3 .dash-bored/scripts/db-schema.py <status|delete>

status  status JSON: compares every table, index and trigger in the database with what
        SCHEMA in decisions/store.py creates. Read-only.
delete  interactive: shows what's out of date, refuses while a process has the file open
        (stop the server first), asks for confirmation, then removes the db and its -wal/-shm.

The project is greenfield: schema changes come without migrations, so an old database is
deleted, not upgraded (AGENTS.md). The database is found the way `decide serve` finds it:
DECISIONS_DB, else `db` in DECISIONS_CONFIG / ./decisions.toml / ~/.config/decisions/config.toml,
else ~/.local/share/decisions/decisions.db. Stdlib only; runs on the macOS system python3 (3.9).
"""
import json
import os
import re
import sqlite3
import subprocess
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


def objects(conn):
    """{(type, name): normalized sql} for every table, index and trigger."""
    rows = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL "
                        "AND name NOT LIKE 'sqlite_%'").fetchall()
    return {(t, n): " ".join(sql.split()) for t, n, sql in rows}


def expected():
    with open(STORE) as fh:
        m = re.search(r'^SCHEMA = """(.*?)"""', fh.read(), re.S | re.M)
    if not m:
        sys.exit("SCHEMA not found in decisions/store.py")
    mem = sqlite3.connect(":memory:")
    mem.executescript(m.group(1))
    return objects(mem)


def differences(path):
    """Human-readable differences between the database and the checkout's schema ([] = current)."""
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        have = objects(conn)
    finally:
        conn.close()
    want = expected()
    diffs = []
    for kind, name in sorted(set(want) - set(have)):
        diffs.append("missing %s %s" % (kind, name))
    for kind, name in sorted(set(have) - set(want)):
        diffs.append("obsolete %s %s" % (kind, name))
    for kind, name in sorted(set(want) & set(have)):
        if want[(kind, name)] == have[(kind, name)]:
            continue
        if kind != "table":
            diffs.append("%s %s is defined differently" % (kind, name))
            continue
        cols = lambda sql: set(re.findall(r"[(,]\s*([a-z_]+) [A-Z]", sql))
        missing = cols(want[(kind, name)]) - cols(have[(kind, name)])
        extra = cols(have[(kind, name)]) - cols(want[(kind, name)])
        diffs.append("%s: %s" % (name, ", ".join(["no %s column" % c for c in sorted(missing)] +
                                                 ["obsolete %s column" % c for c in sorted(extra)])
                                  or "constraints changed"))
    return diffs


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
    try:
        diffs = differences(path)
    except sqlite3.Error as exc:
        out("error", "%s is unreadable: %s" % (short(path), exc))
        return
    if diffs:
        out("warning", "%s predates this checkout's schema (%s). There are no migrations: Kill server, "
            "Delete old database, then Restart server." % (short(path), "; ".join(diffs[:4])
                                                                    + ("; …" if len(diffs) > 4 else "")))
        return
    n = counts(path)
    out("healthy", "%s matches the schema%s." % (short(path), " · %d decisions, %d events" % tuple(n) if n else ""))


def side_files(path):
    return [p for p in (path, path + "-wal", path + "-shm") if os.path.exists(p)]


def delete():
    path = db_path()
    if not os.path.exists(path):
        print("Nothing to delete: no database at %s." % short(path))
        return
    try:
        diffs = differences(path)
        print("Schema: " + ("; ".join(diffs) if diffs else "matches this checkout (it is not out of date)"))
    except sqlite3.Error as exc:
        print("Schema: unreadable (%s)" % exc)
    n = counts(path)
    if n:
        print("Contains %d decisions and %d events." % tuple(n))
    # Listed after reading it: opening a WAL database creates its -wal and -shm files.
    files = side_files(path)
    holders = subprocess.run(["lsof", "-t"] + files, capture_output=True, text=True).stdout.split()
    if holders:
        ps = subprocess.run(["ps", "-o", "pid=,command=", "-p", ",".join(holders)], capture_output=True, text=True)
        sys.exit("Refusing: the database is open in\n%s\nStop the server first (Kill server), then run this again."
                 % ps.stdout.rstrip())
    print("This permanently deletes:\n  " + "\n  ".join(short(p) for p in files))
    print("Machine tokens live in it too: connected machines need new ones afterwards (Settings → Machines).")
    try:
        answer = input("Type delete to confirm: ").strip()
    except EOFError:
        answer = ""
    if answer != "delete":
        sys.exit("Not deleted.")
    for p in side_files(path):
        os.remove(p)
    print("Deleted. Start the server (Serve / restart) to create a fresh database.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "status":
        status()
    elif cmd == "delete":
        delete()
    else:
        sys.exit("usage: db-schema.py <status|delete>")
