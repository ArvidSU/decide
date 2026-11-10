"""Fill a running server with demo data: python3 examples/seed.py

Goals from a vision down to technical ones, decisions toward them in every state (waiting on you, waiting
on another decision, decided, changed, expired), and goals with a proof to agree on or ask for.
"""
import json
import os
import socket
import urllib.request

BASE = os.environ.get("DECISIONS_URL", "http://127.0.0.1:8787").rstrip("/")
TOKEN = os.environ.get("DECISIONS_TOKEN", "")
HERE = os.path.dirname(os.path.abspath(__file__))
# The resumer on this machine runs the demo proofs when you press "Prove it".
MACHINE = os.environ.get("DECISIONS_MACHINE") or socket.gethostname()
AGENT, ME = "claude-code", "me"


def call(method, path, body=None, actor=AGENT):
    headers = {"Content-Type": "application/json", "X-Actor": actor}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers=headers)
    return json.load(urllib.request.urlopen(req))


def goal(title, serves=(), actor=ME, **extra):
    return call("POST", "/api/goals", {"title": title, "serves": list(serves)} | extra, actor)["id"]


def decision(body, actor=AGENT):
    return call("POST", "/api/decisions", {"source": actor} | body, actor)["id"]


def resolve(did, selected, note=""):
    call("POST", f"/api/decisions/{did}/resolve", {"selected": selected, "note": note}, ME)


# ---------------------------------------------------------------------------- goals: why decisions are made

vision = goal("Be the tool EU utilities run their field work on", tags=["business"],
              description="Every crew plans, does and reports its work in one place.")
enterprise = goal("Win the first 10 enterprise customers", [vision], tags=["business"])
onboarding = goal("A new customer is live within a day", [enterprise], tags=["product"],
                  description="Self-serve import of their assets and crews, without a call with us.")
fast = goal("Search answers in under 200 ms", [onboarding], AGENT, tags=["tech"],
            description="p95 for `/search`, measured from outside. Today it's 1.8 s.")
upgrades = goal("The fleet upgrades itself after a release", [enterprise], AGENT, tags=["tech"],
                description="No customer runs a version more than a day old, and nobody deploys by hand.")
platform = goal("Run every customer on one shared platform", [enterprise], tags=["tech"])

# ---------------------------------------------------------------------------- decisions toward them

# Decided, then changed: the first choice shows up under Insights as a change of mind and a dead end.
cache = decision({
    "title": "Which caching strategy for /search?",
    "context": "p95 is 1.8 s and the data changes about hourly.\n\n- **Redis** is fastest but is new infrastructure\n"
               "- **HTTP caching** is nearly free, and misses on the first request per region",
    "goals": [fast], "tags": ["performance"],
    "links": [{"title": "LIN-88", "url": "https://linear.app/acme/issue/LIN-88"}],
    "attributes": {"effort_days": {"label": "Effort", "type": "number", "unit": "days", "better": "lower"},
                   "p95_ms": {"label": "Est. p95", "type": "number", "unit": "ms", "better": "lower"},
                   "new_infra": {"label": "New infrastructure", "type": "boolean"}},
    "options": [
        {"id": "redis", "label": "Redis read-through", "attributes": {"effort_days": 2, "p95_ms": 120, "new_infra": True},
         "attribute_meta": {"p95_ms": {"estimate": True, "source": "load test on staging"}}},
        {"id": "http", "label": "HTTP cache headers + CDN", "attributes": {"effort_days": 0.5, "p95_ms": 300, "new_infra": False},
         "attribute_meta": {"p95_ms": {"estimate": True, "source": "CDN docs, not measured"}}},
        {"id": "matview", "label": "Materialized view", "attributes": {"effort_days": 3, "p95_ms": 200, "new_infra": False}},
    ],
})
call("POST", f"/api/decisions/{cache}/exclude", {"option_ids": ["matview"], "reason": "Refresh locks the table"}, ME)
resolve(cache, ["http"], "Cheapest thing first")
call("POST", f"/api/decisions/{cache}/reopen", {"note": "300 ms isn't under 200 ms"}, ME)
resolve(cache, ["redis"], "HTTP caching alone missed the target")
call("PATCH", f"/api/decisions/{cache}", {"issues": ["https://linear.app/acme/issue/LIN-93/redis-read-through"]})

# Only matters because Redis was picked: unlocked by the decision above, and waiting on you.
decision({
    "title": "Which Redis client?",
    "context": "Follows from picking Redis for `/search`.",
    "goals": [fast], "tags": ["performance"],
    "depends_on": [{"decision": cache, "when": ["redis"], "note": "Only matters if we run Redis"}],
    "attributes": {"async": {"label": "Async API", "type": "boolean"}, "stars_k": {"label": "GitHub stars", "type": "number", "unit": "k", "better": "higher"}},
    "options": [{"label": "redis-py", "attributes": {"async": True, "stars_k": 13}},
                {"label": "valkey-py", "attributes": {"async": True, "stars_k": 1}}],
})

# Waiting on you, with an attribute you asked for, your own option, and a session to resume when you decide.
sprint = decision({
    "title": "What should we build next sprint?",
    "context": "Backlog triage from LIN-412. Capacity is about 8 engineer-days.\n\n- Effort is in engineer-days\n"
               "- Impact is a 1–5 estimate from support tickets",
    "max_select": None, "goals": [enterprise], "tags": ["planning"],
    "links": [{"title": "LIN-412", "url": "https://linear.app/acme/issue/LIN-412"},
              {"title": "#product thread", "url": "https://acme.slack.com/archives/C1/p1"}],
    "attributes": {"effort": {"type": "number", "unit": "days", "better": "lower"},
                   "impact": {"type": "number", "better": "higher"}},
    "options": [
        {"label": "SSO via Okta", "description": "Top enterprise ask", "attributes": {"effort": 5, "impact": 5, "area": "auth"}},
        {"label": "CSV export", "attributes": {"effort": 1, "impact": 3, "area": "reporting"}},
        {"label": "Dark mode", "attributes": {"effort": 3, "impact": 2, "area": "ui"}},
        {"label": "Webhook retries", "attributes": {"effort": 2, "impact": 4, "area": "platform"}},
        {"label": "Audit log export", "attributes": {"effort": 2, "impact": 3, "area": "reporting"}},
        {"label": "Mobile push", "attributes": {"effort": 8, "impact": 3, "area": "ui"}},
    ],
    "resume": {"session_id": "demo-session-1", "agent": AGENT, "machine": MACHINE, "cwd": HERE},
})
call("POST", f"/api/decisions/{sprint}/attribute-requests",
     {"key": "risk", "description": "Delivery risk: low / medium / high"}, ME)
call("POST", f"/api/decisions/{sprint}/options",
     {"label": "Bulk asset import", "description": "What the last two trials asked for", "suggested": True}, ME)

# Waits on the sprint: locked, out of your way, until that's decided.
decision({
    "title": "Which identity providers at launch?",
    "context": "Only comes up if SSO makes the sprint.",
    "max_select": None, "goals": [enterprise], "tags": ["planning"],
    "depends_on": [{"decision": sprint, "when": ["1"], "note": "Only if SSO is picked"}],
    "options": [{"label": "Okta"}, {"label": "Entra ID"}, {"label": "Google Workspace"}],
})

# Decided for a goal that was then abandoned: a dead end.
tenancy = decision({
    "title": "How do we isolate customers on the shared platform?",
    "goals": [platform], "tags": ["architecture"],
    "attributes": {"cost_month": {"label": "Cost", "type": "number", "unit": "€/month", "better": "lower"}},
    "options": [{"label": "Schema per customer", "attributes": {"cost_month": 400, "isolation": "medium"}},
                {"label": "Row-level security", "attributes": {"cost_month": 250, "isolation": "low"}}],
})
resolve(tenancy, ["2"], "Cheapest, and Postgres does the work")
call("PATCH", f"/api/goals/{platform}", {"status": "abandoned", "note": "Two customers require data residency in their own country"}, ME)

# Nobody answered in time.
call("PATCH", f"/api/decisions/{decision({'title': 'Squash or merge commits on main?', 'tags': ['housekeeping'], 'options': [{'label': 'Squash'}, {'label': 'Merge'}]})}",
     {"due_at": "2020-01-01T00:00:00Z"})

# Not about work at all, with images: the card view.
img = "https://images.unsplash.com/photo-{}?w=600&q=70".format
decision({
    "title": "Which autumn jacket should I buy?",
    "context": "Found 6 candidates in your size (M). You asked for something that works for **cycling to work**.",
    "attributes": {
        "price": {"type": "number", "unit": "SEK", "better": "lower"},
        "weight_g": {"label": "Weight", "type": "number", "unit": "g", "better": "lower"},
        "waterproof": {"type": "boolean"},
        "color": {"type": "enum"},
    },
    "options": [
        {"label": "Arc'teryx Beta LT", "image": img("1551028719-00167b16eac5"), "url": "https://example.com/1",
         "attributes": {"price": 4800, "weight_g": 395, "waterproof": True, "color": "black", "store": "Addnature"}},
        {"label": "Fjällräven Greenland", "image": img("1591047139829-d91aecb6caea"), "url": "https://example.com/2",
         "attributes": {"price": 2100, "weight_g": 640, "waterproof": False, "color": "green", "store": "Fjällräven"}},
        {"label": "Rains Long Jacket", "image": img("1544923246-77307dd654cb"), "url": "https://example.com/3",
         "attributes": {"price": 1195, "weight_g": 520, "waterproof": True, "color": "black", "store": "Rains"}},
        {"label": "Patagonia Torrentshell", "image": img("1548126032-079a0fb0099d"), "url": "https://example.com/4",
         "attributes": {"price": 1900, "weight_g": 400, "waterproof": True, "color": "navy", "store": "Patagonia"}},
        {"label": "Uniqlo Blocktech Parka", "image": img("1539533018447-63fcce2678e3"), "url": "https://example.com/5",
         "attributes": {"price": 799, "weight_g": 450, "waterproof": True, "color": "navy", "store": "Uniqlo"},
         "attribute_meta": {"price": {"estimate": True, "source": "last season's price"}}},
        {"label": "Barbour Bedale", "image": img("1520975954732-35dd22299614"), "url": "https://example.com/6",
         "attributes": {"price": 4200, "weight_g": 1300, "waterproof": False, "color": "olive", "store": "NK"}},
    ],
}, "shopping-agent")

# ---------------------------------------------------------------------------- proofs: showing a goal is achieved


def proof(gid, success, command, script):
    with open(os.path.join(HERE, script)) as fh:
        body = {"success": success, "command": command, "machine": MACHINE, "cwd": HERE, "timeout": 120,
                "scripts": [{"path": script, "content": fh.read()}]}
    return call("PUT", f"/api/goals/{gid}/proof", body)["proof"]["hash"]


# Agreed on and flagged ready: "Prove it" is on. With `decide resumer run` on this machine it really runs.
agreed = proof(fast, "20 requests from outside answer with a **p95 under 200 ms**.",
               f"python3 prove_latency.py {BASE}/api/health", "prove_latency.py")
call("POST", f"/api/goals/{fast}/proof/agree", {"hash": agreed}, ME)
call("POST", f"/api/goals/{fast}/proof/ready", {"note": "Redis read-through is live"})

# Proposed, for you to read and agree on.
proof(upgrades, "A fresh instance runs the new version **within 10 minutes** of the release images being available.",
      "python3 prove_upgrade.py", "prove_upgrade.py")

print(f"seeded: {BASE}/map")
