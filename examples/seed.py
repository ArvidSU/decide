"""Create a few demo decisions against a running server: python3 examples/seed.py"""
import json
import os
import urllib.request

BASE = os.environ.get("DECISIONS_URL", "http://127.0.0.1:8787")


def post(path, body, actor):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Actor": actor})
    return json.load(urllib.request.urlopen(req))


img = "https://images.unsplash.com/photo-{}?w=600&q=70".format
post("/api/decisions", {
    "title": "Which autumn jacket should I buy?",
    "source": "shopping-agent",
    "context": "Found 6 candidates under your size (M). You asked for something that works for **cycling to work**.",
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
         "attributes": {"price": 799, "weight_g": 450, "waterproof": True, "color": "navy", "store": "Uniqlo"}},
        {"label": "Barbour Bedale", "image": img("1520975954732-35dd22299614"), "url": "https://example.com/6",
         "attributes": {"price": 4200, "weight_g": 1300, "waterproof": False, "color": "olive", "store": "NK"}},
    ],
}, "shopping-agent")

d = post("/api/decisions", {
    "title": "What should we build next sprint?",
    "source": "claude-code",
    "context": "Backlog triage from LIN-412. Capacity is ~8 engineer-days.\n\n- Effort is in engineer-days\n- Impact is a 1–5 estimate from support tickets",
    "max_select": None,
    "links": [{"title": "LIN-412", "url": "https://linear.app/acme/issue/LIN-412"},
              {"title": "#product thread", "url": "https://acme.slack.com/archives/C1/p1"}],
    "tags": ["planning"],
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
}, "claude-code")
post(f"/api/decisions/{d['id']}/attribute-requests",
     {"key": "risk", "description": "Delivery risk: low / medium / high"}, "me")

post("/api/decisions", {
    "title": "Postgres or SQLite for the metrics service?",
    "source": "claude-code",
    "context": "Single writer, ~50 writes/s, needs JSON queries.",
    "options": [{"label": "Postgres (managed)", "attributes": {"cost_month": 25, "ops": "low"}},
                {"label": "SQLite + Litestream", "attributes": {"cost_month": 2, "ops": "medium"}}],
}, "claude-code")
print("seeded")
