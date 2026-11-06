---
name: decisions
description: Ask the user to make a decision through their Decisions app (REST API + GUI) instead of asking in chat, then act on the outcome. Use when you need the user to choose between options — what to build, which approach, which product to buy — especially when the choice can be made asynchronously, benefits from comparing attributes, or should be logged for provenance. Also use when the user mentions a decision ID like D-12.
---

# Decisions

The user runs a decision log, possibly on another machine; `decide` already knows where (set up by `decide connect`, or `DECISIONS_URL`/`DECISIONS_TOKEN`). You create a decision with options. The user filters and picks in a GUI. You get woken up and act on the choice. Decisions are referenced as `D-<n>`, with a page at `<base>/d/D-<n>`.

Use the `decide` CLI. The log records who did what: inside Claude Code or Codex, `decide` names you automatically (`claude-code`, `codex`). In any other agent, prefix commands with `DECISIONS_ACTOR=<your name>`, e.g. `DECISIONS_ACTOR=gemini decide …`. If `decide` isn't on PATH, the raw API equivalents are in the project README.

The API accepts browser requests only from the GUI's own origin. Use the CLI or a server-side HTTP client for integrations; behind an HTTPS proxy, configure the server's `base_url` to match the GUI URL.

## 1. Create the decision

Write the body to a JSON file with your file-writing tool, then pass the path. Heredocs and inline JSON in shell commands are often blocked by permission checks. `-q` prints just the ID.

```sh
decide create -q --json decision.json
```

where `decision.json` contains:

```json
{
  "title": "Which caching strategy for /search?",
  "source": "<your agent name>",
  "context": "p95 is 1.8s and data changes about hourly. Trade-offs: …",
  "attributes": {
    "effort_days": {"type": "number", "unit": "days", "better": "lower"},
    "p95_ms": {"label": "Est. p95", "type": "number", "unit": "ms", "better": "lower"}
  },
  "options": [
    {"id": "redis", "label": "Redis read-through", "attributes": {"effort_days": 2, "p95_ms": 120, "new_infra": true}},
    {"id": "http", "label": "HTTP cache headers + CDN", "attributes": {"effort_days": 0.5, "p95_ms": 300, "new_infra": false}}
  ],
  "links": [{"title": "LIN-88", "url": "https://linear.app/…"}]
}
```

Make it easy to decide:
- **Mark estimates.** If a value is your estimate rather than a looked-up fact (prices from memory, guessed specs), set `"attribute_meta": {"<key>": {"estimate": true, "source": "…"}}` on that option.
- **Attributes are what make the GUI useful.** Give every option the same keys. Use plain JSON numbers (`1299`, not `"1 299 kr"`) and booleans (`true`, not `"yes"`). Put units in `attributes.<key>.unit`.
- **Use `better: "lower"|"higher"`** on numeric attributes, so the best value is highlighted.
- **Put the trade-offs in `context`** (markdown), not in chat. Link where the question came from (issue, thread, PR) in `links`.
- **Selection rules:** the default is to pick exactly one. Use `"max_select": null` for any number, or set `min_select`/`max_select`.
- **Retries:** set `"idempotency_key"` if you might retry the request.
- **Deadline:** an undecided decision **expires** after 4 days by default (the server's `deadline_days`). Expired means the user didn't consider it important enough to answer. If the question is really time-bound, set `"due_at"` (ISO 8601) or `decide create --due 36h`. Don't set long deadlines just to avoid expiry.
- **Images and links:** options can have `url` and `image`. With images, the GUI shows a card view.

Then tell the user the ID and link in one line, and don't also ask the question in chat.

## 2. Wait — without blocking yourself

Run this **in the background** if your agent can (e.g. Claude Code's `run_in_background: true`), so you are re-invoked when it exits and can keep working meanwhile. Otherwise run it in the foreground; it has no time limit:

```sh
decide wait D-12 --on decision.resolved,decision.cancelled,decision.expired,attributes.requested
```

It exits `0` whenever something you're waiting for happens. Read the first output line to see what happened:
- `event: decision.resolved` → go to step 4.
- `event: decision.cancelled` → the user closed it on purpose: stop, and tell the user.
- `event: decision.expired` → nobody decided before the deadline, so treat it as not important enough to block on. Continue with your own best judgement where the task needs an answer, or drop that part. Say what you chose and why. Don't create the same decision again unless the user asks.
- `event: attributes.requested` → go to step 3.

Any non-zero exit is a real error (`124` = the timeout you set). While the decision is still open, the output ends with a `to keep waiting: decide wait …` line. Run exactly that line next, again in the background. While you wait, the user sees your actor name in the decision's Listening panel. For the full JSON, use `decide get D-12 --json`.

**If the user says they're leaving or closing the session, don't start `decide wait`.** Just confirm the decision exists and that the resumer is online, then end your turn.

**If the session ends before the user decides, nothing extra is needed.** Inside Claude Code (`CLAUDE_CODE_SESSION_ID`) or Codex (`CODEX_THREAD_ID`), `decide create` records this session and the current directory as the decision's resume target. The user's resumer service then resumes this session headlessly (`claude --resume …` / `codex exec resume …`) with the outcome as soon as they decide, even days later. In another agent, pass your session ID yourself if you know it: `decide create --agent <name> --session <id> …` (the user's resumer needs an `--agent-command` for that agent); otherwise rely on `decide wait`. Check that this machine's resumer is running with `decide resumer status` (it only resumes sessions from the machine it runs on). If it's offline, tell the user: the GUI shows a "Copy resume command" button so they can resume you by hand, but nothing will happen automatically. Don't try to start background watchers yourself (`nohup … &`); they die with your session and are usually blocked by permissions anyway.

When you're resumed, your prompt starts with `[decisions] D-12 "…" was resolved by …`. Continue with step 4. If it says `… expired`, handle it like `event: decision.expired` above. The resumed session is headless and has limited permissions (by default it can edit files in the project and run `decide`). If you need something it doesn't allow, say so in your final message instead of failing silently.

## 3. Answer attribute requests

`event: attributes.requested`, or a non-empty `pending_attribute_requests`, means the user wants another attribute on the options. `decide get D-12` lists the key, description and missing option IDs. Fill them in, best effort:

```sh
decide fill D-12 --json fill.json
```

where `fill.json` contains `[{"id": "redis", "attributes": {"risk": "medium"}}, {"id": "http", "attributes": {"risk": "low"}}]`.

Use the same value conventions as in step 1. **Mark values you estimated or inferred** with `attribute_meta`, so the GUI shows them as "≈ value" and the user knows what to double-check: `{"id": "redis", "attributes": {"price_sek": 11990}, "attribute_meta": {"price_sek": {"estimate": true, "source": "from memory, Nov 2026"}}}`. You can also set `attribute_meta` when creating options. If you genuinely can't determine a value, leave it out rather than guess.

Then run the `to keep waiting: …` command from the previous `decide wait` output, in the background.

## 4. Act on the decision

`decide get D-12 --json` returns `resolution.selected` (option IDs), `selected_options`, and `resolution.note`. The note is the user's rationale or extra instructions; follow it. Where you act on the decision (commit, PR, Linear comment, a written summary), reference it as `[D-12: title](url)`.

The user may revise a decision later (another `decision.resolved`, or `decision.reopened`). The latest resolution is authoritative. A revision may skip notifying you (e.g. a typo fix), so check `decide get` before acting on an older one.
