"""SQLite storage: decisions, options, the append-only event log, and webhook deliveries.

Decisions and options are freely mutable. Every mutation appends an event to a
hash-chained log that SQLite triggers make immutable (no UPDATE / DELETE), so the
log is the provenance record of what changed, when, and by whom.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .agents import manual_command
from .config import Config

SCHEMA = """
-- Hard boundaries between contexts (work, private, ...). `default` always exists.
CREATE TABLE IF NOT EXISTS spaces (
  name TEXT PRIMARY KEY,
  deadline_days REAL,
  created_at TEXT NOT NULL
);

-- A machine's API token: it sees only its space. Only a hash is stored; the token is shown once.
CREATE TABLE IF NOT EXISTS tokens (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  space TEXT NOT NULL REFERENCES spaces(name),
  hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  rolled_at TEXT,
  last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY,
  num INTEGER NOT NULL UNIQUE,
  space TEXT NOT NULL REFERENCES spaces(name),
  title TEXT NOT NULL,
  context TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  min_select INTEGER NOT NULL DEFAULT 1,
  max_select INTEGER,
  attribute_defs TEXT NOT NULL DEFAULT '{}',
  links TEXT NOT NULL DEFAULT '[]',
  issues TEXT NOT NULL DEFAULT '[]',
  tags TEXT NOT NULL DEFAULT '[]',
  metadata TEXT NOT NULL DEFAULT '{}',
  resolution TEXT,
  due_at TEXT,
  idempotency_key TEXT,
  resume TEXT,
  next_option INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT,
  UNIQUE (space, idempotency_key)
);
CREATE INDEX IF NOT EXISTS decisions_by_space ON decisions(space, status);

CREATE TABLE IF NOT EXISTS options (
  decision_id TEXT NOT NULL REFERENCES decisions(id),
  id TEXT NOT NULL,
  label TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  url TEXT,
  image TEXT,
  attributes TEXT NOT NULL DEFAULT '{}',
  attribute_meta TEXT NOT NULL DEFAULT '{}',
  excluded INTEGER NOT NULL DEFAULT 0,
  exclude_reason TEXT,
  suggested_by TEXT,
  suggested_at TEXT,
  position INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (decision_id, id)
);

-- decision_id can't be decided before depends_on is settled. With when_selected (option ids), it only
-- applies if depends_on picks one of them; any other outcome cancels it. Both are in the same space.
CREATE TABLE IF NOT EXISTS dependencies (
  decision_id TEXT NOT NULL REFERENCES decisions(id),
  depends_on TEXT NOT NULL REFERENCES decisions(id),
  when_selected TEXT,
  note TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL,
  PRIMARY KEY (decision_id, depends_on)
);
CREATE INDEX IF NOT EXISTS dependencies_by_upstream ON dependencies(depends_on);

-- Why decisions are made. A goal serves larger goals (goal_links), from a vision down to technical
-- goals, and decisions are steps toward goals (decision_goals). Everything linked is in one space.
CREATE TABLE IF NOT EXISTS goals (
  id TEXT PRIMARY KEY,
  num INTEGER NOT NULL UNIQUE,
  space TEXT NOT NULL REFERENCES spaces(name),
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  status_note TEXT NOT NULL DEFAULT '',
  issues TEXT NOT NULL DEFAULT '[]',
  tags TEXT NOT NULL DEFAULT '[]',
  proof TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  closed_at TEXT
);
CREATE INDEX IF NOT EXISTS goals_by_space ON goals(space, status);

CREATE TABLE IF NOT EXISTS goal_links (
  goal_id TEXT NOT NULL REFERENCES goals(id),
  serves TEXT NOT NULL REFERENCES goals(id),
  position INTEGER NOT NULL,
  PRIMARY KEY (goal_id, serves)
);
CREATE INDEX IF NOT EXISTS goal_links_by_parent ON goal_links(serves);

CREATE TABLE IF NOT EXISTS decision_goals (
  decision_id TEXT NOT NULL REFERENCES decisions(id),
  goal_id TEXT NOT NULL REFERENCES goals(id),
  position INTEGER NOT NULL,
  PRIMARY KEY (decision_id, goal_id)
);
CREATE INDEX IF NOT EXISTS decision_goals_by_goal ON decision_goals(goal_id);

-- Each space has its own hash chain; space NULL is the server's own (webhooks across all spaces).
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  space TEXT,
  ts TEXT NOT NULL,
  decision_id TEXT,
  goal_id TEXT,
  type TEXT NOT NULL,
  actor TEXT NOT NULL,
  data TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_decision ON events(decision_id, seq);
CREATE INDEX IF NOT EXISTS events_by_goal ON events(goal_id, seq);
CREATE INDEX IF NOT EXISTS events_by_space ON events(space, seq);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;

-- space NULL = hears every space (only the admin token can create one).
CREATE TABLE IF NOT EXISTS subscriptions (
  id TEXT PRIMARY KEY,
  space TEXT REFERENCES spaces(name),
  decision_id TEXT,
  url TEXT NOT NULL,
  secret TEXT NOT NULL,
  events TEXT NOT NULL,
  require_ack INTEGER,
  max_attempts INTEGER,
  refire_interval REAL,
  active INTEGER NOT NULL DEFAULT 1,
  description TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
  id TEXT PRIMARY KEY,
  subscription_id TEXT NOT NULL,
  space TEXT,
  event_seq INTEGER NOT NULL,
  decision_id TEXT,
  event_type TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL,
  require_ack INTEGER NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  total_attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL,
  last_status INTEGER,
  last_error TEXT,
  delivered_at TEXT,
  acked_at TEXT,
  acked_by TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(next_attempt_at);
CREATE INDEX IF NOT EXISTS deliveries_by_decision ON deliveries(decision_id);

CREATE TABLE IF NOT EXISTS delivery_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  delivery_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  url TEXT NOT NULL,
  status INTEGER,
  error TEXT,
  duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS attempts_by_delivery ON delivery_attempts(delivery_id);
CREATE TRIGGER IF NOT EXISTS attempts_no_update BEFORE UPDATE ON delivery_attempts
  BEGIN SELECT RAISE(ABORT, 'delivery attempts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS attempts_no_delete BEFORE DELETE ON delivery_attempts
  BEGIN SELECT RAISE(ABORT, 'delivery attempts are immutable'); END;
"""

# The version of SCHEMA. Each database records the version it's at in `PRAGMA user_version`.
# Changing SCHEMA means adding one to this and a migration below.
SCHEMA_VERSION = 1

# version -> a function taking the connection, that brings a database at the version before to it.
# `Store` runs the ones a database hasn't had, in order, in one transaction with foreign keys off, and
# rolls all of it back unless the result is exactly what SCHEMA creates. Use conn.execute (executescript
# would commit), carry the data over, and leave the rows of `events` as they are: rewriting them breaks
# the hash chain. A migration that has been committed is never edited.
MIGRATIONS: dict = {}

DEFAULT_WEBHOOK_EVENTS = [
    "decision.resolved",
    "decision.reopened",
    "decision.cancelled",
    "decision.expired",
    "attributes.requested",
    "options.suggested",
]
STATUSES = ("open", "resolved", "cancelled", "expired")
# The event that put a decision in each closed status. `expired` = nobody decided before due_at;
# `cancelled` = somebody closed it on purpose.
TERMINAL_EVENTS = {"resolved": "decision.resolved", "cancelled": "decision.cancelled", "expired": "decision.expired"}
# Events that wake the requesting agent's session (via the resumer): the outcome, and while it's open,
# the user asking it to fill values in.
RESUME_TRIGGERS = {"resolved": "decision.resolved", "expired": "decision.expired"}
FILL_TRIGGERS = ("options.suggested", "attributes.requested")
DEADLINE_ACTOR = "deadline"
# Actor for what a decision's dependencies do to it: cancel it when it no longer applies, unlock it.
DEPENDENCY_ACTOR = "dependency"
# Events after which a decision that depends on this one may need another look.
SETTLING_EVENTS = ("decision.resolved", "decision.reopened", "decision.cancelled", "decision.expired")
GOAL_STATUSES = ("active", "achieved", "abandoned")
# The event a goal's status change is logged as.
GOAL_EVENTS = {"active": "goal.reactivated", "achieved": "goal.achieved", "abandoned": "goal.abandoned"}
# How a goal is proved achieved: a command the resumer runs on the agent's machine when the user asks for it.
# These events report on a run, and put it in that state.
PROOF_RUN_EVENTS = ("started", "passed", "failed")
# A proof's result wakes the session that owns the goal (`proof.resume`), the way a resolution wakes a decision's.
PROOF_RESUME_TRIGGERS = ("proof.passed", "proof.failed")
# Loop guard: after this many `proof.failed` in a row the agent isn't resumed again, until the proof passes or
# is proposed anew (`proof.proposed` / `proof.changed`), which reset the count.
PROOF_RESUME_FAILURES = 3
PROOF_TIMEOUT = 3600
# A run that hasn't reported back this long (seconds) after its timeout is given up on, so it can be asked for again.
PROOF_GRACE = 300
MAX_PROOF_SCRIPT = 256 * 1024
GENESIS = "0" * 64
DEFAULT_SPACE = "default"
SPACE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
TOKEN_PREFIX = "dk_"
# A token's last_used_at is written at most this often (seconds), not on every request.
LAST_USED_EVERY = 60


@dataclass(frozen=True)
class Scope:
    """What a caller may see: one space (a machine token), or (spaces=None) all of them (the admin)."""
    spaces: frozenset[str] | None = None
    # Where the caller's new decisions and webhooks go.
    default: str = DEFAULT_SPACE
    # The machine token used, if any.
    token: str | None = None

    @property
    def everything(self) -> bool:
        return self.spaces is None

    def allows(self, space: str | None) -> bool:
        return self.spaces is None or space in self.spaces

    def where(self, column: str) -> tuple[str, list]:
        """SQL condition (with a leading AND) limiting `column` to this scope."""
        if self.spaces is None:
            return "", []
        return f" AND {column} IN ({','.join('?' * len(self.spaces))})", sorted(self.spaces)


ALL = Scope()


class SchemaError(Exception):
    """The database can't be brought to this code's schema. It is left as it was."""


def schema_structure(conn) -> dict:
    """{(kind, name): shape} for every table, index and trigger: what a schema amounts to, whether
    CREATE or ALTER made it."""
    def rows(pragma, name):
        return [tuple(r) for r in conn.execute('PRAGMA %s("%s")' % (pragma, name.replace('"', '""')))]

    shapes: dict = {}
    for kind, name, sql in conn.execute("SELECT type, name, sql FROM sqlite_master "
                                        "WHERE name NOT LIKE 'sqlite_%'").fetchall():
        if kind == "table":
            constraints = []
            for _, index, unique, origin, partial in rows("index_list", name):
                columns = tuple(r[2] for r in rows("index_info", index))
                if origin == "c":
                    shapes[("index", index)] = (name, unique, partial, columns)
                else:  # from a PRIMARY KEY or UNIQUE constraint; its name is generated
                    constraints.append((origin, columns))
            shapes[(kind, name)] = (sorted(r[1:] for r in rows("table_xinfo", name)),
                                    sorted(r[2:] for r in rows("foreign_key_list", name)), sorted(constraints))
        elif kind != "index":
            shapes[(kind, name)] = " ".join(sql.replace("IF NOT EXISTS ", "").split())
    return shapes


def schema_differences(have: dict, want: dict) -> list[str]:
    """How the schema `have` differs from `want`, in words ([] = they're the same)."""
    diffs = [f"no {kind} {name}" for kind, name in sorted(want.keys() - have.keys())]
    diffs += [f"unknown {kind} {name}" for kind, name in sorted(have.keys() - want.keys())]
    for kind, name in sorted(k for k in want.keys() & have.keys() if want[k] != have[k]):
        if kind != "table":
            diffs.append(f"{kind} {name} is defined differently")
            continue
        wanted, found = ({c[0] for c in shape[0]} for shape in (want[kind, name], have[kind, name]))
        diffs.append(f"{name}: " + (", ".join([f"no {c} column" for c in sorted(wanted - found)]
                                              + [f"unknown {c} column" for c in sorted(found - wanted)])
                                    or "columns or constraints are defined differently"))
    return diffs


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ts(value) -> datetime | None:
    """ISO 8601 timestamp as an aware datetime (naive = UTC), or None if it isn't one."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _due_at(value) -> str:
    dt = parse_ts(value)
    if dt is None:
        raise ApiError(400, "due_at must be an ISO 8601 timestamp, e.g. 2026-11-09T17:00:00Z")
    return iso(dt)


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def norm_id(raw: str) -> str:
    m = re.fullmatch(r"(?i)\s*(?:d-?)?(\d+)\s*", str(raw))
    if not m:
        raise ApiError(404, f"no decision {raw!r}")
    return f"D-{int(m.group(1))}"


def norm_goal_id(raw: str) -> str:
    m = re.fullmatch(r"(?i)\s*(?:g-?)?(\d+)\s*", str(raw))
    if not m:
        raise ApiError(404, f"no goal {raw!r}")
    return f"G-{int(m.group(1))}"


def _goal_ids(value, key: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ApiError(400, f"{key} must be a list of goal ids, e.g. [\"G-3\"]")
    return list(dict.fromkeys(norm_goal_id(v) for v in value))


def _label_key(label: str) -> str:
    """Options from different decisions count as the same option when their labels match like this."""
    return " ".join(label.lower().split())


def event_matches(patterns: list[str], event_type: str) -> bool:
    for p in patterns:
        if p == "*" or p == event_type or (p.endswith(".*") and event_type.startswith(p[:-1])):
            return True
    return False


def _req_str(body: dict, key: str) -> str:
    v = body.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ApiError(400, f"'{key}' is required and must be a non-empty string")
    return v.strip()


def _opt_type(body: dict, key: str, kinds, default=None):
    if key not in body or body[key] is None:
        return default
    if not isinstance(body[key], kinds):
        names = kinds.__name__ if isinstance(kinds, type) else "/".join(k.__name__ for k in kinds)
        raise ApiError(400, f"'{key}' must be {names}")
    return body[key]


def _links(value) -> list:
    out = []
    for item in value or []:
        if isinstance(item, str):
            out.append({"url": item})
        elif isinstance(item, dict) and isinstance(item.get("url"), str):
            out.append({k: item[k] for k in ("url", "title", "kind") if k in item})
        else:
            raise ApiError(400, "links must be URLs or {url, title?, kind?} objects")
    return out


def _issue_key(url: str) -> str:
    """What to call an issue by its URL: ABC-123 (Linear, Jira, …), repo#42 (GitHub, GitLab), else the host."""
    parts = urllib.parse.urlsplit(url)
    if m := re.search(r"\b([A-Z][A-Z0-9]*-\d+)\b", parts.path + "?" + parts.query):
        return m.group(1)
    if m := re.search(r"/([^/]+)/(?:-/)?(issues|pull|merge_requests)/(\d+)", parts.path):
        return f"{m.group(1)}{'!' if m.group(2) == 'merge_requests' else '#'}{m.group(3)}"
    return parts.hostname or url


def _issues(value) -> list:
    """`issues`: where a decision or goal is being acted on. URLs or {url, key?}; key defaults to one read off the URL."""
    if not isinstance(value, list):
        raise ApiError(400, "issues must be a list of URLs or {url, key?} objects")
    out = {}
    for item in value:
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict) or not isinstance(item.get("url"), str) \
                or not re.match(r"https?://[^\s/]+", item["url"].strip()):
            raise ApiError(400, "issues must be a list of http(s) URLs or {url, key?} objects")
        url = item["url"].strip()
        key = item.get("key")
        out[url] = {"url": url, "key": key.strip() if isinstance(key, str) and key.strip() else _issue_key(url)}
    return list(out.values())


def _dependencies(value) -> list[dict]:
    """`depends_on`: ["D-12", {"decision": "D-12", "when": ["redis"], "note": "…"}, …]."""
    if not isinstance(value, list):
        raise ApiError(400, "depends_on must be a list of decision ids or {decision, when?, note?} objects")
    out = {}
    for item in value:
        if isinstance(item, str):
            item = {"decision": item}
        if not isinstance(item, dict) or not isinstance(item.get("decision"), str):
            raise ApiError(400, "depends_on must be a list of decision ids or {decision, when?, note?} objects")
        when = item.get("when")
        if isinstance(when, str):
            when = [when]
        if when is not None and (not isinstance(when, list) or not when):
            raise ApiError(400, "depends_on[].when must be a non-empty list of option ids (or left out)")
        did = norm_id(item["decision"])
        out[did] = {"decision": did, "when": [str(w) for w in dict.fromkeys(when)] if when else None,
                    "note": _opt_type(item, "note", str, "")}
    return list(out.values())


def script_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def proof_hash(proof: dict) -> str:
    """Identifies what was agreed on: what success looks like, the command, its scripts and where it runs."""
    return hashlib.sha256(canonical(
        {k: proof[k] for k in ("success", "command", "cwd", "machine", "timeout")}
        | {"scripts": [{"path": s["path"], "sha256": s["sha256"]} for s in proof["scripts"]]}).encode()).hexdigest()


def _proof(body: dict) -> dict:
    """`proof`: {success, command, machine, cwd?, timeout?, scripts?: [{path, content}]}."""
    timeout = _opt_type(body, "timeout", (int, float), PROOF_TIMEOUT)
    if isinstance(timeout, bool) or timeout <= 0:
        raise ApiError(400, "timeout must be a positive number of seconds")
    scripts = {}
    for item in _opt_type(body, "scripts", list, []):
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("path", "content")) \
                or not item["path"].strip():
            raise ApiError(400, "scripts must be a list of {path, content} objects")
        if len(item["content"].encode()) > MAX_PROOF_SCRIPT:
            raise ApiError(400, f"{item['path']} is larger than {MAX_PROOF_SCRIPT // 1024} KB")
        scripts[item["path"]] = {"path": item["path"], "sha256": script_hash(item["content"].encode()),
                                 "content": item["content"]}
    proof = {"success": _req_str(body, "success"), "command": _req_str(body, "command"),
             "machine": _req_str(body, "machine"), "cwd": _opt_type(body, "cwd", str, ""), "timeout": timeout,
             "scripts": list(scripts.values())}
    return proof | {"hash": proof_hash(proof)}


def _resume(value) -> dict | None:
    """How to pick the requesting agent back up once the decision is made."""
    if value is None:
        return None
    required = ("session_id", "agent", "machine")
    if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k].strip() for k in required):
        raise ApiError(400, "resume must be an object with 'session_id', 'agent' and 'machine' (and usually 'cwd')")
    for k in ("cwd", "prompt"):
        if k in value and not isinstance(value[k], str):
            raise ApiError(400, f"resume.{k} must be a string")
    return {k: value[k] for k in ("session_id", "cwd", "agent", "prompt", "machine") if k in value}


class Store:
    def __init__(self, cfg: Config, path: str | None = None):
        self.cfg = cfg
        path = path or cfg.db_path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        # The schema version the database was at: None = it's new, 0 = from before versions were recorded.
        self.schema_was = self._upgrade()
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("INSERT OR IGNORE INTO spaces (name, created_at) VALUES (?, ?)", (DEFAULT_SPACE, now_iso()))
        # Last heartbeat per machine from resumer daemons (`decide resumer run`). In memory only.
        self.resumers: dict[str, dict] = {}
        # Who is long-polling which decision right now: (decision, actor, types) -> info. In memory only.
        self.waiters: dict[tuple, dict] = {}

    # ------------------------------------------------------------------ schema

    def _upgrade(self) -> int | None:
        """Bring the database to SCHEMA_VERSION, keeping its data, or raise SchemaError and leave it
        untouched. Returns the version it was at (None = it's new)."""
        conn = self.conn
        was = conn.execute("PRAGMA user_version").fetchone()[0]
        if not conn.execute("SELECT 1 FROM sqlite_master").fetchone():
            conn.executescript(f"BEGIN; {SCHEMA} PRAGMA user_version={SCHEMA_VERSION}; COMMIT;")
            return None
        if was > SCHEMA_VERSION:
            raise SchemaError(f"the database is at schema version {was}, and this code only knows up to "
                              f"{SCHEMA_VERSION}: it was written by a newer version. Upgrade the code.")
        fresh = sqlite3.connect(":memory:")
        fresh.executescript(SCHEMA)
        want = schema_structure(fresh)
        fresh.close()
        conn.execute("PRAGMA foreign_keys=OFF")  # so a migration can rebuild a table; checked below instead
        conn.execute("BEGIN IMMEDIATE")
        try:
            # A database from before versions were recorded has the first versioned schema.
            for version in range((was or 1) + 1, SCHEMA_VERSION + 1):
                MIGRATIONS[version](conn)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            diffs = schema_differences(schema_structure(conn), want)
            if diffs:
                raise SchemaError(
                    f"the database (schema version {was or 'unrecorded'}) doesn't have the schema of version "
                    f"{SCHEMA_VERSION}{' after migrating' if (was or 1) < SCHEMA_VERSION else ''}: "
                    f"{'; '.join(diffs)}. It was left as it was. A change to SCHEMA needs a migration that "
                    "ends up with exactly what SCHEMA creates.")
            migrated = (was or 1) < SCHEMA_VERSION
            broken = conn.execute("PRAGMA foreign_key_check").fetchall() if migrated else []
            if broken:
                raise SchemaError(f"migrating to schema version {SCHEMA_VERSION} left {len(broken)} rows pointing "
                                  f"at rows that don't exist (in {', '.join(sorted({r[0] for r in broken}))}). "
                                  "The database was left as it was.")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return was

    # ------------------------------------------------------------------ plumbing

    def deadline_days_for(self, space: str) -> float:
        with self.lock:
            row = self.conn.execute("SELECT deadline_days FROM spaces WHERE name=?", (space,)).fetchone()
        return self.cfg.deadline_days if not row or row["deadline_days"] is None else row["deadline_days"]

    def default_due_at(self, space: str) -> str | None:
        days = self.deadline_days_for(space)
        return iso(datetime.now(timezone.utc) + timedelta(days=days)) if days > 0 else None

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.cond.notify_all()

    def q(self, sql, *args):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def q1(self, sql, *args):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def _decision_row(self, did: str, scope: Scope = ALL):
        row = self.conn.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
        # Out of scope looks exactly like missing: other spaces' decisions aren't even confirmed to exist.
        if not row or not scope.allows(row["space"]):
            raise ApiError(404, f"no decision {did}")
        return row

    def _space_for(self, scope: Scope, requested) -> str:
        """The space a caller's new decision or webhook goes to: the one asked for, else their default."""
        space = _opt_type({"space": requested}, "space", str) or scope.default
        if not scope.allows(space) or not self.q1("SELECT 1 FROM spaces WHERE name=?", space):
            raise ApiError(404, f"no space {space!r}")
        return space

    @staticmethod
    def _record(space, ts, decision_id, goal_id, etype, actor, data, prev_hash) -> str:
        record = {"space": space, "ts": ts, "decision_id": decision_id, "goal_id": goal_id, "type": etype,
                  "actor": actor, "data": data, "prev_hash": prev_hash}
        return hashlib.sha256(canonical(record).encode()).hexdigest()

    def _event(self, decision_id, etype, actor, data, space=None, goal_id=None) -> int:
        """Append to the space's hash chain and fan out webhook deliveries. Call inside tx().
        An event about a decision or a goal goes to its space; others to `space`."""
        if decision_id:
            space = self._decision_row(decision_id)["space"]
        elif goal_id:
            space = self._goal_row(goal_id)["space"]
        last = self.conn.execute("SELECT hash FROM events WHERE space IS ? ORDER BY seq DESC LIMIT 1",
                                 (space,)).fetchone()
        prev_hash = last["hash"] if last else GENESIS
        ts = now_iso()
        digest = self._record(space, ts, decision_id, goal_id, etype, actor, data, prev_hash)
        seq = self.conn.execute(
            "INSERT INTO events (space, ts, decision_id, goal_id, type, actor, data, prev_hash, hash) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (space, ts, decision_id, goal_id, etype, actor, canonical(data), prev_hash, digest),
        ).lastrowid
        if not etype.startswith("webhook."):
            self._fanout(seq, ts, decision_id, space, etype, actor, data.get("silenced", ()), goal_id)
        return seq

    def _fanout(self, seq, ts, decision_id, space, etype, actor, silenced=(), goal_id=None):
        subs = self.conn.execute(
            "SELECT * FROM subscriptions WHERE active=1 AND (decision_id=? OR "
            "(decision_id IS NULL AND (space IS NULL OR space=?)))",
            (decision_id, space),
        ).fetchall()
        snapshot = goal = None
        for sub in subs:
            if sub["id"] in silenced or not event_matches(json.loads(sub["events"]), etype):
                continue
            if snapshot is None and decision_id:
                snapshot = self._decision_dict(decision_id)
            if goal is None and goal_id:
                goal = self._goal_dict(goal_id)
            self._enqueue(sub, seq, ts, decision_id, etype, actor, snapshot, goal=goal)

    def _enqueue(self, sub, seq, ts, decision_id, etype, actor, snapshot, replayed=False, goal=None):
        require_ack = self.cfg.webhook_require_ack if sub["require_ack"] is None else bool(sub["require_ack"])
        delivery_id = "dlv_" + secrets.token_hex(8)
        payload = {
            "event": {"seq": seq, "type": etype, "ts": ts, "actor": actor},
            "delivery": {
                "id": delivery_id,
                "subscription_id": sub["id"],
                "require_ack": require_ack,
                "ack_url": f"{self.cfg.public_url}/api/deliveries/{delivery_id}/ack",
                "replayed": replayed,
            },
            "decision": snapshot,
        }
        if goal:
            payload["goal"] = goal
        self.conn.execute(
            """INSERT INTO deliveries (id, subscription_id, space, event_seq, decision_id, event_type, payload,
               status, require_ack, next_attempt_at, created_at) VALUES (?,?,?,?,?,?,?,'pending',?,?,?)""",
            (delivery_id, sub["id"], sub["space"], seq, decision_id, etype, canonical(payload),
             int(require_ack), time.time(), now_iso()),
        )

    def _replay_terminal(self, sub_id: str, decision_id: str):
        """A webhook registered after the decision was already made still hears about it."""
        row = self._decision_row(decision_id)
        terminal = TERMINAL_EVENTS.get(row["status"])
        sub = self.conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
        if not terminal or not event_matches(json.loads(sub["events"]), terminal):
            return
        ev = self.conn.execute(
            "SELECT * FROM events WHERE decision_id=? AND type=? ORDER BY seq DESC LIMIT 1",
            (decision_id, terminal)).fetchone()
        if ev:
            self._enqueue(sub, ev["seq"], ev["ts"], decision_id, terminal, ev["actor"],
                          self._decision_dict(decision_id), replayed=True)

    def _touch(self, did):
        self.conn.execute("UPDATE decisions SET updated_at=? WHERE id=?", (now_iso(), did))

    # ------------------------------------------------------------------ serialization

    @staticmethod
    def _option_dict(row) -> dict:
        return {
            "id": row["id"],
            "label": row["label"],
            "description": row["description"],
            "url": row["url"],
            "image": row["image"],
            "attributes": json.loads(row["attributes"]),
            "attribute_meta": json.loads(row["attribute_meta"]),
            "excluded": bool(row["excluded"]),
            "exclude_reason": row["exclude_reason"],
            "suggested_by": row["suggested_by"],
            "suggested_at": row["suggested_at"],
            "position": row["position"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _decision_dict(self, did: str, row=None) -> dict:
        row = row or self._decision_row(did)
        options = [self._option_dict(r) for r in self.conn.execute(
            "SELECT * FROM options WHERE decision_id=? ORDER BY position, rowid", (did,))]
        defs = json.loads(row["attribute_defs"])
        resolution = json.loads(row["resolution"]) if row["resolution"] else None
        # Values the requester was asked for: a requested attribute on every option, and every attribute on an
        # option the user suggested. A value counts as answered once it's filled in or marked unknown.
        keys = list(dict.fromkeys([*defs, *(k for o in options for k in o["attributes"])]))
        pending = {}
        for key in keys:
            missing = [o["id"] for o in options
                       if not o["excluded"] and (defs.get(key, {}).get("requested") or o["suggested_by"])
                       and o["attributes"].get(key) is None and not o["attribute_meta"].get(key, {}).get("unknown")]
            if missing:
                pending[key] = {"description": defs.get(key, {}).get("description", ""), "missing": missing}
        subs = self.conn.execute(
            "SELECT * FROM subscriptions WHERE decision_id=? ORDER BY created_at", (did,)).fetchall()
        out = {
            "id": row["id"],
            "space": row["space"],
            "url": f"{self.cfg.public_url}/d/{row['id']}",
            "title": row["title"],
            "context": row["context"],
            "source": row["source"],
            "status": row["status"],
            "min_select": row["min_select"],
            "max_select": row["max_select"],
            "attribute_defs": defs,
            "links": json.loads(row["links"]),
            "issues": json.loads(row["issues"]),
            "tags": json.loads(row["tags"]),
            "metadata": json.loads(row["metadata"]),
            "due_at": row["due_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "resolved_at": row["resolved_at"],
            "resolution": resolution,
            "resume": (resume := json.loads(row["resume"]) if row["resume"] else None),
            "resume_command": manual_command(resume),
            "resume_state": self._resume_state(did, row),
            "selected_options": [o for o in options if resolution and o["id"] in resolution["selected"]],
            "pending_attribute_requests": pending,
            "options": options,
            "webhooks": [self._sub_dict(s) for s in subs],
            "goals": [self._goal_ref(g) for g in self._decision_goal_ids(did)],
        }
        return out | self._dependency_view(did, row)

    # ------------------------------------------------------------------ dependencies

    @staticmethod
    def _dep_state(up, when) -> str:
        """`waiting` while the upstream is open; `met` once it's settled in a way this decision applies to;
        `unmet` when it settled any other way (a conditional dependency on something cancelled, expired or
        decided otherwise)."""
        if up["status"] == "open":
            return "waiting"
        if when is None:
            return "met"
        selected = json.loads(up["resolution"])["selected"] if up["status"] == "resolved" else []
        return "met" if set(selected) & set(when) else "unmet"

    def _dep_specs(self, did: str) -> list[dict]:
        return [{"decision": r["depends_on"], "when": json.loads(r["when_selected"]) if r["when_selected"] else None,
                 "note": r["note"]} for r in self.conn.execute(
            "SELECT * FROM dependencies WHERE decision_id=? ORDER BY position", (did,))]

    def _open_upstreams(self, did: str) -> list[str]:
        return [r["id"] for r in self.conn.execute(
            "SELECT d.id FROM dependencies x JOIN decisions d ON d.id = x.depends_on "
            "WHERE x.decision_id=? AND d.status='open' ORDER BY x.position", (did,))]

    def _dependency_view(self, did: str, row) -> dict:
        """What this decision waits on, what it affects, and which upstream changed after it was decided."""
        depends_on, outdated = [], []
        last_resolved = self.conn.execute(
            "SELECT MAX(seq) FROM events WHERE decision_id=? AND type='decision.resolved'", (did,)).fetchone()[0]
        for spec in self._dep_specs(did):
            up = self.conn.execute("SELECT * FROM decisions WHERE id=?", (spec["decision"],)).fetchone()
            labels = dict(self.conn.execute("SELECT id, label FROM options WHERE decision_id=?", (up["id"],)).fetchall())
            depends_on.append(spec | {
                "title": up["title"], "status": up["status"], "state": self._dep_state(up, spec["when"]),
                "when_labels": [labels.get(w, w) for w in spec["when"] or []],
                "selected": json.loads(up["resolution"])["selected"] if up["resolution"] else [],
            })
            if row["status"] == "resolved" and (up["status"] == "open" or self.conn.execute(
                    f"SELECT 1 FROM events WHERE decision_id=? AND seq>? AND type IN ({','.join('?' * len(SETTLING_EVENTS))})",
                    (up["id"], last_resolved or 0, *SETTLING_EVENTS)).fetchone()):
                outdated.append(up["id"])
        affects = [{"decision": r["decision_id"], "title": r["title"], "status": r["status"],
                    "when": json.loads(r["when_selected"]) if r["when_selected"] else None, "note": r["note"],
                    "waiting_on": self._open_upstreams(r["decision_id"]) if r["status"] == "open" else []}
                   for r in self.conn.execute(
                       "SELECT x.*, d.title, d.status FROM dependencies x JOIN decisions d ON d.id = x.decision_id "
                       "WHERE x.depends_on=? ORDER BY d.num", (did,))]
        return {"depends_on": depends_on, "affects": affects, "outdated_by": outdated,
                "waiting_on": [x["decision"] for x in depends_on if x["state"] == "waiting"]
                if row["status"] == "open" else []}

    def _check_dependencies(self, did: str, space: str, specs: list[dict], applies: bool) -> list[dict]:
        """Validate a decision's `depends_on`. `applies`: refuse dependencies it already couldn't meet."""
        for spec in specs:
            up = spec["decision"]
            if up == did:
                raise ApiError(400, "a decision can't depend on itself")
            row = self.conn.execute("SELECT * FROM decisions WHERE id=?", (up,)).fetchone()
            if not row or row["space"] != space:
                raise ApiError(404, f"no decision {up}")
            if spec["when"]:
                ids = {r["id"] for r in self.conn.execute("SELECT id FROM options WHERE decision_id=?", (up,))}
                unknown = [w for w in spec["when"] if w not in ids]
                if unknown:
                    raise ApiError(400, f"{up} has no option {', '.join(map(repr, unknown))}")
            if applies and self._dep_state(row, spec["when"]) == "unmet":
                raise ApiError(409, f"{up} is already {row['status']} without picking "
                                    f"{' or '.join(spec['when'])}, so this wouldn't apply")
            # Following the chain upstream must never lead back here.
            seen, todo = set(), [up]
            while todo:
                cur = todo.pop()
                if cur == did:
                    raise ApiError(400, f"depending on {up} would make a cycle")
                if cur not in seen:
                    seen.add(cur)
                    todo += [r["depends_on"] for r in self.conn.execute(
                        "SELECT depends_on FROM dependencies WHERE decision_id=?", (cur,))]
        return specs

    def _write_dependencies(self, did: str, specs: list[dict]):
        self.conn.execute("DELETE FROM dependencies WHERE decision_id=?", (did,))
        for i, spec in enumerate(specs):
            self.conn.execute(
                "INSERT INTO dependencies (decision_id, depends_on, when_selected, note, position) VALUES (?,?,?,?,?)",
                (did, spec["decision"], canonical(spec["when"]) if spec["when"] else None, spec["note"], i))

    def _restart_deadline(self, row) -> str | None:
        """The due_at a decision gets when it becomes decidable again: its own, unless that has passed or is missing."""
        due = parse_ts(row["due_at"])
        if (due is None and self.deadline_days_for(row["space"]) > 0) or (due and due <= datetime.now(timezone.utc)):
            return self.default_due_at(row["space"])
        return row["due_at"]

    def _unlock(self, did: str, after: str):
        """Every upstream is settled: the decision can be decided now, and its deadline starts."""
        row = self._decision_row(did)
        due_at = self._restart_deadline(row)
        self.conn.execute("UPDATE decisions SET due_at=?, updated_at=? WHERE id=?", (due_at, now_iso(), did))
        self._event(did, "decision.unblocked", DEPENDENCY_ACTOR, {"after": after, "due_at": due_at})

    def _settle_dependents(self, upstream: str, was_open: bool):
        """Bring the decisions that depend on `upstream` in line with its new status. Call inside tx().
        Open ones that no longer apply are cancelled (and so on down the chain); open ones it was holding
        back are unlocked; anything already decided is left as is and shows up as outdated."""
        up = self._decision_row(upstream)
        selected = json.loads(up["resolution"])["selected"] if up["resolution"] else []
        for dep in self.conn.execute("SELECT * FROM dependencies WHERE depends_on=?", (upstream,)).fetchall():
            did = dep["decision_id"]
            row = self._decision_row(did)
            when = json.loads(dep["when_selected"]) if dep["when_selected"] else None
            state = self._dep_state(up, when)
            change = {"decision": upstream, "status": up["status"], "selected": selected}
            if row["status"] == "resolved" or (row["status"] == "open" and state == "waiting"):
                self._event(did, "dependency.changed", DEPENDENCY_ACTOR, change)
            elif row["status"] != "open":
                continue
            elif state == "unmet":
                picked = ", ".join(selected) if selected else "nothing"
                note = (f"No longer applies: {upstream} was {up['status']}" +
                        (f" with {picked}" if up["status"] == "resolved" else "") +
                        f", and this only applied if it picked {' or '.join(when)}.")
                self.conn.execute("UPDATE decisions SET status='cancelled', updated_at=? WHERE id=?", (now_iso(), did))
                self._event(did, "decision.cancelled", DEPENDENCY_ACTOR,
                            {"note": note, "previous_status": "open", "previous_resolution": None, "dependency": change})
                self._settle_dependents(did, was_open=True)
            elif not was_open:
                self._event(did, "dependency.changed", DEPENDENCY_ACTOR, change)
            elif not self._open_upstreams(did):
                self._unlock(did, upstream)

    def _resume_state(self, did: str, row) -> dict | None:
        """Where the requester's resumption stands for the latest resolution (or expiry) or, while the decision
        is open, the latest request to fill values in since it was (re)opened."""
        if not row["resume"]:
            return None
        if row["status"] == "open":
            res = self.conn.execute(
                f"SELECT seq, ts, type FROM events WHERE decision_id=? AND type IN ({','.join('?' * len(FILL_TRIGGERS))}, "
                "'decision.reopened') ORDER BY seq DESC LIMIT 1", (did, *FILL_TRIGGERS)).fetchone()
            trigger = res and res["type"] in FILL_TRIGGERS and res["type"]
        else:
            trigger = RESUME_TRIGGERS.get(row["status"])
            # A resolution that silenced the resumer doesn't count: the state is that of the one before.
            res = trigger and next((ev for ev in self.conn.execute(
                "SELECT seq, ts, data FROM events WHERE decision_id=? AND type=? ORDER BY seq DESC", (did, trigger))
                if "resumer" not in json.loads(ev["data"]).get("silenced", ())), None)
        if not trigger or not res:
            return {"status": "armed"}
        ev = self.conn.execute(
            "SELECT * FROM events WHERE decision_id=? AND type LIKE 'resume.%' AND seq>? ORDER BY seq DESC LIMIT 1",
            (did, res["seq"])).fetchone()
        state = {"status": "pending", "trigger": trigger, "trigger_seq": res["seq"], "since": res["ts"]}
        if ev:
            state |= {"status": ev["type"].split(".", 1)[1], "since": ev["ts"], "actor": ev["actor"],
                      "detail": json.loads(ev["data"])}
        return state

    @staticmethod
    def _sub_dict(row, reveal_secret=False) -> dict:
        out = {
            "id": row["id"],
            "space": row["space"],
            "decision_id": row["decision_id"],
            "url": row["url"],
            "events": json.loads(row["events"]),
            "require_ack": None if row["require_ack"] is None else bool(row["require_ack"]),
            "max_attempts": row["max_attempts"],
            "refire_interval": row["refire_interval"],
            "active": bool(row["active"]),
            "description": row["description"],
            "created_at": row["created_at"],
        }
        if reveal_secret:
            out["secret"] = row["secret"]
        return out

    # ------------------------------------------------------------------ decisions

    def create_decision(self, body: dict, actor: str, scope: Scope = ALL) -> tuple[dict, bool]:
        title = _req_str(body, "title")
        key = _opt_type(body, "idempotency_key", str)
        space = self._space_for(scope, body.get("space"))
        with self.tx():
            if key:
                existing = self.conn.execute(
                    "SELECT id FROM decisions WHERE space=? AND idempotency_key=?", (space, key)).fetchone()
                if existing:
                    return self._decision_dict(existing["id"]), False
            num = (self.conn.execute("SELECT COALESCE(MAX(num), 0) + 1 FROM decisions").fetchone()[0])
            did = f"D-{num}"
            ts = now_iso()
            min_select = _opt_type(body, "min_select", int, 1)
            # Absent max_select means "pick one"; explicit null means unlimited.
            max_select = _opt_type(body, "max_select", int, None) if "max_select" in body else 1
            self._check_select_bounds(min_select, max_select)
            defs = self._clean_defs(_opt_type(body, "attributes", dict, {}))
            deps = self._check_dependencies(did, space, _dependencies(body.get("depends_on", [])), applies=True)
            goals = self._check_goals(_goal_ids(body.get("goals", []), "goals"), space)
            if body.get("due_at") is not None:
                due_at = _due_at(body["due_at"])
            else:
                # The clock starts once it can be decided, not while it waits on another decision.
                waiting = any(self.conn.execute("SELECT status FROM decisions WHERE id=?", (x["decision"],))
                              .fetchone()["status"] == "open" for x in deps)
                due_at = None if waiting else self.default_due_at(space)
            self.conn.execute(
                """INSERT INTO decisions (id, num, space, title, context, source, min_select, max_select,
                   attribute_defs, links, issues, tags, metadata, due_at, idempotency_key, created_at, updated_at,
                   resume)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (did, num, space, title, _opt_type(body, "context", str, ""),
                 _opt_type(body, "source", str, actor), min_select, max_select, canonical(defs),
                 canonical(_links(body.get("links"))), canonical(_issues(body.get("issues") or [])),
                 canonical(_opt_type(body, "tags", list, [])),
                 canonical(_opt_type(body, "metadata", dict, {})), due_at,
                 key, ts, ts, canonical(r) if (r := _resume(body.get("resume"))) else None),
            )
            self._write_dependencies(did, deps)
            self._write_decision_goals(did, goals)
            self._insert_options(did, _opt_type(body, "options", list, []), actor)
            hooks = body.get("webhooks") or ([body["webhook"]] if body.get("webhook") else [])
            created_subs = [self._create_sub(h, did, space) for h in hooks]
            self._event(did, "decision.created", actor, {"decision": self._decision_dict(did)})
            out = self._decision_dict(did)
            # Secrets are only ever revealed in the response that created them.
            out["webhooks"] = created_subs
            return out, True

    @staticmethod
    def _check_select_bounds(min_select, max_select):
        if min_select < 0:
            raise ApiError(400, "min_select must be >= 0")
        if max_select is not None and max_select < max(min_select, 1):
            raise ApiError(400, "max_select must be >= min_select and >= 1 (or null for unlimited)")

    @staticmethod
    def _clean_defs(defs: dict) -> dict:
        out = {}
        for k, v in defs.items():
            if isinstance(v, str):
                v = {"description": v}
            if not isinstance(v, dict):
                raise ApiError(400, f"attribute definition for {k!r} must be an object or a string")
            out[str(k)] = v
        return out

    def get_decision(self, raw_id: str, scope: Scope = ALL) -> dict:
        did = norm_id(raw_id)
        with self.lock:
            return self._decision_dict(did, self._decision_row(did, scope))

    def list_decisions(self, status=None, q=None, tag=None, source=None, space=None, limit=100, offset=0,
                       scope: Scope = ALL) -> list[dict]:
        cond, args = scope.where("space")
        sql = "SELECT * FROM decisions WHERE 1=1" + cond
        if space:
            sql += " AND space=?"
            args.append(space)
        if status and status != "all":
            sql += " AND status=?"
            args.append(status)
        if source:
            sql += " AND source=?"
            args.append(source)
        if q:
            sql += " AND (title LIKE ? OR context LIKE ? OR id LIKE ?)"
            args += [f"%{q}%"] * 3
        if tag:
            sql += " AND EXISTS (SELECT 1 FROM json_each(decisions.tags) WHERE value=?)"
            args.append(tag)
        sql += " ORDER BY num DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        out = []
        with self.lock:
            for row in self.conn.execute(sql, args).fetchall():
                counts = self.conn.execute(
                    "SELECT COUNT(*), SUM(excluded) FROM options WHERE decision_id=?", (row["id"],)).fetchone()
                d = self._decision_dict(row["id"], row)
                out.append({
                    k: d[k] for k in ("id", "space", "url", "title", "source", "status", "tags", "created_at",
                                      "updated_at", "resolved_at", "due_at", "resolution",
                                      "pending_attribute_requests", "min_select", "max_select", "resume_state",
                                      "waiting_on", "outdated_by", "issues")
                } | {"option_count": counts[0], "excluded_count": counts[1] or 0,
                     "goals": [g["goal"] for g in d["goals"]]})
        return out

    def update_decision(self, raw_id: str, body: dict, actor: str, scope: Scope = ALL) -> dict:
        did = norm_id(raw_id)
        with self.tx():
            row = self._decision_row(did, scope)
            changes = {}
            sets = {}
            for field, kinds in (("title", str), ("context", str), ("source", str), ("due_at", str)):
                if field in body:
                    val = _opt_type(body, field, kinds)
                    if field == "title" and not (val or "").strip():
                        raise ApiError(400, "title cannot be empty")
                    if field == "due_at":
                        if val is None and self.deadline_days_for(row["space"]) > 0:
                            raise ApiError(400, "decisions can't go without a deadline; set a later due_at instead")
                        val = None if val is None else _due_at(val)
                    if val != row[field]:
                        changes[field] = [row[field], val]
                        sets[field] = val
            for field, kinds in (("tags", list), ("metadata", dict)):
                if field in body:
                    val = _opt_type(body, field, kinds, [] if kinds is list else {})
                    old = json.loads(row[field])
                    if field == "metadata" and body.get("merge_metadata", True):
                        val = {k: v for k, v in (old | val).items() if v is not None}
                    if val != old:
                        changes[field] = [old, val]
                        sets[field] = canonical(val)
            if "resume" in body:
                val = _resume(body["resume"])
                old = json.loads(row["resume"]) if row["resume"] else None
                if val != old:
                    changes["resume"] = [old, val]
                    sets["resume"] = canonical(val) if val else None
            if "depends_on" in body:
                old = self._dep_specs(did)
                val = self._check_dependencies(did, row["space"], _dependencies(body["depends_on"]),
                                               applies=row["status"] == "open")
                if val != old:
                    changes["depends_on"] = [old, val]
                    was_waiting = bool(self._open_upstreams(did))
                    self._write_dependencies(did, val)
            if "goals" in body:
                old = self._decision_goal_ids(did)
                val = self._check_goals(_goal_ids(body["goals"], "goals"), row["space"])
                if val != old:
                    changes["goals"] = [old, val]
                    self._write_decision_goals(did, val)
            if "links" in body:
                val, old = _links(body["links"]), json.loads(row["links"])
                if val != old:
                    changes["links"] = [old, val]
                    sets["links"] = canonical(val)
            if "issues" in body:
                val, old = _issues(body["issues"] or []), json.loads(row["issues"])
                if val != old:
                    changes["issues"] = [old, val]
                    sets["issues"] = canonical(val)
            if "attributes" in body:
                old = json.loads(row["attribute_defs"])
                new = dict(old)
                for k, v in self._clean_defs(_opt_type(body, "attributes", dict, {})).items():
                    new[k] = (old.get(k, {}) | v) if v else None
                new = {k: v for k, v in new.items() if v is not None}
                if new != old:
                    changes["attributes"] = [old, new]
                    sets["attribute_defs"] = canonical(new)
            mn = _opt_type(body, "min_select", int, row["min_select"])
            mx = (_opt_type(body, "max_select", int) if "max_select" in body else row["max_select"])
            if (mn, mx) != (row["min_select"], row["max_select"]):
                self._check_select_bounds(mn, mx)
                changes["min_select"] = [row["min_select"], mn]
                changes["max_select"] = [row["max_select"], mx]
                sets["min_select"], sets["max_select"] = mn, mx
            if changes:
                sets["updated_at"] = now_iso()
                self.conn.execute(
                    f"UPDATE decisions SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                    (*sets.values(), did))
                self._event(did, "decision.updated", actor, {"changes": changes})
                # Dropping the last dependency it was waiting on lets it be decided now.
                if "depends_on" in changes and row["status"] == "open" and was_waiting and not self._open_upstreams(did):
                    self._unlock(did, None)
            return self._decision_dict(did)

    def resolve(self, raw_id: str, body: dict, actor: str, scope: Scope = ALL) -> dict:
        did = norm_id(raw_id)
        selected = body.get("selected", body.get("option_ids"))
        if isinstance(selected, str):
            selected = [selected]
        if not isinstance(selected, list):
            raise ApiError(400, "'selected' must be a list of option ids")
        selected = list(dict.fromkeys(str(s) for s in selected))
        silence = _opt_type(body, "silence", list, [])
        with self.tx():
            row = self._decision_row(did, scope)
            waiting = self._open_upstreams(did)
            if waiting:
                raise ApiError(409, f"{did} waits on {', '.join(waiting)}: decide that first, "
                                    f"or remove the dependency")
            silence = self._check_silence(did, silence)
            ids = {r["id"] for r in self.conn.execute("SELECT id FROM options WHERE decision_id=?", (did,))}
            unknown = [s for s in selected if s not in ids]
            if unknown:
                raise ApiError(400, f"unknown option ids: {', '.join(unknown)}")
            mn, mx = row["min_select"], row["max_select"]
            if len(selected) < mn or (mx is not None and len(selected) > mx):
                want = f"exactly {mn}" if mn == mx else f"between {mn} and {mx if mx is not None else 'any number of'}"
                raise ApiError(400, f"select {want} options (got {len(selected)})")
            ts = now_iso()
            previous = json.loads(row["resolution"]) if row["resolution"] else None
            resolution = {
                "selected": selected,
                "note": _opt_type(body, "note", str, ""),
                "filters": body.get("filters"),
                "resolved_by": actor,
                "resolved_at": ts,
            }
            self.conn.execute(
                "UPDATE decisions SET status='resolved', resolution=?, resolved_at=?, updated_at=? WHERE id=?",
                (canonical(resolution), ts, ts, did))
            data = {"resolution": resolution, "previous_status": row["status"], "previous_resolution": previous}
            if silence:
                data["silenced"] = silence
            self._event(did, "decision.resolved", actor, data)
            self._settle_dependents(did, was_open=row["status"] == "open")
            return self._decision_dict(did)

    def _check_silence(self, did: str, silence: list) -> list[str]:
        """Listeners a resolution won't notify (e.g. when fixing a typo): `resumer`, `poll:<actor>`, or the id of a
        webhook on this decision. Webhooks that hear every decision always hear it."""
        out = []
        for key in dict.fromkeys(silence):
            if not isinstance(key, str):
                raise ApiError(400, "'silence' must be a list of listener ids")
            if key != "resumer" and not (key.startswith("poll:") and len(key) > 5):
                sub = self.conn.execute("SELECT decision_id FROM subscriptions WHERE id=?", (key,)).fetchone()
                if not sub:
                    raise ApiError(400, f"no listener {key!r}")
                if sub["decision_id"] != did:
                    raise ApiError(400, f"{key} is a global webhook; it can't be silenced")
            out.append(key)
        return out

    def set_status(self, raw_id: str, status: str, note: str, actor: str, scope: Scope = ALL) -> dict:
        did = norm_id(raw_id)
        etype = {"open": "decision.reopened", "cancelled": "decision.cancelled"}[status]
        with self.tx():
            row = self._decision_row(did, scope)
            previous = json.loads(row["resolution"]) if row["resolution"] else None
            data = {"note": note, "previous_status": row["status"], "previous_resolution": previous}
            due_at = row["due_at"]
            if status == "open":
                unmet = [x for x in self._dependency_view(did, row)["depends_on"] if x["state"] == "unmet"]
                if unmet:
                    x = unmet[0]
                    raise ApiError(409, f"{did} only applies if {x['decision']} picks {' or '.join(x['when'])}, "
                                        f"and it's {x['status']}: change {x['decision']} or remove the dependency first")
                # Reopening restarts a passed (or missing) deadline; otherwise it would expire again at once.
                if (new_due := self._restart_deadline(row)) != due_at:
                    due_at = data["due_at"] = new_due
            self.conn.execute(
                "UPDATE decisions SET status=?, resolution=NULL, resolved_at=NULL, due_at=?, updated_at=? WHERE id=?",
                (status, due_at, now_iso(), did))
            self._event(did, etype, actor, data)
            if status != row["status"]:
                self._settle_dependents(did, was_open=row["status"] == "open")
            return self._decision_dict(did)

    def expire_due(self) -> list[str]:
        """Close every open decision whose deadline has passed as `expired` (not `cancelled`):
        nobody decided in time, so it wasn't important enough. Its listeners and resumer are notified."""
        now = datetime.now(timezone.utc)
        expired = []
        with self.tx():
            for (did,) in self.conn.execute(
                    "SELECT id FROM decisions WHERE status='open' AND due_at IS NOT NULL").fetchall():
                # Re-read: expiring one can cancel or unlock others further down the list.
                row = self._decision_row(did)
                if row["status"] != "open":
                    continue
                due = parse_ts(row["due_at"])
                # A decision waiting on another can't be decided yet, so it can't run out of time either.
                if due is None or due > now or self._open_upstreams(row["id"]):
                    continue
                self.conn.execute("UPDATE decisions SET status='expired', updated_at=? WHERE id=?",
                                  (now_iso(), row["id"]))
                self._event(row["id"], "decision.expired", DEADLINE_ACTOR,
                            {"due_at": row["due_at"], "previous_status": "open"})
                self._settle_dependents(row["id"], was_open=True)
                expired.append(row["id"])
        return expired

    def comment(self, raw_id: str, body: dict, actor: str, scope: Scope = ALL) -> dict:
        did = norm_id(raw_id)
        text = _req_str(body, "text")
        with self.tx():
            self._decision_row(did, scope)
            self._touch(did)
            seq = self._event(did, "decision.commented", actor, {"text": text})
        return {"seq": seq, "text": text, "actor": actor}

    # ------------------------------------------------------------------ goals

    def _goal_row(self, gid: str, scope: Scope = ALL):
        row = self.conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
        if not row or not scope.allows(row["space"]):
            raise ApiError(404, f"no goal {gid}")
        return row

    def _goal_ref(self, gid: str) -> dict:
        row = self.conn.execute("SELECT title, status FROM goals WHERE id=?", (gid,)).fetchone()
        return {"goal": gid, "title": row["title"], "status": row["status"]}

    def _decision_goal_ids(self, did: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT goal_id FROM decision_goals WHERE decision_id=? ORDER BY position", (did,))]

    def _serves_ids(self, gid: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT serves FROM goal_links WHERE goal_id=? ORDER BY position", (gid,))]

    def _check_goals(self, ids: list[str], space: str) -> list[str]:
        """Goals a decision works toward, or a goal serves: they exist, in the same space."""
        for gid in ids:
            row = self.conn.execute("SELECT space FROM goals WHERE id=?", (gid,)).fetchone()
            if not row or row["space"] != space:
                raise ApiError(404, f"no goal {gid}")
        return ids

    def _check_serves(self, gid: str, space: str, ids: list[str]) -> list[str]:
        self._check_goals(ids, space)
        for up in ids:
            if up == gid:
                raise ApiError(400, "a goal can't serve itself")
            # Following what it serves upward must never lead back here.
            seen, todo = set(), [up]
            while todo:
                cur = todo.pop()
                if cur == gid:
                    raise ApiError(400, f"serving {up} would make a cycle: {up} already serves {gid}")
                if cur not in seen:
                    seen.add(cur)
                    todo += self._serves_ids(cur)
        return ids

    def _write_decision_goals(self, did: str, ids: list[str]):
        self.conn.execute("DELETE FROM decision_goals WHERE decision_id=?", (did,))
        self.conn.executemany("INSERT INTO decision_goals (decision_id, goal_id, position) VALUES (?,?,?)",
                              [(did, gid, i) for i, gid in enumerate(ids)])

    def _write_serves(self, gid: str, ids: list[str]):
        self.conn.execute("DELETE FROM goal_links WHERE goal_id=?", (gid,))
        self.conn.executemany("INSERT INTO goal_links (goal_id, serves, position) VALUES (?,?,?)",
                              [(gid, up, i) for i, up in enumerate(ids)])

    def _outcome(self, row) -> list[str]:
        """Labels of what a decision picked, if it's resolved."""
        if row["status"] != "resolved" or not row["resolution"]:
            return []
        labels = dict(self.conn.execute("SELECT id, label FROM options WHERE decision_id=?", (row["id"],)).fetchall())
        return [labels.get(s, s) for s in json.loads(row["resolution"])["selected"]]

    def _goal_dict(self, gid: str, row=None) -> dict:
        row = row or self._goal_row(gid)
        subgoals = [{"goal": r["id"], "title": r["title"], "status": r["status"]} for r in self.conn.execute(
            "SELECT g.* FROM goal_links l JOIN goals g ON g.id = l.goal_id WHERE l.serves=? ORDER BY g.num", (gid,))]
        decisions = [{"decision": r["id"], "title": r["title"], "status": r["status"], "outcome": self._outcome(r),
                      "waiting_on": self._open_upstreams(r["id"]) if r["status"] == "open" else []}
                     for r in self.conn.execute(
                         "SELECT d.* FROM decision_goals x JOIN decisions d ON d.id = x.decision_id "
                         "WHERE x.goal_id=? ORDER BY d.num", (gid,))]
        return {
            "id": row["id"],
            "space": row["space"],
            "url": f"{self.cfg.public_url}/g/{row['id']}",
            "title": row["title"],
            "description": row["description"],
            "status": row["status"],
            "status_note": row["status_note"],
            "issues": json.loads(row["issues"]),
            "tags": json.loads(row["tags"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "closed_at": row["closed_at"],
            "proof": self._proof_view(gid, row),
            "serves": [self._goal_ref(up) for up in self._serves_ids(gid)],
            "subgoals": subgoals,
            "decisions": decisions,
        }

    def create_goal(self, body: dict, actor: str, scope: Scope = ALL) -> dict:
        title = _req_str(body, "title")
        space = self._space_for(scope, body.get("space"))
        status = _opt_type(body, "status", str, "active")
        if status not in GOAL_STATUSES:
            raise ApiError(400, f"status must be one of {', '.join(GOAL_STATUSES)}")
        with self.tx():
            num = self.conn.execute("SELECT COALESCE(MAX(num), 0) + 1 FROM goals").fetchone()[0]
            gid, ts = f"G-{num}", now_iso()
            serves = self._check_serves(gid, space, _goal_ids(body.get("serves", []), "serves"))
            self.conn.execute(
                """INSERT INTO goals (id, num, space, title, description, status, status_note, issues, tags,
                   created_at, updated_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (gid, num, space, title, _opt_type(body, "description", str, ""), status,
                 _opt_type(body, "note", str, ""), canonical(_issues(body.get("issues") or [])),
                 canonical(_opt_type(body, "tags", list, [])), ts, ts,
                 None if status == "active" else ts))
            self._write_serves(gid, serves)
            out = self._goal_dict(gid)
            self._event(None, "goal.created", actor, {"goal": out}, goal_id=gid)
            return out

    def get_goal(self, raw_id: str, scope: Scope = ALL) -> dict:
        gid = norm_goal_id(raw_id)
        with self.lock:
            return self._goal_dict(gid, self._goal_row(gid, scope))

    def list_goals(self, status=None, space=None, tag=None, scope: Scope = ALL) -> list[dict]:
        cond, args = scope.where("space")
        sql = "SELECT * FROM goals WHERE 1=1" + cond
        if space:
            sql += " AND space=?"
            args.append(space)
        if tag:
            sql += " AND EXISTS (SELECT 1 FROM json_each(goals.tags) WHERE value=?)"
            args.append(tag)
        if status and status != "all":
            sql += " AND status=?"
            args.append(status)
        out = []
        with self.lock:
            for row in self.conn.execute(sql + " ORDER BY num", args).fetchall():
                n = self.conn.execute(
                    "SELECT COUNT(*) AS total, COALESCE(SUM(d.status='open'), 0) AS open, "
                    "COALESCE(SUM(d.status='resolved'), 0) AS resolved "
                    "FROM decision_goals x JOIN decisions d ON d.id = x.decision_id WHERE x.goal_id=?",
                    (row["id"],)).fetchone()
                out.append({k: row[k] for k in ("id", "space", "title", "status", "status_note", "created_at",
                                                "updated_at", "closed_at")} | {
                    "url": f"{self.cfg.public_url}/g/{row['id']}", "tags": json.loads(row["tags"]),
                    "issues": json.loads(row["issues"]),
                    "serves": self._serves_ids(row["id"]),
                    "proof": (p := self._proof_view(row["id"], row)) and {
                        "agreed": bool(p["agreed"]), "ready": bool(p["ready"]), "run": p["run"] and p["run"]["status"],
                        "resume": p["resume_state"] and p["resume_state"]["status"]},
                    "decisions": {"total": n["total"], "open": n["open"], "resolved": n["resolved"]}})
        return out

    def update_goal(self, raw_id: str, body: dict, actor: str, scope: Scope = ALL) -> dict:
        """Edit fields (one `goal.updated`) and/or change `status` with an optional `note` (`goal.achieved`,
        `goal.abandoned`, `goal.reactivated`)."""
        gid = norm_goal_id(raw_id)
        with self.tx():
            row = self._goal_row(gid, scope)
            changes, sets = {}, {}
            for field in ("title", "description"):
                if field in body:
                    val = _opt_type(body, field, str) or ""
                    if field == "title" and not val.strip():
                        raise ApiError(400, "title cannot be empty")
                    if val != row[field]:
                        changes[field], sets[field] = [row[field], val], val
            if "tags" in body:
                val, old = _opt_type(body, "tags", list, []), json.loads(row["tags"])
                if val != old:
                    changes["tags"], sets["tags"] = [old, val], canonical(val)
            if "issues" in body:
                val, old = _issues(body["issues"] or []), json.loads(row["issues"])
                if val != old:
                    changes["issues"], sets["issues"] = [old, val], canonical(val)
            if "serves" in body:
                old = self._serves_ids(gid)
                val = self._check_serves(gid, row["space"], _goal_ids(body["serves"], "serves"))
                if val != old:
                    changes["serves"] = [old, val]
                    self._write_serves(gid, val)
            status = _opt_type(body, "status", str, row["status"])
            if status not in GOAL_STATUSES:
                raise ApiError(400, f"status must be one of {', '.join(GOAL_STATUSES)}")
            note = _opt_type(body, "note", str, "")
            ts = now_iso()
            if status != row["status"]:
                sets |= {"status": status, "status_note": note, "closed_at": None if status == "active" else ts}
            if sets or changes:
                sets["updated_at"] = ts
                self.conn.execute(f"UPDATE goals SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                                  (*sets.values(), gid))
            if changes:
                self._event(None, "goal.updated", actor, {"changes": changes}, goal_id=gid)
            if status != row["status"]:
                self._event(None, GOAL_EVENTS[status], actor, {"note": note, "previous_status": row["status"]},
                            goal_id=gid)
            return self._goal_dict(gid)

    # ------------------------------------------------------------------ proof: showing a goal was achieved

    def _proof_run(self, gid: str) -> dict | None:
        """The latest time the proof was asked for, and how far it got."""
        req = self.conn.execute(
            "SELECT * FROM events WHERE goal_id=? AND type='proof.requested' ORDER BY seq DESC LIMIT 1", (gid,)).fetchone()
        if not req:
            return None
        run = {"status": "requested", "seq": req["seq"], "hash": json.loads(req["data"])["hash"],
               "requested_by": req["actor"], "requested_at": req["ts"], "since": req["ts"], "detail": {}}
        ev = self.conn.execute(
            f"SELECT * FROM events WHERE goal_id=? AND seq>? AND type IN ({','.join('?' * len(PROOF_RUN_EVENTS))}) "
            "ORDER BY seq DESC LIMIT 1", (gid, req["seq"], *(f"proof.{k}" for k in PROOF_RUN_EVENTS))).fetchone()
        if ev:
            run |= {"status": ev["type"].split(".", 1)[1], "since": ev["ts"], "detail": json.loads(ev["data"]),
                    "event_seq": ev["seq"]}
        return run

    def _proof_failures(self, gid: str) -> int:
        """`proof.failed` in a row: since the proof last passed or was proposed anew."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE goal_id=? AND type='proof.failed' AND seq > COALESCE((SELECT MAX(seq) "
            "FROM events WHERE goal_id=? AND type IN ('proof.passed', 'proof.proposed', 'proof.changed')), 0)",
            (gid, gid)).fetchone()[0]

    def _proof_resume_state(self, gid: str, proof: dict, run: dict | None) -> dict | None:
        """Where the owning session's resumption stands for the latest result of the current proof. Mirrors
        `_resume_state`: `pending` until a `resume.*` event follows the result; `capped` when the proof has
        failed too many times in a row for the agent to be woken again."""
        if not proof.get("resume"):
            return None
        trigger = run and f"proof.{run['status']}"
        if trigger not in PROOF_RESUME_TRIGGERS or run["hash"] != proof["hash"]:
            return {"status": "armed"}
        failures = self._proof_failures(gid) if trigger == "proof.failed" else 0
        state = {"status": "pending", "trigger": trigger, "trigger_seq": run["event_seq"], "since": run["since"],
                 "failures": failures, "max_failures": PROOF_RESUME_FAILURES}
        ev = self.conn.execute(
            "SELECT * FROM events WHERE goal_id=? AND type LIKE 'resume.%' AND seq>? ORDER BY seq DESC LIMIT 1",
            (gid, run["event_seq"])).fetchone()
        if ev:
            state |= {"status": ev["type"].split(".", 1)[1], "since": ev["ts"], "actor": ev["actor"],
                      "detail": json.loads(ev["data"])}
        elif failures > PROOF_RESUME_FAILURES:
            state["status"] = "capped"
        return state

    def _proof_view(self, gid: str, row) -> dict | None:
        if not row["proof"]:
            return None
        proof = json.loads(row["proof"])
        run = self._proof_run(gid)
        waited = run and (datetime.now(timezone.utc) - parse_ts(run["since"])).total_seconds()
        # Asked for and not reported back yet. Past its timeout it's given up on: the resumer died or is offline.
        busy = bool(run and run["status"] in ("requested", "started") and waited <= proof["timeout"] + PROOF_GRACE)
        return proof | {"run": run, "busy": busy, "resume_command": manual_command(proof.get("resume")),
                        "resume_state": self._proof_resume_state(gid, proof, run)}

    def _proof_row(self, gid: str, scope: Scope) -> tuple:
        row = self._goal_row(gid, scope)
        view = self._proof_view(gid, row)
        if not view:
            raise ApiError(404, f"{gid} has no proof; propose one with `decide goal proof {gid} --command … --success …`")
        return row, view

    def _write_proof(self, gid: str, proof: dict | None):
        stored = proof and {k: v for k, v in proof.items() if k not in ("run", "busy", "resume_command", "resume_state")}
        self.conn.execute("UPDATE goals SET proof=?, updated_at=? WHERE id=?",
                          (canonical(stored) if stored else None, now_iso(), gid))

    def set_proof(self, raw_id: str, body: dict, actor: str, scope: Scope = ALL) -> dict:
        """Propose how the goal is proved achieved, or change it. A change is logged with both hashes and
        has to be agreed on again. `resume` (as on a decision) is the session to wake with the result; it isn't
        part of what's agreed on, and is kept when left out."""
        gid = norm_goal_id(raw_id)
        proof = _proof(body)
        with self.tx():
            row = self._goal_row(gid, scope)
            old = self._proof_view(gid, row)
            resume = _resume(body["resume"]) if "resume" in body else old and old.get("resume")
            if old and old["hash"] == proof["hash"]:
                if resume != old.get("resume"):
                    self._write_proof(gid, old | {"resume": resume})
                    self._event(None, "proof.retargeted", actor, {"hash": proof["hash"], "resume": resume}, goal_id=gid)
                return self._goal_dict(gid)
            if old and old["busy"]:
                raise ApiError(409, f"{gid}'s proof is running; change it when it has finished")
            # Whether the goal is ready to be proved is the agent's claim about the work, not about the script.
            proof |= {"resume": resume}
            self._write_proof(gid, proof | {"proposed_by": actor, "proposed_at": now_iso(), "agreed": None,
                                            "ready": old["ready"] if old else None})
            if old:
                changed = [k for k in ("success", "command", "cwd", "machine", "timeout") if old[k] != proof[k]]
                was, now = ({s["path"]: s["sha256"] for s in p["scripts"]} for p in (old, proof))
                changed += [f"script {path}" for path in dict.fromkeys([*was, *now]) if was.get(path) != now.get(path)]
                self._event(None, "proof.changed", actor, {
                    "previous_hash": old["hash"], "hash": proof["hash"], "changed": changed,
                    "was_agreed": bool(old["agreed"]), "proof": proof}, goal_id=gid)
            else:
                self._event(None, "proof.proposed", actor, {"hash": proof["hash"], "proof": proof}, goal_id=gid)
            return self._goal_dict(gid)

    def remove_proof(self, raw_id: str, actor: str, scope: Scope = ALL) -> dict:
        gid = norm_goal_id(raw_id)
        with self.tx():
            _, proof = self._proof_row(gid, scope)
            if proof["busy"]:
                raise ApiError(409, f"{gid}'s proof is running; remove it when it has finished")
            self._write_proof(gid, None)
            self._event(None, "proof.removed", actor, {"hash": proof["hash"]}, goal_id=gid)
            return self._goal_dict(gid)

    @staticmethod
    def _not_proposer(proof: dict, actor: str, what: str):
        if actor == proof["proposed_by"]:
            raise ApiError(403, f"{actor} proposed this proof, so someone else has to {what}")

    def agree_proof(self, raw_id: str, body: dict, actor: str, scope: Scope = ALL) -> dict:
        """Commit to the proof as it stands. `hash` is the one the caller looked at, so nobody agrees to a
        version that changed underneath them."""
        gid = norm_goal_id(raw_id)
        with self.tx():
            _, proof = self._proof_row(gid, scope)
            if body.get("hash") != proof["hash"]:
                raise ApiError(409, f"{gid}'s proof changed (now {proof['hash'][:12]}); look at it again before agreeing")
            self._not_proposer(proof, actor, "agree to it")
            if not proof["agreed"]:
                self._write_proof(gid, proof | {"agreed": {"by": actor, "at": now_iso()}})
                self._event(None, "proof.agreed", actor, {"hash": proof["hash"]}, goal_id=gid)
            return self._goal_dict(gid)

    def ready_proof(self, raw_id: str, body: dict, actor: str, scope: Scope = ALL) -> dict:
        """The agent's claim that the goal is achieved (or, `ready: false`, that it isn't after all). It's what
        lets the user ask for the proof. A `resume` given here replaces the session the result wakes: the
        session that claims the work done is the one that hears how it went."""
        gid = norm_goal_id(raw_id)
        ready = _opt_type(body, "ready", bool, True)
        note = _opt_type(body, "note", str, "")
        with self.tx():
            _, proof = self._proof_row(gid, scope)
            retarget = {"resume": _resume(body["resume"])} if "resume" in body and ready else {}
            if ready or proof["ready"]:
                self._write_proof(gid, proof | retarget | {
                    "ready": {"by": actor, "at": now_iso(), "note": note} if ready else None})
                self._event(None, "proof.ready" if ready else "proof.not_ready", actor, {"note": note} | retarget,
                            goal_id=gid)
            return self._goal_dict(gid)

    def request_proof(self, raw_id: str, actor: str, scope: Scope = ALL) -> dict:
        """The user asks for the proof. The resumer on the proof's machine hears `proof.requested` and runs it."""
        gid = norm_goal_id(raw_id)
        with self.tx():
            _, proof = self._proof_row(gid, scope)
            self._not_proposer(proof, actor, "ask for it")
            if not proof["agreed"]:
                raise ApiError(409, f"{gid}'s proof hasn't been agreed on")
            if not proof["ready"]:
                raise ApiError(409, f"{gid} hasn't been flagged as ready to prove")
            if proof["busy"]:
                raise ApiError(409, f"{gid}'s proof is already running")
            self._event(None, "proof.requested", actor, {"hash": proof["hash"], "machine": proof["machine"]},
                        goal_id=gid)
            return self._goal_dict(gid)

    def proof_event(self, raw_id: str, body: dict, actor: str, scope: Scope = ALL) -> dict:
        """The resumer reports on a run: `started`, then `passed` or `failed`. Passing achieves the goal;
        failing takes back the claim that it's ready."""
        gid = norm_goal_id(raw_id)
        kind = body.get("status")
        if kind not in PROOF_RUN_EVENTS:
            raise ApiError(400, f"status must be one of {', '.join(PROOF_RUN_EVENTS)}")
        with self.tx():
            row, proof = self._proof_row(gid, scope)
            run = proof["run"]
            if not run or run["seq"] != body.get("request_seq") or run["status"] not in ("requested", "started"):
                raise ApiError(409, f"{gid} has no run in progress for request {body.get('request_seq')!r}")
            self._event(None, f"proof.{kind}", actor, {k: v for k, v in body.items() if k != "status"}, goal_id=gid)
            if kind == "failed":
                self._write_proof(gid, proof | {"ready": None})
            # A run of a version that was since replaced proves nothing about what's agreed on now.
            elif kind == "passed" and row["status"] == "active" and run["hash"] == proof["hash"]:
                ts, note = now_iso(), f"Proved: {proof['success'].splitlines()[0]}"
                self.conn.execute("UPDATE goals SET status='achieved', status_note=?, closed_at=?, updated_at=? "
                                  "WHERE id=?", (note, ts, ts, gid))
                self._event(None, GOAL_EVENTS["achieved"], actor,
                            {"note": note, "previous_status": row["status"], "proof": proof["hash"]}, goal_id=gid)
            return self._goal_dict(gid)

    # ------------------------------------------------------------------ the map: goals and decisions as a graph

    def _history(self, dids: set[str]) -> dict[str, dict]:
        """Per decision: every pick it has made (`picks`, oldest first) and every `turn`: a pick it later dropped,
        by changing to something else or by being withdrawn (cancelled or expired after a reopen)."""
        out = {did: {"picks": [], "turns": []} for did in dids}
        for ev in self.conn.execute(
                "SELECT * FROM events WHERE type IN ('decision.resolved', 'decision.cancelled', 'decision.expired') "
                "AND decision_id IS NOT NULL ORDER BY seq"):
            h = out.get(ev["decision_id"])
            if h is None:
                continue
            data = json.loads(ev["data"])
            last = h["picks"][-1] if h["picks"] and not h.get("withdrawn") else None
            if ev["type"] == "decision.resolved":
                pick = {"selected": data["resolution"]["selected"], "seq": ev["seq"], "at": ev["ts"], "by": ev["actor"],
                        "note": data["resolution"].get("note", "")}
                if last and set(last["selected"]) != set(pick["selected"]):
                    h["turns"].append({"from": last["selected"], "to": pick["selected"], "seq": ev["seq"],
                                       "at": ev["ts"], "by": ev["actor"], "note": pick["note"], "withdrawn": False})
                h["picks"].append(pick)
                h["withdrawn"] = False
            elif last:
                h["turns"].append({"from": last["selected"], "to": [], "seq": ev["seq"], "at": ev["ts"],
                                   "by": ev["actor"], "note": data.get("note", ""), "withdrawn": True})
                h["withdrawn"] = True
        return out

    @staticmethod
    def _tagged(goals: dict, decisions: dict, toward: list, tags: list[str]) -> set[str]:
        """Goals and decisions with any of `tags`. A decision also has the tags of the goals it's toward,
        so tagging goals is enough; a goal doesn't inherit from the goals it serves."""
        want = set(tags)
        has = lambda row: bool(want & set(json.loads(row["tags"])))
        out = {g for g, r in goals.items() if has(r)} | {d for d, r in decisions.items() if has(r)}
        return out | {d for d, g in toward if g in out and g in goals}

    def _map_rows(self, scope: Scope, space: str | None, tags=None):
        """Goals, decisions and the links between them in view. With `tags`, only what has one of them
        (see _tagged), and only the links between those."""
        cond, args = scope.where("space")
        if space:
            cond += " AND space=?"
            args.append(space)
        goals = {r["id"]: r for r in self.conn.execute("SELECT * FROM goals WHERE 1=1" + cond + " ORDER BY num", args)}
        decisions = {r["id"]: r for r in
                     self.conn.execute("SELECT * FROM decisions WHERE 1=1" + cond + " ORDER BY num", args)}
        # Links never cross spaces, so the step's side decides whether a link is in view.
        serves = [(r[0], r[1]) for r in self.conn.execute("SELECT goal_id, serves FROM goal_links ORDER BY goal_id, position")
                  if r[0] in goals]
        toward = [(r[0], r[1]) for r in self.conn.execute(
            "SELECT decision_id, goal_id FROM decision_goals ORDER BY decision_id, position") if r[0] in decisions]
        deps = [r for r in self.conn.execute("SELECT * FROM dependencies ORDER BY decision_id, position")
                if r["decision_id"] in decisions]
        if tags:
            keep = self._tagged(goals, decisions, toward, tags)
            goals = {k: v for k, v in goals.items() if k in keep}
            decisions = {k: v for k, v in decisions.items() if k in keep}
            serves = [(a, b) for a, b in serves if a in keep and b in keep]
            toward = [(a, b) for a, b in toward if a in keep and b in keep]
            deps = [r for r in deps if r["decision_id"] in keep and r["depends_on"] in keep]
        return goals, decisions, serves, toward, deps

    @staticmethod
    def _reach(start: str, adjacent: dict[str, list[str]]) -> set[str]:
        seen, todo = set(), list(adjacent.get(start, []))
        while todo:
            cur = todo.pop()
            if cur not in seen:
                seen.add(cur)
                todo += adjacent.get(cur, [])
        return seen

    def graph(self, scope: Scope = ALL, space=None, focus=None, everything=False, tags=None) -> dict:
        """Goals and decisions as one graph. Each edge goes from a step to what it's for: a goal to the goals it
        `serves`, a decision `toward` its goals, and a decision to the ones it `depends_on`. Decisions show up when
        they're linked to a goal, directly or through dependencies (`everything`: all of them). With `focus`
        (G-n or D-n), only what leads to it and what it leads to. With `tags`, only what has one of them (see
        _tagged), plus what those lead up to as `context`, so the chain up to the vision stays whole."""
        with self.lock:
            goals, decisions, serves, toward, deps = self._map_rows(scope, space)
            edges = ([{"from": a, "to": b, "kind": "serves"} for a, b in serves] +
                     [{"from": a, "to": b, "kind": "toward"} for a, b in toward] +
                     [{"from": r["decision_id"], "to": r["depends_on"], "kind": "depends_on",
                       "when": json.loads(r["when_selected"]) if r["when_selected"] else None} for r in deps])
            up, down = {}, {}
            for e in edges:
                up.setdefault(e["from"], []).append(e["to"])
                down.setdefault(e["to"], []).append(e["from"])
            if focus:
                fid = norm_goal_id(focus) if str(focus).strip().upper().startswith("G") else norm_id(focus)
                if fid not in goals and fid not in decisions:
                    raise ApiError(404, f"no {'goal' if fid.startswith('G') else 'decision'} {fid}")
                keep = {fid} | self._reach(fid, up) | self._reach(fid, down)
            elif everything:
                keep = set(goals) | set(decisions)
            else:
                linked = {a for a, _ in toward}
                related = {}
                for r in deps:
                    related.setdefault(r["decision_id"], []).append(r["depends_on"])
                    related.setdefault(r["depends_on"], []).append(r["decision_id"])
                for did in list(linked):
                    linked |= self._reach(did, related)
                keep = set(goals) | linked
            tagged = None
            if tags:
                tagged = self._tagged(goals, decisions, toward, tags)
                keep &= tagged | {a for t in tagged for a in self._reach(t, up)}
            history = self._history({d for d in decisions if d in keep})
            nodes = [{"id": g, "kind": "goal", "space": r["space"], "title": r["title"], "status": r["status"]}
                     for g, r in goals.items() if g in keep]
            nodes += [{"id": d, "kind": "decision", "space": r["space"], "title": r["title"], "status": r["status"],
                       "outcome": self._outcome(r), "changed": len(history[d]["turns"]),
                       "waiting_on": self._open_upstreams(d) if r["status"] == "open" else []}
                      for d, r in decisions.items() if d in keep]
            if tagged is not None:
                for n in nodes:
                    n["context"] = n["id"] not in tagged
            return {"nodes": nodes, "edges": [e for e in edges if e["from"] in keep and e["to"] in keep],
                    "focus": fid if focus else None, "tags": tags or []}

    def insights(self, scope: Scope = ALL, space=None, tags=None) -> dict:
        """What the history says. `dead_ends`: options, matched by label across decisions, that were picked and
        later dropped, or picked for a goal that was abandoned, or keep being ruled out. `reversals`: each time a
        decision changed its mind, with what led to it and the decided ones it pulled the rug from.
        `abandoned_goals`: with how many decisions went into them. With `tags`, only what has one of them."""
        with self.lock:
            goals, decisions, serves, toward, deps = self._map_rows(scope, space, tags)
            history = self._history(set(decisions))
            goals_of, decisions_of = {}, {}
            for did, gid in toward:
                goals_of.setdefault(did, []).append(gid)
                decisions_of.setdefault(gid, []).append(did)
            upstream, downstream, subgoals = {}, {}, {}
            for r in deps:
                upstream.setdefault(r["decision_id"], []).append(r["depends_on"])
                downstream.setdefault(r["depends_on"], []).append(r["decision_id"])
            for gid, parent in serves:
                subgoals.setdefault(parent, []).append(gid)
            options = {}
            for r in self.conn.execute("SELECT * FROM options ORDER BY decision_id, position"):
                if r["decision_id"] in decisions:
                    options.setdefault(r["decision_id"], []).append(r)
            label = lambda did, oid: next((o["label"] for o in options.get(did, []) if o["id"] == oid), oid)

            stats = {}
            for did, row in decisions.items():
                picked = {s for p in history[did]["picks"] for s in p["selected"]}
                current = set(json.loads(row["resolution"])["selected"]) if row["status"] == "resolved" else set()
                goal_status = {goals[g]["status"] for g in goals_of.get(did, []) if g in goals}
                for o in options.get(did, []):
                    s = stats.setdefault(_label_key(o["label"]), {
                        "label": o["label"], "offered": 0, "ruled_out": 0, "picked": 0, "reversed": 0,
                        "abandoned": 0, "achieved": 0, "decisions": []})
                    s["offered"] += 1
                    outcome = None
                    if o["id"] in picked:
                        s["picked"] += 1
                        outcome = ("reopened" if row["status"] == "open" else "reversed" if o["id"] not in current
                                   else "abandoned" if "abandoned" in goal_status
                                   else "achieved" if "achieved" in goal_status else "held")
                        if outcome in s:
                            s[outcome] += 1
                    elif o["excluded"]:
                        s["ruled_out"] += 1
                        outcome = "ruled_out"
                    if outcome:
                        s["decisions"].append({"decision": did, "title": row["title"], "outcome": outcome,
                                               "reason": o["exclude_reason"] if outcome == "ruled_out" else None})
            dead_ends = []
            for s in stats.values():
                s["dead_ends"] = s["reversed"] + s["abandoned"]
                if s["dead_ends"] or s["ruled_out"] > 1:
                    dead_ends.append(s | {"dead_end_rate": round(s["dead_ends"] / s["picked"], 2) if s["picked"] else None})
            dead_ends.sort(key=lambda s: (-s["dead_ends"], -(s["dead_end_rate"] or 0), -s["ruled_out"], s["label"]))

            reversals = []
            for did, h in history.items():
                ancestors = self._reach(did, upstream)
                for t in h["turns"]:
                    # Decisions that depended on this one and were already decided: the turn pulled the rug out.
                    hit = sorted((d for d in self._reach(did, downstream)
                                  if any(p["seq"] < t["seq"] for p in history[d]["picks"])),
                                 key=lambda d: decisions[d]["num"])
                    reversals.append({
                        "decision": did, "title": decisions[did]["title"], "status": decisions[did]["status"],
                        "from": [label(did, s) for s in t["from"]], "to": [label(did, s) for s in t["to"]],
                        "withdrawn": t["withdrawn"], "at": t["at"], "by": t["by"], "note": t["note"],
                        "upstream": sorted(ancestors, key=lambda d: decisions[d]["num"]),
                        "goals": goals_of.get(did, []), "invalidated": hit})
            reversals.sort(key=lambda x: x["at"], reverse=True)

            abandoned = []
            for gid, g in goals.items():
                if g["status"] != "abandoned":
                    continue
                under = {gid} | self._reach(gid, subgoals)
                spent = {d for u in under for d in decisions_of.get(u, [])}
                abandoned.append({"goal": gid, "title": g["title"], "note": g["status_note"], "closed_at": g["closed_at"],
                                  "decisions": len(spent),
                                  "resolved": sum(1 for d in spent if decisions[d]["status"] == "resolved")})
            abandoned.sort(key=lambda x: x["closed_at"] or "", reverse=True)
            return {"dead_ends": dead_ends[:50], "reversals": reversals, "abandoned_goals": abandoned}

    # ------------------------------------------------------------------ resumption

    # `skipped`: a request to fill values in that the resumer left alone (the agent is still waiting on it live).
    RESUME_EVENTS = ("started", "finished", "failed", "manual", "skipped")

    def resume_event(self, raw_id: str, body: dict, actor: str, scope: Scope = ALL) -> dict:
        did = norm_id(raw_id)
        kind = body.get("status")
        if kind not in self.RESUME_EVENTS:
            raise ApiError(400, f"status must be one of {', '.join(self.RESUME_EVENTS)}")
        with self.tx():
            row = self._decision_row(did, scope)
            if not row["resume"]:
                raise ApiError(400, f"{did} has no resume target")
            self._touch(did)
            self._event(did, f"resume.{kind}", actor, {k: v for k, v in body.items() if k != "status"})
            return self._decision_dict(did)

    def goal_resume_event(self, raw_id: str, body: dict, actor: str, scope: Scope = ALL) -> dict:
        """The resumer's (or a person's) report on waking the session that owns a goal, after its proof's result."""
        gid = norm_goal_id(raw_id)
        kind = body.get("status")
        if kind not in self.RESUME_EVENTS:
            raise ApiError(400, f"status must be one of {', '.join(self.RESUME_EVENTS)}")
        with self.tx():
            _, proof = self._proof_row(gid, scope)
            if not proof.get("resume"):
                raise ApiError(400, f"{gid}'s proof has no resume target")
            self._event(None, f"resume.{kind}", actor, {k: v for k, v in body.items() if k != "status"}, goal_id=gid)
            return self._goal_dict(gid)

    def resumer_heartbeat(self, body: dict, actor: str, scope: Scope = ALL) -> dict:
        machine = str(body.get("machine") or body.get("host") or "default")
        with self.lock:
            self.resumers[machine] = {"name": actor, "pid": body.get("pid"), "machine": machine,
                                      "command": body.get("command"), "last_seen": time.time(),
                                      "token": scope.token}
        return self.resumer_status(machine)

    def _resumer_dict(self, r: dict) -> dict:
        return {"online": time.time() - r["last_seen"] < 90, "name": r["name"], "pid": r["pid"],
                "machine": r["machine"], "command": r["command"], "token": r["token"],
                "last_seen": datetime.fromtimestamp(r["last_seen"], timezone.utc).isoformat(timespec="seconds")
                .replace("+00:00", "Z")}

    def resumer_status(self, machine: str | None = None) -> dict:
        """Status of the resumer on one machine, or (no machine) of the most recently seen one,
        plus all of them under `resumers`."""
        everyone = sorted((self._resumer_dict(r) for r in self.resumers.values()),
                          key=lambda r: r["last_seen"], reverse=True)
        if machine:
            r = self.resumers.get(machine)
            main = self._resumer_dict(r) if r else {"online": False, "last_seen": None, "machine": machine}
        else:
            main = next((r for r in everyone if r["online"]), everyone[0] if everyone else
                        {"online": False, "last_seen": None})
        return main | {"resumers": everyone}

    # ------------------------------------------------------------------ spaces and machine tokens

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def scope_for(self, token: str) -> Scope | None:
        """A machine token sees only its space; the admin token (config `token`) sees every space.
        Without an admin token auth is off: anyone sees everything, though a machine token still
        routes to its space. None = not authorized."""
        if token:
            with self.lock:
                row = self.conn.execute("SELECT id, space, last_used_at FROM tokens WHERE hash=?",
                                        (self._hash_token(token),)).fetchone()
                if row:
                    last = parse_ts(row["last_used_at"])
                    if not last or (datetime.now(timezone.utc) - last).total_seconds() > LAST_USED_EVERY:
                        self.conn.execute("UPDATE tokens SET last_used_at=? WHERE id=?", (now_iso(), row["id"]))
                    return Scope(frozenset({row["space"]}), row["space"], row["id"])
        if not self.cfg.token or hmac.compare_digest(token, self.cfg.token):
            return ALL
        return None

    @staticmethod
    def _admin(scope: Scope):
        if not scope.everything:
            raise ApiError(403, "only the admin token can manage spaces and tokens")

    def _space_row(self, name: str):
        row = self.conn.execute("SELECT * FROM spaces WHERE name=?", (name,)).fetchone()
        if not row:
            raise ApiError(404, f"no space {name!r}")
        return row

    def _space_dict(self, row) -> dict:
        n = self.conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(status='open'), 0) AS open FROM decisions WHERE space=?",
            (row["name"],)).fetchone()
        return {"name": row["name"], "deadline_days": row["deadline_days"],
                "effective_deadline_days": self.cfg.deadline_days if row["deadline_days"] is None
                else row["deadline_days"],
                "created_at": row["created_at"], "decisions": n["total"], "open": n["open"],
                "tokens": self.conn.execute("SELECT COUNT(*) FROM tokens WHERE space=?", (row["name"],)).fetchone()[0]}

    def list_spaces(self, scope: Scope = ALL) -> list[dict]:
        cond, args = scope.where("name")
        with self.lock:
            return [self._space_dict(r) for r in
                    self.conn.execute("SELECT * FROM spaces WHERE 1=1" + cond + " ORDER BY name", args).fetchall()]

    @staticmethod
    def _deadline_days(body: dict):
        days = _opt_type(body, "deadline_days", (int, float))
        if days is not None and days < 0:
            raise ApiError(400, "deadline_days must be >= 0 (0 = no default deadline)")
        return days

    def create_space(self, body: dict, actor: str, scope: Scope = ALL) -> dict:
        self._admin(scope)
        name = _req_str(body, "name")
        if not SPACE_NAME.fullmatch(name):
            raise ApiError(400, "space names are 1-40 lowercase letters, digits, - and _")
        days = self._deadline_days(body)
        with self.tx():
            if self.conn.execute("SELECT 1 FROM spaces WHERE name=?", (name,)).fetchone():
                raise ApiError(409, f"space {name!r} already exists")
            self.conn.execute("INSERT INTO spaces (name, deadline_days, created_at) VALUES (?,?,?)",
                              (name, days, now_iso()))
            out = self._space_dict(self._space_row(name))
            self._event(None, "space.created", actor, {"space": out}, name)
            return out

    def update_space(self, name: str, body: dict, actor: str, scope: Scope = ALL) -> dict:
        self._admin(scope)
        with self.tx():
            row = self._space_row(name)
            if "deadline_days" in body:
                days = self._deadline_days(body)
                if days != row["deadline_days"]:
                    self.conn.execute("UPDATE spaces SET deadline_days=? WHERE name=?", (days, name))
                    self._event(None, "space.updated", actor,
                                {"changes": {"deadline_days": [row["deadline_days"], days]}}, name)
            return self._space_dict(self._space_row(name))

    def delete_space(self, name: str, actor: str, scope: Scope = ALL) -> dict:
        """Only an empty space can go. Its machine tokens are revoked; its event chain stays."""
        self._admin(scope)
        with self.tx():
            self._space_row(name)
            if name == DEFAULT_SPACE:
                raise ApiError(409, "the default space can't be deleted")
            if self.conn.execute("SELECT 1 FROM decisions WHERE space=?", (name,)).fetchone():
                raise ApiError(409, f"space {name!r} still has decisions")
            if self.conn.execute("SELECT 1 FROM subscriptions WHERE space=?", (name,)).fetchone():
                raise ApiError(409, f"space {name!r} still has webhooks")
            revoked = [self._token_dict(r) for r in
                       self.conn.execute("SELECT * FROM tokens WHERE space=?", (name,)).fetchall()]
            self.conn.execute("DELETE FROM tokens WHERE space=?", (name,))
            self.conn.execute("DELETE FROM spaces WHERE name=?", (name,))
            self._event(None, "space.deleted", actor, {"revoked_tokens": revoked}, name)
            return {"deleted": name, "revoked_tokens": [t["id"] for t in revoked]}

    def _token_dict(self, row) -> dict:
        seen = sorted((r for r in self.resumers.values() if r.get("token") == row["id"]),
                      key=lambda r: r["last_seen"], reverse=True)
        return {"id": row["id"], "name": row["name"], "space": row["space"], "created_at": row["created_at"],
                "rolled_at": row["rolled_at"], "last_used_at": row["last_used_at"],
                "resumer": self._resumer_dict(seen[0]) if seen else None}

    def _token_row(self, tid: str):
        row = self.conn.execute("SELECT * FROM tokens WHERE id=?", (tid,)).fetchone()
        if not row:
            raise ApiError(404, f"no token {tid}")
        return row

    def list_tokens(self, scope: Scope = ALL) -> list[dict]:
        self._admin(scope)
        with self.lock:
            return [self._token_dict(r) for r in
                    self.conn.execute("SELECT * FROM tokens ORDER BY space, created_at").fetchall()]

    def create_token(self, body: dict, actor: str, scope: Scope = ALL) -> dict:
        """A token for one machine in one space. The secret is in this response only."""
        self._admin(scope)
        name = _req_str(body, "name")[:60]
        space = _opt_type(body, "space", str) or DEFAULT_SPACE
        secret = TOKEN_PREFIX + secrets.token_urlsafe(24)
        tid = "tok_" + secrets.token_hex(6)
        with self.tx():
            self._space_row(space)
            self.conn.execute("INSERT INTO tokens (id, name, space, hash, created_at) VALUES (?,?,?,?,?)",
                              (tid, name, space, self._hash_token(secret), now_iso()))
            out = self._token_dict(self._token_row(tid))
            self._event(None, "token.created", actor, {"token": out}, space)
            return out | {"token": secret}

    def roll_token(self, tid: str, actor: str, scope: Scope = ALL) -> dict:
        """Replace a token's secret: the old one stops working at once."""
        self._admin(scope)
        secret = TOKEN_PREFIX + secrets.token_urlsafe(24)
        with self.tx():
            row = self._token_row(tid)
            self.conn.execute("UPDATE tokens SET hash=?, rolled_at=?, last_used_at=NULL WHERE id=?",
                              (self._hash_token(secret), now_iso(), tid))
            self._event(None, "token.rolled", actor, {"token_id": tid, "name": row["name"]}, row["space"])
            return self._token_dict(self._token_row(tid)) | {"token": secret}

    def revoke_token(self, tid: str, actor: str, scope: Scope = ALL) -> dict:
        self._admin(scope)
        with self.tx():
            row = self._token_row(tid)
            self.conn.execute("DELETE FROM tokens WHERE id=?", (tid,))
            self._event(None, "token.revoked", actor, {"token": self._token_dict(row)}, row["space"])
            return {"revoked": tid}

    # ------------------------------------------------------------------ options

    def _insert_options(self, did: str, items: list, actor: str) -> list[str]:
        row = self._decision_row(did)
        nxt = row["next_option"]
        pos = self.conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM options WHERE decision_id=?", (did,)).fetchone()[0]
        existing = {r["id"] for r in self.conn.execute("SELECT id FROM options WHERE decision_id=?", (did,))}
        added = []
        ts = now_iso()
        for item in items:
            if isinstance(item, str):
                item = {"label": item}
            if not isinstance(item, dict):
                raise ApiError(400, "each option must be an object with at least a 'label'")
            label = _req_str(item, "label")
            oid = item.get("id")
            if oid is None:
                while str(nxt) in existing:
                    nxt += 1
                oid = str(nxt)
                nxt += 1
            oid = str(oid)
            if oid in existing:
                raise ApiError(409, f"option id {oid!r} already exists in {did}")
            existing.add(oid)
            pos += 1
            suggested = bool(item.get("suggested"))
            self.conn.execute(
                """INSERT INTO options (decision_id, id, label, description, url, image, attributes,
                   attribute_meta, suggested_by, suggested_at, position, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (did, oid, label, _opt_type(item, "description", str, ""), _opt_type(item, "url", str),
                 _opt_type(item, "image", str), canonical(_opt_type(item, "attributes", dict, {})),
                 canonical(_opt_type(item, "attribute_meta", dict, {})),
                 actor if suggested else None, ts if suggested else None, pos, ts, ts))
            added.append(oid)
        self.conn.execute("UPDATE decisions SET next_option=? WHERE id=?", (nxt, did))
        return added

    def add_options(self, raw_id: str, body, actor: str, scope: Scope = ALL) -> dict:
        did = norm_id(raw_id)
        items = body if isinstance(body, list) else body.get("options", [body])
        with self.tx():
            self._decision_row(did, scope)
            added = self._insert_options(did, items, actor)
            self._touch(did)
            opts = [self._option_dict(r) for r in self.conn.execute(
                f"SELECT * FROM options WHERE decision_id=? AND id IN ({','.join('?' * len(added))})",
                (did, *added))] if added else []
            # Options the user suggested ask the requester to fill them in, so they get their own event.
            suggested = [o for o in opts if o["suggested_by"]]
            if len(suggested) < len(opts) or not opts:
                self._event(did, "options.added", actor, {"options": [o for o in opts if not o["suggested_by"]]})
            if suggested:
                self._event(did, "options.suggested", actor, {"options": suggested})
            return self._decision_dict(did)

    def _apply_option_patch(self, did: str, oid: str, patch: dict) -> dict | None:
        row = self.conn.execute("SELECT * FROM options WHERE decision_id=? AND id=?", (did, oid)).fetchone()
        if not row:
            raise ApiError(404, f"no option {oid!r} in {did}")
        changes, sets = {}, {}
        for field in ("label", "description", "url", "image"):
            if field in patch:
                val = _opt_type(patch, field, str)
                if field == "label" and not (val or "").strip():
                    raise ApiError(400, "label cannot be empty")
                if field == "description":
                    val = val or ""
                if val != row[field]:
                    changes[field] = [row[field], val]
                    sets[field] = val
        for field in ("attributes", "attribute_meta"):
            if field not in patch:
                continue
            old = json.loads(row[field])
            incoming = _opt_type(patch, field, dict, {})
            new = incoming if patch.get("replace_attributes") else old | incoming
            new = {k: v for k, v in new.items() if v is not None}
            field_changes = {k: [old.get(k), new.get(k)] for k in set(old) | set(new) if old.get(k) != new.get(k)}
            if field_changes:
                changes[field] = field_changes
                sets[field] = canonical(new)
        if "position" in patch:
            sets["position"] = _opt_type(patch, "position", int)
            changes["position"] = [row["position"], sets["position"]]
        if not changes:
            return None
        sets["updated_at"] = now_iso()
        self.conn.execute(
            f"UPDATE options SET {', '.join(f'{k}=?' for k in sets)} WHERE decision_id=? AND id=?",
            (*sets.values(), did, oid))
        return changes

    def update_option(self, raw_id: str, oid: str, patch: dict, actor: str, scope: Scope = ALL) -> dict:
        did = norm_id(raw_id)
        with self.tx():
            self._decision_row(did, scope)
            changes = self._apply_option_patch(did, oid, patch)
            if changes:
                self._touch(did)
                self._event(did, "options.updated", actor, {"options": {oid: changes}})
            return self._decision_dict(did)

    def update_options(self, raw_id: str, body, actor: str, scope: Scope = ALL) -> dict:
        """Bulk patch: [{id, attributes?, label?, ...}]. Handy for filling requested attributes."""
        did = norm_id(raw_id)
        items = body if isinstance(body, list) else body.get("options")
        if not isinstance(items, list):
            raise ApiError(400, "expected a list of option patches, each with an 'id'")
        with self.tx():
            self._decision_row(did, scope)
            all_changes = {}
            for item in items:
                if not isinstance(item, dict) or "id" not in item:
                    raise ApiError(400, "each option patch needs an 'id'")
                ch = self._apply_option_patch(did, str(item["id"]), item)
                if ch:
                    all_changes[str(item["id"])] = ch
            if all_changes:
                self._touch(did)
                self._event(did, "options.updated", actor, {"options": all_changes})
            return self._decision_dict(did)

    def remove_option(self, raw_id: str, oid: str, actor: str, scope: Scope = ALL) -> dict:
        did = norm_id(raw_id)
        with self.tx():
            self._decision_row(did, scope)
            row = self.conn.execute("SELECT * FROM options WHERE decision_id=? AND id=?", (did, oid)).fetchone()
            if not row:
                raise ApiError(404, f"no option {oid!r} in {did}")
            self.conn.execute("DELETE FROM options WHERE decision_id=? AND id=?", (did, oid))
            self._touch(did)
            self._event(did, "option.removed", actor, {"option": self._option_dict(row)})
            return self._decision_dict(did)

    def set_excluded(self, raw_id: str, body: dict, excluded: bool, actor: str, scope: Scope = ALL) -> dict:
        did = norm_id(raw_id)
        oids = body.get("option_ids", body.get("options"))
        if isinstance(oids, str):
            oids = [oids]
        if not isinstance(oids, list) or not oids:
            raise ApiError(400, "'option_ids' must be a non-empty list")
        oids = [str(o) for o in oids]
        reason = _opt_type(body, "reason", str) if excluded else None
        with self.tx():
            self._decision_row(did, scope)
            existing = {r["id"] for r in self.conn.execute("SELECT id FROM options WHERE decision_id=?", (did,))}
            unknown = [o for o in oids if o not in existing]
            if unknown:
                raise ApiError(400, f"unknown option ids: {', '.join(unknown)}")
            ts = now_iso()
            self.conn.executemany(
                "UPDATE options SET excluded=?, exclude_reason=?, updated_at=? WHERE decision_id=? AND id=?",
                [(int(excluded), reason, ts, did, o) for o in oids])
            self._touch(did)
            self._event(did, "options.excluded" if excluded else "options.included", actor,
                        {"option_ids": oids, "reason": reason, "filters": body.get("filters")})
            return self._decision_dict(did)

    # ------------------------------------------------------------------ attribute requests

    def request_attributes(self, raw_id: str, body, actor: str, scope: Scope = ALL) -> dict:
        did = norm_id(raw_id)
        items = body if isinstance(body, list) else body.get("attributes", [body])
        with self.tx():
            row = self._decision_row(did, scope)
            defs = json.loads(row["attribute_defs"])
            requested = {}
            ts = now_iso()
            for item in items:
                if isinstance(item, str):
                    item = {"key": item}
                key = _req_str(item, "key")
                d = defs.get(key, {}) | {k: v for k, v in item.items() if k != "key" and v not in (None, "")}
                d |= {"requested": True, "requested_by": actor, "requested_at": ts}
                defs[key] = requested[key] = d
            self.conn.execute("UPDATE decisions SET attribute_defs=?, updated_at=? WHERE id=?",
                              (canonical(defs), ts, did))
            self._event(did, "attributes.requested", actor, {"attributes": requested})
            return self._decision_dict(did)

    def cancel_attribute_request(self, raw_id: str, key: str, actor: str, scope: Scope = ALL) -> dict:
        did = norm_id(raw_id)
        with self.tx():
            row = self._decision_row(did, scope)
            defs = json.loads(row["attribute_defs"])
            if key not in defs:
                raise ApiError(404, f"no attribute {key!r} on {did}")
            defs[key] = {k: v for k, v in defs[key].items()
                         if k not in ("requested", "requested_by", "requested_at")}
            self.conn.execute("UPDATE decisions SET attribute_defs=?, updated_at=? WHERE id=?",
                              (canonical(defs), now_iso(), did))
            self._event(did, "attributes.request_cancelled", actor, {"key": key})
            return self._decision_dict(did)

    # ------------------------------------------------------------------ events

    @staticmethod
    def _event_dict(row) -> dict:
        return {"seq": row["seq"], "space": row["space"], "ts": row["ts"], "decision_id": row["decision_id"],
                "goal_id": row["goal_id"], "type": row["type"], "actor": row["actor"], "data": json.loads(row["data"]),
                "hash": row["hash"]}

    def events(self, after=0, decision_id=None, types=None, limit=200, scope: Scope = ALL, goal_id=None) -> list[dict]:
        cond, args = scope.where("space")
        sql, args = "SELECT * FROM events WHERE seq > ?" + cond, [after, *args]
        if decision_id:
            did = norm_id(decision_id)
            with self.lock:
                self._decision_row(did, scope)
            sql += " AND decision_id=?"
            args.append(did)
        if goal_id:
            gid = norm_goal_id(goal_id)
            with self.lock:
                self._goal_row(gid, scope)
            sql += " AND goal_id=?"
            args.append(gid)
        sql += " ORDER BY seq LIMIT ?"
        # Type filtering is done in Python so wildcard patterns work.
        args.append(limit if not types else 100000)
        with self.lock:
            rows = [self._event_dict(r) for r in self.conn.execute(sql, args)]
        if types:
            rows = [e for e in rows if event_matches(types, e["type"])][:limit]
        return rows

    def cursor(self) -> int:
        return self.q1("SELECT COALESCE(MAX(seq), 0) FROM events")[0]

    def wait_events(self, after=0, decision_id=None, types=None, timeout=0.0, limit=200,
                    scope: Scope = ALL, goal_id=None, actor: str = "api") -> list[dict]:
        # A long-poll on one goal (`decide goal wait`) is a live session the resumer shouldn't also wake.
        tracked = goal_id and timeout > 0 and (norm_goal_id(goal_id), actor, tuple(types or ()))
        with self._waiting(tracked) if tracked else nullcontext():
            deadline = time.monotonic() + timeout
            with self.cond:
                while True:
                    evs = self.events(after, decision_id, types, limit, scope, goal_id)
                    remaining = deadline - time.monotonic()
                    if evs or remaining <= 0:
                        return evs
                    self.cond.wait(remaining)

    @contextmanager
    def _waiting(self, key: tuple):
        """Count a long-poller on a decision or goal while it's connected."""
        with self.lock:
            w = self.waiters.get(key)
            # Clients re-poll in chunks; treat a quick reconnect as the same continuous wait.
            if not w or (not w["connections"] and time.time() - w["last_seen"] > 90):
                w = self.waiters[key] = {"since": now_iso(), "connections": 0, "last_seen": time.time()}
            w["connections"] += 1
        try:
            yield
        finally:
            with self.lock:
                w["connections"] -= 1
                w["last_seen"] = time.time()

    def wait_decision(self, raw_id: str, types: list[str], after: int | None, timeout: float,
                      actor: str = "api", scope: Scope = ALL) -> dict:
        did = norm_id(raw_id)
        with self.lock:
            self._decision_row(did, scope)
        with self._waiting((did, actor, tuple(types))):
            return self._wait_decision(did, types, after, timeout, actor)

    def _long_pollers(self, subject: str) -> list[dict]:
        """Who is long-polling a decision or goal right now. Call under the lock."""
        out = []
        for (wid, actor, types), w in list(self.waiters.items()):
            idle = time.time() - w["last_seen"]
            if not w["connections"] and idle > 90:
                del self.waiters[(wid, actor, types)]
                continue
            if wid == subject:
                out.append({"kind": "long-poll", "id": f"poll:{actor}", "actor": actor, "events": list(types),
                            "since": w["since"], "connected": w["connections"] > 0, "idle": round(idle, 1)})
        return out

    def goal_listeners(self, raw_id: str, scope: Scope = ALL) -> list[dict]:
        """Sessions long-polling this goal (`decide goal wait`)."""
        gid = norm_goal_id(raw_id)
        with self.lock:
            self._goal_row(gid, scope)
            return self._long_pollers(gid)

    def listeners(self, raw_id: str, scope: Scope = ALL) -> list[dict]:
        """Everyone who will hear about this decision: long-pollers and webhooks."""
        did = norm_id(raw_id)
        with self.lock:
            space = self._decision_row(did, scope)["space"]
            out = self._long_pollers(did)
            subs = self.conn.execute(
                "SELECT * FROM subscriptions WHERE active=1 AND (decision_id=? OR "
                "(decision_id IS NULL AND (space IS NULL OR space=?)))", (did, space))
            for sub in subs.fetchall():
                if not scope.allows(sub["space"]):
                    continue  # an all-spaces webhook is the admin's business
                out.append({"kind": "webhook", "scope": "decision" if sub["decision_id"] else "global",
                            **self._sub_dict(sub)})
        return out

    def _wait_decision(self, did: str, types: list[str], after: int | None, timeout: float, actor: str) -> dict:
        with self.cond:
            d = self._decision_dict(did)
            if after is None:
                # Already in a terminal state the caller cares about? Return immediately.
                terminal = TERMINAL_EVENTS.get(d["status"])
                if terminal and event_matches(types, terminal):
                    ev = self.q1("SELECT * FROM events WHERE decision_id=? AND type=? ORDER BY seq DESC LIMIT 1",
                                 did, terminal)
                    return {"timed_out": False, "event": self._event_dict(ev) if ev else None,
                            "decision": d, "cursor": self.cursor()}
                after = self.cursor()
            deadline = time.monotonic() + timeout
            while True:
                evs = self.wait_events(after, did, types, max(0.0, deadline - time.monotonic()), limit=1)
                # Skip events this poller was silenced for; keep waiting for the next one.
                if not evs or f"poll:{actor}" not in evs[0]["data"].get("silenced", ()):
                    break
                after = evs[0]["seq"]
            return {"timed_out": not evs, "event": evs[0] if evs else None,
                    "decision": self._decision_dict(did),
                    "cursor": evs[0]["seq"] if evs else max(after, self.cursor())}

    def verify_chain(self, scope: Scope = ALL) -> dict:
        """Verify each space's chain on its own (space None = the server's chain)."""
        cond, args = scope.where("space")
        chains: dict = {}
        with self.lock:
            for row in self.conn.execute("SELECT * FROM events WHERE 1=1" + cond + " ORDER BY seq", args):
                c = chains.setdefault(row["space"], {"ok": True, "events": 0, "head": GENESIS})
                if not c["ok"]:
                    continue
                digest = self._record(row["space"], row["ts"], row["decision_id"], row["goal_id"], row["type"],
                                      row["actor"], json.loads(row["data"]), row["prev_hash"])
                if row["prev_hash"] != c["head"] or digest != row["hash"]:
                    c |= {"ok": False, "broken_at": row["seq"]}
                    continue
                c["head"] = row["hash"]
                c["events"] += 1
        return {"ok": all(c["ok"] for c in chains.values()), "events": sum(c["events"] for c in chains.values()),
                "chains": {("*" if k is None else k): v for k, v in sorted(chains.items(), key=lambda kv: kv[0] or "")}}

    # ------------------------------------------------------------------ webhooks

    def _create_sub(self, spec: dict, decision_id: str | None, space: str | None) -> dict:
        if not isinstance(spec, dict):
            raise ApiError(400, "webhook must be an object with a 'url'")
        url = _req_str(spec, "url")
        if not re.match(r"https?://", url):
            raise ApiError(400, "webhook url must be http(s)")
        events = _opt_type(spec, "events", list, DEFAULT_WEBHOOK_EVENTS)
        sid = "wh_" + secrets.token_hex(6)
        secret = _opt_type(spec, "secret", str) or secrets.token_urlsafe(24)
        req_ack = _opt_type(spec, "require_ack", bool)
        self.conn.execute(
            """INSERT INTO subscriptions (id, space, decision_id, url, secret, events, require_ack, max_attempts,
               refire_interval, description, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, space, decision_id, url, secret, canonical(events), None if req_ack is None else int(req_ack),
             _opt_type(spec, "max_attempts", int), _opt_type(spec, "refire_interval", (int, float)),
             _opt_type(spec, "description", str, ""), now_iso()))
        row = self.conn.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone()
        return self._sub_dict(row, reveal_secret=True)

    def create_subscription(self, body: dict, actor: str, decision_id: str | None = None,
                            scope: Scope = ALL) -> dict:
        did = norm_id(decision_id or body["decision_id"]) if (decision_id or body.get("decision_id")) else None
        with self.tx():
            if did:
                space = self._decision_row(did, scope)["space"]
            elif scope.everything and not body.get("space"):
                space = None  # hears every space
            else:
                space = self._space_for(scope, body.get("space"))
            sub = self._create_sub(body, did, space)
            if did and body.get("replay", True):
                self._replay_terminal(sub["id"], did)
            self._event(did, "webhook.created", actor, {"webhook": {k: v for k, v in sub.items() if k != "secret"}},
                        space)
            return sub

    def list_subscriptions(self, decision_id=None, scope: Scope = ALL) -> list[dict]:
        cond, args = scope.where("space")
        if decision_id:
            did = norm_id(decision_id)
            with self.lock:
                self._decision_row(did, scope)
            rows = self.q("SELECT * FROM subscriptions WHERE decision_id=?" + cond + " ORDER BY created_at",
                          did, *args)
        else:
            rows = self.q("SELECT * FROM subscriptions WHERE 1=1" + cond +
                          " ORDER BY decision_id IS NOT NULL, created_at", *args)
        return [self._sub_dict(r) for r in rows]

    def _sub_row(self, sid: str, scope: Scope):
        row = self.conn.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone()
        if not row or not scope.allows(row["space"]):
            raise ApiError(404, f"no webhook {sid}")
        return row

    def update_subscription(self, sid: str, body: dict, actor: str, scope: Scope = ALL) -> dict:
        with self.tx():
            row = self._sub_row(sid, scope)
            sets = {}
            for field, kinds in (("url", str), ("secret", str), ("description", str), ("max_attempts", int),
                                 ("refire_interval", (int, float))):
                if field in body:
                    sets[field] = _opt_type(body, field, kinds)
            if "events" in body:
                sets["events"] = canonical(_opt_type(body, "events", list, DEFAULT_WEBHOOK_EVENTS))
            for field in ("require_ack", "active"):
                if field in body:
                    v = _opt_type(body, field, bool)
                    sets[field] = None if v is None else int(v)
            if sets:
                self.conn.execute(f"UPDATE subscriptions SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                                  (*sets.values(), sid))
                if sets.get("active") == 0:
                    self.conn.execute(
                        "UPDATE deliveries SET status='cancelled', next_attempt_at=NULL "
                        "WHERE subscription_id=? AND next_attempt_at IS NOT NULL", (sid,))
                logged = {k: ("***" if k == "secret" else v) for k, v in sets.items()}
                self._event(row["decision_id"], "webhook.updated", actor, {"webhook_id": sid, "changes": logged},
                            row["space"])
            return self._sub_dict(self.conn.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone())

    def delete_subscription(self, sid: str, actor: str, scope: Scope = ALL) -> dict:
        with self.tx():
            row = self._sub_row(sid, scope)
            self.conn.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
            self.conn.execute(
                "UPDATE deliveries SET status='cancelled', next_attempt_at=NULL "
                "WHERE subscription_id=? AND next_attempt_at IS NOT NULL", (sid,))
            self._event(row["decision_id"], "webhook.deleted", actor, {"webhook": self._sub_dict(row)}, row["space"])
            return {"deleted": sid}

    @staticmethod
    def _delivery_dict(row) -> dict:
        return {k: row[k] for k in ("id", "subscription_id", "space", "event_seq", "decision_id", "event_type", "status",
                                    "attempts", "total_attempts", "last_status", "last_error",
                                    "delivered_at", "acked_at", "acked_by", "created_at")} | {
            "require_ack": bool(row["require_ack"]),
            "next_attempt_at": (datetime.fromtimestamp(row["next_attempt_at"], timezone.utc)
                                .isoformat(timespec="seconds").replace("+00:00", "Z")
                                if row["next_attempt_at"] else None),
        }

    def list_deliveries(self, decision_id=None, status=None, limit=100, scope: Scope = ALL) -> list[dict]:
        cond, args = scope.where("space")
        sql = "SELECT * FROM deliveries WHERE 1=1" + cond
        if decision_id:
            sql += " AND decision_id=?"
            args.append(norm_id(decision_id))
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        args.append(limit)
        return [self._delivery_dict(r) for r in self.q(sql, *args)]

    def _delivery_row(self, delivery_id: str, scope: Scope):
        row = self.conn.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        if not row or not scope.allows(row["space"]):
            raise ApiError(404, f"no delivery {delivery_id}")
        return row

    def delivery_attempts(self, delivery_id: str, scope: Scope = ALL) -> list[dict]:
        with self.lock:
            self._delivery_row(delivery_id, scope)
        return [dict(r) for r in self.q("SELECT * FROM delivery_attempts WHERE delivery_id=? ORDER BY id",
                                        delivery_id)]

    def ack_delivery(self, delivery_id: str, actor: str, scope: Scope = ALL) -> dict:
        with self.tx():
            row = self._delivery_row(delivery_id, scope)
            if row["status"] != "acked":
                self.conn.execute(
                    "UPDATE deliveries SET status='acked', acked_at=?, acked_by=?, next_attempt_at=NULL WHERE id=?",
                    (now_iso(), actor, delivery_id))
                self._event(row["decision_id"], "webhook.acked", actor,
                            {"delivery_id": delivery_id, "event_seq": row["event_seq"]}, row["space"])
            return self._delivery_dict(self.conn.execute("SELECT * FROM deliveries WHERE id=?",
                                                         (delivery_id,)).fetchone())

    def ack_decision(self, raw_id: str, actor: str, scope: Scope = ALL) -> dict:
        did = norm_id(raw_id)
        cond, args = scope.where("space")
        with self.lock:
            self._decision_row(did, scope)
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM deliveries WHERE decision_id=? AND status IN ('pending','delivered')" + cond,
                (did, *args))]
        return {"acked": [self.ack_delivery(i, actor, scope)["id"] for i in ids]}

    def redeliver(self, delivery_id: str, actor: str, scope: Scope = ALL) -> dict:
        with self.tx():
            row = self._delivery_row(delivery_id, scope)
            self.conn.execute(
                "UPDATE deliveries SET status='pending', attempts=0, acked_at=NULL, acked_by=NULL, "
                "next_attempt_at=? WHERE id=?", (time.time(), delivery_id))
            self._event(row["decision_id"], "webhook.redelivered", actor, {"delivery_id": delivery_id}, row["space"])
            return self._delivery_dict(self.conn.execute("SELECT * FROM deliveries WHERE id=?",
                                                         (delivery_id,)).fetchone())

    def due_deliveries(self, limit=20) -> list[dict]:
        rows = self.q(
            """SELECT d.*, s.url AS sub_url, s.secret AS sub_secret, s.max_attempts AS sub_max_attempts,
               s.refire_interval AS sub_refire_interval
               FROM deliveries d JOIN subscriptions s ON s.id = d.subscription_id
               WHERE d.next_attempt_at IS NOT NULL AND d.next_attempt_at <= ?
               AND d.status IN ('pending','delivered') AND s.active=1
               ORDER BY d.next_attempt_at LIMIT ?""", time.time(), limit)
        return [dict(r) for r in rows]

    def record_attempt(self, delivery: dict, status: int | None, error: str | None, duration_ms: int):
        cfg = self.cfg
        ok = status is not None and 200 <= status < 300
        attempts = delivery["attempts"] + 1
        max_attempts = delivery["sub_max_attempts"] if delivery["sub_max_attempts"] is not None \
            else cfg.webhook_max_attempts
        refire = delivery["sub_refire_interval"] or cfg.webhook_refire_interval
        exhausted = max_attempts and attempts >= max_attempts
        ts = now_iso()
        with self.tx():
            current = self.conn.execute("SELECT status FROM deliveries WHERE id=?", (delivery["id"],)).fetchone()
            self.conn.execute(
                "INSERT INTO delivery_attempts (delivery_id, ts, url, status, error, duration_ms) VALUES (?,?,?,?,?,?)",
                (delivery["id"], ts, delivery["sub_url"], status, error, duration_ms))
            if current["status"] in ("acked", "cancelled"):
                # Acked or cancelled while the request was in flight: keep that outcome.
                self.conn.execute("UPDATE deliveries SET attempts=?, total_attempts=total_attempts+1, "
                                  "last_status=?, last_error=? WHERE id=?",
                                  (attempts, status, error, delivery["id"]))
                return
            if ok:
                status_txt = "delivered"
                nxt = None if not delivery["require_ack"] or exhausted else time.time() + refire
            else:
                status_txt = "failed" if exhausted else ("delivered" if delivery["delivered_at"] else "pending")
                backoff = min(cfg.webhook_backoff_base * 2 ** (attempts - 1), cfg.webhook_backoff_max)
                nxt = None if exhausted else time.time() + backoff
            self.conn.execute(
                """UPDATE deliveries SET status=?, attempts=?, total_attempts=total_attempts+1, next_attempt_at=?,
                   last_status=?, last_error=?, delivered_at=COALESCE(delivered_at, ?) WHERE id=?""",
                (status_txt, attempts, nxt, status, error, ts if ok else None, delivery["id"]))
