"""SQLite storage: decisions, options, the append-only event log, and webhook deliveries.

Decisions and options are freely mutable. Every mutation appends an event to a
hash-chained log that SQLite triggers make immutable (no UPDATE / DELETE), so the
log is the provenance record of what changed, when, and by whom.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from .agents import manual_command
from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY,
  num INTEGER NOT NULL UNIQUE,
  title TEXT NOT NULL,
  context TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  min_select INTEGER NOT NULL DEFAULT 1,
  max_select INTEGER,
  attribute_defs TEXT NOT NULL DEFAULT '{}',
  links TEXT NOT NULL DEFAULT '[]',
  tags TEXT NOT NULL DEFAULT '[]',
  metadata TEXT NOT NULL DEFAULT '{}',
  resolution TEXT,
  due_at TEXT,
  idempotency_key TEXT UNIQUE,
  resume TEXT,
  next_option INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT
);

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
  position INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (decision_id, id)
);

CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  decision_id TEXT,
  type TEXT NOT NULL,
  actor TEXT NOT NULL,
  data TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_decision ON events(decision_id, seq);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;

CREATE TABLE IF NOT EXISTS subscriptions (
  id TEXT PRIMARY KEY,
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

DEFAULT_WEBHOOK_EVENTS = [
    "decision.resolved",
    "decision.reopened",
    "decision.cancelled",
    "attributes.requested",
]
STATUSES = ("open", "resolved", "cancelled")
GENESIS = "0" * 64


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def norm_id(raw: str) -> str:
    m = re.fullmatch(r"(?i)\s*(?:d-?)?(\d+)\s*", str(raw))
    if not m:
        raise ApiError(404, f"no decision {raw!r}")
    return f"D-{int(m.group(1))}"


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


def _resume(value) -> dict | None:
    """How to pick the requesting agent back up once the decision is made."""
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("session_id"), str) or not value["session_id"].strip():
        raise ApiError(400, "resume must be an object with a 'session_id' (and usually 'cwd')")
    for k in ("cwd", "agent", "prompt", "machine"):
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
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(options)")}
        if "attribute_meta" not in cols:  # migrate databases created before attribute_meta existed
            self.conn.execute("ALTER TABLE options ADD COLUMN attribute_meta TEXT NOT NULL DEFAULT '{}'")
        if "resume" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(decisions)")}:
            self.conn.execute("ALTER TABLE decisions ADD COLUMN resume TEXT")
        # Last heartbeat per machine from resumer daemons (`decide resumer run`). In memory only.
        self.resumers: dict[str, dict] = {}
        # Who is long-polling which decision right now: (decision, actor, types) -> info. In memory only.
        self.waiters: dict[tuple, dict] = {}

    # ------------------------------------------------------------------ plumbing

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

    def _decision_row(self, did: str):
        row = self.conn.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
        if not row:
            raise ApiError(404, f"no decision {did}")
        return row

    def _event(self, decision_id, etype, actor, data) -> int:
        """Append to the hash-chained log and fan out webhook deliveries. Call inside tx()."""
        last = self.conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = last["hash"] if last else GENESIS
        ts = now_iso()
        record = {"ts": ts, "decision_id": decision_id, "type": etype, "actor": actor,
                  "data": data, "prev_hash": prev_hash}
        digest = hashlib.sha256(canonical(record).encode()).hexdigest()
        seq = self.conn.execute(
            "INSERT INTO events (ts, decision_id, type, actor, data, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
            (ts, decision_id, etype, actor, canonical(data), prev_hash, digest),
        ).lastrowid
        if not etype.startswith("webhook."):
            self._fanout(seq, ts, decision_id, etype, actor)
        return seq

    def _fanout(self, seq, ts, decision_id, etype, actor):
        subs = self.conn.execute(
            "SELECT * FROM subscriptions WHERE active=1 AND (decision_id IS NULL OR decision_id=?)",
            (decision_id,),
        ).fetchall()
        snapshot = None
        for sub in subs:
            if not event_matches(json.loads(sub["events"]), etype):
                continue
            if snapshot is None and decision_id:
                snapshot = self._decision_dict(decision_id)
            self._enqueue(sub, seq, ts, decision_id, etype, actor, snapshot)

    def _enqueue(self, sub, seq, ts, decision_id, etype, actor, snapshot, replayed=False):
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
        self.conn.execute(
            """INSERT INTO deliveries (id, subscription_id, event_seq, decision_id, event_type, payload,
               status, require_ack, next_attempt_at, created_at) VALUES (?,?,?,?,?,?,'pending',?,?,?)""",
            (delivery_id, sub["id"], seq, decision_id, etype, canonical(payload),
             int(require_ack), time.time(), now_iso()),
        )

    def _replay_terminal(self, sub_id: str, decision_id: str):
        """A webhook registered after the decision was already made still hears about it."""
        row = self._decision_row(decision_id)
        terminal = {"resolved": "decision.resolved", "cancelled": "decision.cancelled"}.get(row["status"])
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
        pending = {}
        for key, d in defs.items():
            if not d.get("requested"):
                continue
            missing = [o["id"] for o in options
                       if not o["excluded"] and o["attributes"].get(key) is None]
            if missing:
                pending[key] = {"description": d.get("description", ""), "missing": missing}
        subs = self.conn.execute(
            "SELECT * FROM subscriptions WHERE decision_id=? ORDER BY created_at", (did,)).fetchall()
        out = {
            "id": row["id"],
            "url": f"{self.cfg.public_url}/d/{row['id']}",
            "title": row["title"],
            "context": row["context"],
            "source": row["source"],
            "status": row["status"],
            "min_select": row["min_select"],
            "max_select": row["max_select"],
            "attribute_defs": defs,
            "links": json.loads(row["links"]),
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
        }
        return out

    def _resume_state(self, did: str, row) -> dict | None:
        """Where the requester's resumption stands for the latest resolution."""
        if not row["resume"]:
            return None
        res = self.conn.execute(
            "SELECT seq, ts FROM events WHERE decision_id=? AND type='decision.resolved' ORDER BY seq DESC LIMIT 1",
            (did,)).fetchone()
        if row["status"] != "resolved" or not res:
            return {"status": "armed"}
        ev = self.conn.execute(
            "SELECT * FROM events WHERE decision_id=? AND type LIKE 'resume.%' AND seq>? ORDER BY seq DESC LIMIT 1",
            (did, res["seq"])).fetchone()
        state = {"status": "pending", "resolution_seq": res["seq"], "since": res["ts"]}
        if ev:
            state |= {"status": ev["type"].split(".", 1)[1], "since": ev["ts"], "actor": ev["actor"],
                      "detail": json.loads(ev["data"])}
        return state

    @staticmethod
    def _sub_dict(row, reveal_secret=False) -> dict:
        out = {
            "id": row["id"],
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

    def create_decision(self, body: dict, actor: str) -> tuple[dict, bool]:
        title = _req_str(body, "title")
        key = _opt_type(body, "idempotency_key", str)
        with self.tx():
            if key:
                existing = self.conn.execute(
                    "SELECT id FROM decisions WHERE idempotency_key=?", (key,)).fetchone()
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
            self.conn.execute(
                """INSERT INTO decisions (id, num, title, context, source, min_select, max_select,
                   attribute_defs, links, tags, metadata, due_at, idempotency_key, created_at, updated_at, resume)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (did, num, title, _opt_type(body, "context", str, ""),
                 _opt_type(body, "source", str, actor), min_select, max_select, canonical(defs),
                 canonical(_links(body.get("links"))), canonical(_opt_type(body, "tags", list, [])),
                 canonical(_opt_type(body, "metadata", dict, {})), _opt_type(body, "due_at", str),
                 key, ts, ts, canonical(r) if (r := _resume(body.get("resume"))) else None),
            )
            self._insert_options(did, _opt_type(body, "options", list, []))
            hooks = body.get("webhooks") or ([body["webhook"]] if body.get("webhook") else [])
            created_subs = [self._create_sub(h, did) for h in hooks]
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

    def get_decision(self, raw_id: str) -> dict:
        with self.lock:
            return self._decision_dict(norm_id(raw_id))

    def list_decisions(self, status=None, q=None, tag=None, source=None, limit=100, offset=0) -> list[dict]:
        sql = "SELECT * FROM decisions WHERE 1=1"
        args: list = []
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
                    k: d[k] for k in ("id", "url", "title", "source", "status", "tags", "created_at",
                                      "updated_at", "resolved_at", "due_at", "resolution",
                                      "pending_attribute_requests", "min_select", "max_select", "resume_state")
                } | {"option_count": counts[0], "excluded_count": counts[1] or 0})
        return out

    def update_decision(self, raw_id: str, body: dict, actor: str) -> dict:
        did = norm_id(raw_id)
        with self.tx():
            row = self._decision_row(did)
            changes = {}
            sets = {}
            for field, kinds in (("title", str), ("context", str), ("source", str), ("due_at", str)):
                if field in body:
                    val = _opt_type(body, field, kinds)
                    if field == "title" and not (val or "").strip():
                        raise ApiError(400, "title cannot be empty")
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
            if "links" in body:
                val, old = _links(body["links"]), json.loads(row["links"])
                if val != old:
                    changes["links"] = [old, val]
                    sets["links"] = canonical(val)
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
            return self._decision_dict(did)

    def resolve(self, raw_id: str, body: dict, actor: str) -> dict:
        did = norm_id(raw_id)
        selected = body.get("selected", body.get("option_ids"))
        if isinstance(selected, str):
            selected = [selected]
        if not isinstance(selected, list):
            raise ApiError(400, "'selected' must be a list of option ids")
        selected = list(dict.fromkeys(str(s) for s in selected))
        with self.tx():
            row = self._decision_row(did)
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
            self._event(did, "decision.resolved", actor,
                        {"resolution": resolution, "previous_status": row["status"], "previous_resolution": previous})
            return self._decision_dict(did)

    def set_status(self, raw_id: str, status: str, note: str, actor: str) -> dict:
        did = norm_id(raw_id)
        etype = {"open": "decision.reopened", "cancelled": "decision.cancelled"}[status]
        with self.tx():
            row = self._decision_row(did)
            previous = json.loads(row["resolution"]) if row["resolution"] else None
            self.conn.execute(
                "UPDATE decisions SET status=?, resolution=NULL, resolved_at=NULL, updated_at=? WHERE id=?",
                (status, now_iso(), did))
            self._event(did, etype, actor,
                        {"note": note, "previous_status": row["status"], "previous_resolution": previous})
            return self._decision_dict(did)

    def comment(self, raw_id: str, body: dict, actor: str) -> dict:
        did = norm_id(raw_id)
        text = _req_str(body, "text")
        with self.tx():
            self._decision_row(did)
            self._touch(did)
            seq = self._event(did, "decision.commented", actor, {"text": text})
        return {"seq": seq, "text": text, "actor": actor}

    # ------------------------------------------------------------------ resumption

    RESUME_EVENTS = ("started", "finished", "failed", "manual")

    def resume_event(self, raw_id: str, body: dict, actor: str) -> dict:
        did = norm_id(raw_id)
        kind = body.get("status")
        if kind not in self.RESUME_EVENTS:
            raise ApiError(400, f"status must be one of {', '.join(self.RESUME_EVENTS)}")
        with self.tx():
            row = self._decision_row(did)
            if not row["resume"]:
                raise ApiError(400, f"{did} has no resume target")
            self._touch(did)
            self._event(did, f"resume.{kind}", actor, {k: v for k, v in body.items() if k != "status"})
            return self._decision_dict(did)

    def resumer_heartbeat(self, body: dict, actor: str) -> dict:
        machine = str(body.get("machine") or body.get("host") or "default")
        with self.lock:
            self.resumers[machine] = {"name": actor, "pid": body.get("pid"), "machine": machine,
                                      "command": body.get("command"), "last_seen": time.time()}
        return self.resumer_status(machine)

    def _resumer_dict(self, r: dict) -> dict:
        return {"online": time.time() - r["last_seen"] < 90, "name": r["name"], "pid": r["pid"],
                "machine": r["machine"], "command": r["command"],
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

    # ------------------------------------------------------------------ options

    def _insert_options(self, did: str, items: list) -> list[str]:
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
            self.conn.execute(
                """INSERT INTO options (decision_id, id, label, description, url, image, attributes,
                   attribute_meta, position, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (did, oid, label, _opt_type(item, "description", str, ""), _opt_type(item, "url", str),
                 _opt_type(item, "image", str), canonical(_opt_type(item, "attributes", dict, {})),
                 canonical(_opt_type(item, "attribute_meta", dict, {})), pos, ts, ts))
            added.append(oid)
        self.conn.execute("UPDATE decisions SET next_option=? WHERE id=?", (nxt, did))
        return added

    def add_options(self, raw_id: str, body, actor: str) -> dict:
        did = norm_id(raw_id)
        items = body if isinstance(body, list) else body.get("options", [body])
        with self.tx():
            added = self._insert_options(did, items)
            self._touch(did)
            opts = [self._option_dict(r) for r in self.conn.execute(
                f"SELECT * FROM options WHERE decision_id=? AND id IN ({','.join('?' * len(added))})",
                (did, *added))] if added else []
            self._event(did, "options.added", actor, {"options": opts})
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

    def update_option(self, raw_id: str, oid: str, patch: dict, actor: str) -> dict:
        did = norm_id(raw_id)
        with self.tx():
            self._decision_row(did)
            changes = self._apply_option_patch(did, oid, patch)
            if changes:
                self._touch(did)
                self._event(did, "options.updated", actor, {"options": {oid: changes}})
            return self._decision_dict(did)

    def update_options(self, raw_id: str, body, actor: str) -> dict:
        """Bulk patch: [{id, attributes?, label?, ...}]. Handy for filling requested attributes."""
        did = norm_id(raw_id)
        items = body if isinstance(body, list) else body.get("options")
        if not isinstance(items, list):
            raise ApiError(400, "expected a list of option patches, each with an 'id'")
        with self.tx():
            self._decision_row(did)
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

    def remove_option(self, raw_id: str, oid: str, actor: str) -> dict:
        did = norm_id(raw_id)
        with self.tx():
            row = self.conn.execute("SELECT * FROM options WHERE decision_id=? AND id=?", (did, oid)).fetchone()
            if not row:
                raise ApiError(404, f"no option {oid!r} in {did}")
            self.conn.execute("DELETE FROM options WHERE decision_id=? AND id=?", (did, oid))
            self._touch(did)
            self._event(did, "option.removed", actor, {"option": self._option_dict(row)})
            return self._decision_dict(did)

    def set_excluded(self, raw_id: str, body: dict, excluded: bool, actor: str) -> dict:
        did = norm_id(raw_id)
        oids = body.get("option_ids", body.get("options"))
        if isinstance(oids, str):
            oids = [oids]
        if not isinstance(oids, list) or not oids:
            raise ApiError(400, "'option_ids' must be a non-empty list")
        oids = [str(o) for o in oids]
        reason = _opt_type(body, "reason", str) if excluded else None
        with self.tx():
            self._decision_row(did)
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

    def request_attributes(self, raw_id: str, body, actor: str) -> dict:
        did = norm_id(raw_id)
        items = body if isinstance(body, list) else body.get("attributes", [body])
        with self.tx():
            row = self._decision_row(did)
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

    def cancel_attribute_request(self, raw_id: str, key: str, actor: str) -> dict:
        did = norm_id(raw_id)
        with self.tx():
            row = self._decision_row(did)
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
        return {"seq": row["seq"], "ts": row["ts"], "decision_id": row["decision_id"], "type": row["type"],
                "actor": row["actor"], "data": json.loads(row["data"]), "hash": row["hash"]}

    def events(self, after=0, decision_id=None, types=None, limit=200) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE seq > ?", [after]
        if decision_id:
            sql += " AND decision_id=?"
            args.append(norm_id(decision_id))
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

    def wait_events(self, after=0, decision_id=None, types=None, timeout=0.0, limit=200) -> list[dict]:
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                evs = self.events(after, decision_id, types, limit)
                remaining = deadline - time.monotonic()
                if evs or remaining <= 0:
                    return evs
                self.cond.wait(remaining)

    def wait_decision(self, raw_id: str, types: list[str], after: int | None, timeout: float,
                      actor: str = "api") -> dict:
        did = norm_id(raw_id)
        key = (did, actor, tuple(types))
        with self.lock:
            w = self.waiters.get(key)
            # Clients re-poll in chunks; treat a quick reconnect as the same continuous wait.
            if not w or (not w["connections"] and time.time() - w["last_seen"] > 90):
                w = self.waiters[key] = {"since": now_iso(), "connections": 0, "last_seen": time.time()}
            w["connections"] += 1
        try:
            return self._wait_decision(did, types, after, timeout)
        finally:
            with self.lock:
                w["connections"] -= 1
                w["last_seen"] = time.time()

    def listeners(self, raw_id: str) -> list[dict]:
        """Everyone who will hear about this decision: long-pollers and webhooks."""
        did = norm_id(raw_id)
        out = []
        with self.lock:
            for (wdid, actor, types), w in list(self.waiters.items()):
                idle = time.time() - w["last_seen"]
                if not w["connections"] and idle > 90:
                    del self.waiters[(wdid, actor, types)]
                    continue
                if wdid == did:
                    out.append({"kind": "long-poll", "actor": actor, "events": list(types), "since": w["since"],
                                "connected": w["connections"] > 0})
            subs = self.conn.execute(
                "SELECT * FROM subscriptions WHERE active=1 AND (decision_id=? OR decision_id IS NULL)", (did,))
            for sub in subs.fetchall():
                out.append({"kind": "webhook", "scope": "decision" if sub["decision_id"] else "global",
                            **self._sub_dict(sub)})
        return out

    def _wait_decision(self, did: str, types: list[str], after: int | None, timeout: float) -> dict:
        with self.cond:
            d = self._decision_dict(did)
            if after is None:
                # Already in a terminal state the caller cares about? Return immediately.
                terminal = {"resolved": "decision.resolved", "cancelled": "decision.cancelled"}.get(d["status"])
                if terminal and event_matches(types, terminal):
                    ev = self.q1("SELECT * FROM events WHERE decision_id=? AND type=? ORDER BY seq DESC LIMIT 1",
                                 did, terminal)
                    return {"timed_out": False, "event": self._event_dict(ev) if ev else None,
                            "decision": d, "cursor": self.cursor()}
                after = self.cursor()
            evs = self.wait_events(after, did, types, timeout, limit=1)
            return {"timed_out": not evs, "event": evs[0] if evs else None,
                    "decision": self._decision_dict(did),
                    "cursor": evs[0]["seq"] if evs else max(after, self.cursor())}

    def verify_chain(self) -> dict:
        prev = GENESIS
        count = 0
        with self.lock:
            for row in self.conn.execute("SELECT * FROM events ORDER BY seq"):
                record = {"ts": row["ts"], "decision_id": row["decision_id"], "type": row["type"],
                          "actor": row["actor"], "data": json.loads(row["data"]), "prev_hash": row["prev_hash"]}
                digest = hashlib.sha256(canonical(record).encode()).hexdigest()
                if row["prev_hash"] != prev or digest != row["hash"]:
                    return {"ok": False, "events": count, "broken_at": row["seq"]}
                prev = row["hash"]
                count += 1
        return {"ok": True, "events": count, "head": prev}

    # ------------------------------------------------------------------ webhooks

    def _create_sub(self, spec: dict, decision_id: str | None) -> dict:
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
            """INSERT INTO subscriptions (id, decision_id, url, secret, events, require_ack, max_attempts,
               refire_interval, description, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (sid, decision_id, url, secret, canonical(events), None if req_ack is None else int(req_ack),
             _opt_type(spec, "max_attempts", int), _opt_type(spec, "refire_interval", (int, float)),
             _opt_type(spec, "description", str, ""), now_iso()))
        row = self.conn.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone()
        return self._sub_dict(row, reveal_secret=True)

    def create_subscription(self, body: dict, actor: str, decision_id: str | None = None) -> dict:
        did = norm_id(decision_id or body["decision_id"]) if (decision_id or body.get("decision_id")) else None
        with self.tx():
            if did:
                self._decision_row(did)
            sub = self._create_sub(body, did)
            if did and body.get("replay", True):
                self._replay_terminal(sub["id"], did)
            self._event(did, "webhook.created", actor, {"webhook": {k: v for k, v in sub.items() if k != "secret"}})
            return sub

    def list_subscriptions(self, decision_id=None) -> list[dict]:
        if decision_id:
            rows = self.q("SELECT * FROM subscriptions WHERE decision_id=? ORDER BY created_at", norm_id(decision_id))
        else:
            rows = self.q("SELECT * FROM subscriptions ORDER BY decision_id IS NOT NULL, created_at")
        return [self._sub_dict(r) for r in rows]

    def update_subscription(self, sid: str, body: dict, actor: str) -> dict:
        with self.tx():
            row = self.conn.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone()
            if not row:
                raise ApiError(404, f"no webhook {sid}")
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
                self._event(row["decision_id"], "webhook.updated", actor, {"webhook_id": sid, "changes": logged})
            return self._sub_dict(self.conn.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone())

    def delete_subscription(self, sid: str, actor: str) -> dict:
        with self.tx():
            row = self.conn.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone()
            if not row:
                raise ApiError(404, f"no webhook {sid}")
            self.conn.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
            self.conn.execute(
                "UPDATE deliveries SET status='cancelled', next_attempt_at=NULL "
                "WHERE subscription_id=? AND next_attempt_at IS NOT NULL", (sid,))
            self._event(row["decision_id"], "webhook.deleted", actor, {"webhook": self._sub_dict(row)})
            return {"deleted": sid}

    @staticmethod
    def _delivery_dict(row) -> dict:
        return {k: row[k] for k in ("id", "subscription_id", "event_seq", "decision_id", "event_type", "status",
                                    "attempts", "total_attempts", "last_status", "last_error",
                                    "delivered_at", "acked_at", "acked_by", "created_at")} | {
            "require_ack": bool(row["require_ack"]),
            "next_attempt_at": (datetime.fromtimestamp(row["next_attempt_at"], timezone.utc)
                                .isoformat(timespec="seconds").replace("+00:00", "Z")
                                if row["next_attempt_at"] else None),
        }

    def list_deliveries(self, decision_id=None, status=None, limit=100) -> list[dict]:
        sql, args = "SELECT * FROM deliveries WHERE 1=1", []
        if decision_id:
            sql += " AND decision_id=?"
            args.append(norm_id(decision_id))
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        args.append(limit)
        return [self._delivery_dict(r) for r in self.q(sql, *args)]

    def delivery_attempts(self, delivery_id: str) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM delivery_attempts WHERE delivery_id=? ORDER BY id",
                                        delivery_id)]

    def ack_delivery(self, delivery_id: str, actor: str) -> dict:
        with self.tx():
            row = self.conn.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            if not row:
                raise ApiError(404, f"no delivery {delivery_id}")
            if row["status"] != "acked":
                self.conn.execute(
                    "UPDATE deliveries SET status='acked', acked_at=?, acked_by=?, next_attempt_at=NULL WHERE id=?",
                    (now_iso(), actor, delivery_id))
                self._event(row["decision_id"], "webhook.acked", actor,
                            {"delivery_id": delivery_id, "event_seq": row["event_seq"]})
            return self._delivery_dict(self.conn.execute("SELECT * FROM deliveries WHERE id=?",
                                                         (delivery_id,)).fetchone())

    def ack_decision(self, raw_id: str, actor: str) -> dict:
        did = norm_id(raw_id)
        with self.lock:
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM deliveries WHERE decision_id=? AND status IN ('pending','delivered')", (did,))]
        return {"acked": [self.ack_delivery(i, actor)["id"] for i in ids]}

    def redeliver(self, delivery_id: str, actor: str) -> dict:
        with self.tx():
            row = self.conn.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            if not row:
                raise ApiError(404, f"no delivery {delivery_id}")
            self.conn.execute(
                "UPDATE deliveries SET status='pending', attempts=0, acked_at=NULL, acked_by=NULL, "
                "next_attempt_at=? WHERE id=?", (time.time(), delivery_id))
            self._event(row["decision_id"], "webhook.redelivered", actor, {"delivery_id": delivery_id})
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
