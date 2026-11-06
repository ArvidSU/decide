"""`decide` command line: run the server, and give agents create / wait / listen primitives.

Client commands find the server from DECISIONS_URL / DECISIONS_TOKEN, else from
~/.config/decisions/client.toml (written by `decide connect`), else http://127.0.0.1:8787.
DECISIONS_ACTOR names who is acting (default: the agent `decide` runs under, if recognized).
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import agents
from .config import load_config
from .store import event_matches
from .webhooks import verify

EXIT_TIMEOUT = 124


CLIENT_CONFIG = os.path.expanduser(os.environ.get("DECISIONS_CLIENT_CONFIG", "~/.config/decisions/client.toml"))


def _client_config() -> dict:
    try:
        with open(CLIENT_CONFIG, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def machine_name() -> str:
    """Identifies this machine, so a decision is resumed where its session lives."""
    import socket
    return os.environ.get("DECISIONS_MACHINE") or socket.gethostname()


class Client:
    def __init__(self, url=None, token=None, actor=None):
        file = _client_config()
        self.url = (url or os.environ.get("DECISIONS_URL") or file.get("url") or "http://127.0.0.1:8787").rstrip("/")
        self.token = token if token is not None else os.environ.get("DECISIONS_TOKEN", file.get("token", ""))
        self.actor = actor or os.environ.get("DECISIONS_ACTOR") or agents.detect_agent() or ""

    def call(self, method, path, body=None, timeout=30, raw=False):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.actor:
            headers["X-Actor"] = self.actor
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode()
        except urllib.error.HTTPError as exc:
            msg = exc.read().decode()
            try:
                msg = json.loads(msg)["error"]
            except Exception:
                pass
            if exc.code == 401:
                msg += " (run `decide connect <url> --token <token>`, or set DECISIONS_TOKEN)"
            sys.exit(f"error: {exc.code} {msg}")
        except urllib.error.URLError as exc:
            sys.exit(f"error: cannot reach {self.url} ({exc.reason}). Is `decide serve` running?")
        except (http.client.HTTPException, ConnectionError, TimeoutError) as exc:  # e.g. server restarted mid-request
            sys.exit(f"error: lost connection to {self.url} ({type(exc).__name__}: {exc})")
        return text if raw else json.loads(text)


def _print(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _until(iso: str) -> str:
    """'in 3d 4h' / '2h ago' for an ISO timestamp."""
    try:
        s = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() - time.time()
    except ValueError:
        return iso
    a = abs(s)
    text = (f"{int(a // 86400)}d {int(a % 86400 // 3600)}h" if a >= 86400 else
            f"{int(a // 3600)}h {int(a % 3600 // 60)}m" if a >= 3600 else f"{int(a // 60)}m")
    return f"in {text}" if s >= 0 else f"{text} ago"


def _summary(d: dict) -> str:
    by_id = {o["id"]: o for o in d["options"]}
    lines = [f"{d['id']}  [{d['status']}]  {d['title']}", f"  {d['url']}"]
    for x in d.get("depends_on", []):
        cond = f", only applies if it picks {' or '.join(x['when'])}" if x["when"] else ""
        state = {"waiting": "locked until it's decided", "met": "settled", "unmet": "doesn't apply"}[x["state"]]
        lines.append(f"  depends on {x['decision']} [{x['status']}] {x['title']}{cond}: {state}")
    for x in d.get("affects", []):
        cond = f" (only if this picks {' or '.join(x['when'])})" if x["when"] else ""
        lines.append(f"  affects {x['decision']} [{x['status']}] {x['title']}{cond}")
    if d.get("outdated_by"):
        lines.append(f"  outdated: {', '.join(d['outdated_by'])} changed after this was decided")
    if d.get("waiting_on"):
        lines.append(f"  waiting on {', '.join(d['waiting_on'])}: it can't be decided or expire until then")
    elif d.get("due_at") and d["status"] == "open":
        lines.append(f"  expires {_until(d['due_at'])} ({d['due_at']}) unless decided")
    elif d["status"] == "expired":
        lines.append(f"  expired {_until(d['due_at']) if d.get('due_at') else ''}: nobody decided before the deadline")
    if d.get("resolution"):
        r = d["resolution"]
        chosen = ", ".join(f"{s}: {by_id[s]['label']}" if s in by_id else s for s in r["selected"])
        lines.append(f"  decided: {chosen}" + (f"  — {r['note']}" if r.get("note") else ""))
    for key, p in d.get("pending_attribute_requests", {}).items():
        lines.append(f"  attribute requested: {key!r} ({p['description']}) missing on options {', '.join(p['missing'])}")
        lines.append(f"    -> fill with: decide fill {d['id']}  (stdin: [{{\"id\": ..., \"attributes\": {{\"{key}\": ...}}}}])")
    return "\n".join(lines)


# ---------------------------------------------------------------------------- commands

def cmd_serve(args):
    from .server import serve
    serve(load_config(args.config, {"host": args.host, "port": args.port, "db": args.db, "token": args.token}),
          insecure=args.insecure)


def cmd_connect(args):
    """Point this machine's `decide` at a server and remember it."""
    url = args.url.rstrip("/")
    if "://" not in url:
        url = "http://" + url
    cfg = Client(url=url, token=args.token or "").call("GET", "/api/config")
    os.makedirs(os.path.dirname(CLIENT_CONFIG), exist_ok=True)
    fd = os.open(CLIENT_CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(f"# written by `decide connect`\nurl = {json.dumps(url)}\n")
        if args.token:
            fh.write(f"token = {json.dumps(args.token)}\n")
    space = "all spaces" if cfg["admin"] else f"space {cfg['space']}"
    print(f"connected to {url} (server {cfg.get('version', '?')}, auth {'on' if cfg['auth'] else 'off'}, {space}); "
          f"saved to {CLIENT_CONFIG}")
    if os.environ.get("DECISIONS_URL") and os.environ["DECISIONS_URL"].rstrip("/") != url:
        print(f"note: DECISIONS_URL={os.environ['DECISIONS_URL']} is set and takes precedence", file=sys.stderr)


def _skill_dirs(args) -> list[str]:
    """Where to install the skill: --dir, the --agent(s) named, or every agent found on this machine
    plus the shared ~/.agents/skills that most other agents read."""
    import shutil
    if args.dir:
        return [args.dir]
    if args.agent:
        unknown = [a for a in args.agent if a not in agents.AGENTS]
        if unknown:
            sys.exit(f"error: unknown agent {', '.join(unknown)} (known: {', '.join(agents.AGENTS)}); use --dir")
        roots = [agents.AGENTS[a].skills_dir for a in args.agent]
    else:
        roots = [a.skills_dir for a in agents.AGENTS.values()
                 if shutil.which(a.binary) or os.path.isdir(os.path.dirname(os.path.expanduser(a.skills_dir)))]
        roots.append(agents.SHARED_SKILLS_DIR)
    return [os.path.join(r, "decisions") for r in dict.fromkeys(roots)]


def cmd_skill_install(args):
    """Install the skill that teaches agents to use decisions."""
    from .dist import SKILL
    for dest in _skill_dirs(args):
        dest = os.path.expanduser(dest)
        target = os.path.join(dest, "SKILL.md")
        if os.path.islink(target) and not args.force:
            print(f"{target} is a symlink to {os.path.realpath(target)}; leaving it (use --force to replace)")
            continue
        os.makedirs(dest, exist_ok=True)
        if os.path.lexists(target):
            os.remove(target)
        with open(target, "w") as fh:
            fh.write(SKILL.read_text())
        print(f"installed skill: {target}")


def cmd_skill_show(args):
    from .dist import SKILL
    print(SKILL.read_text(), end="")


def cmd_create(args):
    if args.json:
        raw = sys.stdin.read() if args.json == "-" else open(args.json).read()
        body = json.loads(raw)
    else:
        if not args.title:
            sys.exit("error: --title (or --json) is required")
        body = {"title": args.title}
    if args.title:
        body["title"] = args.title
    if args.context:
        body["context"] = args.context
    if args.option:
        body.setdefault("options", []).extend({"label": o} for o in args.option)
    if args.multi:
        body["max_select"] = None
    if args.source:
        body["source"] = args.source
    if args.space or os.environ.get("DECISIONS_SPACE"):
        body["space"] = args.space or os.environ["DECISIONS_SPACE"]
    if args.webhook:
        body.setdefault("webhooks", []).append({"url": args.webhook, **({"require_ack": True} if args.require_ack else {})})
    if args.meta:
        body.setdefault("metadata", {}).update(dict(m.split("=", 1) for m in args.meta))
    if args.due:
        body["due_at"] = _parse_due(args.due)
    for dep in args.depends_on or []:
        up, _, when = dep.partition("=")
        body.setdefault("depends_on", []).append({"decision": up, **({"when": when.split(",")} if when else {})})
    # Inside an agent session, remember how to resume it once the decision is made.
    detected = (args.agent or agents.detect_agent() or "agent", args.session) if args.session \
        else agents.detect_session()
    if detected and "resume" not in body and not args.no_resume:
        body["resume"] = {"session_id": detected[1], "cwd": os.getcwd(), "agent": detected[0],
                          "machine": machine_name()}
    d = Client().call("POST", "/api/decisions", body)
    if args.quiet:
        print(d["id"])
    else:
        _print(d)


def _parse_due(raw: str) -> str:
    """'36h', '2d', '90m', '1w' from now, or an ISO timestamp as is."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([mhdw])\s*", raw)
    if not m:
        return raw
    seconds = float(m.group(1)) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    return datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def cmd_get(args):
    c = Client()
    if args.markdown:
        print(c.call("GET", f"/api/decisions/{args.id}/markdown", raw=True), end="")
    elif args.json:
        _print(c.call("GET", f"/api/decisions/{args.id}"))
    else:
        print(_summary(c.call("GET", f"/api/decisions/{args.id}")))


def cmd_list(args):
    q = f"status={args.status}&limit={args.limit}" + (f"&space={args.space}" if args.space else "")
    decisions = Client().call("GET", f"/api/decisions?{q}")["decisions"]
    # Show which space each is in only when the token sees more than one.
    show_space = len({d["space"] for d in decisions}) > 1
    for d in decisions:
        pending = " (attributes requested)" if d["pending_attribute_requests"] else ""
        due = (f"  [waiting on {', '.join(d['waiting_on'])}]" if d.get("waiting_on") else
               f"  [expires {_until(d['due_at'])}]" if d.get("due_at") and d["status"] == "open" else "")
        if d.get("outdated_by"):
            due += f"  [outdated by {', '.join(d['outdated_by'])}]"
        space = f"{d['space']:<10} " if show_space else ""
        print(f"{d['id']:>7}  {space}{d['status']:<9} {d['title']}{pending}{due}")


def cmd_wait(args):
    """Block until the decision resolves (or another chosen event happens).

    Exit 0 whenever a watched event happened (the first output line names it), 124 on timeout,
    1 on error. Ends with the exact command to keep waiting without re-waking on this event.
    """
    c = Client()
    types = args.on.split(",")
    deadline = time.monotonic() + args.timeout if args.timeout else None
    after = args.after
    while True:
        chunk = 60.0 if deadline is None else max(0.0, min(60.0, deadline - time.monotonic()))
        q = f"types={','.join(types)}&timeout={chunk}" + (f"&after={after}" if after is not None else "")
        res = c.call("GET", f"/api/decisions/{args.id}/wait?{q}", timeout=chunk + 30)
        if not res["timed_out"]:
            break
        after = res["cursor"]
        if deadline is not None and time.monotonic() >= deadline:
            if not args.quiet:
                print(f"timed out waiting for {args.id}", file=sys.stderr)
            sys.exit(EXIT_TIMEOUT)
    etype = res["event"]["type"] if res["event"] else res["decision"]["status"]
    d = res["decision"]
    if args.json:
        _print(res)
    elif not args.quiet:
        print(f"event: {etype}")
        print(_summary(d))
        if d["status"] == "open":
            print(f"\nto keep waiting: decide wait {d['id']} --on {args.on} --after {res['cursor']}")
    if args.ack:
        c.call("POST", f"/api/decisions/{args.id}/ack")
    sys.exit(0)


def _payload_env(payload: dict, payload_file: str) -> dict:
    d = payload.get("decision") or {}
    by_id = {o["id"]: o for o in d.get("options", [])}
    res = d.get("resolution") or {}
    env = {
        "DECISION_EVENT": payload["event"]["type"],
        "DECISION_DELIVERY_ID": payload["delivery"]["id"],
        "DECISION_ID": d.get("id", ""),
        "DECISION_TITLE": d.get("title", ""),
        "DECISION_STATUS": d.get("status", ""),
        "DECISION_URL": d.get("url", ""),
        "DECISION_SELECTED": ",".join(res.get("selected", [])),
        "DECISION_SELECTED_LABELS": "; ".join(by_id[s]["label"] for s in res.get("selected", []) if s in by_id),
        "DECISION_NOTE": res.get("note") or "",
        "DECISION_PAYLOAD_FILE": payload_file,
        "DECISION_JSON": json.dumps(payload, ensure_ascii=False),
    }
    for k, v in (d.get("metadata") or {}).items():
        if isinstance(v, (str, int, float, bool)):
            env["DECISION_META_" + "".join(ch if ch.isalnum() else "_" for ch in k).upper()] = str(v)
    return env


def _receive(host, port, secret, events, exec_cmd, once, on_ready=None, emit=None):
    """Run a webhook receiver until stopped (or, with once, until one event is handled).

    For each matching event: run exec_cmd (decision data only in DECISION_* env vars, never
    interpolated into the command string) or call emit(payload); then, if that succeeded and the
    delivery requires it, ack it. A failed command is not acked, so the next re-fire retries it.
    Returns the last successfully handled payload.
    """
    done: dict[str, bool] = {}
    done_lock = threading.Lock()
    finished = threading.Event()
    result: dict = {}
    client = Client()
    emit = emit or (lambda payload: print(json.dumps(payload, ensure_ascii=False), flush=True))

    def handle(payload: dict):
        delivery = payload["delivery"]
        with done_lock:
            if delivery["id"] in done:
                # Re-fired delivery we already handled: just ack again.
                if done[delivery["id"]] and delivery.get("require_ack"):
                    client.call("POST", f"/api/deliveries/{delivery['id']}/ack")
                return
            if once and finished.is_set():
                return
            done[delivery["id"]] = False
        ok = True
        if exec_cmd:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            proc = subprocess.run(exec_cmd, shell=True, env=os.environ | _payload_env(payload, fh.name))
            ok = proc.returncode == 0
            print(f"[decide] {payload['event']['type']} {delivery['id']}: command exited {proc.returncode}",
                  file=sys.stderr, flush=True)
        else:
            emit(payload)
        with done_lock:
            if ok:
                done[delivery["id"]] = True
            else:
                del done[delivery["id"]]
        if ok and delivery.get("require_ack"):
            client.call("POST", f"/api/deliveries/{delivery['id']}/ack")
        if ok:
            result["payload"] = payload
            if once:
                finished.set()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if secret and not verify(secret, body, self.headers.get("X-Decisions-Signature")):
                self.send_response(401)
                self.end_headers()
                return
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            if not events or any(event_matches([e], payload["event"]["type"]) for e in events):
                threading.Thread(target=handle, args=(payload,), daemon=True).start()

    httpd = ThreadingHTTPServer((host, port), H)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        if on_ready:
            on_ready(httpd.server_port)
        finished.wait()
        time.sleep(0.2)  # let the final ack/response flush
    finally:
        httpd.shutdown()
    return result.get("payload")


def cmd_listen(args):
    """Long-running receiver for webhooks you registered yourself (e.g. a session-resume daemon)."""
    secret = args.secret or os.environ.get("DECISIONS_WEBHOOK_SECRET", "")
    ready = lambda port: print(f"[listen] receiving webhooks on http://{args.host}:{port}/", file=sys.stderr, flush=True)
    try:
        _receive(args.host, args.port, secret, [e for e in args.events.split(",") if e],
                 args.exec, args.once, on_ready=ready)
    except KeyboardInterrupt:
        pass


def _is_local(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


def _local_ip_towards(host: str) -> str:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((socket.gethostbyname(host), 9))  # UDP: no packet is sent, just picks the route
        return sock.getsockname()[0]


def cmd_watch(args):
    """Register a throwaway webhook on one decision, wait for it, act, clean up.

    Exits 0 once an event is handled. If --exec fails, the delivery is not acked and the
    command is retried on the next re-fire.
    """
    client = Client()
    secret = secrets.token_hex(16)  # in memory only
    events = args.on.split(",")
    sub: dict = {}
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    listen_host, callback_host = args.host or "127.0.0.1", args.host or "127.0.0.1"
    server_host = urllib.parse.urlparse(client.url).hostname or "127.0.0.1"
    if not args.host and not args.callback_url and not _is_local(server_host):
        # The server is on another machine: listen on all interfaces and give it the address
        # this machine uses to reach it. (Needs that machine to be able to connect back to us.)
        listen_host, callback_host = "0.0.0.0", _local_ip_towards(server_host)
    elif not args.host and not args.callback_url:
        # Same machine, but the server may be in a container, where 127.0.0.1 is the container.
        # It then says how it reaches us (e.g. host.docker.internal), and we listen beyond loopback
        # (on Linux that name is the Docker bridge, not 127.0.0.1). Deliveries are signed either way.
        via = client.call("GET", "/api/config").get("callback_host")
        if via:
            listen_host, callback_host = "0.0.0.0", via

    def register(port):
        url = args.callback_url or f"http://{callback_host}:{port}/"
        sub.update(client.call("POST", f"/api/decisions/{args.id}/webhooks", {
            "url": url, "secret": secret, "events": events, "require_ack": True,
            "description": f"decide watch (pid {os.getpid()})",
        }))
        print(f"[watch] {args.id}: webhook {sub['id']} -> {url}, waiting for {', '.join(events)}",
              file=sys.stderr, flush=True)

    def emit(payload):
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
        else:
            replay = " (already decided before watch started)" if payload["delivery"].get("replayed") else ""
            print(f"event: {payload['event']['type']}{replay}")
            print(_summary(payload["decision"]), flush=True)

    try:
        payload = _receive(listen_host, args.port, secret, events, args.exec, not args.forever,
                           on_ready=register, emit=emit)
    except KeyboardInterrupt:
        payload = None
    finally:
        if sub and not args.keep:
            client.call("DELETE", f"/api/webhooks/{sub['id']}")
    sys.exit(130 if payload is None else 0)


def cmd_fill(args):
    """Bulk-patch options from JSON: [{"id": "1", "attributes": {...}}, ...]."""
    raw = sys.stdin.read() if args.json == "-" else open(args.json).read()
    d = Client().call("PATCH", f"/api/decisions/{args.id}/options", json.loads(raw))
    print(_summary(d))


# ---------------------------------------------------------------------------- resumer

STATE_DIR = os.path.expanduser(os.environ.get("DECISIONS_STATE_DIR", "~/.local/state/decisions"))
RESUMER_SERVICE = "com.decisions.resumer"
SERVER_SERVICE = "com.decisions.server"


def _resume_prompt(d: dict) -> str:
    instruction = d["resume"].get("prompt") or "Continue the task that was waiting on this decision."
    if d["status"] == "expired":
        return (f'[decisions] {d["id"]} "{d["title"]}" expired: the user didn\'t decide before its deadline '
                f'({d.get("due_at")}), so it was closed unanswered. Treat it as not important enough to block on: '
                f'continue without it, using your own best judgement where the task needs an answer (or drop '
                f'that part), and say in your final message what you chose and why. {instruction} '
                f'Full details: `decide get {d["id"]} --json`.')
    r = d["resolution"]
    labels = "; ".join(o["label"] for o in d["selected_options"]) or "(nothing)"
    return (f'[decisions] {d["id"]} "{d["title"]}" was resolved by {r["resolved_by"]}: {labels}. '
            f'Note: {r.get("note") or "(none)"}. {instruction} '
            f'Full details: `decide get {d["id"]} --json`.')


def _agent_commands(args) -> dict[str, str]:
    """--agent-command NAME=CMD flags as {agent: command}."""
    out = {}
    for item in args.agent_command or []:
        name, sep, cmd = item.partition("=")
        if not sep or not name or not cmd:
            sys.exit(f"error: --agent-command expects NAME=COMMAND, got {item!r}")
        out[name] = cmd
    return out


def _resume_one(client: Client, d: dict, command: str | None, log_dir: str):
    state = d["resume_state"]
    seq = state["trigger_seq"]
    cwd = d["resume"].get("cwd") or os.path.expanduser("~")
    agent = d["resume"]["agent"]
    error = (f"cwd does not exist: {cwd}" if not os.path.isdir(cwd) else
             f"no resume command for agent {agent!r}; give the resumer --agent-command {agent}=..."
             if not command else None)
    if error:
        client.call("POST", f"/api/decisions/{d['id']}/resume-events",
                    {"status": "failed", "trigger_seq": seq, "error": error})
        print(f"[resumer] {d['id']}: {error}", flush=True)
        return
    log = os.path.join(log_dir, f"{d['id']}-{seq}.log")
    payload = {"event": {"type": state.get("trigger", "decision.resolved"), "seq": seq}, "delivery": {"id": ""},
               "decision": d}
    env = os.environ | _payload_env(payload, "") | {
        "RESUME_SESSION_ID": d["resume"]["session_id"], "RESUME_CWD": cwd, "RESUME_AGENT": agent,
        "RESUME_PROMPT": _resume_prompt(d)}
    with open(log, "w") as fh:
        proc = subprocess.Popen(command, shell=True, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        client.call("POST", f"/api/decisions/{d['id']}/resume-events",
                    {"status": "started", "trigger_seq": seq, "pid": proc.pid, "log": log})
        print(f"[resumer] {d['id']}: resuming {agent} session {d['resume']['session_id']} (pid {proc.pid}, log {log})",
              flush=True)
        code = proc.wait()
    with open(log) as fh:
        tail = fh.read()[-1500:]
    client.call("POST", f"/api/decisions/{d['id']}/resume-events",
                {"status": "finished" if code == 0 else "failed", "trigger_seq": seq,
                 "exit_code": code, "log": log, "output_tail": tail})
    print(f"[resumer] {d['id']}: exited {code}", flush=True)


def cmd_resumer_run(args):
    """Long-running: resume the requesting agent session whenever a decision with a resume target
    is resolved, or expires undecided (unless --no-expired). Pulls the event feed with a persisted cursor, so nothing is missed while it (or the
    machine) is down; heartbeats so the GUI can warn you when it isn't running."""
    client = Client(actor=os.environ.get("DECISIONS_ACTOR") or "resumer")
    me = machine_name()
    os.makedirs(STATE_DIR, exist_ok=True)
    log_dir = os.path.join(STATE_DIR, "resume-logs")
    os.makedirs(log_dir, exist_ok=True)
    cursor_file = os.path.join(STATE_DIR, "resumer.cursor")
    max_age = args.max_age_days * 86400
    try:
        cursor = int(open(cursor_file).read())
    except (OSError, ValueError):
        cursor = client.call("GET", "/api/config")["cursor"]  # first run: only new decisions
        with open(cursor_file, "w") as fh:  # so decisions resolved while we're down are caught up later
            fh.write(str(cursor))
    overrides = _agent_commands(args)
    described = args.command or "; ".join(f"{a}: {agents.resume_command(a, overrides)}"
                                          for a in dict.fromkeys([*agents.AGENTS, *overrides]))
    types = "decision.resolved" + ("" if args.no_expired else ",decision.expired")
    print(f"[resumer] {me}: watching {client.url} from event {cursor} for {types}; commands: {described}", flush=True)
    last_beat = 0.0
    while True:
        if time.monotonic() - last_beat > 30:
            try:
                client.call("POST", "/api/resumer/heartbeat",
                            {"pid": os.getpid(), "machine": me, "command": described})
                last_beat = time.monotonic()
            except SystemExit as exc:  # server down: keep trying
                print(f"[resumer] {exc}", flush=True)
                time.sleep(10)
                continue
        try:
            res = client.call("GET", f"/api/events?after={cursor}&wait=25&types={types}", timeout=60)
        except SystemExit as exc:
            print(f"[resumer] {exc}", flush=True)
            time.sleep(10)
            continue
        for ev in res["events"]:
            d = client.call("GET", f"/api/decisions/{ev['decision_id']}")
            state = d.get("resume_state") or {}
            target = d.get("resume") or {}
            # Sessions live on one machine; leave other machines' decisions to their own resumer.
            mine = target.get("machine") == me
            age = time.time() - datetime.fromisoformat(ev["ts"].replace("Z", "+00:00")).timestamp()
            # Only the latest resolution, only if nobody (e.g. you, manually) already resumed it.
            if mine and state.get("status") == "pending" and state.get("trigger_seq") == ev["seq"]:
                if age > max_age:
                    print(f"[resumer] {d['id']}: skipping, {d['status']} {age / 86400:.1f} days ago", flush=True)
                else:
                    command = args.command or agents.resume_command(target["agent"], overrides)
                    threading.Thread(target=_resume_one, args=(client, d, command, log_dir),
                                     daemon=True).start()
            cursor = ev["seq"]
            with open(cursor_file, "w") as fh:
                fh.write(str(cursor))
        if not res["events"] and res["cursor"] > cursor:
            cursor = res["cursor"]


# ---------------------------------------------------------------------------- background services
# launchd (macOS) or systemd --user (Linux): start at login / boot, restart if it dies.

def _plist_path(label):
    return os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")


def _unit_name(label):
    return label.removeprefix("com.").replace(".", "-") + ".service"


def _unit_path(label):
    return os.path.expanduser(f"~/.config/systemd/user/{_unit_name(label)}")


def _service_path(label):
    return _plist_path(label) if sys.platform == "darwin" else _unit_path(label)


def _install_service(label: str, argv: list[str], env: dict, log: str):
    import plistlib
    import shlex
    if sys.platform == "darwin":
        plist = {"Label": label, "ProgramArguments": argv, "EnvironmentVariables": env, "RunAtLoad": True,
                 "KeepAlive": True, "StandardOutPath": log, "StandardErrorPath": log}
        os.makedirs(os.path.dirname(_plist_path(label)), exist_ok=True)
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
        with open(_plist_path(label), "wb") as fh:
            plistlib.dump(plist, fh)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", _plist_path(label)], check=True)
    elif sys.platform.startswith("linux"):
        unit = "\n".join([
            "[Unit]", f"Description=decisions ({label})", "After=network-online.target", "",
            "[Service]", f"ExecStart={shlex.join(argv)}",
            *(f"Environment={shlex.quote(f'{k}={v}')}" for k, v in env.items()),
            "Restart=always", "RestartSec=5", f"StandardOutput=append:{log}", f"StandardError=append:{log}", "",
            "[Install]", "WantedBy=default.target", ""])
        os.makedirs(os.path.dirname(_unit_path(label)), exist_ok=True)
        with open(_unit_path(label), "w") as fh:
            fh.write(unit)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", _unit_name(label)], check=True)
        subprocess.run(["systemctl", "--user", "restart", _unit_name(label)], check=True)
        print("note: to keep it running while you're logged out: sudo loginctl enable-linger $USER")
    else:
        sys.exit(f"error: don't know how to install a background service on {sys.platform}; "
                 f"run `{shlex.join(argv)}` yourself")
    print(f"installed {_service_path(label)}; logs: {log}")


def _uninstall_service(label: str):
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
    elif sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "--user", "disable", "--now", _unit_name(label)], capture_output=True)
    if os.path.exists(_service_path(label)):
        os.remove(_service_path(label))
    print(f"uninstalled {label}")


def _service_env(*tools: str | None) -> dict:
    """PATH for the service, plus explicitly set DECISIONS_* env (the client config file covers the rest)."""
    dirs = list(dict.fromkeys(os.path.dirname(t) for t in tools if t))
    dirs += ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"]
    env = {"PATH": ":".join(dict.fromkeys(dirs))}
    for k in ("DECISIONS_URL", "DECISIONS_TOKEN", "DECISIONS_MACHINE", "DECISIONS_CONFIG", "DECISIONS_CLIENT_CONFIG"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _which_decide() -> str:
    import shutil
    decide = shutil.which("decide")
    if not decide:
        sys.exit("error: `decide` is not on PATH (install it first, e.g. `uv tool install --editable .`)")
    return decide


def cmd_resumer_install(args):
    import shutil
    decide = _which_decide()
    found = {a.name: shutil.which(a.binary) for a in agents.AGENTS.values()}
    if not args.command and not any(found.values()) and not args.agent_command:
        print(f"warning: no known agent ({', '.join(a.binary for a in agents.AGENTS.values())}) found on PATH; "
              "resuming will fail until one is installed (or pass --agent-command)", file=sys.stderr)
    Client().call("GET", "/api/config")  # fail now, not in the background, if the server is unreachable
    os.makedirs(STATE_DIR, exist_ok=True)
    argv = [decide, "resumer", "run", *(["--command", args.command] if args.command else []),
            *(["--no-expired"] if args.no_expired else [])]
    for item in args.agent_command or []:
        argv += ["--agent-command", item]
    _install_service(RESUMER_SERVICE, argv, _service_env(decide, *found.values()),
                     os.path.join(STATE_DIR, "resumer.log"))


def cmd_resumer_uninstall(args):
    _uninstall_service(RESUMER_SERVICE)


def cmd_resumer_status(args):
    me = machine_name()
    st = Client().call("GET", f"/api/resumer?machine={urllib.parse.quote(me)}")
    if st["online"]:
        print(f"online on this machine ({me}): pid {st['pid']}, last seen {st['last_seen']}")
    else:
        print(f"OFFLINE on this machine ({me}) (last seen: {st['last_seen'] or 'never since server start'})")
    for r in st["resumers"]:
        if r["machine"] != me:
            print(f"  other machine {r['machine']}: {'online' if r['online'] else 'offline'}, last seen {r['last_seen']}")
    path = _service_path(RESUMER_SERVICE)
    print(f"service: {'installed' if os.path.exists(path) else 'not installed'} ({path})")
    sys.exit(0 if st["online"] else 1)


def cmd_server_install(args):
    """Run the server as a background service on this machine. With --host 0.0.0.0, it's reachable
    from other machines, and a token is generated if the config doesn't have one."""
    from .config import is_loopback
    decide = _which_decide()
    path = os.path.expanduser(args.config or os.environ.get("DECISIONS_CONFIG") or "~/.config/decisions/config.toml")
    cfg = load_config(path if os.path.exists(path) else None, {"host": args.host, "port": args.port, "db": args.db})
    if not os.path.exists(path):
        token = cfg.token or (secrets.token_urlsafe(24) if not is_loopback(cfg.host) else "")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(f"# written by `decide server install`\nhost = {json.dumps(cfg.host)}\nport = {cfg.port}\n"
                     f"db = {json.dumps(cfg.db)}\ntoken = {json.dumps(token)}\n"
                     f"base_url = {json.dumps(args.base_url or '')}\n")
        cfg.token = token
        print(f"wrote {path}")
    elif args.host or args.port or args.db or args.base_url:
        sys.exit(f"error: {path} exists; edit host/port/db/base_url there instead of passing flags")
    if not is_loopback(cfg.host) and not cfg.token:
        sys.exit(f"error: {path} has host = {cfg.host!r} but no token; add `token = \"...\"` "
                 f"(e.g. {secrets.token_urlsafe(24)})")
    os.makedirs(STATE_DIR, exist_ok=True)
    _install_service(SERVER_SERVICE, [decide, "serve", "--config", path], _service_env(decide),
                     os.path.join(STATE_DIR, "server.log"))
    url = cfg.public_url
    print(f"\nGUI: {url}" + (f"/#token={cfg.token}" if cfg.token else "") + "  (this link holds the admin token)")
    if not is_loopback(cfg.host):
        print("set up another machine: in the GUI, Settings → Machines → Connect a machine. It creates a token for "
              "one space and the command that installs decide, the skill and the resumer with it.")


def cmd_server_uninstall(args):
    _uninstall_service(SERVER_SERVICE)


def cmd_ack(args):
    _print(Client().call("POST", f"/api/decisions/{args.id}/ack"))


def cmd_verify(args):
    res = Client().call("GET", "/api/events/verify")
    _print(res)
    sys.exit(0 if res["ok"] else 1)


def cmd_space_list(args):
    for sp in Client().call("GET", "/api/spaces")["spaces"]:
        days = f"{sp['effective_deadline_days']:g}d" + ("" if sp["deadline_days"] is not None else " (server default)")
        print(f"{sp['name']:<16} {sp['open']:>3} open / {sp['decisions']:<4} deadline {days:<22} "
              f"{sp['tokens']} token{'' if sp['tokens'] == 1 else 's'}")


def cmd_space_create(args):
    body = {"name": args.name} | ({"deadline_days": args.deadline_days} if args.deadline_days is not None else {})
    _print(Client().call("POST", "/api/spaces", body))


def cmd_space_set(args):
    days = None if args.deadline_days == "default" else float(args.deadline_days)
    _print(Client().call("PATCH", f"/api/spaces/{args.name}", {"deadline_days": days}))


def cmd_space_delete(args):
    _print(Client().call("DELETE", f"/api/spaces/{args.name}"))


def cmd_token_list(args):
    for t in Client().call("GET", "/api/tokens")["tokens"]:
        used = f"used {_until(t['last_used_at'])}" if t["last_used_at"] else "never used"
        resumer = f", resumer {'online' if t['resumer']['online'] else 'offline'}" if t["resumer"] else ""
        print(f"{t['id']}  {t['space']:<12} {t['name']:<24} {used}{resumer}")


def _print_new_token(c: Client, t: dict):
    install = c.call("GET", "/api/config")["install_url"]
    print(f"{t['id']} for {t['name']!r} in space {t['space']}. The token is shown only now:\n  {t['token']}\n"
          f"set up the machine (installs decide, the skill and the resumer):\n"
          f"  curl -fsSL {install} | sh -s -- --token {t['token']}\n"
          f"or, where decide is installed: decide connect {c.url} --token {t['token']}")


def cmd_token_create(args):
    c = Client()
    _print_new_token(c, c.call("POST", "/api/tokens", {"name": args.name, "space": args.space}))


def cmd_token_roll(args):
    c = Client()
    _print_new_token(c, c.call("POST", f"/api/tokens/{args.id}/roll"))


def cmd_token_revoke(args):
    _print(Client().call("DELETE", f"/api/tokens/{args.id}"))


def main(argv=None):
    from . import __version__
    p = argparse.ArgumentParser(prog="decide", description="General-purpose decision log.")
    p.add_argument("--version", action="version", version=f"decide {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the GUI + API server")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--db")
    s.add_argument("--token")
    s.add_argument("--config")
    s.add_argument("--insecure", action="store_true", help="allow listening on the network without a token")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("server", help="run the server as a background service (launchd / systemd --user)")
    ssub = s.add_subparsers(dest="server_cmd", required=True)
    r = ssub.add_parser("install", help="install and start; prints how to set up other machines")
    r.add_argument("--host", help="e.g. 0.0.0.0 to accept other machines (a token is generated)")
    r.add_argument("--port", type=int)
    r.add_argument("--db")
    r.add_argument("--base-url", help="URL other machines use, if not http://<hostname>:<port>")
    r.add_argument("--config", help="config file (default ~/.config/decisions/config.toml; created if missing)")
    r.set_defaults(fn=cmd_server_install)
    r = ssub.add_parser("uninstall", help="stop and remove the service")
    r.set_defaults(fn=cmd_server_uninstall)

    s = sub.add_parser("connect", help="point this machine's decide (and resumer) at a server")
    s.add_argument("url", help="e.g. http://homeserver:8787")
    s.add_argument("--token")
    s.set_defaults(fn=cmd_connect)

    s = sub.add_parser("skill", help="install the agent skill (SKILL.md) that teaches agents to use decisions")
    ksub = s.add_subparsers(dest="skill_cmd", required=True)
    r = ksub.add_parser("install", help="write <skills dir>/decisions/SKILL.md for each agent on this machine "
                                        f"and {agents.SHARED_SKILLS_DIR}")
    r.add_argument("--agent", action="append", help=f"only this agent ({', '.join(agents.AGENTS)}); repeatable")
    r.add_argument("--dir", help="install into exactly this directory instead")
    r.add_argument("--force", action="store_true", help="replace a symlinked skill")
    r.set_defaults(fn=cmd_skill_install)
    r = ksub.add_parser("show", help="print the skill, e.g. to paste into an agent's instructions")
    r.set_defaults(fn=cmd_skill_show)

    s = sub.add_parser("create", help="create a decision (flags, or full JSON body via --json FILE|-)")
    s.add_argument("--title")
    s.add_argument("--context")
    s.add_argument("--option", "-o", action="append", help="option label (repeatable)")
    s.add_argument("--multi", action="store_true", help="allow selecting any number of options")
    s.add_argument("--source")
    s.add_argument("--space", help="space to create it in (default: the token's space, or DECISIONS_SPACE)")
    s.add_argument("--webhook", help="webhook URL to notify")
    s.add_argument("--require-ack", action="store_true", help="re-fire webhook until acked")
    s.add_argument("--meta", action="append", help="metadata key=value (repeatable)")
    s.add_argument("--due", help="deadline, after which the undecided decision expires: 36h, 2d, 1w, or an ISO "
                                 "timestamp (default: the server's deadline_days, normally 4 days)")
    s.add_argument("--depends-on", action="append", metavar="D-N[=OPT,...]",
                   help="locked until decision D-N is decided; with =OPT,... it only applies if D-N picks one "
                        "of those options, and is cancelled otherwise (repeatable)")
    s.add_argument("--json", help="read the request body from a file, or - for stdin")
    s.add_argument("--quiet", "-q", action="store_true", help="print only the decision id")
    s.add_argument("--no-resume", action="store_true",
                   help="don't record this agent session for automatic resumption")
    s.add_argument("--session", help="agent session to resume when decided (default: detected, or DECISIONS_SESSION_ID)")
    s.add_argument("--agent", help=f"which agent --session belongs to ({', '.join(agents.AGENTS)}, or any name "
                                   "the resumer has an --agent-command for)")
    s.set_defaults(fn=cmd_create)

    s = sub.add_parser("get", help="show a decision")
    s.add_argument("id")
    s.add_argument("--json", action="store_true")
    s.add_argument("--markdown", action="store_true")
    s.set_defaults(fn=cmd_get)

    s = sub.add_parser("list", help="list decisions")
    s.add_argument("--status", default="open")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--space", help="only this space")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("wait", help="block until a decision is resolved (long-poll; no inbound port needed)")
    s.add_argument("id")
    s.add_argument("--on", default="decision.resolved,decision.cancelled,decision.expired",
                   help="comma-separated event types to wake on, e.g. add attributes.requested")
    s.add_argument("--after", type=int, help="only events after this cursor (from a previous wait)")
    s.add_argument("--timeout", type=float, default=0, help="seconds; 0 = wait forever")
    s.add_argument("--json", action="store_true")
    s.add_argument("--ack", action="store_true", help="ack the decision's pending webhook deliveries")
    s.add_argument("--quiet", "-q", action="store_true")
    s.set_defaults(fn=cmd_wait)

    s = sub.add_parser("listen", help="receive webhooks and run a command for each (e.g. resume an agent)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8790)
    s.add_argument("--secret", help="verify signatures (or DECISIONS_WEBHOOK_SECRET)")
    s.add_argument("--events", default="", help="comma-separated event types to act on (default: all received)")
    s.add_argument("--exec", help="shell command to run per event; decision data is in DECISION_* env vars")
    s.add_argument("--once", action="store_true", help="exit after the first successfully handled event")
    s.set_defaults(fn=cmd_listen)

    s = sub.add_parser("watch", help="wait for a decision via its own throwaway webhook; optionally run a command")
    s.add_argument("id")
    s.add_argument("--on", default="decision.resolved,decision.cancelled,decision.expired",
                   help="comma-separated event types, e.g. add attributes.requested")
    s.add_argument("--exec", help="shell command to run on the event; decision data is in DECISION_* env vars")
    s.add_argument("--forever", action="store_true", help="keep handling events instead of exiting after one")
    s.add_argument("--keep", action="store_true", help="leave the webhook registered on exit")
    s.add_argument("--host", help="interface to listen on (default 127.0.0.1, or all if the server is remote)")
    s.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    s.add_argument("--callback-url", help="URL the server should call, if not http://host:port/")
    s.add_argument("--json", action="store_true", help="print the full payload")
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("fill", help="set option attributes, e.g. answering an attribute request")
    s.add_argument("id")
    s.add_argument("--json", default="-", help='file or - (stdin) with [{"id": ..., "attributes": {...}}]')
    s.set_defaults(fn=cmd_fill)

    s = sub.add_parser("resumer", help="resume agent sessions automatically when their decisions are made")
    rsub = s.add_subparsers(dest="resumer_cmd", required=True)
    r = rsub.add_parser("run", help="run in the foreground")
    resume_help = ("shell command run in the session's cwd for every agent; gets RESUME_SESSION_ID, "
                   "RESUME_PROMPT, RESUME_AGENT, DECISION_* (default: each agent's own resume command)")
    agent_help = "NAME=COMMAND: resume command for one agent, adding or replacing a built-in one (repeatable)"
    r.add_argument("--command", help=resume_help)
    r.add_argument("--agent-command", action="append", help=agent_help)
    expired_help = "don't resume sessions whose decision expired undecided (default: resume them, saying so)"
    r.add_argument("--max-age-days", type=float, default=14, help="don't resume decisions resolved longer ago")
    r.add_argument("--no-expired", action="store_true", help=expired_help)
    r.set_defaults(fn=cmd_resumer_run)
    r = rsub.add_parser("install", help="install as a background service (launchd / systemd --user)")
    r.add_argument("--command", help=resume_help)
    r.add_argument("--agent-command", action="append", help=agent_help)
    r.add_argument("--no-expired", action="store_true", help=expired_help)
    r.set_defaults(fn=cmd_resumer_install)
    r = rsub.add_parser("uninstall", help="remove the background service")
    r.set_defaults(fn=cmd_resumer_uninstall)
    r = rsub.add_parser("status", help="is this machine's resumer online?")
    r.set_defaults(fn=cmd_resumer_status)

    s = sub.add_parser("ack", help="acknowledge all pending webhook deliveries for a decision")
    s.add_argument("id")
    s.set_defaults(fn=cmd_ack)

    s = sub.add_parser("verify", help="verify the integrity of the hash-chained event log")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("space", help="spaces: hard boundaries between contexts like work and private (admin token)")
    psub = s.add_subparsers(dest="space_cmd", required=True)
    r = psub.add_parser("list", help="spaces with their open decisions, deadline and tokens")
    r.set_defaults(fn=cmd_space_list)
    r = psub.add_parser("create", help="create a space")
    r.add_argument("name", help="lowercase letters, digits, - and _")
    r.add_argument("--deadline-days", type=float, help="default deadline for its decisions (default: the server's)")
    r.set_defaults(fn=cmd_space_create)
    r = psub.add_parser("set", help="change a space's default deadline")
    r.add_argument("name")
    r.add_argument("--deadline-days", required=True, help="days, or `default` for the server's deadline_days")
    r.set_defaults(fn=cmd_space_set)
    r = psub.add_parser("delete", help="delete an empty space; revokes its tokens")
    r.add_argument("name")
    r.set_defaults(fn=cmd_space_delete)

    s = sub.add_parser("token", help="machine tokens: each sees one space (admin token)")
    tsub = s.add_subparsers(dest="token_cmd", required=True)
    r = tsub.add_parser("list", help="tokens with when they were last used and their machine's resumer")
    r.set_defaults(fn=cmd_token_list)
    r = tsub.add_parser("create", help="create a token for a machine and print the command that sets it up")
    r.add_argument("name", help="which machine, e.g. laptop")
    r.add_argument("--space", default="default")
    r.set_defaults(fn=cmd_token_create)
    r = tsub.add_parser("roll", help="replace a token's secret; the old one stops working at once")
    r.add_argument("id")
    r.set_defaults(fn=cmd_token_roll)
    r = tsub.add_parser("revoke", help="delete a token")
    r.add_argument("id")
    r.set_defaults(fn=cmd_token_revoke)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
