"""Markdown summary of a decision, for pasting into Slack, Linear, PRs, etc."""
from __future__ import annotations


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(_fmt(v) for v in value)
    return str(value)


def decision_markdown(d: dict) -> str:
    lines = [f"### [{d['id']}]({d['url']}): {d['title']}", ""]
    meta = [f"**Status:** {d['status']}"]
    if d["source"]:
        meta.append(f"**Source:** {d['source']}")
    meta.append(f"**Created:** {d['created_at'][:16].replace('T', ' ')} UTC")
    lines += [" · ".join(meta), ""]
    if d["context"]:
        lines += [d["context"].strip(), ""]

    by_id = {o["id"]: o for o in d["options"]}
    res = d["resolution"]
    if res:
        chosen = ", ".join(f"**{by_id[s]['label']}**" if s in by_id else f"`{s}`" for s in res["selected"])
        lines.append(f"**Decided:** {chosen} — by {res['resolved_by']} at {res['resolved_at'][:16].replace('T', ' ')} UTC")
        if res.get("note"):
            lines += ["", f"> {res['note']}"]
        lines.append("")

    keys = list(dict.fromkeys([*d["attribute_defs"], *(k for o in d["options"] for k in o["attributes"])]))
    if d["options"]:
        header = ["", "Option", *(d["attribute_defs"].get(k, {}).get("label", k) for k in keys)]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))
        for o in d["options"]:
            mark = "✅" if res and o["id"] in res["selected"] else ("✗" if o["excluded"] else "")
            label = f"[{o['label']}]({o['url']})" if o["url"] else o["label"]
            if o["excluded"] and o["exclude_reason"]:
                label += f" _(excluded: {o['exclude_reason']})_"
            meta = o.get("attribute_meta", {})
            cells = [mark, label, *(("~" if meta.get(k, {}).get("estimate") else "") + _fmt(o["attributes"][k])
                                    if k in o["attributes"] else "—" for k in keys)]
            lines.append("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
        lines.append("")
    if any(m.get("estimate") for o in d["options"] for m in o.get("attribute_meta", {}).values()):
        lines.append("_~ = estimate_\n")
    if d["links"]:
        lines.append("**Links:** " + ", ".join(f"[{l.get('title') or l['url']}]({l['url']})" for l in d["links"]))
    return "\n".join(lines).rstrip() + "\n"
