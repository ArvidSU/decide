"""Dashboard sources for the Decisions server.

Usage: python3 .dash-bored/scripts/decisions-api.py <view>

Views: server, resumer, chain, deliveries (status JSON); open, recent,
delivery-list (list JSON); activity (chart JSON).

Reads the server from DECISIONS_URL (default http://127.0.0.1:8787) and sends
DECISIONS_TOKEN as a bearer token when set. Read-only; stdlib only and
compatible with the macOS system python3.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("DECISIONS_URL", "http://127.0.0.1:8787").rstrip("/")
TOKEN = os.environ.get("DECISIONS_TOKEN", "")


def get(path, timeout=4):
    req = urllib.request.Request(BASE + "/api" + path, headers={"X-Actor": "dash-bored"})
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def parse_ts(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def ago(value):
    ts = parse_ts(value)
    if not ts:
        return "never"
    secs = int((datetime.now(timezone.utc) - ts).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return "%d%s ago" % (secs // size, unit)
    return "just now"


def out(value):
    print(json.dumps(value))


def server():
    try:
        get("/health", timeout=3)
        cfg = get("/config", timeout=3)
    except Exception as exc:  # unreachable is a state to show, not a crash
        out({"state": "error", "detail": "Not reachable at %s (%s). Start it from Develop → Serve." % (BASE, reason(exc))})
        return
    open_n = len(get("/decisions?status=open")["decisions"])
    auth = "token required" if cfg.get("auth") else "no token"
    out({"state": "healthy",
         "detail": "Up at %s · v%s · %s · %d open" % (BASE, cfg.get("version", "?"), auth, open_n)})


def resumer():
    r = get("/resumer")
    everyone = r.get("resumers") or ([r] if r.get("last_seen") else [])
    if not everyone:
        out({"state": "warning", "detail": "No resumer has ever checked in. Resolved decisions won't resume sessions automatically."})
        return
    online = [x for x in everyone if x.get("online")]
    parts = ["%s %s (seen %s)" % (x.get("machine") or x.get("name"), "online" if x.get("online") else "offline",
                                   ago(x.get("last_seen"))) for x in everyone]
    out({"state": "healthy" if online else "error",
         "detail": "; ".join(parts),
         "segments": [seg for seg in ({"label": "online", "value": len(online), "state": "healthy"},
                                      {"label": "offline", "value": len(everyone) - len(online), "state": "error"})
                      if seg["value"]]})


def chain():
    v = get("/events/verify", timeout=8)
    if v.get("ok"):
        chains = v.get("chains", {})
        out({"state": "healthy", "detail": "%d events in %d hash chain%s (%s), all intact" % (
            v.get("events", 0), len(chains), "" if len(chains) == 1 else "s", ", ".join(chains) or "none yet")})
    else:
        out({"state": "error", "detail": "Chain broken: %s" % json.dumps({k: x for k, x in v.items() if k != "ok"})[:300]})


def deliveries():
    ds = get("/deliveries")["deliveries"]
    counts = {}
    for d in ds:
        counts[d["status"]] = counts.get(d["status"], 0) + 1
    failed, pending, waiting = counts.get("failed", 0), counts.get("pending", 0), counts.get("delivered", 0)
    if failed:
        state, detail = "error", "%d failed delivery(ies): redeliver from the list below or the GUI." % failed
    elif pending or waiting:
        state, detail = "warning", "%d retrying, %d delivered but awaiting ack." % (pending, waiting)
    else:
        state, detail = "healthy", "All %d deliveries acknowledged." % len(ds) if ds else "No webhook deliveries yet."
    seg_states = {"acked": "healthy", "delivered": "warning", "pending": "warning", "failed": "error"}
    segments = [{"label": k, "value": counts[k], "state": seg_states.get(k, "unknown")}
                for k in ("acked", "delivered", "pending", "failed") if counts.get(k)]
    res = {"state": state, "detail": detail}
    if segments:
        res["segments"] = segments
    out(res)


def option_summary(d):
    bits = ["%d options" % d.get("option_count", 0)]
    if d.get("excluded_count"):
        bits.append("%d ruled out" % d["excluded_count"])
    return bits


def open_list():
    items = []
    for d in get("/decisions?status=open")["decisions"]:
        bits = option_summary(d)
        pending = d.get("pending_attribute_requests") or {}
        if pending:
            bits.append("waiting on requester for: " + ", ".join(sorted(pending)))
        bits += ["from " + (d.get("source") or "unknown"), "opened " + ago(d.get("created_at"))]
        if d.get("due_at"):
            bits.append("expires " + d["due_at"][:16].replace("T", " ") + " UTC")
        items.append({
            "id": d["id"],
            "title": "%s · %s" % (d["id"], d["title"]),
            "detail": " · ".join(bits),
            "state": "warning" if pending else "pending",
            "tags": d.get("tags") or [],
            "url": d["url"],
            "decision": d["id"],
            "prompt": ("Decision %s (\"%s\") is still open in the Decisions app at %s. Fetch it with `decide get %s`, "
                       "summarize the options and trade-offs, fill in any requested attributes you can source, "
                       "and recommend a choice. Do not resolve it." % (d["id"], d["title"], d["url"], d["id"])),
        })
    out(items)


def recent_list():
    ds = [d for status in ("resolved", "cancelled", "expired") for d in get("/decisions?status=" + status)["decisions"]]
    ds.sort(key=lambda d: d.get("updated_at") or "", reverse=True)
    items = []
    for d in ds[:20]:
        rs = d.get("resume_state") or {}
        res = d.get("resolution") or {}
        if d["status"] in ("cancelled", "expired"):
            state, what = "closed", d["status"] + (" (nobody decided in time)" if d["status"] == "expired" else "")
        else:
            picked = ", ".join(res.get("selected") or []) or "nothing"
            what = "picked " + picked
            state = {"finished": "success", "failed": "failed", "started": "running",
                     "pending": "pending", "manual": "success"}.get(rs.get("status"), "done")
        bits = [what, ago(d.get("resolved_at") or d.get("updated_at"))]
        if res.get("note"):
            bits.append("“%s”" % res["note"][:80])
        if rs.get("status"):
            bits.append("agent resume: " + rs["status"])
        items.append({"id": d["id"], "title": "%s · %s" % (d["id"], d["title"]), "detail": " · ".join(bits),
                      "state": state, "tags": d.get("tags") or [], "url": d["url"], "decision": d["id"]})
    out(items)


def delivery_list():
    items = []
    for d in get("/deliveries")["deliveries"][:30]:
        st = d["status"]
        bits = [st, "attempt %s" % d.get("total_attempts", d.get("attempts", 0))]
        if d.get("last_status"):
            bits.append("HTTP %s" % d["last_status"])
        if d.get("last_error"):
            bits.append(str(d["last_error"])[:80])
        if d.get("acked_by"):
            bits.append("acked by %s" % d["acked_by"])
        bits.append(ago(d.get("created_at")))
        items.append({"id": d["id"], "title": "%s · %s" % (d.get("decision_id"), d.get("event_type")),
                      "detail": " · ".join(bits),
                      "state": {"acked": "success", "delivered": "pending", "pending": "warning", "failed": "failed"}.get(st, st),
                      "tags": [st], "delivery": d["id"], "decision": d.get("decision_id") or ""})
    out(items)


def activity():
    days = 14
    today = datetime.now(timezone.utc).date()
    labels = [(today - timedelta(days=i)) for i in range(days - 1, -1, -1)]
    created = dict.fromkeys(labels, 0)
    resolved = dict.fromkeys(labels, 0)
    ds = []
    for status in ("open", "resolved", "cancelled", "expired"):
        ds += get("/decisions?status=" + status)["decisions"]
    for d in ds:
        c, r = parse_ts(d.get("created_at")), parse_ts(d.get("resolved_at"))
        if c and c.date() in created:
            created[c.date()] += 1
        if r and r.date() in resolved:
            resolved[r.date()] += 1
    out({"labels": [x.strftime("%b %d") for x in labels],
         "series": [{"label": "Created", "values": [created[x] for x in labels]},
                    {"label": "Resolved", "values": [resolved[x] for x in labels]}]})


def reason(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP %d" % exc.code
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return exc.__class__.__name__


VIEWS = {"server": server, "resumer": resumer, "chain": chain, "deliveries": deliveries,
         "open": open_list, "recent": recent_list, "delivery-list": delivery_list, "activity": activity}

if __name__ == "__main__":
    view = sys.argv[1] if len(sys.argv) > 1 else ""
    if view not in VIEWS:
        sys.exit("usage: decisions-api.py {%s}" % "|".join(VIEWS))
    try:
        VIEWS[view]()
    except urllib.error.HTTPError as exc:
        hint = " (set DECISIONS_TOKEN in .dash-bored/.env)" if exc.code == 401 else ""
        sys.exit("Decisions API %s: HTTP %d%s" % (BASE, exc.code, hint))
    except (urllib.error.URLError, OSError) as exc:
        sys.exit("Decisions server not reachable at %s: %s" % (BASE, reason(exc)))
