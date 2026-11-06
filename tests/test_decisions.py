import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import datetime
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# Don't let a real `decide connect` on this machine leak into tests.
os.environ["DECISIONS_CLIENT_CONFIG"] = os.path.join(tempfile.gettempdir(), "decisions-test-no-client.toml")
os.environ.pop("DECISIONS_TOKEN", None)

from decisions.config import Config  # noqa: E402
from decisions.server import make_server  # noqa: E402
from decisions.webhooks import verify  # noqa: E402


class Receiver:
    """A webhook endpoint that records requests and can be told to fail the first N."""

    def __init__(self, fail_first=0):
        self.received = []
        self.fail_first = fail_first
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.received.append((dict(self.headers), body))
                code = 500 if len(outer.received) <= outer.fail_first else 200
                self.send_response(code)
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/"

    def wait_for(self, n, timeout=10):
        deadline = time.time() + timeout
        while len(self.received) < n and time.time() < deadline:
            time.sleep(0.05)
        return len(self.received) >= n


class DecisionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(port=0, db=os.path.join(self.tmp, "t.db"),
                          webhook_backoff_base=0.1, webhook_backoff_max=0.5, webhook_refire_interval=0.3)
        self.httpd, self.store, self.dispatcher = make_server(self.cfg)
        self.cfg.port = self.httpd.server_port
        self.dispatcher.poll_interval = 0.05
        self.dispatcher.start()
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.cfg.port}"

    def tearDown(self):
        self.dispatcher.stop()
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, method, path, body=None, actor="agent", expect=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "X-Actor": actor})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                status, text = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode()
        if expect is not None:
            self.assertEqual(status, expect, text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def create(self, **extra):
        body = {
            "title": "Which jacket?",
            "context": "For **autumn**.",
            "attributes": {"price": {"type": "number", "unit": "SEK", "better": "lower"}},
            "options": [
                {"label": "Wool coat", "attributes": {"price": 2400, "color": "navy"}},
                {"label": "Rain shell", "attributes": {"price": 900, "color": "green"}},
                {"id": "sku-3", "label": "Parka", "attributes": {"price": 3100}},
            ],
        } | extra
        return self.call("POST", "/api/decisions", body, expect=201)

    # ---------------------------------------------------------------- core

    def test_browser_origins(self):
        def request(method, origin=None, fetch_site=None):
            headers = {"Content-Type": "application/json"}
            if origin is not None:
                headers["Origin"] = origin
            if fetch_site:
                headers["Sec-Fetch-Site"] = fetch_site
            req = urllib.request.Request(
                self.base + "/api/decisions", method=method, headers=headers,
                data=b'{"title":"Browser test"}' if method == "POST" else None)
            try:
                response = urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                response.read()
                self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
                return response.status

        for origin in ("https://unrelated.example", "null", self.base + ".evil.example",
                       "http://[invalid", self.base + "/path", "http://localhost:1"):
            for method in ("GET", "POST", "OPTIONS"):
                with self.subTest(origin=origin, method=method):
                    self.assertEqual(request(method, origin), 403)
        self.assertEqual(self.call("GET", "/api/decisions"), {"decisions": []})
        self.assertEqual(request("POST", fetch_site="cross-site"), 403)
        self.assertEqual(request("POST", self.base, "same-origin"), 201)
        self.assertEqual(request("OPTIONS", self.base), 204)
        self.assertEqual(request("POST"), 201)  # CLI/agent clients

        self.cfg.base_url = "https://decisions.example"
        self.assertEqual(request("POST", "https://decisions.example:443"), 201)
        self.assertEqual(request("POST", self.base), 403)
        self.assertEqual(request("POST", "http://decisions.example"), 403)
        self.cfg.token = "test-admin-token"
        self.assertEqual(request("GET", "https://unrelated.example"), 403)
        self.assertEqual(request("GET", "https://decisions.example"), 401)

    def test_create_get_and_ids(self):
        d = self.create(links=["https://linear.app/x/issue/ABC-1"], metadata={"session_id": "s1"})
        self.assertEqual(d["id"], "D-1")
        self.assertEqual([o["id"] for o in d["options"]], ["1", "2", "sku-3"])
        self.assertEqual(d["max_select"], 1)
        self.assertEqual(self.call("GET", "/api/decisions/d1")["id"], "D-1")
        self.assertEqual(self.call("GET", "/api/decisions/1")["title"], "Which jacket?")
        self.assertEqual(self.create()["id"], "D-2")
        md = self.call("GET", "/api/decisions/D-1/markdown")
        self.assertIn("[D-1]", md)
        self.assertIn("Rain shell", md)

    def test_idempotency_key(self):
        a = self.create(idempotency_key="k1")
        b = self.call("POST", "/api/decisions", {"title": "x", "idempotency_key": "k1"}, expect=200)
        self.assertEqual(a["id"], b["id"])

    def test_resolution_rules_and_changes(self):
        d = self.create()
        self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["1", "2"]}, expect=400)
        self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["nope"]}, expect=400)
        r = self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["2"], "note": "cheap"}, actor="me", expect=200)
        self.assertEqual(r["status"], "resolved")
        self.assertEqual(r["selected_options"][0]["label"], "Rain shell")
        # Changes are never prevented: edit, re-resolve, reopen.
        self.call("PATCH", f"/api/decisions/{d['id']}/options/2", {"attributes": {"price": 950}}, expect=200)
        r = self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["sku-3"]}, actor="me", expect=200)
        self.assertEqual(r["resolution"]["selected"], ["sku-3"])
        self.assertEqual(self.call("POST", f"/api/decisions/{d['id']}/reopen", {})["status"], "open")
        multi = self.create(max_select=None, min_select=2)
        self.call("POST", f"/api/decisions/{multi['id']}/resolve", {"selected": ["1"]}, expect=400)
        self.call("POST", f"/api/decisions/{multi['id']}/resolve", {"selected": ["1", "2", "sku-3"]}, expect=200)

    def test_exclusion_and_attribute_requests(self):
        d = self.create()
        did = d["id"]
        d = self.call("POST", f"/api/decisions/{did}/exclude", {"option_ids": ["sku-3"], "reason": "too warm"}, actor="me")
        self.assertTrue(d["options"][2]["excluded"])
        d = self.call("POST", f"/api/decisions/{did}/attribute-requests",
                      {"key": "waterproof", "description": "Is it waterproof?"}, actor="me", expect=201)
        # Excluded options aren't asked for.
        self.assertEqual(d["pending_attribute_requests"]["waterproof"]["missing"], ["1", "2"])
        d = self.call("PATCH", f"/api/decisions/{did}/options",
                      [{"id": "1", "attributes": {"waterproof": False}}, {"id": "2", "attributes": {"waterproof": True}}])
        self.assertEqual(d["pending_attribute_requests"], {})
        types = [e["type"] for e in self.call("GET", f"/api/decisions/{did}/events")["events"]]
        self.assertEqual(types, ["decision.created", "options.excluded", "attributes.requested", "options.updated"])

    # ---------------------------------------------------------------- log

    def test_event_log_is_immutable_and_chained(self):
        d = self.create()
        self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["1"]})
        self.assertTrue(self.call("GET", "/api/events/verify")["ok"])
        conn = sqlite3.connect(self.cfg.db_path)
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE events SET actor='mallory'")
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM events")
        # Bypass the triggers to simulate tampering: verification must notice.
        conn.execute("DROP TRIGGER events_no_update")
        conn.execute("UPDATE events SET actor='mallory' WHERE seq=1")
        conn.commit()
        self.assertFalse(self.call("GET", "/api/events/verify")["ok"])

    # ---------------------------------------------------------------- webhooks

    def test_webhook_fires_signed_and_retries(self):
        rx = Receiver(fail_first=2)
        d = self.create(webhooks=[{"url": rx.url, "secret": "s3cret"}])
        self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["2"]}, actor="me")
        self.assertTrue(rx.wait_for(3))
        headers, body = rx.received[-1]
        self.assertTrue(verify("s3cret", body, headers["X-Decisions-Signature"]))
        payload = json.loads(body)
        self.assertEqual(payload["event"]["type"], "decision.resolved")
        self.assertEqual(payload["decision"]["resolution"]["selected"], ["2"])
        time.sleep(0.3)
        [dl] = self.call("GET", f"/api/deliveries?decision={d['id']}")["deliveries"]
        self.assertEqual((dl["status"], dl["total_attempts"]), ("delivered", 3))
        self.assertEqual(len(self.call("GET", f"/api/deliveries/{dl['id']}/attempts")["attempts"]), 3)

    def test_refire_until_acked(self):
        rx = Receiver()
        d = self.create(webhooks=[{"url": rx.url, "require_ack": True}])
        self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["1"]})
        self.assertTrue(rx.wait_for(2, timeout=5), "should re-fire while unacked")
        payload = json.loads(rx.received[0][1])
        ack = urllib.request.urlopen(urllib.request.Request(payload["delivery"]["ack_url"], method="POST", data=b""))
        self.assertEqual(json.loads(ack.read())["status"], "acked")
        n = len(rx.received)
        time.sleep(0.8)
        self.assertLessEqual(len(rx.received), n + 1)  # at most one already in flight

    def test_global_webhook_and_event_filter(self):
        rx = Receiver()
        self.call("POST", "/api/webhooks", {"url": rx.url, "events": ["decision.*"]}, expect=201)
        d = self.create()
        self.call("POST", f"/api/decisions/{d['id']}/exclude", {"option_ids": ["1"]})
        self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["2"]})
        self.assertTrue(rx.wait_for(2))
        time.sleep(0.3)
        types = [json.loads(b)["event"]["type"] for _, b in rx.received]
        self.assertEqual(sorted(types), ["decision.created", "decision.resolved"])

    # ---------------------------------------------------------------- waiting

    def test_long_poll_wait(self):
        d = self.create()
        threading.Timer(0.3, lambda: self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["1"]})).start()
        t = time.time()
        res = self.call("GET", f"/api/decisions/{d['id']}/wait?timeout=10")
        self.assertLess(time.time() - t, 5)
        self.assertFalse(res["timed_out"])
        self.assertEqual(res["event"]["type"], "decision.resolved")
        # Already resolved: returns immediately.
        self.assertFalse(self.call("GET", f"/api/decisions/{d['id']}/wait?timeout=10")["timed_out"])
        self.assertTrue(self.call("GET", f"/api/decisions/{self.create()['id']}/wait?timeout=0.2")["timed_out"])

    def test_cli_wait_and_listen(self):
        env = os.environ | {"DECISIONS_URL": self.base, "PYTHONPATH": ROOT}
        d = self.create()
        out = os.path.join(self.tmp, "out.txt")
        listener = subprocess.Popen(
            [sys.executable, "-m", "decisions", "listen", "--port", "0", "--once", "--secret", "k",
             "--exec", f'echo "$DECISION_ID $DECISION_SELECTED_LABELS $DECISION_META_SESSION_ID" > {out}'],
            env=env, stderr=subprocess.PIPE, text=True)
        line = listener.stderr.readline()
        port = int(line.rsplit(":", 1)[1].strip(" /\n"))
        self.call("POST", f"/api/decisions/{d['id']}/webhooks",
                  {"url": f"http://127.0.0.1:{port}/", "secret": "k", "require_ack": True}, expect=201)
        self.call("PATCH", f"/api/decisions/{d['id']}", {"metadata": {"session_id": "abc"}})
        waiter = subprocess.Popen([sys.executable, "-m", "decisions", "wait", d["id"], "--timeout", "20", "-q"], env=env)
        time.sleep(0.5)
        self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["2"]}, actor="me")
        self.assertEqual(waiter.wait(15), 0)
        self.assertEqual(listener.wait(15), 0)
        with open(out) as fh:
            self.assertEqual(fh.read().strip(), f"{d['id']} Rain shell abc")
        time.sleep(0.3)
        [dl] = self.call("GET", f"/api/deliveries?decision={d['id']}")["deliveries"]
        self.assertEqual(dl["status"], "acked")

    def test_late_webhook_is_replayed(self):
        rx = Receiver()
        d = self.create()
        self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["1"]})
        self.call("POST", f"/api/decisions/{d['id']}/webhooks", {"url": rx.url}, expect=201)
        self.assertTrue(rx.wait_for(1))
        payload = json.loads(rx.received[0][1])
        self.assertEqual(payload["event"]["type"], "decision.resolved")
        self.assertTrue(payload["delivery"]["replayed"])

    def test_cli_watch(self):
        env = os.environ | {"DECISIONS_URL": self.base, "PYTHONPATH": ROOT}
        watch = lambda id_, *extra: subprocess.Popen(
            [sys.executable, "-m", "decisions", "watch", id_, *extra],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # Decided before the watch started: replayed, exits at once.
        done = self.create()
        self.call("POST", f"/api/decisions/{done['id']}/resolve", {"selected": ["2"]})
        p = watch(done["id"])
        out, _ = p.communicate(timeout=15)
        self.assertEqual(p.returncode, 0)
        self.assertIn("already decided", out)
        self.assertIn("Rain shell", out)
        # Decided while watching, with --exec; webhook cleaned up afterwards.
        d = self.create()
        flag = os.path.join(self.tmp, "flag")
        p = watch(d["id"], "--exec", f'echo "$DECISION_SELECTED" > {flag}')
        p.stderr.readline()  # "[watch] ... waiting"
        self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["sku-3"]})
        p.communicate(timeout=15)
        self.assertEqual(p.returncode, 0)
        with open(flag) as fh:
            self.assertEqual(fh.read().strip(), "sku-3")
        self.assertEqual(self.call("GET", f"/api/webhooks?decision={d['id']}")["webhooks"], [])
        [dl] = self.call("GET", f"/api/deliveries?decision={d['id']}")["deliveries"]
        self.assertEqual(dl["status"], "acked")

    def test_cli_watch_uses_servers_callback_host(self):
        """A server in a container says how it reaches its own machine; watch registers that, not 127.0.0.1."""
        self.cfg.callback_host = "localhost"  # stands in for host.docker.internal
        env = os.environ | {"DECISIONS_URL": self.base, "PYTHONPATH": ROOT}
        d = self.create()
        p = subprocess.Popen([sys.executable, "-m", "decisions", "watch", d["id"]],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertIn("-> http://localhost:", p.stderr.readline())
        self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["2"]})
        out, _ = p.communicate(timeout=15)
        self.assertEqual(p.returncode, 0)
        self.assertIn("event: decision.resolved", out)

    def test_attribute_meta_marks_estimates(self):
        d = self.create()
        d = self.call("PATCH", f"/api/decisions/{d['id']}/options", [
            {"id": "1", "attributes": {"price": 2300}, "attribute_meta": {"price": {"estimate": True, "source": "memory"}}}])
        self.assertEqual(d["options"][0]["attribute_meta"], {"price": {"estimate": True, "source": "memory"}})
        md = self.call("GET", f"/api/decisions/{d['id']}/markdown")
        self.assertIn("~2300", md)
        self.assertIn("~ = estimate", md)

    def test_listeners_show_long_pollers_and_webhooks(self):
        d = self.create(webhooks=[{"url": "http://127.0.0.1:9/"}])
        t = threading.Thread(target=lambda: self.call("GET", f"/api/decisions/{d['id']}/wait?timeout=3", actor="haiku"))
        t.start()
        time.sleep(0.3)
        ls = self.call("GET", f"/api/decisions/{d['id']}/listeners")["listeners"]
        kinds = {(l["kind"], l.get("actor")) for l in ls}
        self.assertIn(("long-poll", "haiku"), kinds)
        self.assertIn(("webhook", None), kinds)
        self.assertTrue(next(l for l in ls if l["kind"] == "long-poll")["connected"])
        t.join()
        # Between re-polls the waiter is still shown, just not connected.
        poller = next(l for l in self.call("GET", f"/api/decisions/{d['id']}/listeners")["listeners"] if l["kind"] == "long-poll")
        self.assertFalse(poller["connected"])

    def test_resolve_can_silence_listeners(self):
        mine, everyone = Receiver(), Receiver()
        self.call("POST", "/api/webhooks", {"url": everyone.url}, expect=201)
        d = self.create(webhooks=[{"url": mine.url}],
                        resume={"session_id": "s1", "agent": "claude", "machine": "m", "cwd": "/"})
        hook = d["webhooks"][0]["id"]
        glob = next(l["id"] for l in self.call("GET", f"/api/decisions/{d['id']}/listeners")["listeners"]
                    if l["scope"] == "global")
        self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["1"]}, actor="me")
        self.assertTrue(mine.wait_for(1) and everyone.wait_for(1))
        self.call("POST", f"/api/decisions/{d['id']}/resume-events", {"status": "manual"}, actor="me")
        # Global webhooks always hear; unknown listeners are an error.
        self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["2"], "silence": [glob]}, expect=400)
        self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["2"], "silence": ["wh_nope"]}, expect=400)

        cursor = self.call("GET", "/api/events?limit=1000")["cursor"]
        got = {}
        t = threading.Thread(target=lambda: got.update(self.call(
            "GET", f"/api/decisions/{d['id']}/wait?timeout=5&after={cursor}", actor="haiku")))
        t.start()
        time.sleep(0.3)
        # Fixing a typo: only the global webhook hears; the resumer's state stays as it was.
        silenced = self.call("POST", f"/api/decisions/{d['id']}/resolve",
                             {"selected": ["1"], "note": "fixed typo", "silence": [hook, "resumer", "poll:haiku"]})
        self.assertTrue(everyone.wait_for(2))
        time.sleep(0.3)
        self.assertEqual(len(mine.received), 1)
        self.assertEqual(silenced["resume_state"]["status"], "manual")
        self.assertTrue(t.is_alive(), "a silenced long-poller keeps waiting")
        ev = self.call("GET", f"/api/decisions/{d['id']}/events?types=decision.resolved")["events"][-1]
        self.assertEqual(ev["data"]["silenced"], [hook, "resumer", "poll:haiku"])
        # The next resolution reaches everyone again.
        again = self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["2"]}, actor="me")
        t.join()
        self.assertEqual(got["event"]["seq"], ev["seq"] + 1)
        self.assertTrue(mine.wait_for(2))
        self.assertEqual(again["resume_state"]["status"], "pending")

    def test_resumer_resumes_catches_up_and_respects_manual(self):
        state = os.path.join(self.tmp, "state")
        out = os.path.join(self.tmp, "resumed.txt")
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        env = os.environ | {"DECISIONS_URL": self.base, "PYTHONPATH": ROOT, "DECISIONS_STATE_DIR": state,
                            "CLAUDE_CODE_SESSION_ID": "sess-123"}
        decide = lambda *a, **kw: subprocess.run([sys.executable, "-m", "decisions", *a], env=env, cwd=work,
                                                  capture_output=True, text=True, **kw)
        # `decide create` inside Claude Code records the session automatically.
        did = decide("create", "--title", "Pick one", "-o", "A", "-o", "B", "-q").stdout.strip()
        d = self.call("GET", f"/api/decisions/{did}")
        self.assertEqual(d["resume"]["session_id"], "sess-123")
        self.assertEqual(os.path.realpath(d["resume"]["cwd"]), os.path.realpath(work))
        self.assertEqual(d["resume_state"]["status"], "armed")

        start = lambda: subprocess.Popen(
            [sys.executable, "-m", "decisions", "resumer", "run", "--command",
             f'echo "$RESUME_SESSION_ID|$PWD|$RESUME_PROMPT" >> {out}'],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        wait_status = lambda i, want: next((True for _ in range(100) if self.call(
            "GET", f"/api/decisions/{i}")["resume_state"]["status"] == want or time.sleep(0.1)), False)

        resumer = start()
        self.assertEqual(resumer.stdout.readline()[:10], "[resumer] ")
        time.sleep(0.3)
        self.assertTrue(self.call("GET", "/api/resumer")["online"])
        self.call("POST", f"/api/decisions/{did}/resolve", {"selected": ["2"], "note": "go"}, actor="me")
        self.assertTrue(wait_status(did, "finished"))
        line = open(out).read().strip()
        self.assertTrue(line.startswith("sess-123|"))
        self.assertIn("B. Note: go.", line)
        resumer.terminate(); resumer.wait()

        # Resolved while the resumer was down: picked up when it comes back.
        d2 = decide("create", "--title", "Second", "-o", "X", "-q").stdout.strip()
        d3 = decide("create", "--title", "Third", "-o", "Y", "-q").stdout.strip()
        self.call("POST", f"/api/decisions/{d2}/resolve", {"selected": ["1"]}, actor="me")
        self.call("POST", f"/api/decisions/{d3}/resolve", {"selected": ["1"]}, actor="me")
        self.assertEqual(self.call("GET", f"/api/decisions/{d2}")["resume_state"]["status"], "pending")
        # ...unless you already resumed it yourself.
        self.call("POST", f"/api/decisions/{d3}/resume-events", {"status": "manual"}, actor="me")
        resumer = start()
        self.assertTrue(wait_status(d2, "finished"))
        time.sleep(0.5)
        resumer.terminate(); resumer.wait()
        self.assertEqual(len(open(out).read().strip().splitlines()), 2)
        self.assertEqual(self.call("GET", f"/api/decisions/{d3}")["resume_state"]["status"], "manual")

    def test_resume_targets_for_any_agent(self):
        state = os.path.join(self.tmp, "state")
        out = os.path.join(self.tmp, "resumed.txt")
        base = {k: v for k, v in os.environ.items()
                if k not in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "DECISIONS_SESSION_ID",
                             "DECISIONS_AGENT", "DECISIONS_ACTOR")}
        base |= {"DECISIONS_URL": self.base, "PYTHONPATH": ROOT, "DECISIONS_STATE_DIR": state}
        decide = lambda env, *a: subprocess.run([sys.executable, "-m", "decisions", *a], env=base | env,
                                                cwd=self.tmp, capture_output=True, text=True).stdout.strip()
        get = lambda i: self.call("GET", f"/api/decisions/{i}")
        # Codex is detected from its env var, and names the actor.
        codex = decide({"CODEX_THREAD_ID": "thr-1"}, "create", "--title", "C", "-o", "A", "-q")
        self.assertEqual((get(codex)["resume"]["agent"], get(codex)["resume"]["session_id"]), ("codex", "thr-1"))
        self.assertEqual(self.call("GET", f"/api/decisions/{codex}/events")["events"][0]["actor"], "codex")
        self.assertIn("codex resume thr-1", get(codex)["resume_command"])
        # Any other agent: generic env vars or flags.
        other = decide({"DECISIONS_AGENT": "gemini", "DECISIONS_SESSION_ID": "g-1"}, "create", "--title", "G",
                       "-o", "A", "-q")
        self.assertEqual(get(other)["resume"]["agent"], "gemini")
        self.assertIsNone(get(other)["resume_command"])
        flagged = decide({}, "create", "--title", "F", "-o", "A", "-q", "--agent", "aider", "--session", "a-1")
        self.assertEqual((get(flagged)["resume"]["agent"], get(flagged)["resume"]["session_id"]), ("aider", "a-1"))
        self.assertIsNone(get(decide({}, "create", "--title", "N", "-o", "A", "-q"))["resume"])

        # Each agent gets its own command; agents without one are marked failed, not silently skipped.
        resumer = subprocess.Popen(
            [sys.executable, "-m", "decisions", "resumer", "run",
             "--agent-command", f'codex=echo "codex $RESUME_SESSION_ID $RESUME_AGENT" >> {out}',
             "--agent-command", f'gemini=echo "gemini $RESUME_SESSION_ID" >> {out}'],
            env=base, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            resumer.stdout.readline()
            time.sleep(0.3)
            for did in (codex, other, flagged):
                self.call("POST", f"/api/decisions/{did}/resolve", {"selected": ["1"]}, actor="me")
            for _ in range(100):
                if all(get(i)["resume_state"]["status"] in ("finished", "failed") for i in (codex, other, flagged)):
                    break
                time.sleep(0.1)
        finally:
            resumer.terminate(); resumer.wait()
        self.assertEqual(sorted(open(out).read().strip().splitlines()), ["codex thr-1 codex", "gemini g-1"])
        self.assertEqual(get(flagged)["resume_state"]["status"], "failed")

    def test_resumer_only_resumes_its_own_machine(self):
        out = os.path.join(self.tmp, "resumed.txt")
        env = os.environ | {"DECISIONS_URL": self.base, "PYTHONPATH": ROOT, "DECISIONS_MACHINE": "laptop",
                            "DECISIONS_STATE_DIR": os.path.join(self.tmp, "state")}
        mk = lambda machine: self.call("POST", "/api/decisions", {"title": machine, "options": ["A"], "resume": {
            "session_id": f"s-{machine}", "cwd": self.tmp, "agent": "claude-code", "machine": machine}}, expect=201)["id"]
        mine, theirs = mk("laptop"), mk("desktop")
        resumer = subprocess.Popen([sys.executable, "-m", "decisions", "resumer", "run", "--command",
                                    f'echo "$RESUME_SESSION_ID" >> {out}'],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            resumer.stdout.readline()
            time.sleep(0.3)
            self.assertTrue(self.call("GET", "/api/resumer?machine=laptop")["online"])
            self.assertFalse(self.call("GET", "/api/resumer?machine=desktop")["online"])
            for did in (theirs, mine):
                self.call("POST", f"/api/decisions/{did}/resolve", {"selected": ["1"]}, actor="me")
            for _ in range(100):
                if self.call("GET", f"/api/decisions/{mine}")["resume_state"]["status"] == "finished":
                    break
                time.sleep(0.1)
            time.sleep(0.3)
        finally:
            resumer.terminate(); resumer.wait()
        self.assertEqual(open(out).read().split(), ["s-laptop"])
        self.assertEqual(self.call("GET", f"/api/decisions/{theirs}")["resume_state"]["status"], "pending")
        self.assertEqual([r["machine"] for r in self.call("GET", "/api/resumer")["resumers"]], ["laptop"])

    # ---------------------------------------------------------------- deadlines

    def past(self, seconds=1):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))

    def test_decisions_get_a_default_deadline(self):
        d = self.create()
        due = time.mktime(time.strptime(d["due_at"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        self.assertAlmostEqual(due - time.time(), 4 * 86400, delta=60)
        own = self.create(due_at="2030-01-02T03:04:05+02:00")
        self.assertEqual(own["due_at"], "2030-01-02T01:04:05.000Z")
        self.call("POST", "/api/decisions", {"title": "x", "due_at": "friday"}, expect=400)
        self.call("PATCH", f"/api/decisions/{d['id']}", {"due_at": None}, expect=400)
        self.assertIn("Expires:", self.call("GET", f"/api/decisions/{d['id']}/markdown"))

    def test_expiry_is_distinct_from_cancelling_and_notifies(self):
        rx = Receiver()
        self.call("POST", "/api/webhooks", {"url": rx.url}, expect=201)  # default events
        overdue, cancelled, later = self.create(), self.create(), self.create()
        self.call("POST", f"/api/decisions/{cancelled['id']}/cancel", {"note": "moot"}, actor="me")
        res = {}
        waiter = threading.Thread(target=lambda: res.update(
            self.call("GET", f"/api/decisions/{overdue['id']}/wait?timeout=10")))
        waiter.start()
        time.sleep(0.2)
        self.call("PATCH", f"/api/decisions/{overdue['id']}", {"due_at": self.past()})
        self.assertEqual(self.store.expire_due(), [overdue["id"]])
        waiter.join(10)
        self.assertEqual(res["event"]["type"], "decision.expired")
        self.assertEqual(res["event"]["actor"], "deadline")
        statuses = {d["id"]: d["status"] for d in self.call("GET", "/api/decisions?status=all")["decisions"]}
        self.assertEqual([statuses[x["id"]] for x in (overdue, cancelled, later)], ["expired", "cancelled", "open"])
        self.assertEqual([d["id"] for d in self.call("GET", "/api/decisions?status=expired")["decisions"]],
                         [overdue["id"]])
        self.assertTrue(rx.wait_for(2))
        types = sorted(json.loads(b)["event"]["type"] for _, b in rx.received)
        self.assertEqual(types, ["decision.cancelled", "decision.expired"])
        # Only open decisions expire, and only once.
        self.assertEqual(self.store.expire_due(), [])
        # A webhook registered later still hears about it.
        rx2 = Receiver()
        self.call("POST", f"/api/decisions/{overdue['id']}/webhooks", {"url": rx2.url}, expect=201)
        self.assertTrue(rx2.wait_for(1))
        self.assertEqual(json.loads(rx2.received[0][1])["event"]["type"], "decision.expired")

    def test_dispatcher_expires_in_the_background_and_reopen_restarts_the_deadline(self):
        self.dispatcher.expire_interval = 0.05
        d = self.create(due_at=self.past())
        deadline = time.time() + 5
        while self.call("GET", f"/api/decisions/{d['id']}")["status"] != "expired" and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.call("GET", f"/api/decisions/{d['id']}")["status"], "expired")
        r = self.call("POST", f"/api/decisions/{d['id']}/reopen", {}, actor="me")
        self.assertEqual(r["status"], "open")
        self.assertGreater(r["due_at"], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3 * 86400)))
        time.sleep(0.3)
        self.assertEqual(self.call("GET", f"/api/decisions/{d['id']}")["status"], "open")

    def test_resumer_resumes_expired_decisions(self):
        state = os.path.join(self.tmp, "state")
        out = os.path.join(self.tmp, "resumed.txt")
        env = os.environ | {"DECISIONS_URL": self.base, "PYTHONPATH": ROOT, "DECISIONS_STATE_DIR": state,
                            "CLAUDE_CODE_SESSION_ID": "sess-x"}
        run = lambda *a: subprocess.run([sys.executable, "-m", "decisions", *a], env=env, cwd=self.tmp,
                                        capture_output=True, text=True)
        did = run("create", "--title", "Nobody cares", "-o", "A", "--due", "2h", "-q").stdout.strip()
        self.assertIn("expires in 1h 5", run("get", did).stdout)
        resumer = subprocess.Popen(
            [sys.executable, "-m", "decisions", "resumer", "run", "--command",
             f'echo "$DECISION_EVENT|$RESUME_PROMPT" >> {out}'],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            resumer.stdout.readline()
            self.call("PATCH", f"/api/decisions/{did}", {"due_at": self.past()})
            self.store.expire_due()
            deadline = time.time() + 10
            while self.call("GET", f"/api/decisions/{did}")["resume_state"]["status"] != "finished" \
                    and time.time() < deadline:
                time.sleep(0.1)
            st = self.call("GET", f"/api/decisions/{did}")["resume_state"]
            self.assertEqual((st["status"], st["trigger"]), ("finished", "decision.expired"))
        finally:
            resumer.terminate(); resumer.wait()
        line = open(out).read()
        self.assertTrue(line.startswith("decision.expired|[decisions] "))
        self.assertIn("expired", line)

    # ---------------------------------------------------------------- dependencies

    def caching(self):
        return self.create(title="Which cache?", options=[{"id": "redis", "label": "Redis"},
                                                          {"id": "http", "label": "HTTP cache"}])

    def types(self, did):
        return [(e["type"], e["actor"]) for e in self.call("GET", f"/api/decisions/{did}/events")["events"]]

    def test_a_dependent_decision_is_locked_until_its_upstream_is_decided(self):
        up = self.caching()
        client = self.create(title="Which Redis client?",
                             depends_on=[{"decision": up["id"], "when": ["redis"], "note": "only with Redis"}])
        later = self.create(title="Cache TTL?", depends_on=[up["id"]], due_at=self.past())
        self.assertEqual(client["waiting_on"], [up["id"]])
        self.assertIsNone(client["due_at"])  # the clock starts once it can be decided
        dep = client["depends_on"][0]
        self.assertEqual((dep["state"], dep["when"], dep["when_labels"], dep["note"]),
                         ("waiting", ["redis"], ["Redis"], "only with Redis"))
        affects = self.call("GET", f"/api/decisions/{up['id']}")["affects"]
        self.assertEqual([(a["decision"], a["when"]) for a in affects], [(client["id"], ["redis"]), (later["id"], None)])
        listed = {d["id"]: d for d in self.call("GET", "/api/decisions")["decisions"]}
        self.assertEqual(listed[client["id"]]["waiting_on"], [up["id"]])
        self.assertIn("waits on", self.call("POST", f"/api/decisions/{client['id']}/resolve", {"selected": ["1"]},
                                            expect=409)["error"])
        # Waiting decisions don't expire, even past their own due_at.
        self.assertEqual(self.store.expire_due(), [])
        self.assertIn(f"Depends on:** {up['id']}", self.call("GET", f"/api/decisions/{client['id']}/markdown"))

        self.call("POST", f"/api/decisions/{up['id']}/resolve", {"selected": ["redis"]}, actor="me", expect=200)
        for d in (client, later):
            d = self.call("GET", f"/api/decisions/{d['id']}")
            self.assertEqual((d["status"], d["waiting_on"]), ("open", []))
            self.assertGreater(d["due_at"], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3 * 86400)))
            self.assertEqual(self.types(d["id"])[-1], ("decision.unblocked", "dependency"))
        self.call("POST", f"/api/decisions/{client['id']}/resolve", {"selected": ["1"]}, expect=200)

    def test_a_conditional_dependency_cancels_down_the_chain(self):
        rx = Receiver()
        self.call("POST", "/api/webhooks", {"url": rx.url, "events": ["decision.cancelled"]}, expect=201)
        up = self.caching()
        client = self.create(title="Which Redis client?", depends_on=[{"decision": up["id"], "when": "redis"}])
        pool = self.create(title="Pool size?", depends_on=[{"decision": client["id"], "when": ["1"]}])
        docs = self.create(title="Document it?", depends_on=[client["id"]])
        self.assertEqual(pool["waiting_on"], [client["id"]])
        self.call("POST", f"/api/decisions/{up['id']}/resolve", {"selected": ["http"]}, actor="me", expect=200)
        status = lambda d: self.call("GET", f"/api/decisions/{d['id']}")["status"]
        self.assertEqual([status(d) for d in (client, pool, docs)], ["cancelled", "cancelled", "open"])
        ev = self.call("GET", f"/api/decisions/{client['id']}/events")["events"][-1]
        self.assertEqual((ev["type"], ev["actor"]), ("decision.cancelled", "dependency"))
        self.assertIn(f"{up['id']} was resolved with http", ev["data"]["note"])
        # Depending on something cancelled is settled for a plain dependency: it can be decided now.
        self.assertEqual(self.types(docs["id"])[-1], ("decision.unblocked", "dependency"))
        self.assertTrue(rx.wait_for(2))
        # It can't come back while the premise is false, nor can a new one be built on it.
        self.assertIn("only applies if", self.call("POST", f"/api/decisions/{client['id']}/reopen", {},
                                                   expect=409)["error"])
        self.call("POST", "/api/decisions", {"title": "x", "depends_on": [{"decision": up["id"], "when": ["redis"]}]},
                  expect=409)
        # Change the upstream, and it can.
        self.call("POST", f"/api/decisions/{up['id']}/resolve", {"selected": ["redis"]}, actor="me", expect=200)
        self.assertEqual(self.call("POST", f"/api/decisions/{client['id']}/reopen", {}, expect=200)["waiting_on"], [])

    def test_changing_an_upstream_flags_what_was_decided_on_it(self):
        up = self.caching()
        down = self.create(title="Cache TTL?", depends_on=[up["id"]])
        self.call("POST", f"/api/decisions/{up['id']}/resolve", {"selected": ["redis"]}, actor="me", expect=200)
        down = self.call("POST", f"/api/decisions/{down['id']}/resolve", {"selected": ["1"]}, actor="me", expect=200)
        self.assertEqual(down["outdated_by"], [])
        self.call("POST", f"/api/decisions/{up['id']}/reopen", {}, actor="me", expect=200)
        down = self.call("GET", f"/api/decisions/{down['id']}")
        # Left as decided, but flagged, and its listeners hear why.
        self.assertEqual((down["status"], down["outdated_by"]), ("resolved", [up["id"]]))
        self.assertEqual(self.types(down["id"])[-1], ("dependency.changed", "dependency"))
        self.assertEqual(self.call("GET", "/api/decisions?status=resolved")["decisions"][0]["outdated_by"], [up["id"]])
        self.call("POST", f"/api/decisions/{down['id']}/resolve", {"selected": ["1"]}, expect=409)
        self.call("POST", f"/api/decisions/{up['id']}/resolve", {"selected": ["http"]}, actor="me", expect=200)
        self.assertEqual(self.call("GET", f"/api/decisions/{down['id']}")["outdated_by"], [up["id"]])
        # Deciding it again (even the same way) confirms it against the new outcome.
        down = self.call("POST", f"/api/decisions/{down['id']}/resolve", {"selected": ["1"]}, actor="me", expect=200)
        self.assertEqual(down["outdated_by"], [])

    def test_dependencies_are_validated_and_can_be_dropped(self):
        up, other = self.caching(), self.create()
        self.call("POST", "/api/decisions", {"title": "x", "depends_on": ["D-999"]}, expect=404)
        self.call("POST", "/api/decisions", {"title": "x", "depends_on": [{"decision": up["id"], "when": ["nope"]}]},
                  expect=400)
        self.call("POST", "/api/decisions", {"title": "x", "depends_on": "D-1"}, expect=400)
        down = self.create(depends_on=[up["id"]])
        self.call("PATCH", f"/api/decisions/{down['id']}", {"depends_on": [down["id"]]}, expect=400)
        self.assertIn("cycle", self.call("PATCH", f"/api/decisions/{up['id']}", {"depends_on": [down["id"]]},
                                         expect=400)["error"])
        # Adding one locks it; dropping the last one it waits on unlocks it and starts its deadline.
        d = self.call("PATCH", f"/api/decisions/{down['id']}", {"depends_on": [up["id"], other["id"]]}, expect=200)
        self.assertEqual(d["waiting_on"], [up["id"], other["id"]])
        d = self.call("PATCH", f"/api/decisions/{down['id']}", {"depends_on": [other["id"]]}, expect=200)
        self.assertEqual(self.types(d["id"])[-1][0], "decision.updated")
        d = self.call("PATCH", f"/api/decisions/{down['id']}", {"depends_on": []}, actor="me", expect=200)
        self.assertEqual((d["waiting_on"], self.types(d["id"])[-1]), ([], ("decision.unblocked", "dependency")))
        self.assertIsNotNone(d["due_at"])

    def test_cli_depends_on(self):
        env = os.environ | {"DECISIONS_URL": self.base, "PYTHONPATH": ROOT}
        run = lambda *a: subprocess.run([sys.executable, "-m", "decisions", *a], env=env, cwd=self.tmp,
                                        capture_output=True, text=True)
        up = self.caching()
        did = run("create", "--title", "Client?", "-o", "a", "--depends-on", f"{up['id']}=redis", "-q",
                  "--no-resume").stdout.strip()
        self.assertEqual(self.call("GET", f"/api/decisions/{did}")["depends_on"][0]["when"], ["redis"])
        shown = run("get", did).stdout
        self.assertIn(f"depends on {up['id']} [open] Which cache?, only applies if it picks redis", shown)
        self.assertIn(f"waiting on {up['id']}", shown)
        self.assertIn(f"[waiting on {up['id']}]", run("list").stdout)
        self.assertIn(f"affects {did}", run("get", up["id"]).stdout)


    # ---------------------------------------------------------------- goals and the map

    def goal(self, title, serves=(), **extra):
        return self.call("POST", "/api/goals", {"title": title, "serves": list(serves)} | extra, expect=201)

    def test_goals_form_a_hierarchy_that_decisions_work_toward(self):
        vision = self.goal("Be the default tool for EU utilities")
        onboard = self.goal("Onboard a customer in a day", [vision["id"]], description="Days, not months.")
        self.assertEqual((vision["id"], onboard["id"]), ("G-1", "G-2"))
        self.assertEqual(onboard["serves"], [{"goal": "G-1", "title": vision["title"], "status": "active"}])
        d = self.create(goals=["g2"])
        self.assertEqual([g["goal"] for g in d["goals"]], ["G-2"])
        self.assertEqual(self.call("GET", "/api/decisions")["decisions"][0]["goals"], ["G-2"])
        self.assertIn("**Toward:** G-2: Onboard a customer in a day", self.call("GET", f"/api/decisions/{d['id']}/markdown"))
        g = self.call("GET", "/api/goals/G-1")
        self.assertEqual([x["goal"] for x in g["subgoals"]], ["G-2"])
        g = self.call("GET", "/api/goals/G-2")
        self.assertEqual([(x["decision"], x["status"]) for x in g["decisions"]], [(d["id"], "open")])
        listed = {x["id"]: x for x in self.call("GET", "/api/goals")["goals"]}
        self.assertEqual((listed["G-2"]["serves"], listed["G-2"]["decisions"]["open"]), (["G-1"], 1))
        # Unlinking is an edit of the decision, and logged as one.
        d = self.call("PATCH", f"/api/decisions/{d['id']}", {"goals": []}, expect=200)
        self.assertEqual(d["goals"], [])
        self.assertEqual(self.types(d["id"])[-1], ("decision.updated", "agent"))

    def test_goals_are_validated(self):
        a = self.goal("A")
        b = self.goal("B", [a["id"]])
        c = self.goal("C", [b["id"]])
        self.call("POST", "/api/goals", {"title": ""}, expect=400)
        self.call("POST", "/api/goals", {"title": "x", "serves": ["G-99"]}, expect=404)
        self.call("POST", "/api/goals", {"title": "x", "status": "done"}, expect=400)
        self.call("PATCH", f"/api/goals/{a['id']}", {"serves": [a["id"]]}, expect=400)
        self.assertIn("cycle", self.call("PATCH", f"/api/goals/{a['id']}", {"serves": [c["id"]]}, expect=400)["error"])
        self.call("POST", "/api/decisions", {"title": "x", "goals": ["G-99"]}, expect=404)
        self.call("POST", "/api/decisions", {"title": "x", "goals": "nope"}, expect=404)
        self.call("GET", "/api/goals/G-99", expect=404)

    def test_goal_status_and_events(self):
        rx = Receiver()
        self.call("POST", "/api/webhooks", {"url": rx.url, "events": ["goal.*"]}, expect=201)
        g = self.goal("One shared platform")
        g = self.call("PATCH", f"/api/goals/{g['id']}", {"title": "Run everyone on one platform", "status": "abandoned",
                                                          "note": "Data residency"}, actor="me", expect=200)
        self.assertEqual((g["status"], g["status_note"]), ("abandoned", "Data residency"))
        self.assertIsNotNone(g["closed_at"])
        evs = self.call("GET", f"/api/goals/{g['id']}/events")["events"]
        self.assertEqual([(e["type"], e["goal_id"]) for e in evs],
                         [("goal.created", g["id"]), ("goal.updated", g["id"]), ("goal.abandoned", g["id"])])
        self.assertEqual(evs[-1]["data"], {"note": "Data residency", "previous_status": "active"})
        g = self.call("PATCH", f"/api/goals/{g['id']}", {"status": "active"}, expect=200)
        self.assertIsNone(g["closed_at"])
        self.assertTrue(rx.wait_for(4))
        payloads = {p["event"]["type"]: p for p in (json.loads(b) for _, b in rx.received)}
        self.assertEqual(set(payloads), {"goal.created", "goal.updated", "goal.abandoned", "goal.reactivated"})
        self.assertEqual((payloads["goal.abandoned"]["goal"]["id"], payloads["goal.abandoned"]["decision"]), (g["id"], None))
        self.assertTrue(self.call("GET", "/api/events/verify")["ok"])

    def tenancy(self):
        """A pivot: multi-tenant, decided on top of, then changed to per-customer, and its goal abandoned."""
        vision = self.goal("Grid planning for every utility")
        shared = self.goal("One shared platform", [vision["id"]])
        tenancy = self.create(title="Tenancy model?", goals=[shared["id"]],
                              options=[{"id": "mt", "label": "Multi-tenant"}, {"id": "pc", "label": "Per-customer"}])
        isolation = self.create(title="Data isolation?", depends_on=[tenancy["id"]],
                                options=[{"id": "rls", "label": "Row-level security"}, {"id": "db", "label": "DB per tenant"}])
        loose = self.create(title="Unrelated?", options=["a", "b"])
        resolve = lambda d, pick, note="": self.call("POST", f"/api/decisions/{d['id']}/resolve",
                                                     {"selected": [pick], "note": note}, actor="me", expect=200)
        resolve(tenancy, "mt")
        resolve(isolation, "rls")
        self.call("POST", f"/api/decisions/{tenancy['id']}/reopen", {}, actor="me", expect=200)
        resolve(tenancy, "pc", "Data residency")
        return vision, shared, tenancy, isolation, loose

    def test_the_map_traces_what_led_where(self):
        vision, shared, tenancy, isolation, loose = self.tenancy()
        g = self.call("GET", "/api/graph")
        nodes = {n["id"]: n for n in g["nodes"]}
        # Decisions linked to a goal show up, and so does what they depend on; unrelated ones don't.
        self.assertEqual(set(nodes), {vision["id"], shared["id"], tenancy["id"], isolation["id"]})
        self.assertEqual((nodes[tenancy["id"]]["outcome"], nodes[tenancy["id"]]["changed"]), (["Per-customer"], 1))
        self.assertIn({"from": shared["id"], "to": vision["id"], "kind": "serves"}, g["edges"])
        self.assertIn({"from": tenancy["id"], "to": shared["id"], "kind": "toward"}, g["edges"])
        self.assertIn({"from": isolation["id"], "to": tenancy["id"], "kind": "depends_on", "when": None}, g["edges"])
        self.assertIn(loose["id"], {n["id"] for n in self.call("GET", "/api/graph?all=1")["nodes"]})
        # Focused on a decision: everything that led to it, and what it led to.
        focus = self.call("GET", f"/api/graph?focus={isolation['id']}")
        self.assertEqual({n["id"] for n in focus["nodes"]}, {vision["id"], shared["id"], tenancy["id"], isolation["id"]})
        focus = self.call("GET", f"/api/graph?focus={shared['id']}")
        self.assertEqual({n["id"] for n in focus["nodes"]}, {vision["id"], shared["id"], tenancy["id"], isolation["id"]})
        self.call("GET", "/api/graph?focus=D-99", expect=404)

    def test_insights_find_dead_ends_and_reversals(self):
        vision, shared, tenancy, isolation, loose = self.tenancy()
        self.call("PATCH", f"/api/goals/{shared['id']}", {"status": "abandoned", "note": "Residency"}, expect=200)
        for _ in range(2):
            d = self.create(title="Where to run it?", options=[{"id": "k8s", "label": "Kubernetes"}, {"id": "fly", "label": "Fly"}])
            self.call("POST", f"/api/decisions/{d['id']}/exclude", {"option_ids": ["k8s"], "reason": "Ops"}, expect=200)
        i = self.call("GET", "/api/insights")
        dead = {x["label"]: x for x in i["dead_ends"]}
        self.assertEqual((dead["Multi-tenant"]["picked"], dead["Multi-tenant"]["reversed"]), (1, 1))
        # Still picked, but for a goal that was abandoned.
        self.assertEqual(dead["Per-customer"]["abandoned"], 1)
        self.assertEqual((dead["Kubernetes"]["ruled_out"], dead["Kubernetes"]["dead_ends"]), (2, 0))
        self.assertNotIn("Fly", dead)
        [turn] = i["reversals"]
        self.assertEqual((turn["decision"], turn["from"], turn["to"], turn["note"], turn["invalidated"], turn["goals"]),
                         (tenancy["id"], ["Multi-tenant"], ["Per-customer"], "Data residency", [isolation["id"]],
                          [shared["id"]]))
        [gone] = i["abandoned_goals"]
        self.assertEqual((gone["goal"], gone["decisions"], gone["note"]), (shared["id"], 1, "Residency"))
        # Withdrawing a decision that was made counts as a reversal too.
        self.call("POST", f"/api/decisions/{isolation['id']}/reopen", {}, expect=200)
        self.call("POST", f"/api/decisions/{isolation['id']}/cancel", {"note": "moot"}, expect=200)
        withdrawn = [x for x in self.call("GET", "/api/insights")["reversals"] if x["decision"] == isolation["id"]]
        self.assertEqual((withdrawn[0]["withdrawn"], withdrawn[0]["upstream"]), (True, [tenancy["id"]]))

    def test_the_map_filters_on_tags(self):
        vision = self.goal("Grid planning for every utility", tags=["business"])
        sales = self.goal("Win three utilities this year", [vision["id"]], tags=["business"])
        tech = self.goal("Deploy in a day", [vision["id"]], tags=["tech"])
        infra = self.create(title="Where to run it?", goals=[tech["id"]], options=["Fly", "ECS"])
        tagged = self.create(title="Logging?", tags=["tech"], goals=[sales["id"]], options=["a", "b"])
        pricing = self.create(title="Pricing model?", goals=[sales["id"]], options=["Seats", "Usage"])
        g = self.call("GET", "/api/graph?tags=tech")
        nodes = {n["id"]: n["context"] for n in g["nodes"]}
        # Decisions have their goals' tags; what the matches lead up to stays, as context.
        self.assertEqual(nodes, {tech["id"]: False, infra["id"]: False, tagged["id"]: False,
                                 vision["id"]: True, sales["id"]: True})
        self.assertNotIn(pricing["id"], nodes)
        self.assertEqual(g["tags"], ["tech"])
        both = {n["id"] for n in self.call("GET", "/api/graph?tag=tech&tag=business")["nodes"]}
        self.assertIn(pricing["id"], both)
        self.assertNotIn("context", self.call("GET", "/api/graph")["nodes"][0])
        self.assertEqual([x["id"] for x in self.call("GET", "/api/goals?tag=business")["goals"]], [vision["id"], sales["id"]])
        # Insights only count what has the tag.
        self.call("POST", f"/api/decisions/{pricing['id']}/resolve", {"selected": ["1"]}, expect=200)
        self.call("POST", f"/api/decisions/{pricing['id']}/resolve", {"selected": ["2"]}, expect=200)
        self.assertEqual(self.call("GET", "/api/insights?tags=tech")["reversals"], [])
        self.assertEqual(len(self.call("GET", "/api/insights?tags=business")["reversals"]), 1)

    def test_cli_goals_map_and_insights(self):
        env = os.environ | {"DECISIONS_URL": self.base, "PYTHONPATH": ROOT}
        run = lambda *a: subprocess.run([sys.executable, "-m", "decisions", *a], env=env, cwd=self.tmp,
                                        capture_output=True, text=True)
        vision = run("goal", "create", "Vision", "-q").stdout.strip()
        sub = run("goal", "create", "Subgoal", "--serves", vision, "-q").stdout.strip()
        did = run("create", "--title", "Which?", "-o", "a", "-o", "b", "--goal", sub, "-q", "--no-resume").stdout.strip()
        self.assertEqual(self.call("GET", f"/api/decisions/{did}")["goals"][0]["goal"], sub)
        self.assertIn(f"toward {sub} [active] Subgoal", run("get", did).stdout)
        tree = run("goal", "list").stdout.splitlines()
        self.assertTrue(tree[0].strip().startswith(f"{vision}  active"), tree)
        self.assertTrue(tree[1].startswith("    ") and "Subgoal  (1 decision, 1 open)" in tree[1], tree)
        self.assertIn(f"decision {did} [open] Which?", run("goal", "get", sub).stdout)
        self.assertIn(f"toward {sub}", run("map", "--focus", did).stdout)
        run("goal", "set", sub, "--tags", "tech")
        self.assertIn(did, run("map", "--tag", "tech").stdout)
        self.assertNotIn(did, run("map", "--tag", "business").stdout)
        out = run("goal", "set", sub, "--status", "abandoned", "--note", "nope")
        self.assertEqual(json.loads(out.stdout)["status"], "abandoned", out.stderr)
        self.assertIn("Abandoned goals", run("insights").stdout)


class RemoteTest(unittest.TestCase):
    """The server on one machine, agents on others: token auth, installing from the server, connect."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(port=0, db=os.path.join(self.tmp, "t.db"), token="s3cret")
        self.httpd, self.store, self.dispatcher = make_server(self.cfg)
        self.cfg.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.cfg.port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.read()

    def test_refuses_network_without_token(self):
        from decisions.server import serve
        with self.assertRaises(SystemExit) as ctx:
            serve(Config(host="0.0.0.0", port=0, db=os.path.join(self.tmp, "x.db")))
        self.assertIn("without a token", str(ctx.exception))

    def test_install_assets_are_public_and_the_wheel_installs(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/decisions")
        self.assertEqual(ctx.exception.code, 401)
        script = self.get("/install.sh").decode()
        self.assertIn(f'SERVER="{self.base}"', script)
        name = script.split('WHEEL="$SERVER/install/', 1)[1].split('"', 1)[0]
        self.assertRegex(name, r"^decisions-0\.1\.0\+[0-9a-f]{10}-py3-none-any\.whl$")
        whl = os.path.join(self.tmp, name)
        with open(whl, "wb") as fh:
            fh.write(self.get(f"/install/{name}"))
        with zipfile.ZipFile(whl) as archive:
            license_path = next(p for p in archive.namelist() if p.endswith("/licenses/LICENSE"))
            self.assertIn(b"MIT License", archive.read(license_path))
        self.assertIn(b"name: decisions", self.get("/install/SKILL.md"))
        target = os.path.join(self.tmp, "site")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "--no-index",
                        "--target", target, whl], check=True)
        self.assertTrue(os.path.exists(os.path.join(target, "decisions", "skill", "SKILL.md")))
        self.assertTrue(os.path.exists(os.path.join(target, "decisions", "static", "app.js")))
        ver = subprocess.run([sys.executable, "-m", "decisions", "--version"], capture_output=True, text=True,
                             cwd=self.tmp, env=os.environ | {"PYTHONPATH": target}, check=True)
        self.assertEqual(ver.stdout.strip(), "decide 0.1.0")

    def test_connect_remembers_server_and_token(self):
        client_cfg = os.path.join(self.tmp, "client.toml")
        env = {k: v for k, v in os.environ.items() if k != "DECISIONS_URL"} | {
            "PYTHONPATH": ROOT, "DECISIONS_CLIENT_CONFIG": client_cfg}
        decide = lambda *a: subprocess.run([sys.executable, "-m", "decisions", *a], env=env, cwd=self.tmp,
                                           capture_output=True, text=True)
        bad = decide("connect", self.base, "--token", "wrong")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("401", bad.stderr)
        self.assertFalse(os.path.exists(client_cfg))
        self.assertEqual(decide("connect", self.base, "--token", "s3cret").returncode, 0)
        self.assertEqual(os.stat(client_cfg).st_mode & 0o777, 0o600)
        created = decide("create", "--title", "Remote?", "-o", "yes", "-q")
        self.assertEqual(created.stdout.strip(), "D-1", created.stderr)
        skill_dir = os.path.join(self.tmp, "skills", "decisions")
        self.assertEqual(decide("skill", "install", "--dir", skill_dir).returncode, 0)
        self.assertIn("name: decisions", open(os.path.join(skill_dir, "SKILL.md")).read())
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, ".claude"))
        r = subprocess.run([sys.executable, "-m", "decisions", "skill", "install"], capture_output=True, text=True,
                           env=os.environ | {"PYTHONPATH": ROOT, "HOME": home, "PATH": "/usr/bin:/bin"}, cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        for d in (".claude/skills", ".agents/skills"):
            self.assertTrue(os.path.exists(os.path.join(home, d, "decisions", "SKILL.md")), d)
        self.assertFalse(os.path.exists(os.path.join(home, ".codex")))

    def test_server_install_writes_config_with_token_and_service(self):
        """Service managers are mocked: this checks what would be installed, without installing it."""
        import plistlib
        import tomllib
        from unittest import mock
        from decisions import cli
        home, conf = os.path.join(self.tmp, "home"), os.path.join(self.tmp, "srv.toml")
        with mock.patch.dict(os.environ, {"HOME": home}), mock.patch.object(cli.sys, "platform", "darwin"), \
                mock.patch.object(cli.subprocess, "run") as run, mock.patch.object(cli, "_which_decide",
                                                                                 return_value="/x/bin/decide"):
            cli.main(["server", "install", "--host", "0.0.0.0", "--port", "9999", "--config", conf])
        written = tomllib.load(open(conf, "rb"))
        self.assertEqual((written["host"], written["port"]), ("0.0.0.0", 9999))
        self.assertGreater(len(written["token"]), 20)
        self.assertEqual(os.stat(conf).st_mode & 0o777, 0o600)
        plist = plistlib.load(open(os.path.join(home, "Library/LaunchAgents/com.decisions.server.plist"), "rb"))
        self.assertEqual(plist["ProgramArguments"], ["/x/bin/decide", "serve", "--config", conf])
        self.assertTrue(plist["KeepAlive"])
        self.assertIn(["launchctl", "bootstrap", f"gui/{os.getuid()}",
                       os.path.join(home, "Library/LaunchAgents/com.decisions.server.plist")],
                      [c.args[0] for c in run.call_args_list])


class SpacesTest(unittest.TestCase):
    """Spaces are hard boundaries: a space's token sees only that space; the admin token sees all."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(port=0, db=os.path.join(self.tmp, "t.db"), token="admin", webhook_backoff_base=0.1)
        self.httpd, self.store, self.dispatcher = make_server(self.cfg)
        self.cfg.port = self.httpd.server_port
        self.dispatcher.poll_interval = 0.05
        self.dispatcher.start()
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.cfg.port}"
        # Tests name tokens by alias (tok-work); call() swaps in the real secret.
        self.tokens = {}
        self.call("POST", "/api/spaces", {"name": "work", "deadline_days": 1}, expect=201)
        self.call("POST", "/api/spaces", {"name": "private"}, expect=201)
        for space in ("work", "private"):
            t = self.call("POST", "/api/tokens", {"name": "laptop", "space": space}, expect=201)
            self.tokens[f"tok-{space}"] = t["token"]
            self.tokens[f"id-{space}"] = t["id"]

    def tearDown(self):
        self.dispatcher.stop()
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, method, path, body=None, token="admin", expect=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.tokens.get(token, token)}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                status, text = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode()
        if expect is not None:
            self.assertEqual(status, expect, f"{method} {path}: {text}")
        return json.loads(text) if text.startswith(("{", "[")) else text

    def create(self, token, **extra):
        return self.call("POST", "/api/decisions", {"title": "Which?", "options": ["a", "b"]} | extra,
                         token=token, expect=201)

    def test_space_token_routes_and_isolates(self):
        w = self.create("tok-work")
        p = self.create("tok-private")
        self.assertEqual((w["space"], p["space"]), ("work", "private"))
        ids = lambda token, q="": [d["id"] for d in self.call("GET", "/api/decisions" + q, token=token)["decisions"]]
        self.assertEqual(ids("tok-work"), [w["id"]])
        self.assertEqual(ids("tok-private"), [p["id"]])
        self.assertEqual(ids("admin"), [p["id"], w["id"]])
        self.assertEqual(ids("admin", "?space=work"), [w["id"]])
        # A space token can't create elsewhere; the admin can create anywhere.
        self.call("POST", "/api/decisions", {"title": "x", "space": "private"}, token="tok-work", expect=404)
        self.call("POST", "/api/decisions", {"title": "x", "space": "nope"}, token="admin", expect=404)
        self.assertEqual(self.create("admin", space="private")["space"], "private")
        self.assertEqual(self.create("admin")["space"], "default")
        cfg = self.call("GET", "/api/config", token="tok-work")
        self.assertEqual((cfg["space"], [s["name"] for s in cfg["spaces"]]), ("work", ["work"]))
        self.assertEqual([s["name"] for s in self.call("GET", "/api/config")["spaces"]],
                         ["default", "private", "work"])
        self.call("GET", "/api/decisions", token="wrong", expect=401)

    def test_every_decision_route_hides_other_spaces(self):
        from decisions.server import ROUTES
        p = self.create("tok-private")
        checked = 0
        for method, pattern, name in ROUTES:
            if "/api/decisions/" not in pattern.pattern:
                continue
            path = pattern.pattern.strip("^$").replace("([^/]+)", p["id"], 1).replace("([^/]+)", "1")
            body = {"title": "x", "selected": ["1"], "text": "hi", "status": "manual", "option_ids": ["1"],
                    "url": "http://127.0.0.1:9/", "label": "c", "key": "k", "options": [{"id": "1", "label": "c"}]} if method != "GET" else None
            self.call(method, path + ("?timeout=0" if path.endswith("/wait") else ""), body, token="tok-work",
                      expect=404)
            checked += 1
        self.assertGreater(checked, 15)
        self.call("GET", f"/api/events?decision={p['id']}", token="tok-work", expect=404)
        self.call("GET", f"/api/webhooks?decision={p['id']}", token="tok-work", expect=404)
        self.call("POST", "/api/webhooks", {"url": "http://127.0.0.1:9/", "decision_id": p["id"]},
                  token="tok-work", expect=404)
        # Dependencies stay inside a space too, even for the admin token.
        self.call("POST", "/api/decisions", {"title": "x", "depends_on": [p["id"]]}, token="tok-work", expect=404)
        self.call("POST", "/api/decisions", {"title": "x", "space": "work", "depends_on": [p["id"]]}, expect=404)
        # So do goals: a token can't see another space's goals, or link to them.
        g = self.call("POST", "/api/goals", {"title": "Private goal"}, token="tok-private", expect=201)
        self.assertEqual(g["space"], "private")
        self.call("GET", f"/api/goals/{g['id']}", token="tok-work", expect=404)
        self.assertEqual(self.call("GET", "/api/goals", token="tok-work")["goals"], [])
        self.assertEqual(self.call("GET", "/api/graph", token="tok-work")["nodes"], [])
        self.call("POST", "/api/goals", {"title": "x", "serves": [g["id"]]}, token="tok-work", expect=404)
        self.call("POST", "/api/decisions", {"title": "x", "goals": [g["id"]]}, token="tok-work", expect=404)
        self.call("PATCH", f"/api/decisions/{p['id']}", {"goals": [g["id"]]}, token="tok-private", expect=200)
        # ...and none of it changed anything.
        self.assertEqual(self.call("GET", f"/api/decisions/{p['id']}", token="tok-private")["status"], "open")

    def test_event_feed_is_scoped(self):
        w = self.create("tok-work")
        p = self.create("tok-private")
        feed = lambda token: {e["decision_id"] or e["space"]
                              for e in self.call("GET", "/api/events", token=token)["events"]}
        self.assertEqual(feed("tok-work"), {w["id"], "work"})  # "work": its space.created and token.created
        self.assertEqual(feed("admin"), {w["id"], p["id"], "work", "private"})

    def test_webhooks_only_hear_their_space(self):
        rx_work, rx_all = Receiver(), Receiver()
        hook = self.call("POST", "/api/webhooks", {"url": rx_work.url, "events": ["decision.created"]},
                         token="tok-work", expect=201)
        self.assertEqual(hook["space"], "work")
        everywhere = self.call("POST", "/api/webhooks", {"url": rx_all.url, "events": ["decision.created"]},
                               expect=201)
        self.assertIsNone(everywhere["space"])
        w = self.create("tok-work")
        p = self.create("tok-private")
        self.assertTrue(rx_all.wait_for(2))
        self.assertTrue(rx_work.wait_for(1))
        time.sleep(0.3)
        self.assertEqual([json.loads(b)["decision"]["id"] for _, b in rx_work.received], [w["id"]])
        self.assertEqual(sorted(json.loads(b)["decision"]["id"] for _, b in rx_all.received), sorted([w["id"], p["id"]]))
        # The work token can't see or touch the admin's all-spaces webhook or its deliveries.
        self.assertEqual([h["id"] for h in self.call("GET", "/api/webhooks", token="tok-work")["webhooks"]],
                         [hook["id"]])
        self.call("DELETE", f"/api/webhooks/{everywhere['id']}", token="tok-work", expect=404)
        self.assertTrue(all(d["space"] == "work" for d in
                            self.call("GET", "/api/deliveries", token="tok-work")["deliveries"]))
        self.assertEqual(len(self.call("GET", "/api/deliveries")["deliveries"]), 3)
        self.assertEqual([x["kind"] for x in
                          self.call("GET", f"/api/decisions/{w['id']}/listeners", token="tok-work")["listeners"]],
                         ["webhook"])

    def test_each_space_has_its_own_hash_chain(self):
        for token in ("tok-work", "tok-private", "tok-work", "tok-private"):
            d = self.create(token)
            self.call("POST", f"/api/decisions/{d['id']}/resolve", {"selected": ["1"]}, token=token, expect=200)
        v = self.call("GET", "/api/events/verify")
        self.assertTrue(v["ok"])
        # Each: space.created, token.created, then two decisions created and resolved.
        self.assertEqual({k: c["events"] for k, c in v["chains"].items()}, {"private": 6, "work": 6})
        self.assertEqual(list(self.call("GET", "/api/events/verify", token="tok-work")["chains"]), ["work"])
        conn = sqlite3.connect(self.cfg.db_path)
        conn.execute("DROP TRIGGER events_no_update")
        conn.execute("UPDATE events SET actor='mallory' WHERE seq=(SELECT MIN(seq) FROM events WHERE space='work')")
        conn.commit()
        conn.close()
        v = self.call("GET", "/api/events/verify")
        self.assertFalse(v["ok"])
        self.assertTrue(v["chains"]["private"]["ok"])
        self.assertTrue(self.call("GET", "/api/events/verify", token="tok-private")["ok"])

    def test_idempotency_keys_and_deadlines_are_per_space(self):
        w = self.create("tok-work", idempotency_key="k")
        p = self.create("tok-private", idempotency_key="k")
        self.assertNotEqual(w["id"], p["id"])
        self.assertEqual(self.call("POST", "/api/decisions", {"title": "again", "idempotency_key": "k"},
                                   token="tok-work", expect=200)["id"], w["id"])
        days = lambda d: (datetime.fromisoformat(d["due_at"]) - datetime.fromisoformat(d["created_at"])).days
        self.assertEqual((days(w), days(p)), (1, self.cfg.deadline_days))

    def test_only_the_admin_manages_spaces_and_tokens(self):
        for method, path in (("GET", "/api/tokens"), ("POST", "/api/tokens"), ("POST", "/api/spaces"),
                             ("PATCH", "/api/spaces/work"), ("DELETE", "/api/spaces/work"),
                             ("POST", f"/api/tokens/{self.tokens['id-work']}/roll"),
                             ("DELETE", f"/api/tokens/{self.tokens['id-private']}")):
            self.call(method, path, {"name": "x", "space": "work"}, token="tok-work", expect=403)
        # A machine token still sees its own space, and nothing else.
        self.assertEqual([sp["name"] for sp in self.call("GET", "/api/spaces", token="tok-work")["spaces"]], ["work"])
        cfg = self.call("GET", "/api/config", token="tok-work")
        self.assertEqual((cfg["admin"], cfg["space"], [s["name"] for s in cfg["spaces"]]), (False, "work", ["work"]))
        self.assertTrue(self.call("GET", "/api/config")["admin"])

    def test_tokens_are_hashed_shown_once_rolled_and_revoked(self):
        secret, tid = self.tokens["tok-work"], self.tokens["id-work"]
        self.assertTrue(secret.startswith("dk_"))
        conn = sqlite3.connect(self.cfg.db_path)
        stored = conn.execute("SELECT hash FROM tokens WHERE id=?", (tid,)).fetchone()[0]
        conn.close()
        self.assertNotIn(secret, stored)
        listed = {t["id"]: t for t in self.call("GET", "/api/tokens")["tokens"]}
        self.assertNotIn("token", listed[tid])
        self.assertIsNone(listed[tid]["last_used_at"])
        self.call("POST", "/api/resumer/heartbeat", {"machine": "laptop"}, token="tok-work", expect=200)
        t = {t["id"]: t for t in self.call("GET", "/api/tokens")["tokens"]}[tid]
        self.assertIsNotNone(t["last_used_at"])
        self.assertEqual((t["resumer"]["machine"], t["resumer"]["online"]), ("laptop", True))
        rolled = self.call("POST", f"/api/tokens/{tid}/roll", expect=200)
        self.assertEqual((rolled["id"], rolled["space"]), (tid, "work"))
        self.call("GET", "/api/decisions", token="tok-work", expect=401)
        self.assertEqual(self.call("GET", "/api/config", token=rolled["token"])["space"], "work")
        self.call("DELETE", f"/api/tokens/{tid}", expect=200)
        self.call("GET", "/api/decisions", token=rolled["token"], expect=401)
        self.call("POST", "/api/tokens", {"name": "x", "space": "nope"}, expect=404)
        types = [e["type"] for e in self.call("GET", "/api/events?limit=500")["events"]]
        self.assertEqual([t for t in types if t.startswith("token.")],
                         ["token.created", "token.created", "token.rolled", "token.revoked"])
        self.assertNotIn(rolled["token"], json.dumps(self.call("GET", "/api/events?limit=500")))

    def test_space_lifecycle(self):
        self.call("POST", "/api/spaces", {"name": "work"}, expect=409)
        self.call("POST", "/api/spaces", {"name": "Not OK"}, expect=400)
        self.call("POST", "/api/spaces", {"name": "home", "deadline_days": -1}, expect=400)
        sp = self.call("PATCH", "/api/spaces/private", {"deadline_days": 30}, expect=200)
        self.assertEqual((sp["deadline_days"], sp["effective_deadline_days"]), (30, 30))
        sp = self.call("PATCH", "/api/spaces/private", {"deadline_days": None}, expect=200)
        self.assertEqual((sp["deadline_days"], sp["effective_deadline_days"]), (None, self.cfg.deadline_days))
        self.create("tok-work")
        self.call("DELETE", "/api/spaces/work", expect=409)
        self.call("DELETE", "/api/spaces/default", expect=409)
        gone = self.call("DELETE", "/api/spaces/private", expect=200)
        self.assertEqual(gone["revoked_tokens"], [self.tokens["id-private"]])
        self.call("GET", "/api/decisions", token="tok-private", expect=401)
        self.assertEqual([s["name"] for s in self.call("GET", "/api/spaces")["spaces"]], ["default", "work"])
        # Its chain stays verifiable after it's gone.
        self.assertTrue(self.call("GET", "/api/events/verify")["chains"]["private"]["ok"])

    def test_without_an_admin_token_machine_tokens_still_route(self):
        from decisions.store import Store
        store = Store(Config(db=os.path.join(self.tmp, "open.db")))
        store.create_space({"name": "work"}, "me")
        t = store.create_token({"name": "laptop", "space": "work"}, "me")
        self.assertTrue(store.scope_for("").everything)
        self.assertTrue(store.scope_for("anything").everything)
        self.assertEqual((store.scope_for(t["token"]).default, store.scope_for(t["token"]).token), ("work", t["id"]))


if __name__ == "__main__":
    unittest.main()
