"""HTTP server: JSON REST API under /api, the single-page GUI everywhere else."""
from __future__ import annotations

import json
import mimetypes
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, dist
from .config import Config, is_loopback
from .markdown import decision_markdown
from .store import ALL, DEFAULT_WEBHOOK_EVENTS, ApiError, Scope, Store
from .webhooks import Dispatcher

STATIC = Path(__file__).parent / "static"
MAX_WAIT = 300.0
ROUTES: list[tuple[str, re.Pattern, str]] = []


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn.__name__))
        return fn
    return deco


def _types(qs, default):
    raw = qs.get("types", [""])[0]
    return [t.strip() for t in raw.split(",") if t.strip()] or default


class Handler(BaseHTTPRequestHandler):
    server_version = "decisions/0.1"
    store: Store
    cfg: Config
    scope: Scope = ALL

    # ------------------------------------------------------------------ plumbing

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/events") and "/wait" not in self.path:
            super().log_message(fmt, *args)

    def _send(self, status: int, body, content_type="application/json; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else (
            body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False, indent=1).encode())
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ApiError(400, f"invalid JSON: {exc}")

    @property
    def actor(self) -> str:
        return (self.headers.get("X-Actor") or "").strip()[:100] or "api"

    def _scope(self) -> Scope | None:
        auth = self.headers.get("Authorization", "")
        supplied = auth[7:] if auth.startswith("Bearer ") else self.headers.get("X-Api-Key", "")
        return self.store.scope_for(supplied)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Actor, X-Api-Key")
        self.end_headers()

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_PUT(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        url = urlparse(self.path)
        self.qs = parse_qs(url.query)
        path = url.path.rstrip("/") or "/"
        try:
            if path == "/install.sh" or path.startswith("/install/"):
                return self._install(path)
            if not path.startswith("/api/"):
                return self._static(path)
            if path != "/api/health":
                scope = self._scope()
                if scope is None:
                    raise ApiError(401, "missing or invalid token")
                self.scope = scope
            for m, pattern, name in ROUTES:
                match = pattern.match(path)
                if match and m == method:
                    result = getattr(self, name)(*match.groups())
                    if result is None:  # handler wrote its own response
                        return
                    if isinstance(result, tuple):
                        return self._send(result[0], result[1])
                    return self._send(200, result)
            raise ApiError(404 if not any(p.match(path) for _, p, _ in ROUTES) else 405, f"{method} {path}")
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            traceback.print_exc()
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _static(self, path: str):
        name = path.removeprefix("/static/") if path.startswith("/static/") else "index.html"
        file = (STATIC / name).resolve()
        if STATIC.resolve() not in file.parents or not file.is_file():
            raise ApiError(404, "not found")
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, file.read_bytes(), ctype, {"Cache-Control": "no-cache"})

    def _server_url(self) -> str:
        """The URL this client reached us at (what other machines should use), unless base_url is set."""
        if self.cfg.base_url:
            return self.cfg.public_url
        host = self.headers.get("Host")
        proto = self.headers.get("X-Forwarded-Proto", "http")
        return f"{proto}://{host}" if host else self.cfg.public_url

    def _install(self, path: str):
        """Public (no token): the installer and the package. Neither contains secrets."""
        name, data = dist.wheel()
        if path == "/install.sh":
            return self._send(200, dist.install_script(self._server_url()), "text/x-shellscript; charset=utf-8")
        if path == f"/install/{name}" or path == "/install/decisions.whl":
            return self._send(200, data, "application/octet-stream",
                              {"Content-Disposition": f'attachment; filename="{name}"'})
        if path == "/install/SKILL.md":
            return self._send(200, dist.SKILL.read_bytes(), "text/markdown; charset=utf-8")
        raise ApiError(404, "not found")

    # ------------------------------------------------------------------ meta

    @route("GET", "/api/health")
    def health(self):
        return {"ok": True}

    @route("GET", "/api/config")
    def config(self):
        c = self.cfg
        return {"base_url": c.public_url, "owner": c.owner, "auth": bool(c.token), "version": __version__,
                "callback_host": c.callback_host or None, "deadline_days": c.deadline_days,
                "install_url": self._server_url() + "/install.sh",
                "default_webhook_events": DEFAULT_WEBHOOK_EVENTS, "cursor": self.store.cursor(),
                "space": self.scope.default, "admin": self.scope.everything,
                "spaces": [{"name": sp["name"], "deadline_days": sp["effective_deadline_days"]}
                           for sp in self.store.list_spaces(self.scope)],
                "webhook": {"timeout": c.webhook_timeout, "max_attempts": c.webhook_max_attempts,
                            "backoff_base": c.webhook_backoff_base, "backoff_max": c.webhook_backoff_max,
                            "require_ack": c.webhook_require_ack, "refire_interval": c.webhook_refire_interval}}

    # ------------------------------------------------------------------ spaces and machine tokens

    @route("GET", "/api/spaces")
    def list_spaces(self):
        return {"spaces": self.store.list_spaces(self.scope)}

    @route("POST", "/api/spaces")
    def create_space(self):
        return 201, self.store.create_space(self._body(), self.actor, self.scope)

    @route("PATCH", "/api/spaces/([^/]+)")
    def update_space(self, name):
        return self.store.update_space(name, self._body(), self.actor, self.scope)

    @route("DELETE", "/api/spaces/([^/]+)")
    def delete_space(self, name):
        return self.store.delete_space(name, self.actor, self.scope)

    @route("GET", "/api/tokens")
    def list_tokens(self):
        return {"tokens": self.store.list_tokens(self.scope)}

    @route("POST", "/api/tokens")
    def create_token(self):
        return 201, self.store.create_token(self._body(), self.actor, self.scope)

    @route("POST", "/api/tokens/([^/]+)/roll")
    def roll_token(self, tid):
        return self.store.roll_token(tid, self.actor, self.scope)

    @route("DELETE", "/api/tokens/([^/]+)")
    def revoke_token(self, tid):
        return self.store.revoke_token(tid, self.actor, self.scope)

    # ------------------------------------------------------------------ decisions

    @route("GET", "/api/decisions")
    def list_decisions(self):
        g = lambda k, d=None: self.qs.get(k, [d])[0]
        return {"decisions": self.store.list_decisions(
            status=g("status"), q=g("q"), tag=g("tag"), source=g("source"), space=g("space"),
            limit=int(g("limit", 100)), offset=int(g("offset", 0)), scope=self.scope)}

    @route("POST", "/api/decisions")
    def create_decision(self):
        decision, created = self.store.create_decision(self._body(), self.actor, self.scope)
        return (201 if created else 200), decision

    @route("GET", "/api/decisions/([^/]+)")
    def get_decision(self, did):
        return self.store.get_decision(did, self.scope)

    @route("PATCH", "/api/decisions/([^/]+)")
    def update_decision(self, did):
        return self.store.update_decision(did, self._body(), self.actor, self.scope)

    @route("GET", "/api/decisions/([^/]+)/markdown")
    def markdown(self, did):
        self._send(200, decision_markdown(self.store.get_decision(did, self.scope)), "text/markdown; charset=utf-8")

    @route("POST", "/api/decisions/([^/]+)/resolve")
    def resolve(self, did):
        return self.store.resolve(did, self._body(), self.actor, self.scope)

    @route("POST", "/api/decisions/([^/]+)/reopen")
    def reopen(self, did):
        return self.store.set_status(did, "open", self._body().get("note", ""), self.actor, self.scope)

    @route("POST", "/api/decisions/([^/]+)/cancel")
    def cancel(self, did):
        return self.store.set_status(did, "cancelled", self._body().get("note", ""), self.actor, self.scope)

    @route("POST", "/api/decisions/([^/]+)/comments")
    def comment(self, did):
        return 201, self.store.comment(did, self._body(), self.actor, self.scope)

    @route("GET", "/api/decisions/([^/]+)/wait")
    def wait(self, did):
        g = lambda k, d=None: self.qs.get(k, [d])[0]
        after = g("after")
        return self.store.wait_decision(
            did, _types(self.qs, ["decision.resolved", "decision.cancelled", "decision.expired"]),
            int(after) if after not in (None, "") else None,
            min(float(g("timeout", 60)), MAX_WAIT), self.actor, self.scope)

    @route("POST", "/api/decisions/([^/]+)/resume-events")
    def resume_event(self, did):
        return 201, self.store.resume_event(did, self._body(), self.actor, self.scope)

    @route("POST", "/api/resumer/heartbeat")
    def resumer_heartbeat(self):
        return self.store.resumer_heartbeat(self._body(), self.actor, self.scope)

    @route("GET", "/api/resumer")
    def resumer(self):
        return self.store.resumer_status(self.qs.get("machine", [None])[0])

    @route("GET", "/api/decisions/([^/]+)/listeners")
    def listeners(self, did):
        return {"listeners": self.store.listeners(did, self.scope)}

    @route("POST", "/api/decisions/([^/]+)/ack")
    def ack_decision(self, did):
        return self.store.ack_decision(did, self.actor, self.scope)

    # ------------------------------------------------------------------ options

    @route("POST", "/api/decisions/([^/]+)/options")
    def add_options(self, did):
        return 201, self.store.add_options(did, self._body(), self.actor, self.scope)

    @route("PATCH", "/api/decisions/([^/]+)/options")
    def update_options(self, did):
        return self.store.update_options(did, self._body(), self.actor, self.scope)

    @route("PATCH", "/api/decisions/([^/]+)/options/([^/]+)")
    def update_option(self, did, oid):
        return self.store.update_option(did, oid, self._body(), self.actor, self.scope)

    @route("DELETE", "/api/decisions/([^/]+)/options/([^/]+)")
    def remove_option(self, did, oid):
        return self.store.remove_option(did, oid, self.actor, self.scope)

    @route("POST", "/api/decisions/([^/]+)/exclude")
    def exclude(self, did):
        return self.store.set_excluded(did, self._body(), True, self.actor, self.scope)

    @route("POST", "/api/decisions/([^/]+)/include")
    def include(self, did):
        return self.store.set_excluded(did, self._body(), False, self.actor, self.scope)

    # ------------------------------------------------------------------ attribute requests

    @route("POST", "/api/decisions/([^/]+)/attribute-requests")
    def request_attributes(self, did):
        return 201, self.store.request_attributes(did, self._body(), self.actor, self.scope)

    @route("DELETE", "/api/decisions/([^/]+)/attribute-requests/([^/]+)")
    def cancel_attribute_request(self, did, key):
        return self.store.cancel_attribute_request(did, key, self.actor, self.scope)

    # ------------------------------------------------------------------ events

    @route("GET", "/api/decisions/([^/]+)/events")
    def decision_events(self, did):
        return {"events": self.store.events(int(self.qs.get("after", [0])[0]), did,
                                            limit=int(self.qs.get("limit", [1000])[0]), scope=self.scope)}

    @route("GET", "/api/events")
    def events(self):
        g = lambda k, d=None: self.qs.get(k, [d])[0]
        evs = self.store.wait_events(
            int(g("after", 0)), g("decision"), _types(self.qs, None),
            min(float(g("wait", 0)), MAX_WAIT), int(g("limit", 200)), self.scope)
        return {"events": evs, "cursor": evs[-1]["seq"] if evs else max(int(g("after", 0)), 0)}

    @route("GET", "/api/events/verify")
    def verify(self):
        return self.store.verify_chain(self.scope)

    # ------------------------------------------------------------------ webhooks

    @route("GET", "/api/webhooks")
    def list_webhooks(self):
        return {"webhooks": self.store.list_subscriptions(self.qs.get("decision", [None])[0], self.scope)}

    @route("POST", "/api/webhooks")
    def create_webhook(self):
        return 201, self.store.create_subscription(self._body(), self.actor, scope=self.scope)

    @route("POST", "/api/decisions/([^/]+)/webhooks")
    def create_decision_webhook(self, did):
        return 201, self.store.create_subscription(self._body(), self.actor, did, self.scope)

    @route("PATCH", "/api/webhooks/([^/]+)")
    def update_webhook(self, sid):
        return self.store.update_subscription(sid, self._body(), self.actor, self.scope)

    @route("DELETE", "/api/webhooks/([^/]+)")
    def delete_webhook(self, sid):
        return self.store.delete_subscription(sid, self.actor, self.scope)

    @route("GET", "/api/deliveries")
    def deliveries(self):
        g = lambda k, d=None: self.qs.get(k, [d])[0]
        return {"deliveries": self.store.list_deliveries(g("decision"), g("status"), int(g("limit", 100)), self.scope)}

    @route("GET", "/api/deliveries/([^/]+)/attempts")
    def attempts(self, delivery_id):
        return {"attempts": self.store.delivery_attempts(delivery_id, self.scope)}

    @route("POST", "/api/deliveries/([^/]+)/ack")
    def ack_delivery(self, delivery_id):
        return self.store.ack_delivery(delivery_id, self.actor, self.scope)

    @route("POST", "/api/deliveries/([^/]+)/redeliver")
    def redeliver(self, delivery_id):
        return self.store.redeliver(delivery_id, self.actor, self.scope)


def make_server(cfg: Config, store: Store | None = None) -> tuple[ThreadingHTTPServer, Store, Dispatcher]:
    store = store or Store(cfg)
    handler = type("BoundHandler", (Handler,), {"store": store, "cfg": cfg})
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), handler)
    httpd.daemon_threads = True
    dispatcher = Dispatcher(store)
    return httpd, store, dispatcher


def serve(cfg: Config, insecure: bool = False):
    if not is_loopback(cfg.host) and not cfg.token and not insecure:
        raise SystemExit(f"error: refusing to listen on {cfg.host} without a token: anyone on the network could "
                         "read and resolve decisions. Set `token` in the config (or --token / DECISIONS_TOKEN), "
                         "or pass --insecure.")
    httpd, store, dispatcher = make_server(cfg)
    dispatcher.start()
    print(f"decisions: GUI + API on {cfg.public_url}  (db: {cfg.db_path}, auth: {'on' if cfg.token else 'off'})")
    if cfg.host in ("0.0.0.0", "::", "") and not cfg.base_url:
        print(f"  warning: base_url is not set, so decision links use {cfg.public_url}. "
              "Set base_url (DECISIONS_BASE_URL) to the URL other machines use.")
    if not is_loopback(cfg.host):
        print("  other machines: Settings → Machines in the GUI (or `decide token create`) makes each one a token "
              "for one space and the command that installs it")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        dispatcher.stop()
        httpd.server_close()
