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

    def test_suggested_option_asks_for_its_attributes(self):
        did = self.create()["id"]
        d = self.call("POST", f"/api/decisions/{did}/options",
                      {"label": "Down jacket", "description": "The one from Fjällräven", "suggested": True},
                      actor="me", expect=201)
        o = d["options"][-1]
        self.assertEqual((o["id"], o["suggested_by"]), ("3", "me"))
        self.assertIsNone(d["options"][0]["suggested_by"])
        # Every attribute the others have is asked for, alongside requested ones.
        self.assertEqual({k: p["missing"] for k, p in d["pending_attribute_requests"].items()},
                         {"price": ["3"], "color": ["3"]})
        # Filled in, or marked unknown, counts as answered.
        d = self.call("PATCH", f"/api/decisions/{did}/options",
                      [{"id": "3", "attributes": {"price": 2900},
                        "attribute_meta": {"color": {"unknown": True, "note": "comes in several"}}}])
        self.assertEqual(d["pending_attribute_requests"], {})
        events = self.call("GET", f"/api/decisions/{did}/events")["events"]
        self.assertEqual([e["type"] for e in events], ["decision.created", "options.suggested", "options.updated"])
        self.assertEqual(events[1]["data"]["options"][0]["label"], "Down jacket")
        # An agent adding options plainly isn't suggesting them.
        d = self.call("POST", f"/api/decisions/{did}/options", [{"label": "Fleece"}], actor="agent", expect=201)
        self.assertIsNone(d["options"][-1]["suggested_by"])
        self.assertEqual(self.call("GET", f"/api/decisions/{did}/events")["events"][-1]["type"], "options.added")

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

    def test_resumer_resumes_to_fill_in_unless_the_session_is_waiting_live(self):
        state = os.path.join(self.tmp, "state")
        out = os.path.join(self.tmp, "resumed.txt")
        env = os.environ | {"DECISIONS_URL": self.base, "PYTHONPATH": ROOT, "DECISIONS_STATE_DIR": state,
                            "CLAUDE_CODE_SESSION_ID": "sess-x"}
        run = lambda *a: subprocess.run([sys.executable, "-m", "decisions", *a], env=env, cwd=self.tmp,
                                        capture_output=True, text=True)
        did = run("create", "--title", "Which jacket?", "-o", "Wool coat", "-q").stdout.strip()
        self.call("PATCH", f"/api/decisions/{did}/options/1", {"attributes": {"price": 2400}})
        settled = lambda: self.call("GET", f"/api/decisions/{did}")["resume_state"]
        def until(status):
            deadline = time.time() + 10
            while settled()["status"] != status and time.time() < deadline:
                time.sleep(0.1)
            return settled()
        resumer = subprocess.Popen(
            [sys.executable, "-m", "decisions", "resumer", "run", "--command",
             f'echo "$DECISION_EVENT|$RESUME_PROMPT" >> {out}'],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            resumer.stdout.readline()
            self.call("POST", f"/api/decisions/{did}/options", {"label": "Down jacket", "suggested": True},
                      actor="me", expect=201)
            st = until("finished")
            self.assertEqual((st["status"], st["trigger"]), ("finished", "options.suggested"))
            # A session still waiting on it hears the request itself, so it isn't resumed a second time.
            waiter = subprocess.Popen([sys.executable, "-m", "decisions", "wait", did, "--on", "attributes.requested"],
                                      env=env, stdout=subprocess.DEVNULL)
            for _ in range(50):
                if self.call("GET", f"/api/decisions/{did}/listeners")["listeners"]:
                    break
                time.sleep(0.1)
            self.call("POST", f"/api/decisions/{did}/attribute-requests", {"key": "warmth"}, actor="me", expect=201)
            waiter.wait(10)
            st = until("skipped")
            self.assertEqual((st["status"], st["trigger"]), ("skipped", "attributes.requested"))
            self.assertIn("waiting on it live", st["detail"]["reason"])
        finally:
            resumer.terminate(); resumer.wait()
        lines = open(out).read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("options.suggested|[decisions] "))
        self.assertIn('their own option "Down jacket"', lines[0])
        # Reopening starts over: it's armed for the outcome again.
        self.call("POST", f"/api/decisions/{did}/resolve", {"selected": ["1"], "silence": ["resumer"]}, actor="me")
        self.call("POST", f"/api/decisions/{did}/reopen", {})
        self.assertEqual(settled(), {"status": "armed"})

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

    def test_proof_is_agreed_on_flagged_ready_and_asked_for(self):
        rx = Receiver()
        self.call("POST", "/api/webhooks", {"url": rx.url, "events": ["proof.requested"]}, expect=201)
        gid = self.goal("Fleet upgrades itself")["id"]
        spec = {"success": "An EC2 upgrades within 10 minutes of a release", "command": "./prove.sh", "machine": "mb",
                "cwd": "/work", "scripts": [{"path": "prove.sh", "content": "exit 0\n"}]}
        self.call("PUT", f"/api/goals/{gid}/proof", {"command": "x", "machine": "mb"}, expect=400)
        self.call("POST", f"/api/goals/{gid}/proof/ready", {}, expect=404)
        p = self.call("PUT", f"/api/goals/{gid}/proof", spec, expect=200)["proof"]
        self.assertEqual((p["proposed_by"], p["agreed"], p["ready"], p["run"], p["timeout"]), ("agent", None, None, None, 3600))
        first = p["hash"]
        # The same again changes nothing, and logs nothing.
        self.assertEqual(self.call("PUT", f"/api/goals/{gid}/proof", spec)["proof"]["hash"], first)
        run = lambda actor="me", expect=None: self.call("POST", f"/api/goals/{gid}/proof/run", actor=actor, expect=expect)
        run(expect=409)  # not agreed on
        # Whoever proposed it can't agree to it, and you agree to the version you looked at.
        self.call("POST", f"/api/goals/{gid}/proof/agree", {"hash": first}, expect=403)
        self.call("POST", f"/api/goals/{gid}/proof/agree", {"hash": "0" * 64}, actor="me", expect=409)
        p = self.call("POST", f"/api/goals/{gid}/proof/agree", {"hash": first}, actor="me", expect=200)["proof"]
        self.assertEqual(p["agreed"]["by"], "me")
        run(expect=409)  # the agent hasn't flagged it ready

        # A changed script is logged with both hashes, and has to be agreed on again.
        changed = spec | {"scripts": [{"path": "prove.sh", "content": "exit 1\n"}]}
        p = self.call("PUT", f"/api/goals/{gid}/proof", changed, expect=200)["proof"]
        self.assertIsNone(p["agreed"])
        ev = self.call("GET", f"/api/goals/{gid}/events")["events"][-1]
        self.assertEqual((ev["type"], ev["data"]["previous_hash"], ev["data"]["hash"], ev["data"]["changed"],
                          ev["data"]["was_agreed"]), ("proof.changed", first, p["hash"], ["script prove.sh"], True))
        self.call("POST", f"/api/goals/{gid}/proof/agree", {"hash": first}, actor="me", expect=409)
        self.call("POST", f"/api/goals/{gid}/proof/agree", {"hash": p["hash"]}, actor="me", expect=200)
        p = self.call("POST", f"/api/goals/{gid}/proof/ready", {"note": "released 1.4"}, expect=200)["proof"]
        self.assertEqual((p["ready"]["by"], p["ready"]["note"]), ("agent", "released 1.4"))
        self.assertEqual(self.call("GET", "/api/goals?status=all")["goals"][0]["proof"],
                         {"agreed": True, "ready": True, "run": None, "resume": None})

        run(actor="agent", expect=403)
        p = run(expect=201)["proof"]
        self.assertEqual((p["run"]["status"], p["run"]["requested_by"], p["busy"]), ("requested", "me", True))
        seq = p["run"]["seq"]
        run(expect=409)  # already running
        self.call("PUT", f"/api/goals/{gid}/proof", spec, expect=409)
        self.assertTrue(rx.wait_for(1))
        payload = json.loads(rx.received[0][1])
        self.assertEqual((payload["event"]["type"], payload["goal"]["id"], payload["goal"]["proof"]["command"]),
                         ("proof.requested", gid, "./prove.sh"))

        report = lambda body, expect: self.call("POST", f"/api/goals/{gid}/proof/events", body, actor="resumer", expect=expect)
        report({"status": "passed", "request_seq": seq - 1}, 409)
        report({"status": "started", "request_seq": seq, "pid": 1}, 201)
        # Failing takes back the claim that it's ready, so the button is off until the agent flags it again.
        g = report({"status": "failed", "request_seq": seq, "exit_code": 1, "output_tail": "still on 1.3"}, 201)
        self.assertEqual((g["status"], g["proof"]["ready"], g["proof"]["busy"], g["proof"]["run"]["status"]),
                         ("active", None, False, "failed"))
        report({"status": "passed", "request_seq": seq}, 409)
        run(expect=409)
        self.call("POST", f"/api/goals/{gid}/proof/ready", {}, expect=200)
        seq = run(expect=201)["proof"]["run"]["seq"]
        g = report({"status": "passed", "request_seq": seq, "exit_code": 0}, 201)
        self.assertEqual((g["status"], g["status_note"]), ("achieved", "Proved: An EC2 upgrades within 10 minutes of a release"))
        types = [e["type"] for e in self.call("GET", f"/api/goals/{gid}/events")["events"]]
        self.assertEqual(types[-3:], ["proof.requested", "proof.passed", "goal.achieved"])
        g = self.call("DELETE", f"/api/goals/{gid}/proof", actor="me", expect=200)
        self.assertIsNone(g["proof"])
        self.assertTrue(self.call("GET", "/api/events/verify")["ok"])

    def test_resumer_runs_a_goals_proof_only_while_its_script_is_what_was_agreed_on(self):
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        env = os.environ | {"DECISIONS_URL": self.base, "PYTHONPATH": ROOT, "DECISIONS_MACHINE": "mb",
                            "DECISIONS_STATE_DIR": os.path.join(self.tmp, "state"), "DECISIONS_ACTOR": "claude-code"}
        decide = lambda *a: subprocess.run([sys.executable, "-m", "decisions", *a], env=env, cwd=work,
                                           capture_output=True, text=True)
        script = os.path.join(work, "prove.sh")
        def write(body):
            with open(script, "w") as fh:
                fh.write(body)
        write('echo "checking $GOAL_ID"; test -f released\n')
        gid = decide("goal", "create", "Ship it", "-q").stdout.strip()
        out = decide("goal", "proof", gid, "--command", "sh prove.sh", "--success", "`released` exists", "--script", "prove.sh")
        self.assertIn("waiting for the user to agree", out.stdout, out.stderr)
        proof = lambda: self.call("GET", f"/api/goals/{gid}")["proof"]
        self.assertEqual((proof()["machine"], os.path.realpath(proof()["cwd"]), proof()["scripts"][0]["path"]),
                         ("mb", os.path.realpath(work), "prove.sh"))
        agree = lambda: self.call("POST", f"/api/goals/{gid}/proof/agree", {"hash": proof()["hash"]}, actor="me", expect=200)
        agree()
        self.assertIn("agreed and ready", decide("goal", "ready", gid, "--note", "done").stdout)

        resumer = subprocess.Popen([sys.executable, "-m", "decisions", "resumer", "run"], env=env | {"DECISIONS_ACTOR": "resumer"},
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: (resumer.terminate(), resumer.wait(), resumer.stdout.close()))
        self.assertEqual(resumer.stdout.readline()[:10], "[resumer] ")
        time.sleep(0.3)

        def prove(want):
            waiter = subprocess.Popen([sys.executable, "-m", "decisions", "goal", "wait", gid, "--timeout", "20"],
                                      env=env, cwd=work, stdout=subprocess.PIPE, text=True)
            time.sleep(0.5)
            self.call("POST", f"/api/goals/{gid}/proof/run", actor="me", expect=201)
            heard = waiter.communicate(timeout=30)[0]
            self.assertTrue(heard.startswith(f"event: proof.{want} by resumer"), heard)
            return heard, self.call("GET", f"/api/goals/{gid}")

        # Not achieved yet: the command fails, the agent hears why, and the goal stays active.
        heard, g = prove("failed")
        self.assertIn(f"checking {gid}", heard)
        self.assertEqual((g["status"], g["proof"]["run"]["detail"]["exit_code"], g["proof"]["ready"]), ("active", 1, None))

        # The script was edited after it was agreed on: it isn't run at all.
        write("exit 0\n")
        decide("goal", "ready", gid)
        heard, g = prove("failed")
        self.assertIn("changed since it was agreed on", g["proof"]["run"]["detail"]["error"])
        self.assertNotIn("exit_code", g["proof"]["run"]["detail"])

        # Recording the change (nothing else to repeat) means agreeing again; then it runs and achieves the goal.
        write('echo "checking $GOAL_ID"; test -f released\n# v2\n')
        self.assertIn("waiting for the user to agree", decide("goal", "proof", gid).stdout)
        agree()
        open(os.path.join(work, "released"), "w").close()
        decide("goal", "ready", gid)
        heard, g = prove("passed")
        self.assertEqual((g["status"], g["proof"]["run"]["status"]), ("achieved", "passed"))
        self.assertIn("passed", decide("goal", "get", gid).stdout)

    def test_proof_result_arms_the_goals_session_for_resumption(self):
        gid = self.goal("Ship it")["id"]
        spec = {"success": "released", "command": "./prove.sh", "machine": "mb", "cwd": "/work"}
        session = {"session_id": "sess-1", "cwd": "/work", "agent": "claude-code", "machine": "mb"}
        proof = lambda: self.call("GET", f"/api/goals/{gid}")["proof"]
        state = lambda: proof()["resume_state"]
        # No session recorded: nothing to resume. A session on the proof is armed, outside what's agreed on.
        self.assertIsNone(self.call("PUT", f"/api/goals/{gid}/proof", spec, expect=200)["proof"]["resume_state"])
        self.call("POST", f"/api/goals/{gid}/resume-events", {"status": "manual"}, actor="me", expect=400)
        first = proof()["hash"]
        p = self.call("PUT", f"/api/goals/{gid}/proof", spec | {"resume": session}, expect=200)["proof"]
        self.assertEqual((p["hash"], p["resume"], p["resume_state"]), (first, session, {"status": "armed"}))
        self.assertIn("claude --resume sess-1", p["resume_command"])
        self.assertEqual(self.call("GET", f"/api/goals/{gid}/events")["events"][-1]["type"], "proof.retargeted")
        # Agreeing is news for the session: pending against that event, until the resumer reports in.
        self.call("POST", f"/api/goals/{gid}/proof/agree", {"hash": first}, actor="me", expect=200)
        agreed = self.call("GET", f"/api/goals/{gid}/events")["events"][-1]
        self.assertEqual(state(), {"status": "pending", "trigger": "proof.agreed", "trigger_seq": agreed["seq"],
                                   "since": agreed["ts"], "failures": 0, "max_failures": 3})
        # Flagging ready from another session makes that one the target; leaving `resume` out keeps it. A session
        # that flags the goal ready knows it was agreed on: nothing is left to tell it.
        self.call("POST", f"/api/goals/{gid}/proof/ready", {"resume": session | {"session_id": "sess-2"}}, expect=200)
        self.assertEqual(state(), {"status": "armed"})
        self.call("POST", f"/api/goals/{gid}/proof/ready", {"ready": False}, expect=200)
        self.call("POST", f"/api/goals/{gid}/resume-events", {"status": "finished", "trigger_seq": agreed["seq"]},
                  actor="resumer", expect=201)
        self.assertEqual((state()["status"], state()["trigger"]), ("finished", "proof.agreed"))
        self.call("POST", f"/api/goals/{gid}/proof/ready", {}, expect=200)
        self.assertEqual(state()["status"], "finished")
        self.assertEqual(proof()["resume"]["session_id"], "sess-2")
        self.call("PUT", f"/api/goals/{gid}/proof", spec, expect=200)
        self.assertEqual(proof()["resume"]["session_id"], "sess-2")

        report = lambda status, **x: self.call("POST", f"/api/goals/{gid}/proof/events",
                                               {"status": status, "request_seq": proof()["run"]["seq"]} | x,
                                               actor="resumer", expect=201)
        resumed = lambda status, **x: self.call("POST", f"/api/goals/{gid}/resume-events",
                                                {"status": status, "trigger_seq": state()["trigger_seq"]} | x,
                                                actor="resumer", expect=201)
        fail = lambda: (self.call("POST", f"/api/goals/{gid}/proof/ready", {}, expect=200),
                        self.call("POST", f"/api/goals/{gid}/proof/run", actor="me", expect=201),
                        report("failed", exit_code=1, output_tail="still on 1.3"))
        # A failed run: pending against that very event, until the resumer reports in.
        fail()
        events = self.call("GET", f"/api/goals/{gid}/events")["events"]
        self.assertEqual(state(), {"status": "pending", "trigger": "proof.failed", "trigger_seq": events[-1]["seq"],
                                   "since": events[-1]["ts"], "failures": 1, "max_failures": 3})
        resumed("started", pid=1)
        self.assertEqual(state()["status"], "started")
        g = resumed("finished", exit_code=0)
        self.assertEqual((g["proof"]["resume_state"]["status"], g["proof"]["resume_state"]["actor"]), ("finished", "resumer"))
        # The loop guard: the fourth failure in a row isn't offered for resumption.
        fail()
        self.assertEqual((state()["status"], state()["failures"]), ("pending", 2))
        resumed("finished")
        fail()
        self.assertEqual((state()["status"], state()["failures"]), ("pending", 3))
        resumed("finished")
        fail()
        self.assertEqual((state()["status"], state()["failures"]), ("capped", 4))
        # Proposing the proof anew resets it, and agreeing to that is news again; passing arms nothing further
        # and achieves the goal.
        self.call("PUT", f"/api/goals/{gid}/proof", spec | {"command": "./prove2.sh"}, expect=200)
        self.assertEqual(state(), {"status": "armed"})
        self.call("POST", f"/api/goals/{gid}/proof/agree", {"hash": proof()["hash"]}, actor="me", expect=200)
        self.assertEqual((state()["status"], state()["trigger"]), ("pending", "proof.agreed"))
        fail()
        self.assertEqual((state()["status"], state()["failures"]), ("pending", 1))
        resumed("finished")
        self.call("POST", f"/api/goals/{gid}/proof/ready", {}, expect=200)
        self.call("POST", f"/api/goals/{gid}/proof/run", actor="me", expect=201)
        g = report("passed", exit_code=0)
        self.assertEqual((g["status"], g["proof"]["resume_state"]["trigger"], g["proof"]["resume_state"]["failures"]),
                         ("achieved", "proof.passed", 0))
        # A session in `decide goal wait` is listed, so the resumer can leave it to hear the result itself.
        self.assertEqual(self.call("GET", f"/api/goals/{gid}/listeners")["listeners"], [])
        t = threading.Thread(target=self.call, args=("GET", f"/api/events?goal={gid}&types=proof.passed&after=999999&wait=2"),
                             kwargs={"actor": "claude-code"}, daemon=True)
        t.start(); time.sleep(0.3)
        live = self.call("GET", f"/api/goals/{gid}/listeners")["listeners"]
        self.assertEqual((live[0]["actor"], live[0]["connected"], live[0]["events"]), ("claude-code", True, ["proof.passed"]))
        t.join()
        self.assertTrue(self.call("GET", "/api/events/verify")["ok"])

    def test_resumer_wakes_the_goals_session_with_the_proofs_result(self):
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        out = os.path.join(self.tmp, "resumed.txt")
        state_dir = os.path.join(self.tmp, "state")
        env = os.environ | {"DECISIONS_URL": self.base, "PYTHONPATH": ROOT, "DECISIONS_MACHINE": "mb",
                            "DECISIONS_STATE_DIR": state_dir, "CLAUDE_CODE_SESSION_ID": "sess-g"}
        decide = lambda *a: subprocess.run([sys.executable, "-m", "decisions", *a], env=env, cwd=work,
                                           capture_output=True, text=True)
        with open(os.path.join(work, "prove.sh"), "w") as fh:
            fh.write('echo "checking $GOAL_ID"; test -f released\n')
        gid = decide("goal", "create", "Ship it", "-q").stdout.strip()
        decide("goal", "proof", gid, "--command", "sh prove.sh", "--success", "`released` exists", "--script", "prove.sh")
        goal = lambda i=gid: self.call("GET", f"/api/goals/{i}")
        self.assertEqual((goal()["proof"]["resume"]["session_id"], goal()["proof"]["resume"]["machine"],
                          goal()["proof"]["resume_state"]), ("sess-g", "mb", {"status": "armed"}))
        agree = lambda i=gid: self.call("POST", f"/api/goals/{i}/proof/agree", {"hash": goal(i)["proof"]["hash"]},
                                        actor="me", expect=200)
        agree()
        # One record per wake-up: the prompt spans lines (it ends with the proof's output).
        lines = lambda: open(out).read().split("<<END>>\n")[:-1] if os.path.exists(out) else []
        settled = lambda i=gid: next((True for _ in range(150) if goal(i)["proof"]["resume_state"]["status"]
                                      in ("finished", "failed", "skipped", "capped") or time.sleep(0.1)), False)
        start = lambda: subprocess.Popen(
            [sys.executable, "-m", "decisions", "resumer", "run", "--command",
             f'echo "$RESUME_SESSION_ID|$PROOF_RESULT|$GOAL_ID|$RESUME_PROMPT" >> {out}; echo "<<END>>" >> {out}'],
            env=env | {"DECISIONS_ACTOR": "resumer"}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        resumer = start()
        self.assertEqual(resumer.stdout.readline()[:10], "[resumer] ")
        time.sleep(0.3)

        # The proof fails: the session that flagged the goal ready is woken, told why, and what to do.
        decide("goal", "ready", gid, "--note", "done")
        self.call("POST", f"/api/goals/{gid}/proof/run", actor="me", expect=201)
        self.assertTrue(settled())
        self.assertEqual(len(lines()), 1)
        self.assertTrue(lines()[0].startswith(f"sess-g|failed|{gid}|[decisions] {gid}"), lines())
        for s in ("failed (exit 1", "failure 1 of 3 in a row", "Fix the work, not the proof", f"decide goal ready {gid}",
                  "Don't run `decide goal wait`", f"checking {gid}"):
            self.assertIn(s, lines()[0])
        self.assertEqual(goal()["proof"]["resume_state"]["status"], "finished")

        # Replaying the whole feed resumes nothing twice: the result was already acted on.
        resumer.terminate(); resumer.wait()
        with open(os.path.join(state_dir, "resumer.cursor"), "w") as fh:
            fh.write("0")
        resumer = start()
        resumer.stdout.readline()
        time.sleep(1)
        self.assertEqual(len(lines()), 1)

        # A session still in `decide goal wait` hears it itself; the resumer leaves it alone.
        waiter = subprocess.Popen([sys.executable, "-m", "decisions", "goal", "wait", gid, "--timeout", "20"],
                                  env=env | {"DECISIONS_ACTOR": "claude-code"}, cwd=work, stdout=subprocess.PIPE, text=True)
        time.sleep(0.5)
        decide("goal", "ready", gid)
        self.call("POST", f"/api/goals/{gid}/proof/run", actor="me", expect=201)
        self.assertTrue(waiter.communicate(timeout=30)[0].startswith("event: proof.failed"))
        self.assertTrue(settled())
        self.assertEqual((goal()["proof"]["resume_state"]["status"], len(lines())), ("skipped", 1))

        # Passing: the goal is achieved and the session (its wait long over) is told to carry on.
        time.sleep(5.5)
        open(os.path.join(work, "released"), "w").close()
        decide("goal", "ready", gid)
        self.call("POST", f"/api/goals/{gid}/proof/run", actor="me", expect=201)
        self.assertTrue(settled())
        self.assertEqual((goal()["status"], len(lines())), ("achieved", 2))
        self.assertTrue(lines()[1].startswith(f"sess-g|passed|{gid}|[decisions] {gid}"), lines())
        self.assertIn("is achieved", lines()[1])
        self.assertIn("Continue with the next piece of work", lines()[1])

        # The proof runs elsewhere, but the session lives here: this resumer wakes it. Until the cap.
        other = self.goal("Elsewhere")["id"]
        self.call("PUT", f"/api/goals/{other}/proof", {
            "success": "x", "command": "x", "machine": "ci", "cwd": "/w",
            "resume": {"session_id": "sess-e", "cwd": work, "agent": "claude-code", "machine": "mb"}}, expect=200)
        # It's first woken when the user agrees to the proof, and told to do the work.
        agree(other)
        self.assertTrue(settled(other))
        self.assertEqual((goal(other)["proof"]["resume_state"]["status"], len(lines())), ("finished", 3))
        self.assertTrue(lines()[2].startswith(f'sess-e|agreed|{other}|[decisions] {other} "Elsewhere": me agreed to '
                                              f'its proof `x`. Do the work now.'), lines())
        self.assertIn(f"decide goal ready {other}", lines()[2])
        # Unless it's in `decide goal wait` for that: one waiting for something else is still woken.
        for n, (on, status) in enumerate((("proof.agreed,proof.removed", "skipped"), ("proof.passed", "finished"))):
            waited = self.goal(f"Waited for {n}")["id"]
            self.call("PUT", f"/api/goals/{waited}/proof", {
                "success": "x", "command": "x", "machine": "ci", "cwd": "/w",
                "resume": {"session_id": "sess-w", "cwd": work, "agent": "claude-code", "machine": "mb"}}, expect=200)
            waiter = subprocess.Popen([sys.executable, "-m", "decisions", "goal", "wait", waited, "--on", on,
                                       "--timeout", "5"], env=env | {"DECISIONS_ACTOR": "claude-code"}, cwd=work,
                                      stdout=subprocess.PIPE, text=True)
            time.sleep(0.5)
            agree(waited)
            self.assertTrue(settled(waited))
            self.assertEqual((goal(waited)["proof"]["resume_state"]["status"], len(lines())), (status, 3 + n))
            waiter.kill(); waiter.wait(); waiter.stdout.close()
        self.assertTrue(lines()[3].startswith("sess-w|agreed|"))
        for n in range(1, 5):
            self.call("POST", f"/api/goals/{other}/proof/ready", {}, expect=200)
            seq = self.call("POST", f"/api/goals/{other}/proof/run", actor="me", expect=201)["proof"]["run"]["seq"]
            self.call("POST", f"/api/goals/{other}/proof/events", {"status": "failed", "request_seq": seq, "exit_code": 2},
                      actor="ci-resumer", expect=201)
            self.assertTrue(settled(other))
            self.assertEqual((goal(other)["proof"]["resume_state"]["status"], len(lines())),
                             ("finished" if n <= 3 else "capped", 4 + min(n, 3)), n)
        self.assertIn("failure 3 of 3 in a row. After this one you won't be resumed again", lines()[-1])
        self.assertTrue(lines()[-1].startswith("sess-e|failed|"))

        # And a session on another machine is that machine's business.
        theirs = self.goal("Theirs")["id"]
        self.call("PUT", f"/api/goals/{theirs}/proof", {
            "success": "x", "command": "x", "machine": "mb", "cwd": work,
            "resume": {"session_id": "sess-t", "cwd": work, "agent": "claude-code", "machine": "desktop"}}, expect=200)
        agree(theirs)
        self.call("POST", f"/api/goals/{theirs}/proof/ready", {}, expect=200)
        self.call("POST", f"/api/goals/{theirs}/proof/run", actor="me", expect=201)
        for _ in range(100):
            if goal(theirs)["proof"]["run"]["status"] == "failed":
                break
            time.sleep(0.1)
        time.sleep(0.5)
        resumer.terminate(); resumer.wait()
        self.assertEqual((goal(theirs)["proof"]["run"]["status"], goal(theirs)["proof"]["resume_state"]["status"]),
                         ("failed", "pending"))
        self.assertEqual(len(lines()), 7)

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

    def test_issues_record_where_a_decision_is_acted_on(self):
        d = self.create(issues=["https://linear.app/acme/issue/LIN-42/cache-search"])
        self.assertEqual(d["issues"], [{"url": "https://linear.app/acme/issue/LIN-42/cache-search", "key": "LIN-42"}])
        did = d["id"]
        self.call("POST", f"/api/decisions/{did}/resolve", {"selected": ["1"]}, expect=200)
        keys = lambda urls: [i["key"] for i in self.call("PATCH", f"/api/decisions/{did}", {"issues": urls})["issues"]]
        self.assertEqual(keys([
            "https://acme.atlassian.net/browse/OPS-7",
            "https://acme.atlassian.net/jira/software/projects/OPS/boards/1?selectedIssue=OPS-8",
            "https://github.com/ArvidSU/decide/issues/12",
            "https://github.com/ArvidSU/decide/pull/13",
            "https://gitlab.com/acme/api/-/merge_requests/5",
            {"url": "https://tracker.example/t/991", "key": "Ticket 991"},
            "https://tracker.example/t/992",
            "https://github.com/ArvidSU/decide/issues/12",  # the same issue twice counts once
        ]), ["OPS-7", "OPS-8", "decide#12", "decide#13", "api!5", "Ticket 991", "tracker.example"])
        for bad in (["javascript:alert(1)"], [{"key": "LIN-1"}], "https://linear.app/x"):
            self.call("PATCH", f"/api/decisions/{did}", {"issues": bad}, expect=400)
        # Linking one is a logged change, and doesn't reopen or otherwise touch the decision.
        d = self.call("GET", f"/api/decisions/{did}")
        self.assertEqual(d["status"], "resolved")
        changes = [e for e in self.call("GET", f"/api/decisions/{did}/events")["events"] if e["type"] == "decision.updated"]
        self.assertEqual(changes[-1]["data"]["changes"]["issues"][1], d["issues"])
        self.assertIn("**Acted on in:** [OPS-7](https://acme.atlassian.net/browse/OPS-7)",
                      self.call("GET", f"/api/decisions/{did}/markdown"))
        self.assertEqual(self.call("GET", "/api/decisions?status=resolved")["decisions"][0]["issues"], d["issues"])

    def test_cli_issue(self):
        env = os.environ | {"DECISIONS_URL": self.base, "PYTHONPATH": ROOT}
        run = lambda *a: subprocess.run([sys.executable, "-m", "decisions", *a], env=env, cwd=self.tmp,
                                        capture_output=True, text=True)
        did = run("create", "--title", "Which?", "-o", "a", "--issue", "https://linear.app/a/issue/LIN-1",
                  "-q", "--no-resume").stdout.strip()
        out = run("issue", did, "https://github.com/o/r/issues/4", "--key", "GH-4")
        self.assertEqual(out.stdout.split("\n")[:2], ["LIN-1  https://linear.app/a/issue/LIN-1",
                                                      "GH-4  https://github.com/o/r/issues/4"], out.stderr)
        self.assertIn("acted on in GH-4: https://github.com/o/r/issues/4", run("get", did).stdout)
        run("issue", did, "LIN-1", "--remove")
        self.assertEqual([i["key"] for i in self.call("GET", f"/api/decisions/{did}")["issues"]], ["GH-4"])
        self.assertNotEqual(run("issue", did, "LIN-1", "--remove").returncode, 0)

    def test_issues_link_at_any_status(self):
        d = self.create()
        d = self.call("PATCH", f"/api/decisions/{d['id']}", {"issues": ["https://linear.app/a/issue/LIN-5"]})
        self.assertEqual((d["status"], [i["key"] for i in d["issues"]]), ("open", ["LIN-5"]))

    def test_issues_record_where_a_goal_is_worked_on(self):
        g = self.goal("Ship search", issues=["https://linear.app/acme/issue/LIN-40/search"])
        self.assertEqual(g["issues"], [{"url": "https://linear.app/acme/issue/LIN-40/search", "key": "LIN-40"}])
        gid = g["id"]
        for status in ("achieved", "active", "abandoned"):
            self.call("PATCH", f"/api/goals/{gid}", {"status": status})
            urls = [*[i["url"] for i in g["issues"]], f"https://github.com/o/r/issues/{len(g['issues'])}"]
            g = self.call("PATCH", f"/api/goals/{gid}", {"issues": urls})
            self.assertEqual(g["status"], status)
        self.assertEqual([i["key"] for i in g["issues"]], ["LIN-40", "r#1", "r#2", "r#3"])
        g = self.call("PATCH", f"/api/goals/{gid}", {"issues": [{"url": "https://tracker.example/t/1", "key": "T1"}]})
        self.assertEqual(g["issues"], [{"url": "https://tracker.example/t/1", "key": "T1"}])
        for bad in (["javascript:alert(1)"], [{"key": "LIN-1"}], "https://linear.app/x"):
            self.call("PATCH", f"/api/goals/{gid}", {"issues": bad}, expect=400)
        self.call("POST", "/api/goals", {"title": "x", "issues": ["nope"]}, expect=400)
        # Each change is a logged `goal.updated`; setting the same list again is not.
        self.call("PATCH", f"/api/goals/{gid}", {"issues": g["issues"]})
        changes = [e["data"]["changes"] for e in self.call("GET", f"/api/goals/{gid}/events")["events"]
                   if e["type"] == "goal.updated"]
        self.assertEqual(len(changes), 4)
        self.assertEqual(changes[-1]["issues"][1], g["issues"])
        listed = {x["id"]: x for x in self.call("GET", "/api/goals?status=all")["goals"]}
        self.assertEqual(listed[gid]["issues"], g["issues"])

    def test_cli_goal_issue(self):
        env = os.environ | {"DECISIONS_URL": self.base, "PYTHONPATH": ROOT}
        run = lambda *a: subprocess.run([sys.executable, "-m", "decisions", *a], env=env, cwd=self.tmp,
                                        capture_output=True, text=True)
        gid = run("goal", "create", "Ship it", "--issue", "https://linear.app/a/issue/LIN-1", "-q").stdout.strip()
        out = run("goal", "issue", gid, "https://github.com/o/r/issues/4", "--key", "GH-4")
        self.assertEqual(out.stdout.split("\n")[:2], ["LIN-1  https://linear.app/a/issue/LIN-1",
                                                      "GH-4  https://github.com/o/r/issues/4"], out.stderr)
        self.assertIn("acted on in GH-4: https://github.com/o/r/issues/4", run("goal", "get", gid).stdout)
        run("goal", "issue", gid, "LIN-1", "--remove")
        self.assertEqual([i["key"] for i in self.call("GET", f"/api/goals/{gid}")["issues"]], ["GH-4"])
        self.assertNotEqual(run("goal", "issue", gid, "LIN-1", "--remove").returncode, 0)

    # ---------------------------------------------------------------- feedback

    def test_feedback_is_sent_listed_and_closed(self):
        session = {"session_id": "sess-9", "agent": "claude-code", "machine": "mb", "cwd": "/work"}
        self.call("POST", "/api/feedback", {"kind": "rant", "title": "x"}, expect=400)
        self.call("POST", "/api/feedback", {"kind": "bug"}, expect=400)
        self.call("POST", "/api/feedback", {"kind": "bug", "title": "x", "session": {"agent": "a"}}, expect=400)
        f = self.call("POST", "/api/feedback", {"kind": "bug", "title": "wait exits 0 on a timeout",
                                                "body": "Expected **124**.", "session": session | {"prompt": "p"}},
                      expect=201)
        self.assertEqual((f["id"], f["kind"], f["status"], f["actor"], f["session"], f["space"]),
                         ("F-1", "bug", "open", "agent", session, "default"))
        self.assertTrue(f["url"].endswith("/feedback#F-1"))
        self.call("POST", "/api/feedback", {"kind": "feature", "title": "Compare two options"}, expect=201)
        listed = lambda q="": [x["id"] for x in self.call("GET", f"/api/feedback{q}")["feedback"]]
        self.assertEqual(listed(), ["F-2", "F-1"])
        self.assertEqual(self.call("GET", "/api/feedback/f-1")["body"], "Expected **124**.")
        self.call("GET", "/api/feedback/F-7", expect=404)
        self.call("PATCH", "/api/feedback/F-1", {"status": "fixed"}, expect=400)
        done = self.call("PATCH", "/api/feedback/F-1", {"status": "done", "note": "Fixed in abc123"}, actor="me",
                         expect=200)
        self.assertEqual((done["status"], done["status_note"]), ("done", "Fixed in abc123"))
        self.call("PATCH", "/api/feedback/F-1", {"status": "done", "note": "Fixed in abc123"}, actor="me")  # no change
        self.assertEqual((listed("?status=open"), listed("?status=done")), (["F-2"], ["F-1"]))
        events = [e for e in self.call("GET", "/api/events?limit=500")["events"] if e["type"].startswith("feedback.")]
        self.assertEqual([(e["type"], e["actor"]) for e in events],
                         [("feedback.sent", "agent"), ("feedback.sent", "agent"), ("feedback.updated", "me")])
        self.assertEqual(events[0]["data"]["feedback"]["title"], "wait exits 0 on a timeout")
        self.assertEqual(events[2]["data"], {"id": "F-1", "status": ["open", "done"], "note": "Fixed in abc123"})
        self.assertTrue(self.call("GET", "/api/events/verify")["ok"])

        # The CLI records the agent session it runs in.
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        env = os.environ | {"DECISIONS_URL": self.base, "PYTHONPATH": ROOT, "CLAUDE_CODE_SESSION_ID": "sess-cli",
                            "DECISIONS_MACHINE": "mb"}
        decide = lambda *a: subprocess.run([sys.executable, "-m", "decisions", "feedback", *a], env=env, cwd=work,
                                           capture_output=True, text=True)
        sent = decide("send", "Say which space", "--kind", "improvement", "--body", "The error lists them.")
        self.assertIn("F-3 sent", sent.stdout, sent.stderr)
        got = self.call("GET", "/api/feedback/F-3")
        self.assertEqual((got["actor"], got["session"]["session_id"], got["session"]["agent"], got["session"]["machine"]),
                         ("claude-code", "sess-cli", "claude-code", "mb"))
        self.assertEqual(decide("send", "x", "--kind", "rant").returncode, 2)
        out = decide("list").stdout
        self.assertIn("F-3", out)
        self.assertIn("Say which space", out)
        self.assertNotIn("F-1", out)
        self.assertIn("The error lists them.", decide("get", "F-3").stdout)
        self.assertIn("dismissed", decide("set", "F-3", "--status", "dismissed", "--note", "by design").stdout)
        self.assertEqual(self.call("GET", "/api/feedback/F-3")["status_note"], "by design")

    def test_a_session_is_due_for_feedback_once_everything_it_waited_on_has_happened(self):
        from unittest import mock
        from decisions import store as st
        session = lambda sid, machine="mb": {"session_id": sid, "agent": "claude-code", "machine": machine, "cwd": "/w"}
        due = lambda machine="mb": self.call("GET", f"/api/feedback/due?machine={machine}")["sessions"]
        d1 = self.create(resume=session("s1"))["id"]
        d2 = self.create(resume=session("s1"))["id"]
        d3 = self.create(resume=session("s2"))["id"]
        d4 = self.create(resume=session("s3", "other"))["id"]
        gid = self.goal("Ship it")["id"]
        self.call("PUT", f"/api/goals/{gid}/proof", {"success": "released", "command": "./prove.sh", "machine": "mb",
                                                     "resume": session("s1")}, expect=200)
        with mock.patch.object(st, "FEEDBACK_QUIET", 0):
            self.assertEqual(due(), [])
            # Decided, but the session hasn't heard yet.
            for did in (d1, d3, d4):
                self.call("POST", f"/api/decisions/{did}/resolve", {"selected": ["1"]}, actor="me", expect=200)
            self.assertEqual(due(), [])
            for did in (d1, d3, d4):
                self.call("POST", f"/api/decisions/{did}/resume-events", {"status": "started"}, expect=201)
            self.assertEqual(due(), [])
            for did in (d1, d3, d4):
                self.call("POST", f"/api/decisions/{did}/resume-events", {"status": "finished"}, expect=201)
            # s2 has nothing left; s1 still waits on a decision and a goal; s3 lives on another machine.
            self.assertEqual(due(), [session("s2") | {"space": "default", "about": [d3]}])
            self.assertEqual([s["session_id"] for s in due("other")], ["s3"])
            self.call("POST", f"/api/decisions/{d2}/cancel", {}, actor="me", expect=200)
            self.assertEqual([s["session_id"] for s in due()], ["s2"])
            self.call("PATCH", f"/api/goals/{gid}", {"status": "abandoned"}, actor="me", expect=200)
            self.assertEqual({s["session_id"]: s["about"] for s in due()}, {"s2": [d3], "s1": [d1, d2, gid]})
            # Reopening a decision makes its session wait again.
            self.call("POST", f"/api/decisions/{d2}/reopen", {}, actor="me", expect=200)
            self.assertEqual([s["session_id"] for s in due()], ["s2"])
            self.call("POST", f"/api/decisions/{d2}/cancel", {}, actor="me", expect=200)
            # A session that sent feedback by itself isn't asked.
            self.call("POST", "/api/feedback", {"kind": "bug", "title": "x", "session": session("s1")}, expect=201)
            self.assertEqual([s["session_id"] for s in due()], ["s2"])
            # Only what just finished, and not before it has been quiet for a while.
            with mock.patch.object(st, "FEEDBACK_WINDOW", 0):
                self.assertEqual(due(), [])
            with mock.patch.object(st, "FEEDBACK_QUIET", 600):
                self.assertEqual(due(), [])

            # Each session is asked once: the first resumer to report claims it.
            ask = lambda status, expect, **kw: self.call("POST", "/api/feedback/asks",
                                                         {"status": status, "session_id": "s2"} | kw, actor="resumer", expect=expect)
            ask("finished", 404)
            ask("started", 400)
            ask("asked", 400)
            asked = ask("started", 201, agent="claude-code", machine="mb", about=[d3], log="/tmp/x.log")
            self.assertEqual((asked["status"], asked["about"], asked["finished_at"]), ("started", [d3], None))
            ask("started", 409, agent="claude-code", machine="mb")
            self.assertEqual(due(), [])
            self.assertEqual(ask("finished", 201, exit_code=0)["status"], "finished")
        self.assertEqual([a["session_id"] for a in self.call("GET", "/api/feedback")["asks"]], ["s2"])
        events = [e for e in self.call("GET", "/api/events?limit=500")["events"] if e["type"].startswith("feedback.ask")]
        self.assertEqual([e["type"] for e in events], ["feedback.asked", "feedback.ask_finished"])
        self.assertEqual(events[0]["data"], {"session_id": "s2", "agent": "claude-code", "machine": "mb",
                                             "about": [d3], "log": "/tmp/x.log"})
        self.assertTrue(self.call("GET", "/api/events/verify")["ok"])

    def test_resumer_asks_a_finished_session_for_feedback_in_a_fork(self):
        from unittest import mock
        from decisions import agents, store as st
        self.assertIn("--fork-session", agents.feedback_command("claude-code"))
        self.assertIn("exec fork", agents.feedback_command("codex"))
        self.assertIsNone(agents.feedback_command("gemini"))
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        env = os.environ | {"DECISIONS_URL": self.base, "PYTHONPATH": ROOT, "DECISIONS_MACHINE": "mb",
                            "DECISIONS_STATE_DIR": os.path.join(self.tmp, "state")}
        session = {"session_id": "sess-fb", "agent": "claude-code", "machine": "mb", "cwd": work}
        did = self.create(resume=session)["id"]
        self.call("POST", f"/api/decisions/{did}/cancel", {}, actor="me", expect=200)
        asks = lambda: self.call("GET", "/api/feedback")["asks"]
        # What the fork sends counts as the session's: the command is run as if inside it.
        command = (f'claude-code={sys.executable} -m decisions feedback send "from $RESUME_SESSION_ID in $PWD" '
                   f'--kind improvement --body "$RESUME_PROMPT"')
        start = lambda *flags: subprocess.Popen(
            [sys.executable, "-m", "decisions", "resumer", "run", "--feedback-command", command, *flags],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        with mock.patch.object(st, "FEEDBACK_QUIET", 0):
            # A machine that isn't opted in asks nobody.
            resumer = start()
            self.assertEqual(resumer.stdout.readline()[:10], "[resumer] ")
            time.sleep(1)
            resumer.terminate(); resumer.wait()
            self.assertEqual(asks(), [])
            resumer = start("--feedback")
            done = next((True for _ in range(100) if [a["status"] for a in asks()] == ["finished"] or time.sleep(0.1)),
                        False)
            resumer.terminate(); resumer.wait()
        self.assertTrue(done, resumer.stdout.read())
        sent = self.call("GET", "/api/feedback")["feedback"]
        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0]["session"]["session_id"], sent[0]["actor"]), ("sess-fb", "claude-code"))
        self.assertIn("from sess-fb in ", sent[0]["title"])
        self.assertIn(os.path.basename(work), sent[0]["title"])
        self.assertIn(f"has happened ({did})", sent[0]["body"])
        self.assertIn("decide feedback send", sent[0]["body"])


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

    def test_a_machine_follows_the_servers_build(self):
        """The server names its build; a resumer installed from it reruns the installer when that changes, and
        restarts once the code on disk isn't what it runs. `sh` is a stand-in: nothing is really installed."""
        from unittest import mock
        from decisions import dist
        def api(method, path, body=None):
            req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body else None,
                                         headers={"Authorization": "Bearer s3cret", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        name = self.get("/install.sh").decode().split('WHEEL="$SERVER/install/', 1)[1].split('"', 1)[0]
        build = name.split("+")[1].split("-")[0]
        self.assertEqual(api("GET", "/api/config")["build"], build)
        self.assertEqual(dist.fingerprint(), build)
        beat = api("POST", "/api/resumer/heartbeat", {"machine": "old-mb", "build": "0123456789"})
        self.assertEqual((beat["build"], beat["server_build"]), ("0123456789", build))
        self.assertEqual(api("GET", "/api/resumer?machine=old-mb")["build"], "0123456789")

        # A machine set up from the server: its decide is the wheel, which records the build it is.
        site, bin_dir, out = (os.path.join(self.tmp, d) for d in ("site", "bin", "out"))
        whl = os.path.join(self.tmp, name)
        with open(whl, "wb") as fh:
            fh.write(self.get(f"/install/{name}"))
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "--no-index", "--target", site, whl],
                       check=True)
        os.makedirs(bin_dir), os.makedirs(out)
        with open(os.path.join(bin_dir, "sh"), "w") as fh:  # the "installer": records the call, changes the code
            fh.write('#!/bin/sh\n/bin/cat > "$OUT/installer.sh"\necho "$*" > "$OUT/args"\n'
                     'echo "$DECISIONS_TOKEN" > "$OUT/token"\necho "# newer" >> "$SITE/decisions/markdown.py"\n')
        os.chmod(os.path.join(bin_dir, "sh"), 0o755)
        env = {k: v for k, v in os.environ.items() if k != "DECISIONS_TOKEN"} | {
            "PYTHONPATH": site, "DECISIONS_URL": self.base, "DECISIONS_CLIENT_CONFIG": os.path.join(self.tmp, "c.toml"),
            "DECISIONS_STATE_DIR": os.path.join(self.tmp, "state"), "DECISIONS_MACHINE": "mb", "OUT": out, "SITE": site,
            "HOME": os.path.join(self.tmp, "home"), "PATH": bin_dir + os.pathsep + os.environ["PATH"]}
        decide = lambda *a, **kw: subprocess.run([sys.executable, "-m", "decisions", *a], cwd=self.tmp,
                                                 capture_output=True, text=True, env=env | {"DECISIONS_TOKEN": "s3cret"} | kw)
        same = decide("update")
        self.assertEqual(same.returncode, 0, same.stderr)
        self.assertIn(f"up to date: this decide is {build}", same.stdout)
        self.assertFalse(os.listdir(out))

        log = os.path.join(self.tmp, "resumer.log")
        with mock.patch.object(dist, "fingerprint", return_value="feedfeed00"):  # the server was upgraded
            check = decide("update", "--check")
            self.assertEqual(check.returncode, 1)
            self.assertIn(f"out of date: this decide is {build}, {self.base} serves feedfeed00", check.stdout)
            # A checkout isn't the server's to replace.
            dev = decide("update", PYTHONPATH=ROOT)
            self.assertNotEqual(dev.returncode, 0)
            self.assertIn("wasn't installed from a server", dev.stderr)
            self.assertFalse(os.listdir(out))
            # Opted out: it keeps running what it has.
            with open(log, "w") as fh:
                quiet = subprocess.Popen([sys.executable, "-m", "decisions", "resumer", "run", "--no-update"],
                                         cwd=self.tmp, stdout=fh, stderr=subprocess.STDOUT,
                                         env=env | {"DECISIONS_TOKEN": "s3cret", "DECISIONS_MACHINE": "quiet-mb"})
            try:
                deadline = time.time() + 20
                while time.time() < deadline and not api("GET", "/api/resumer?machine=quiet-mb")["online"]:
                    time.sleep(0.1)
                self.assertEqual(api("GET", "/api/resumer?machine=quiet-mb")["build"], build)
                self.assertFalse(os.listdir(out))
            finally:
                quiet.terminate()
                quiet.wait()
            with open(log, "w") as fh:
                resumer = subprocess.Popen([sys.executable, "-m", "decisions", "resumer", "run"], cwd=self.tmp,
                                           stdout=fh, stderr=subprocess.STDOUT, env=env | {"DECISIONS_TOKEN": "s3cret"})
            try:
                deadline = time.time() + 30
                while time.time() < deadline and open(log).read().count("watching") < 2:
                    time.sleep(0.1)
                said = open(log).read()
                self.assertIn(f"{self.base} runs build feedfeed00; updating decide and the skill", said)
                self.assertIn(f"restarting with build", said)
                self.assertEqual(said.count("watching"), 2, said)  # it came back, with the new code
                self.assertEqual(said.count("updating decide"), 1, said)
                self.assertNotEqual(api("GET", "/api/resumer?machine=mb")["build"], build)
            finally:
                resumer.terminate()
                resumer.wait()
        # The installer got the token in its environment, not on its command line, and left the resumer alone.
        self.assertEqual(open(os.path.join(out, "args")).read().strip(), "-s -- --no-resumer --no-skill")
        self.assertEqual(open(os.path.join(out, "token")).read().strip(), "s3cret")
        self.assertIn(f'SERVER="{self.base}"', open(os.path.join(out, "installer.sh")).read())

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


class FollowTest(unittest.TestCase):
    """.dash-bored/scripts/remote.py follow: the server's machine deploys origin's main when it moves, and only
    forward. Docker is mocked (roll_out); git and rsync are real."""

    def setUp(self):
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location("remote", os.path.join(ROOT, ".dash-bored/scripts/remote.py"))
        self.remote = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.remote)
        self.tmp = tempfile.mkdtemp()
        self.origin, self.clone, self.dir = (os.path.join(self.tmp, d) for d in ("origin", "clone", "server"))
        os.makedirs(os.path.join(self.origin, "decisions"))
        os.makedirs(self.dir)
        self.git(self.origin, "init", "-q", "-b", "main")
        self.first = self.push("one")
        self.git(self.tmp, "clone", "-q", self.origin, self.clone)
        self.remote.ROOT = self.clone
        self.rolled = []
        self.fail = None
        def roll_out(target, head):
            self.rolled.append(head)
            if self.fail:
                raise self.fail
            with open(os.path.join(target.path, ".deployed"), "w") as fh:
                fh.write(head + " now\n")
        for patch in (mock.patch.object(self.remote, "roll_out", roll_out),
                      mock.patch.object(self.remote.os, "execve"), mock.patch("builtins.print")):
            patch.start()
            self.addCleanup(patch.stop)

    def git(self, cwd, *args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd,
                              check=True, capture_output=True, text=True).stdout.strip()

    def push(self, text, repo=None):
        """A commit on origin's main (or in `repo`). Returns its short hash."""
        repo = repo or self.origin
        for name in ("LICENSE", "decisions/code.py"):
            with open(os.path.join(repo, name), "w") as fh:
                fh.write(text)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", text)
        return self.git(repo, "rev-parse", "--short", "HEAD")

    def follow(self):
        import argparse
        return self.remote.follow(argparse.Namespace(dir=self.dir, branch="main"))["state"]

    def test_deploys_new_commits_and_only_forward(self):
        self.assertEqual(self.follow(), "healthy")  # nothing ran yet: deploy what's there
        self.assertEqual(self.rolled, [self.first])
        self.assertEqual(self.follow(), "healthy")
        self.assertEqual(self.rolled, [self.first])
        second = self.push("two")
        self.assertEqual(self.follow(), "healthy")
        self.assertEqual(self.rolled, [self.first, second])
        self.remote.os.execve.assert_called_once()  # it pulled, so it started over with the script it pulled
        self.assertEqual(self.remote.os.execve.call_args.args[2]["FOLLOW_PULLED"], "1")
        self.assertEqual(json.load(open(os.path.join(self.dir, ".follow/status.json")))["state"], "healthy")

        # Someone deployed work that isn't pushed: leave the server on it, don't take it back to main.
        elsewhere = os.path.join(self.tmp, "laptop")
        self.git(self.tmp, "clone", "-q", self.origin, elsewhere)
        unpushed = self.push("three, on the laptop", elsewhere)
        with open(os.path.join(self.dir, ".deployed"), "w") as fh:
            fh.write(unpushed + " now\n")
        self.assertEqual(self.follow(), "waiting")
        with open(os.path.join(self.origin, "README.md"), "w") as fh:
            fh.write("unrelated")
        self.git(self.origin, "add", "-A")
        self.git(self.origin, "commit", "-q", "-m", "unrelated")
        self.assertEqual(self.follow(), "waiting")
        self.assertEqual(self.rolled, [self.first, second])
        # Once main contains it, the server moves on from there.
        self.git(self.origin, "pull", "-q", "--no-rebase", "--no-edit", elsewhere, "main")
        merged = self.git(self.origin, "rev-parse", "--short", "HEAD")
        self.assertEqual(self.follow(), "healthy")
        self.assertEqual(self.rolled, [self.first, second, merged])

    def test_a_commit_that_cant_upgrade_the_database_is_tried_once_and_other_failures_later(self):
        self.fail = self.remote.CantUpgrade("no")
        self.assertEqual(self.follow(), "stuck")
        self.assertEqual(self.follow(), "stuck")
        self.assertEqual(self.rolled, [self.first])
        self.fail = SystemExit("docker isn't running")
        fixed = self.push("fixed")
        self.assertEqual(self.follow(), "error")
        self.assertEqual(self.follow(), "error")
        self.assertEqual(self.rolled, [self.first, fixed])
        self.fail = None
        self.remote.RETRY_AFTER = 0
        self.assertEqual(self.follow(), "healthy")
        self.assertEqual(self.rolled, [self.first, fixed, fixed])

    def test_shipping_keeps_what_belongs_to_the_server(self):
        for name, text in ((".env", "DECISIONS_BASE_URL=http://x:1\n"), (".deployed", "abc now\n"), ("stale.py", "")):
            with open(os.path.join(self.dir, name), "w") as fh:
                fh.write(text)
        local = self.remote.Local(self.dir)
        self.assertEqual(self.remote.ship(local), self.remote.fingerprint(self.clone))
        self.assertEqual(sorted(os.listdir(self.dir)), [".deployed", ".env", "LICENSE", "decisions"])
        self.assertEqual(local.env("DECISIONS_BASE_URL"), "http://x:1")
        self.assertEqual(local.run("exit 3", check=False), 3)


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
        # With several spaces the admin has to say which.
        err = self.call("POST", "/api/decisions", {"title": "x", "options": ["a", "b"]}, token="admin", expect=400)
        self.assertIn("default, private, work", err["error"])
        self.call("POST", "/api/goals", {"title": "x"}, token="admin", expect=400)
        self.call("POST", "/api/tokens", {"name": "laptop"}, token="admin", expect=400)
        self.assertIsNone(self.call("GET", "/api/config")["space"])
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
                             ("PATCH", f"/api/tokens/{self.tokens['id-work']}"),
                             ("DELETE", "/api/resumer?machine=laptop"),
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

    def test_moving_a_token_to_another_space(self):
        tid = self.tokens["id-work"]
        w = self.create("tok-work")
        self.call("PATCH", f"/api/tokens/{tid}", {"space": "nope"}, expect=404)
        self.call("PATCH", f"/api/tokens/{tid}", {"name": ""}, expect=400)
        self.call("PATCH", "/api/tokens/tok_nope", {"space": "private"}, expect=404)
        before = len(self.call("GET", "/api/events?limit=500")["events"])
        self.assertEqual(self.call("PATCH", f"/api/tokens/{tid}", {"space": "work"}, expect=200)["space"], "work")
        self.assertEqual(len(self.call("GET", "/api/events?limit=500")["events"]), before)  # nothing changed
        moved = self.call("PATCH", f"/api/tokens/{tid}", {"space": "private", "name": "desk"}, expect=200)
        self.assertEqual((moved["space"], moved["name"]), ("private", "desk"))
        self.assertNotIn("token", moved)
        # The same secret now sees private, and work no more.
        self.assertEqual(self.call("GET", "/api/config", token="tok-work")["space"], "private")
        self.call("GET", f"/api/decisions/{w['id']}", token="tok-work", expect=404)
        self.assertEqual(self.create("tok-work")["space"], "private")
        # Logged in both spaces' chains.
        events = [e for e in self.call("GET", "/api/events?limit=500")["events"] if e["type"] == "token.updated"]
        self.assertEqual([e["space"] for e in events], ["work", "private"])
        self.assertEqual(events[0]["data"], {"token_id": tid, "name": "desk",
                                             "changes": {"name": ["laptop", "desk"], "space": ["work", "private"]}})
        self.assertTrue(self.call("GET", "/api/events/verify")["ok"])
        counts = {sp["name"]: sp["tokens"] for sp in self.call("GET", "/api/spaces")["spaces"]}
        self.assertEqual((counts["work"], counts["private"]), (0, 2))

    def test_a_machine_opts_in_to_its_sessions_being_asked_for_feedback(self):
        tid = self.tokens["id-work"]
        beat = lambda token: self.call("POST", "/api/resumer/heartbeat", {"machine": "m"}, token=token, expect=200)
        self.assertFalse(self.call("GET", "/api/tokens")["tokens"][0]["feedback"])
        self.assertFalse(beat("tok-work")["feedback"])
        self.call("PATCH", f"/api/tokens/{tid}", {"feedback": "yes"}, expect=400)
        self.call("PATCH", f"/api/tokens/{tid}", {"feedback": True}, token="tok-work", expect=403)
        self.assertTrue(self.call("PATCH", f"/api/tokens/{tid}", {"feedback": True}, expect=200)["feedback"])
        self.assertEqual((beat("tok-work")["feedback"], beat("tok-private")["feedback"], beat("admin")["feedback"]),
                         (True, False, False))
        event = self.call("GET", "/api/events?limit=500")["events"][-1]
        self.assertEqual((event["type"], event["space"], event["data"]["changes"]),
                         ("token.updated", "work", {"feedback": [False, True]}))
        # Renaming it leaves it opted in.
        self.assertTrue(self.call("PATCH", f"/api/tokens/{tid}", {"name": "desk"}, expect=200)["feedback"])
        self.assertFalse(self.call("PATCH", f"/api/tokens/{tid}", {"feedback": False}, expect=200)["feedback"])

    def test_feedback_stays_in_its_space(self):
        session = {"session_id": "s1", "agent": "codex", "machine": "m"}
        f = self.call("POST", "/api/feedback", {"kind": "bug", "title": "x", "session": session}, token="tok-work",
                      expect=201)
        self.assertEqual(f["space"], "work")
        self.call("POST", "/api/feedback", {"kind": "bug", "title": "x"}, expect=400)  # the admin says which space
        self.call("POST", "/api/feedback", {"kind": "bug", "title": "x", "space": "work"}, token="tok-private", expect=404)
        ids = lambda token, q="": [x["id"] for x in self.call("GET", f"/api/feedback{q}", token=token)["feedback"]]
        self.assertEqual((ids("tok-work"), ids("tok-private"), ids("admin"), ids("admin", "?space=private")),
                         (["F-1"], [], ["F-1"], []))
        self.call("GET", "/api/feedback/F-1", token="tok-private", expect=404)
        self.call("PATCH", "/api/feedback/F-1", {"status": "done"}, token="tok-private", expect=404)
        # A machine only hears about its own space's sessions, and can't report on another's.
        d = self.create("tok-work", resume=session | {"session_id": "s2"})
        self.call("POST", f"/api/decisions/{d['id']}/cancel", {}, expect=200)
        from unittest import mock
        from decisions import store as st
        with mock.patch.object(st, "FEEDBACK_QUIET", 0):
            due = lambda token: [s["session_id"] for s in
                                 self.call("GET", "/api/feedback/due?machine=m", token=token)["sessions"]]
            self.assertEqual((due("tok-work"), due("tok-private")), (["s2"], []))
        ask = {"session_id": "s2", "agent": "codex", "machine": "m"}
        self.call("POST", "/api/feedback/asks", ask | {"status": "started"}, token="tok-work", expect=201)
        self.call("POST", "/api/feedback/asks", ask | {"status": "finished"}, token="tok-private", expect=404)
        self.assertEqual(self.call("GET", "/api/feedback", token="tok-private")["asks"], [])
        self.assertTrue(self.call("GET", "/api/events/verify")["ok"])
        # A space with feedback isn't empty.
        self.call("DELETE", "/api/spaces/work", expect=409)

    def test_forgetting_resumers(self):
        seen = lambda: [r["machine"] for r in self.call("GET", "/api/resumer")["resumers"]]
        self.call("POST", "/api/resumer/heartbeat", {"machine": "laptop"}, token="tok-work", expect=200)
        self.call("POST", "/api/resumer/heartbeat", {"machine": "server"}, expect=200)
        self.assertEqual(sorted(seen()), ["laptop", "server"])
        self.call("DELETE", "/api/resumer?machine=server", expect=200)
        self.call("DELETE", "/api/resumer?machine=server", expect=404)
        self.assertEqual(seen(), ["laptop"])
        # Deleting a machine's token takes its resumer off the list too.
        self.call("DELETE", f"/api/tokens/{self.tokens['id-work']}", expect=200)
        self.assertEqual(seen(), [])

    def test_space_lifecycle(self):
        self.call("POST", "/api/spaces", {"name": "work"}, expect=409)
        self.call("POST", "/api/spaces", {"name": "Not OK"}, expect=400)
        self.call("POST", "/api/spaces", {"name": "home", "deadline_days": -1}, expect=400)
        sp = self.call("PATCH", "/api/spaces/private", {"deadline_days": 30}, expect=200)
        self.assertEqual((sp["deadline_days"], sp["effective_deadline_days"]), (30, 30))
        sp = self.call("PATCH", "/api/spaces/private", {"deadline_days": None}, expect=200)
        self.assertEqual((sp["deadline_days"], sp["effective_deadline_days"]), (None, self.cfg.deadline_days))
        # Each space gets a color no other has, unless it's given one; the GUI reads them from the config.
        colors = lambda: {s["name"]: s["color"] for s in self.call("GET", "/api/config")["spaces"]}
        self.assertEqual(colors()["default"], "indigo")
        self.assertEqual(len(set(colors().values())), 3)
        self.call("POST", "/api/spaces", {"name": "home", "color": "mauve"}, expect=400)
        self.assertEqual(self.call("POST", "/api/spaces", {"name": "home", "color": "indigo"}, expect=201)["color"], "indigo")
        self.call("PATCH", "/api/spaces/home", {"color": "mauve"}, expect=400)
        self.call("PATCH", "/api/spaces/home", {"color": "slate"}, token="tok-work", expect=403)
        before = self.call("GET", "/api/events/verify")["chains"]["home"]["events"]
        sp = self.call("PATCH", "/api/spaces/home", {"color": "slate", "deadline_days": 2}, expect=200)
        self.assertEqual((sp["color"], sp["deadline_days"], colors()["home"]), ("slate", 2, "slate"))
        self.call("PATCH", "/api/spaces/home", {"color": "slate"}, expect=200)  # no change, no event
        self.assertEqual(self.call("GET", "/api/events/verify")["chains"]["home"]["events"], before + 1)
        self.call("DELETE", "/api/spaces/home", expect=200)
        self.create("tok-work")
        self.call("DELETE", "/api/spaces/work", expect=409)
        gone = self.call("DELETE", "/api/spaces/private", expect=200)
        self.assertEqual(gone["revoked_tokens"], [self.tokens["id-private"]])
        self.call("GET", "/api/decisions", token="tok-private", expect=401)
        self.assertEqual([s["name"] for s in self.call("GET", "/api/spaces")["spaces"]], ["default", "work"])
        # The space a database starts with goes like any other, once it's empty.
        goal = self.call("POST", "/api/goals", {"title": "x", "space": "default"}, expect=201)
        self.assertIn("goals", self.call("DELETE", "/api/spaces/default", expect=409)["error"])
        self.store.conn.execute("DELETE FROM goals WHERE id=?", (goal["id"],))
        self.call("DELETE", "/api/spaces/default", expect=200)
        # With one space left, the admin's new decisions and tokens go there, and it can't be deleted.
        self.assertEqual(self.call("GET", "/api/config")["space"], "work")
        self.assertEqual(self.create("admin")["space"], "work")
        self.assertEqual(self.call("POST", "/api/tokens", {"name": "laptop"}, expect=201)["space"], "work")
        self.assertIn("only space", self.call("DELETE", "/api/spaces/work", expect=409)["error"])
        self.assertTrue(self.call("GET", "/api/events/verify")["chains"]["default"]["ok"])
        # And it doesn't come back when the database is opened again.
        from decisions.store import Store
        again = Store(self.cfg)
        self.assertEqual([s["name"] for s in again.list_spaces()], ["work"])
        again.conn.close()
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


class SchemaTest(unittest.TestCase):
    """Upgrading the code keeps the data: databases are versioned and migrated when they're opened."""

    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "t.db")

    def open(self):
        from decisions.store import Store
        return Store(Config(db=self.db))

    def version(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def upgraded(self, schema, migration):
        """Patch the store to a next version with `schema` appended to SCHEMA and reached by `migration`."""
        from unittest import mock
        from decisions import store
        return mock.patch.multiple(store, SCHEMA=store.SCHEMA + schema, SCHEMA_VERSION=store.SCHEMA_VERSION + 1,
                                   MIGRATIONS=store.MIGRATIONS | {store.SCHEMA_VERSION + 1: migration})

    def test_every_version_after_the_first_has_a_migration(self):
        from decisions.store import MIGRATIONS, SCHEMA_VERSION
        self.assertEqual(sorted(MIGRATIONS), list(range(2, SCHEMA_VERSION + 1)))

    def test_a_new_database_is_at_the_current_version(self):
        from decisions.store import SCHEMA_VERSION
        self.assertIsNone(self.open().schema_was)
        self.assertEqual(self.version(), SCHEMA_VERSION)
        self.assertEqual(self.open().schema_was, SCHEMA_VERSION)

    def test_a_database_from_before_versions_is_adopted_with_its_data(self):
        from unittest import mock
        from decisions import store as st
        # Such a database has the first versioned schema, so this is as if SCHEMA were still at version 1.
        with mock.patch.multiple(st, SCHEMA_VERSION=1, MIGRATIONS={}):
            store = self.open()
            goal = store.create_goal({"title": "Keep me"}, "me")
            store.conn.execute("PRAGMA user_version=0")
            store.conn.close()
            store = self.open()
            self.assertEqual((store.schema_was, self.version()), (0, 1))
            self.assertEqual(store.get_goal(goal["id"])["title"], "Keep me")

    def test_a_migration_carries_the_data_over(self):
        from decisions.store import SCHEMA_VERSION as was
        store = self.open()
        goal = store.create_goal({"title": "Keep me"}, "me")
        store.conn.close()

        def migrate(conn):
            conn.execute("ALTER TABLE goals ADD COLUMN shouted TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE goals SET shouted = upper(title)")
            conn.execute("CREATE INDEX goals_by_shouted ON goals(shouted)")

        with self.upgraded("ALTER TABLE goals ADD COLUMN shouted TEXT NOT NULL DEFAULT '';"
                           "CREATE INDEX goals_by_shouted ON goals(shouted);", migrate):
            store = self.open()
            self.assertEqual((store.schema_was, self.version()), (was, was + 1))
            self.assertEqual(store.get_goal(goal["id"])["title"], "Keep me")
            self.assertEqual(store.conn.execute("SELECT shouted FROM goals").fetchone()[0], "KEEP ME")
            self.assertTrue(store.verify_chain()["ok"])
            store.create_goal({"title": "Still works"}, "me")
            store.conn.close()
            self.assertEqual(self.open().schema_was, was + 1)

    def test_version_2_gives_spaces_colors(self):
        from decisions import store as st
        store = self.open()
        for name in ("private", "work"):
            store.create_space({"name": name, "deadline_days": 3}, "me")
        goal = store.create_goal({"title": "Keep me", "space": "work"}, "me")
        # Back to version 1: spaces without the column, and nothing later versions added.
        store.conn.execute("ALTER TABLE spaces DROP COLUMN color")
        self.before_feedback(store.conn)
        store.conn.execute("PRAGMA user_version=1")
        store.conn.close()
        store = self.open()
        self.assertEqual((store.schema_was, self.version()), (1, st.SCHEMA_VERSION))
        spaces = {s["name"]: s for s in store.list_spaces()}
        self.assertEqual({n: s["color"] for n, s in spaces.items()}, {"default": "indigo", "private": "teal", "work": "orange"})
        self.assertEqual(spaces["work"]["deadline_days"], 3)
        self.assertEqual(store.get_goal(goal["id"])["title"], "Keep me")
        self.assertTrue(store.verify_chain()["ok"])
        fresh = sqlite3.connect(":memory:")
        fresh.executescript(st.SCHEMA)
        self.assertEqual(st.schema_differences(st.schema_structure(store.conn), st.schema_structure(fresh)), [])

    @staticmethod
    def before_feedback(conn):
        """Take a database back to before version 3."""
        conn.execute("ALTER TABLE tokens DROP COLUMN feedback")
        conn.execute("DROP TABLE feedback")
        conn.execute("DROP TABLE feedback_asks")

    def test_version_3_adds_feedback_and_leaves_machines_opted_out(self):
        from decisions import store as st
        store = self.open()
        token = store.create_token({"name": "laptop"}, "me")
        goal = store.create_goal({"title": "Keep me"}, "me")
        self.before_feedback(store.conn)
        store.conn.execute("PRAGMA user_version=2")
        store.conn.close()
        store = self.open()
        self.assertEqual((store.schema_was, self.version()), (2, st.SCHEMA_VERSION))
        kept = store.list_tokens()[0]
        self.assertEqual((kept["id"], kept["name"], kept["feedback"]), (token["id"], "laptop", False))
        self.assertEqual(store.scope_for(token["token"]).token, token["id"])
        self.assertEqual(store.get_goal(goal["id"])["title"], "Keep me")
        self.assertTrue(store.verify_chain()["ok"])
        fresh = sqlite3.connect(":memory:")
        fresh.executescript(st.SCHEMA)
        self.assertEqual(st.schema_differences(st.schema_structure(store.conn), st.schema_structure(fresh)), [])
        self.assertEqual(store.send_feedback({"kind": "bug", "title": "x"}, "agent")["id"], "F-1")
        self.assertTrue(store.update_token(token["id"], {"feedback": True}, "me")["feedback"])
        self.assertTrue(store.verify_chain()["ok"])

    def test_a_migration_that_misses_the_schema_changes_nothing(self):
        from decisions.store import SCHEMA_VERSION, SchemaError
        store = self.open()
        goal = store.create_goal({"title": "Keep me"}, "me")
        store.conn.close()

        def migrate(conn):
            conn.execute("ALTER TABLE goals ADD COLUMN shouted TEXT")
            conn.execute("DELETE FROM goals")

        with self.upgraded("ALTER TABLE goals ADD COLUMN shouted TEXT NOT NULL DEFAULT '';", migrate):
            with self.assertRaisesRegex(SchemaError, "goals"):
                self.open()
        self.assertEqual(self.version(), SCHEMA_VERSION)
        store = self.open()
        self.assertEqual(store.get_goal(goal["id"])["title"], "Keep me")

    def test_a_schema_change_without_a_migration_is_refused(self):
        from unittest import mock
        from decisions import store
        self.open().conn.close()
        with mock.patch.object(store, "SCHEMA", store.SCHEMA + "CREATE TABLE extra (id TEXT);"):
            with self.assertRaisesRegex(store.SchemaError, "no table extra"):
                self.open()

    def test_a_database_from_newer_code_is_refused(self):
        from decisions.store import SCHEMA_VERSION, SchemaError
        store = self.open()
        store.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        store.conn.close()
        with self.assertRaisesRegex(SchemaError, "newer version"):
            self.open()
        self.assertEqual(self.version(), SCHEMA_VERSION + 1)

    def test_decide_migrate_upgrades_without_serving(self):
        run = lambda: subprocess.run([sys.executable, "-m", "decisions.cli", "migrate", "--db", self.db],
                                     capture_output=True, text=True, cwd=ROOT)
        self.assertIn("created at schema version", run().stdout)
        self.assertIn("already at schema version", run().stdout)
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE extra (id TEXT)")
        conn.close()
        res = run()
        self.assertEqual(res.returncode, 1)
        self.assertIn("unknown table extra", res.stderr)


if __name__ == "__main__":
    unittest.main()
