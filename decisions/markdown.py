"""Markdown summary of a decision, for pasting into Slack, Linear, PRs, etc."""
from __future__ import annotations


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(_fmt(v) for v in value)
    return str(value)


# The parts of the summary, in the order they are written. `include` picks among them.
FIELDS = ("ref", "title", "status", "relations", "context", "outcome", "options", "descriptions", "links")


def parse_include(raw: str | None) -> tuple[str, ...]:
    """'title,context,options' as fields. Nothing means all of them."""
    names = [n.strip() for n in (raw or "").split(",") if n.strip()]
    unknown = [n for n in names if n not in FIELDS]
    if unknown:
        raise ValueError(f"unknown field {', '.join(unknown)}; choose from {', '.join(FIELDS)}")
    return tuple(names) or FIELDS


def decision_markdown(d: dict, include=FIELDS) -> str:
    has = set(include).__contains__
    lines = []
    if has("ref"):
        lines += [f"### [{d['id']}]({d['url']})" + (f": {d['title']}" if has("title") else ""), ""]
    elif has("title"):
        # Without the reference it is a document of its own, for someone who can't open the app.
        lines += [f"# {d['title']}", ""]
    if has("status"):
        meta = [f"**Status:** {d['status']}"]
        if d["source"]:
            meta.append(f"**Source:** {d['source']}")
        meta.append(f"**Created:** {d['created_at'][:16].replace('T', ' ')} UTC")
        if d.get("due_at") and d["status"] in ("open", "expired"):
            label = "Expires" if d["status"] == "open" else "Expired"
            meta.append(f"**{label}:** {d['due_at'][:16].replace('T', ' ')} UTC")
        lines += [" · ".join(meta), ""]
    if has("relations"):
        start = len(lines)
        for g in d.get("goals", []):
            lines.append(f"**Toward:** {g['goal']}: {g['title']}" + (f" ({g['status']})" if g["status"] != "active" else ""))
        for x in d.get("depends_on", []):
            cond = f" — only if it picks {' or '.join(x['when_labels'])}" if x["when"] else ""
            lines.append(f"**Depends on:** {x['decision']}: {x['title']} ({x['status']}){cond}")
        for x in d.get("affects", []):
            labels = {o["id"]: o["label"] for o in d["options"]}
            cond = f" — only if this picks {' or '.join(labels.get(w, w) for w in x['when'])}" if x["when"] else ""
            lines.append(f"**Affects:** {x['decision']}: {x['title']} ({x['status']}){cond}")
        if d.get("outdated_by"):
            lines.append(f"**Outdated:** {', '.join(d['outdated_by'])} changed after this was decided")
        if len(lines) > start:
            lines.append("")
    if has("context") and d["context"]:
        lines += [d["context"].strip(), ""]

    by_id = {o["id"]: o for o in d["options"]}
    # What was picked is part of the outcome: left out, the table doesn't give it away either.
    res = d["resolution"] if has("outcome") else None
    if res:
        chosen = ", ".join(f"**{by_id[s]['label']}**" if s in by_id else f"`{s}`" for s in res["selected"])
        lines.append(f"**Decided:** {chosen} — by {res['resolved_by']} at {res['resolved_at'][:16].replace('T', ' ')} UTC")
        if res.get("note"):
            lines += ["", f"> {res['note']}"]
        lines.append("")

    keys = list(dict.fromkeys([*d["attribute_defs"], *(k for o in d["options"] for k in o["attributes"])]))
    if has("options") and d["options"]:
        header = ["", "Option", *(d["attribute_defs"].get(k, {}).get("label", k) for k in keys)]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))
        for o in d["options"]:
            mark = "✅" if res and o["id"] in res["selected"] else ("✗" if o["excluded"] else "")
            label = f"[{o['label']}]({o['url']})" if o["url"] else o["label"]
            if o["suggested_by"]:
                label += f" _(suggested by {o['suggested_by']})_"
            if o["excluded"] and o["exclude_reason"]:
                label += f" _(excluded: {o['exclude_reason']})_"
            meta = o.get("attribute_meta", {})
            cells = [mark, label, *(("~" if meta.get(k, {}).get("estimate") else "") + _fmt(o["attributes"][k])
                                    if k in o["attributes"] else "?" if meta.get(k, {}).get("unknown") else "—"
                                    for k in keys)]
            lines.append("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
        lines.append("")
        if any(m.get("estimate") for o in d["options"] for m in o.get("attribute_meta", {}).values()):
            lines.append("_~ = estimate_\n")
    described = [o for o in d["options"] if o["description"]] if has("descriptions") else []
    if described:
        lines += [f"- **{o['label']}**: {' '.join(o['description'].split())}" for o in described]
        lines.append("")
    if has("links"):
        if d["issues"]:
            lines.append("**Acted on in:** " + ", ".join(f"[{i['key']}]({i['url']})" for i in d["issues"]))
        if d["links"]:
            lines.append("**Links:** " + ", ".join(f"[{l.get('title') or l['url']}]({l['url']})" for l in d["links"]))
    return "\n".join(lines).rstrip() + "\n"
