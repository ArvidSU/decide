# Decisions

A general-purpose decision log. Agents and external apps **create decisions over REST**; you **resolve them in a fast GUI** by filtering out what isn't viable and selecting one or more options; a **webhook fires** so the requester can act. Every change is recorded in an **immutable, hash-chained log** for provenance.

It doesn't care what is being decided: a jacket to buy, the next feature to build, a database to use. Options carry arbitrary attributes; you can ask the requester for more attributes and it fills them in, best effort.

Zero dependencies: Python ≥ 3.11 stdlib + SQLite + a vanilla-JS GUI.

Early development: ready for experimentation, with APIs and storage still subject to change.

Clone the repository and start the server (Python 3.11 or newer):

```sh
git clone https://github.com/ArvidSU/decide.git
cd decide
python3 -m decisions serve            # GUI + API on http://127.0.0.1:8787
```

Open http://127.0.0.1:8787 in your browser. Leave the server running and, in a second terminal
in the cloned repository, optionally load demo decisions or install the CLI:

```sh
python3 examples/seed.py              # optional: demo goals, decisions, proofs and feedback
python3 -m unittest discover tests    # tests
uv tool install --editable .          # puts `decide` on PATH
decide skill install                  # teach your agents (Claude Code, Codex, anything reading ~/.agents/skills)
```

## Concepts

| | |
|---|---|
| **Space** | A hard boundary between contexts: `work`, `private`, later maybe one shared with someone. Every decision, webhook and event belongs to exactly one. Each machine gets its own token for one space (Settings → Machines, or `decide token create`). An agent using it creates decisions there automatically and can't see any other space. Other spaces' decisions look the same as missing ones (404). The admin `token` from the config sees and manages every space. A new database starts with one space, `default`. With only one space, the GUI doesn't show spaces at all, and nobody has to name it. An empty space can be deleted, `default` too, as long as another is left. Each space has a `color` (`indigo`, `teal`, `orange`, `pink`, `green`, `purple`, `blue` or `slate`) to tell them apart: a new space gets one no other has, and Settings → Spaces or `decide space set --color` changes it. The GUI wears the color of the space picked in the sidebar, or under "All spaces" that of the decision or goal that's open, in the browser tab's icon too, and marks each space's name with a dot in its color. Use tags for topics inside a space. |
| **Machine token** | `dk_…`, for one machine in one space. Only its hash is stored, so it's shown once, when it's created or rolled. Rolling gives it a new secret and the old one stops working at once. Moving it to another space keeps the secret, so nothing is reinstalled: the machine sees the new space from its next request, and what it created stays in the old one. Revoking deletes it. Settings → Machines shows when each was last used and whether that machine's resumer is online, and has Connect (the install command; roll to get it with a token filled in), Move, Roll and Delete for each. Creating, rolling, moving and revoking are logged in the space's chain (a move in both), without the secret. |
| **Decision** | `D-12`: title, context (markdown), source, options, `min_select`/`max_select` (default: pick exactly one; `"max_select": null` = any number), links, issues, tags, metadata, `due_at`. Status `open` → `resolved` / `cancelled` / `expired`. Nothing is locked: you can edit, re-resolve or reopen at any time; every change is logged. |
| **Deadline** | Every open decision has a `due_at`, by default 4 days after it's created (`deadline_days`). If nobody decides by then, it **expires**: status `expired`, event `decision.expired` (actor `deadline`). That's distinct from `cancelled`, which someone does on purpose. If nobody took action, it wasn't very important. Its waiters, webhooks and resumer are notified, the same as for a resolution. Extend it with `PATCH {"due_at": …}` or the GUI's +1 day / +4 days. Reopening an expired decision starts a new deadline. |
| **Goal** | `G-3`: why decisions are made. Title, description (markdown), status `active` / `achieved` / `abandoned` (with a note on why), issues, tags. A goal `serves` larger goals, so goals form a map from the business vision through strategic and product goals down to technical ones. A decision is a step `toward` one or more goals (`goals: ["G-3"]`). Links stay within a space, and cycles are refused. An open decision whose every goal was abandoned is flagged. |
| **Proof** | How a goal is shown to be achieved, so nobody has to take it on trust. An agent proposes one on the goal: what success looks like in words, a `command` that exits 0 when it's true, and the scripts that command runs (e.g. start an EC2 instance, trigger a release, check the instance upgraded within 10 minutes of the images being available). You **agree** to it in the GUI. That commits both of you: everything agreed on is hashed, a change is logged with both hashes (`proof.changed`) and has to be agreed on again, and whoever proposed it can't be the one to agree. When the agent thinks the goal is achieved, it flags it **ready**, which turns on the goal's **Prove it** button. Pressing it logs `proof.requested`. The resumer on the agent's machine hears it, checks the scripts are still byte for byte what was agreed on (if not, it doesn't run), runs the command in the directory it was proposed from, and reports `proof.passed` or `proof.failed` with the output. Passing marks the goal `achieved`. Failing turns the button off until the agent flags it ready again. The agent hears each step: `decide goal proof` and `decide goal ready` record the agent's session on the proof (`proof.resume`, like a decision's resume target), and the resumer on that session's machine resumes it when you agree, told to do the work, and again with the result, the exit code and the output, told to carry on with the next piece of work (passed) or to fix the work and flag the goal ready again (failed). A session still in `decide goal wait` hears it there instead. After 3 failures in a row the agent isn't woken again until the proof passes or is proposed anew; the goal's page says so. |
| **Issue** | Where a decision or goal is being acted on: a ticket in Linear, Jira, GitHub or any tracker, as `issues: [{url, key}]`. It's the evidence that a decision was carried out, not just made. Link one at any status, before or after it's decided. `key` is what the GUI shows (`LIN-42`, `decide#12`); leave it out and it's read off the URL. `links` are different: they say where the question came from. For now an issue is only a link; nothing is read from the tracker. Later, the server can fetch each issue's status (open, in progress, done, won't do) so Insights can show decisions that were never acted on, stalled, or whose issue was closed without being done. |
| **Map** | Goals and decisions as one graph: goal → the goals it serves, decision → its goals, decision → the decisions it depends on. Focus on any goal or decision to see everything that led to it and everything it led to. Filter it by topic with tags (`business`, `product`, `tech`, …): a decision has its own tags plus those of the goals it's toward, so tagging goals is enough. What the matching goals and decisions lead up to stays on the map, faded, so the chain up to the vision isn't cut. |
| **Insights** | What the history says. **Dead ends**: options, matched by label across decisions, that were picked and later reversed or withdrawn, picked for a goal that was then abandoned, or keep being ruled out. **Changes of mind**: every time a decision was changed or withdrawn after it was made, with the decisions upstream of it (what led there) and the decided ones downstream (what it pulled the rug from). **Abandoned goals**: with how many decisions went into them. To keep this honest, revisit a decision by reopening it, not by creating a new one. |
| **Feedback** | `F-4`: what an agent tells you about Decisions itself, so it gets better from use: a `bug`, an `improvement` or a `feature` request, with a title, a body (markdown), who sent it and from which session. `decide feedback send` from any session. `F-4` counts on this server; each also has a unique id (`fb-3f9a1c07d2`) that names it everywhere, which is how it gets closed by itself: the code lists the feedback it addresses in `decisions/fixed.txt`, and a server that starts with code listing one of its own open or reported pieces marks it `done`. You mark each `done` or `dismissed` with a note, or **report it upstream**: that opens a new issue for the app's developers (`feedback_issues_url`) with the text filled in for you to edit and submit, and marks it `reported`. Only the text, its unique id and the app's version go along, nothing about the session, the machine or the space. When the developers address it and you upgrade, yours is closed. It's also collected without anyone thinking of it: `decide` notes every command that fails inside an agent session (a usage error, or an error from the server), on that machine. On a machine you opt in (Settings → Machines, `decide token set ID --feedback on`, or `decide resumer install --feedback`), once everything such a session waited on has happened (its decisions are closed, its goals achieved or abandoned, and it has been resumed with the news) and it's been quiet for 10 minutes, the resumer forks the session and shows the fork those failures: which were Decisions' doing (the skill said wrong, the error didn't help, it can't do what was needed), and which a slip of its own? Only the first kind is sent. The session itself is left as it was, and a session in which nothing failed isn't asked. Each session is asked once, not at all if it already sent feedback, and only for what finished in the last day. |
| **Option** | `label`, `description`, `url`, `image`, `attributes` (any JSON), `suggested_by`. The ID is yours (e.g. a SKU) or auto-assigned (`1`, `2`, …). |
| **Attribute defs** | Optional hints per attribute key: `label`, `type` (`number`/`boolean`/`enum`/`tags`/`text`/`url`), `unit`, `better` (`lower`/`higher`), `description`. Without them the GUI infers types from the values. |
| **Attribute request** | You ask for a new attribute in the GUI. The decision's `pending_attribute_requests` lists which options are missing it, and an `attributes.requested` webhook fires. The requester PATCHes the values in. |
| **Your own option** | None of the options fit, so you add one in the GUI (`suggested: true` on the new option): a name, and if you like a description and a link, written for whoever decides like any other option's. It's recorded with `suggested_by`, an `options.suggested` webhook fires, and every attribute it's missing joins `pending_attribute_requests`, so the requester fills it in like an attribute request, resumed by the resumer if its session has ended. You can pick it right away. |
| **Attribute meta** | Per-option provenance for values: `attribute_meta: {"price": {"estimate": true, "source": "…", "note": "…"}}`. Estimates show as "≈ value" in the GUI and "~value" in markdown. `{"unknown": true, "note": "…"}` answers a request for a value that couldn't be found, shown as "?". |
| **Listening** | The decision page shows who will hear about it: agents long-polling right now (by `X-Actor`) and webhooks. `GET /api/decisions/{id}/listeners`. Click the eye next to a listener to resolve without notifying it (global webhooks always hear). |
| **Dependency** | `D-13` `depends_on` `D-12` when it only makes sense once D-12 is decided. Until then D-13 is **locked**: it can't be resolved (409), it doesn't count as waiting on you, it sorts last, and it doesn't expire. Its deadline starts when it unlocks (`decision.unblocked`). With `when: [option ids]` it only applies if D-12 picks one of them. Any other outcome, including D-12 being cancelled or expiring, **cancels D-13** (actor `dependency`, with the reason), and so on down the chain. Without `when`, any outcome unlocks it. A dependent that was already decided is never changed. If D-12 is reopened or changes afterwards, it's flagged in `outdated_by` for a second look, and its listeners hear `dependency.changed`. Dependencies stay within a space, and cycles are refused. Best of all, avoid them: ask in order, or merge tightly coupled choices into one decision. |
| **Exclusion** | Ruling an option out (✕, or "Exclude N hidden" to turn current filters into exclusions). It's persisted with a reason, so the log shows *why* options were dropped. |
| **Event log** | Append-only and SHA-256 hash-chained, with one chain per space, so a space's history can be verified or shared without the others. SQLite triggers reject UPDATE/DELETE. `GET /api/events/verify` checks each chain the token can see. |
| **Reference** | Click the `D-12` chip to copy `[D-12: title](url)` for Slack / Linear. "Copy summary" copies a markdown table of options and the outcome. |

## GUI

* Sidebar: what's waiting on you and when the next one expires, then Open (most urgent first, with a countdown) / Resolved / Expired / Cancelled, search. Tag chips show only decisions with any of the picked tags (remembered per browser); "Goals' tags too" counts the tags of the goals a decision is toward, as the map does. The tab title shows the open count. On phones it's a drawer behind the menu button.
* Open decisions show how long is left and what happens at the deadline. The callout turns amber under a day and red under 6 hours.
* Filters sit in a toolbar: one button per attribute opens a popover (min/max, yes/no, value checkboxes with counts, text contains), plus free-text search. Active filters show as removable chips, with "Keep options with unknown values" so best-effort gaps don't hide options, and "Rule out N hidden" to record the filter as the reason. Short lists without attributes skip the filters.
* Best value per attribute is marked when `better` is set. Click column headers to sort. Table or card view (cards when options have images, and on phones).
* Keyboard: `j`/`k` move, `space` select, `x` rule out, `/` search, `⌘/Ctrl+Enter` decide (listed under the options). Every rule-out offers Undo and Add reason; Alt-click Rule out to give the reason up front.
* Resolved, expired and cancelled decisions open as a read-only record: the outcome and its reason, every option considered and the log. Reopen to change one. "Next open" jumps to the most urgent open decision.
* Dependencies: a decision waiting on another says "Don't worry about this yet", links to the one to decide first, and can't be decided until then ("Decide anyway" removes the dependency). Its upstream lists what it affects, and as you select options the decide bar says what deciding would do: cancel, unlock, or flag a decided one for a second look. A decided decision whose upstream changed shows "Worth a second look", with "Still holds" to confirm it without waking its listeners. "Depends on…" (or ⌘K) adds one.
* Map (the map icon, or ⌘K): the goal graph, laid out with what something is for above it. Hover or focus any node to light up what led to it and what it led to. Topic chips filter the map, the outline and the insights by tag (remembered per browser, and in the link). Switching spaces redraws it. Below it, the goals as an outline and the insights. A goal's page lists what it serves, its subgoals and the decisions toward it, with its own part of the map. A decision shows the goals it's toward under its title ("Goal…" links one, including after it's decided), and "Trace on the map". Tags sit under the title of a goal or decision: "+ tag" adds them (comma-separated), × removes one.
* A goal's page has its proof: what success looks like, the command, each script (folded, with its hash), where it runs and whether that machine's resumer is online. "Agree" commits to it. "Prove it" is always there but only works once it's agreed on and the agent flagged the goal ready; hover it to see what it's waiting for. While it runs the page follows along, then shows "Proved" or "Proof failed" with the output, and whether the agent's session has been told ("Telling the claude-code session to fix it…", "heard it live", or that it wasn't, with "Copy resume command").
* "None of these? Add your own option" under the options: a name and details for the agent (a link goes in `url`). It shows up marked "yours", and every value the agent was asked for, on it or in a requested column, shows as pulsing dots until it arrives. Filters don't hide those.
* Decisions and goals show their issues under the title, at any status: "+ Issue" links the ticket where it's being acted on, each one links to the tracker, and × unlinks it.
* Live: new options and attribute values appear as the agent adds them.
* Feedback (the speech bubble, or ⌘K): what agents sent about Decisions itself, by status, with Done / Report upstream / Dismiss and a note on what came of it, and the sessions that were asked, each with the commands that failed in it. The icon has a dot while any is open.
* Settings: connect a machine, opt it in to asking its sessions for feedback, global webhooks (pick the events), delivery policy, log integrity.

## API

Base: `http://127.0.0.1:8787/api`. JSON in and out. Send `X-Actor: <name>` so the log records who did what. If a token is configured, send `Authorization: Bearer <token>`.

Browser API requests must come from the GUI's own origin; unrelated website origins are
rejected, including preflight requests. CLI and agent clients can connect without an
`Origin` header. Behind an HTTPS reverse proxy, set `base_url` to the GUI's public URL
so its origin is recognized. Cross-origin browser integrations are not supported.

| Method & path | |
|---|---|
| `POST /decisions` | Create. Body: `title`, `context`, `source`, `options[]`, `attributes{}`, `goals[]` (goal ids it's a step toward), `min_select`, `max_select`, `links[]`, `issues[]` (URLs or `{url, key?}`), `tags[]`, `metadata{}`, `due_at` (ISO 8601; default now + the space's `deadline_days`, counted from when it unlocks if it waits on another decision), `depends_on[]` (`"D-12"` or `{decision, when?: [option ids], note?}`; 409 if `when` can no longer be met), `idempotency_key` (per space), `webhooks[]`, `space` (default: the token's space; the admin token has to name one once there are several). Returns 201, or 200 with the existing decision if `idempotency_key` matches. |
| `GET /decisions?status=open&q=&tag=&source=&space=` | List (`status`: `open`, `resolved`, `cancelled`, `expired`, `all`), limited to the token's spaces |
| `GET /decisions/{id}` | Full decision incl. options, resolution, `selected_options`, `pending_attribute_requests`, and dependencies: `depends_on` (each with the upstream's `status` and `state`: `waiting` / `met` / `unmet`), `waiting_on` (what locks it), `affects` (decisions depending on this one) and `outdated_by` (upstreams that changed after this was decided) |
| `PATCH /decisions/{id}` | Edit any field (`metadata` merges; `null` removes a key; `depends_on` and `issues` replace the list, and dropping the last dependency it waits on unlocks it) |
| `GET /decisions/{id}/markdown` | Markdown summary |
| `GET /goals?status=&space=&tag=` · `POST /goals` | Goals with what each `serves`, its `issues` and its decision counts (`status`: `active`, `achieved`, `abandoned`, `all`). Create: `title`, `description`, `serves[]`, `issues[]` (URLs or `{url, key?}`), `tags[]`, `space`. |
| `GET /goals/{id}` · `PATCH /goals/{id}` | Full goal with `serves`, `subgoals` and `decisions` (with their outcome). PATCH: `title`, `description`, `tags`, `serves` and `issues` (replace the list), `status` with an optional `note`. |
| `GET /goals/{id}/events` | The goal's log: `goal.created`, `goal.updated`, `goal.achieved`, `goal.abandoned`, `goal.reactivated`, and its proof's: `proof.proposed`, `proof.changed`, `proof.agreed`, `proof.ready`, `proof.not_ready`, `proof.requested`, `proof.started`, `proof.passed`, `proof.failed`, `proof.removed`, `proof.retargeted` (another session will hear the result), and the resumer's `resume.started` / `resume.finished` / `resume.failed` / `resume.skipped` / `resume.manual` for each agreement and result |
| `PUT` / `DELETE /goals/{id}/proof` | Propose, change or remove the goal's proof. Body: `success`, `command`, `machine` (whose resumer runs it), `cwd`, `timeout` (seconds, default 3600), `scripts[]` (`{path, content}`, paths relative to `cwd`). The goal's `proof` has those plus each script's `sha256`, `hash` (of all of it), `proposed_by`, `agreed` (`{by, at}`), `ready` (`{by, at, note}`), `run` (`{status: requested / started / passed / failed, seq, hash, since, detail}`) and `busy`. A changed proof is no longer agreed on. 409 while it runs. Optional `resume` (`{session_id, agent, machine, cwd?}`, as on a decision): the session to wake with the result. It isn't part of the hash, is kept when left out, and gives the proof `resume_command` and `resume_state` (`armed` until it's agreed on, and again once the agent has flagged the goal ready, until the result; then `pending` / `started` / `finished` / `failed` / `skipped` / `manual` with `trigger` (`proof.agreed` / `proof.passed` / `proof.failed`), `trigger_seq`, `failures` in a row and `max_failures`, or `capped` when failures exceed the cap, 3). |
| `POST /goals/{id}/proof/agree` | `{hash}`: the version you looked at (409 if it changed since). 403 for whoever proposed it. |
| `POST /goals/{id}/proof/ready` | `{ready?: true, note?, resume?}`: the agent's claim that the goal is achieved. `ready: false` takes it back. `resume` makes this session the one to wake with the result. |
| `POST /goals/{id}/proof/run` | Ask for the proof (the GUI's button). 409 unless it's agreed on, ready and not already running; 403 for whoever proposed it. A run that hasn't reported back 5 minutes after its timeout is given up on and can be asked for again. |
| `POST /goals/{id}/proof/events` | The resumer's report: `{status: started / passed / failed, request_seq, exit_code?, output_tail?, error?, …}` |
| `POST /goals/{id}/resume-events` | The resumer's (or your) report on waking the session after the proof was agreed to or has run: `{status: started / finished / failed / skipped / manual, trigger_seq, …}`. 400 without a `resume` on the proof. |
| `GET /goals/{id}/listeners` | Sessions in `decide goal wait` on it right now (`{actor, events, connected, idle, since}`), which the resumer leaves to hear what they're waiting for themselves. |
| `GET /graph?space=&focus=&all=1&tags=` | `{nodes, edges}`: goals and the decisions linked to them (directly or through dependencies; `all=1` adds the rest). Edges `serves`, `toward`, `depends_on` point from the step to what it's for. `focus=G-3` or `D-12`: only what leads to it and what it leads to. Decision nodes carry their `outcome` and how often they `changed` their mind. `tags=tech,product` (or `tag=` repeated): only what has any of them, plus what that leads up to, marked `context: true`. |
| `GET /insights?space=&tags=` | `{dead_ends, reversals, abandoned_goals}` (see Insights above), optionally only for what has one of the tags |
| `POST /decisions/{id}/options` | Add one, a list, or `{options: [...]}`. `suggested: true` on an option marks it as the user's own (see above). |
| `PATCH /decisions/{id}/options` | Bulk patch `[{id, attributes: {...}}]`; attributes merge, `null` deletes |
| `PATCH` / `DELETE /decisions/{id}/options/{oid}` | Edit / remove one option |
| `POST /decisions/{id}/exclude` · `/include` | `{option_ids, reason?}` |
| `POST /decisions/{id}/attribute-requests` | `{key, description}` (normally done from the GUI) |
| `POST /decisions/{id}/resolve` | `{selected: [ids], note?, silence?: [listener ids]}`. 409 while it waits on another decision. `silence` skips notifying those listeners, e.g. when fixing a typo: `resumer`, `poll:<actor>`, or the id of a webhook on this decision. Global webhooks always hear. Recorded as `silenced` on the event. |
| `POST /decisions/{id}/reopen` · `/cancel` · `/comments` | Reopening is refused (409) while a `when` dependency can't be met. |
| `GET /decisions/{id}/wait?timeout=60&types=…&after=…` | Long-poll until resolved/cancelled/expired (or other event types). Returns immediately if already in one of those states. |
| `GET /decisions/{id}/events` · `GET /events?after=N&wait=30&types=&decision=&goal=` | Event log; the global feed can be long-polled instead of using webhooks |
| `GET /spaces` · `POST /spaces` · `PATCH/DELETE /spaces/{name}` | Spaces with their decision counts and `color`. Create: `{name, deadline_days?, color?}`. PATCH `{deadline_days?, color?}` (`deadline_days: null` = the server's). Only an empty space can be deleted, which revokes its tokens; the last space can't be. Everything except listing needs the admin token (403). |
| `GET /tokens` · `POST /tokens` · `PATCH /tokens/{id}` · `POST /tokens/{id}/roll` · `DELETE /tokens/{id}` | Machine tokens (admin token only). Create: `{name, space}` (`space` can be left out while there's only one). PATCH `{space?, name?}` moves it to another space or renames it, keeping the secret. Create and roll return the secret as `token`, the only time it's shown. |
| `GET /feedback?status=&space=` · `POST /feedback` | `{feedback, asks}`: feedback on Decisions newest first (`status`: `open`, `reported`, `done`, `dismissed`, `all`), and the sessions most recently asked for it, each with its `bumps` (the commands that failed in it: `{ts, command, error}`). Send: `{kind: bug / improvement / feature, title, body?, session?: {session_id, agent, machine, cwd?}, space?}`. Logged as `feedback.sent`. |
| `GET /feedback/{id}` · `PATCH /feedback/{id}` | One piece of feedback, by its id (`F-4`) or its `uid` (`fb-…`, the same on every server). PATCH `{status: open / reported / done / dismissed, note?}` (`feedback.updated`). A server that starts with code whose `decisions/fixed.txt` lists the `uid` does that PATCH itself, as actor `update`. |
| `GET /feedback/due?machine=` | `{sessions}`: the sessions on that machine to ask for feedback now (see Feedback above), each `{session_id, agent, machine, cwd, space, about: [ids it waited on]}`. A resumer on an opted-in machine polls this, and asks those in which a command failed. |
| `POST /feedback/asks` | The resumer's report on asking one: `{status: started, session_id, agent, machine, about?, bumps?, space?}` claims the session (409 if it was asked before), then `{status: finished / failed, session_id, exit_code?, …}`. Logged as `feedback.asked`, `feedback.ask_finished`, `feedback.ask_failed`. |
| `GET /events/verify` | Verify each space's hash chain: `{ok, events, chains: {space: {ok, events, head, broken_at?}}}` (`*` = webhooks covering every space) |
| `POST /decisions/{id}/webhooks` · `GET/POST /webhooks` · `PATCH/DELETE /webhooks/{id}` | Per-decision or global subscriptions. A global one covers the token's space, or `space` if given; from the admin token without `space`, every space |
| `GET /deliveries?decision=` · `GET /deliveries/{id}/attempts` | Delivery status and immutable attempt log |
| `POST /deliveries/{id}/ack` · `POST /decisions/{id}/ack` · `POST /deliveries/{id}/redeliver` | |

### Webhooks

Subscription: `{url, secret?, events?, require_ack?, max_attempts?, refire_interval?}`. Default events: `decision.resolved`, `decision.reopened`, `decision.cancelled`, `decision.expired`, `attributes.requested`, `options.suggested`. Use `["*"]` or `["decision.*"]` for more, e.g. `decision.unblocked` (what it waited on is settled) and `dependency.changed` (something it depends on was reopened or decided again). Global webhooks can also hear `goal.*` and `proof.*` (e.g. `proof.requested`, to run a proof somewhere a resumer doesn't); those payloads carry the goal as `goal` and `decision: null`. If you omit `secret`, one is generated; it is returned only in the creating response.

```http
POST <your url>
X-Decisions-Event: decision.resolved
X-Decisions-Delivery: dlv_…
X-Decisions-Attempt: 1
X-Decisions-Signature: sha256=<hex HMAC-SHA256(secret, raw body)>

{"event": {"seq", "type", "ts", "actor"},
 "delivery": {"id", "subscription_id", "require_ack", "ack_url"},
 "decision": { …full decision snapshot at event time… }}
```

**So none are missed:** non-2xx or network errors are retried with exponential backoff (`webhook_backoff_base` × 2ⁿ, capped at `webhook_backoff_max`, up to `webhook_max_attempts`; 0 = forever). With `require_ack: true`, a 2xx isn't enough: the delivery **re-fires every `refire_interval` seconds until the receiver POSTs to `ack_url`**. Use this when "received" and "acted on" differ, e.g. resuming an agent session. Any delivery can be re-fired manually from the GUI. A webhook registered on a decision that is already resolved/cancelled gets that event replayed (`delivery.replayed: true`; opt out with `"replay": false`). Deliveries carry a stable `delivery.id`, so receivers can dedupe.

## Agents: acting on a decision

Decisions are async. There are three ways to act when one is made:

**1. Block during a session (long-poll, no inbound port):**
```sh
decide wait D-12 --timeout 3600   # exit 0 when a watched event happens (first line names it) · 124 timeout · 1 error
decide wait D-12 --on decision.resolved,decision.expired,attributes.requested,options.suggested   # also wake to fill values in
```
Agents that can run commands in the background (e.g. Claude Code's `run_in_background`) are re-invoked when it exits, so they can keep working meanwhile.

**2. Wait for a webhook, with zero setup:**
```sh
decide watch D-12                 # registers its own signed webhook, prints the outcome, cleans up
decide watch D-12 --exec '…'      # …or runs a command (see below) and exits
```
`watch` picks a free port, keeps the secret in memory, registers with `require_ack`, and deletes the subscription on exit. If the decision was already made, the server replays it, so starting late is never a problem.

**3. Resume a session days later, after it has ended (the "walk away for the weekend" path):**

Decisions can carry a **resume target**: `"resume": {"session_id", "agent", "machine", "cwd", "prompt"?}`. `decide create` fills it in automatically when it runs inside a supported agent, from the agent's session env var and the current directory (`--no-resume` to opt out). Agents don't need to start anything.

| Agent | Detected from | Resumed with |
|---|---|---|
| `claude-code` | `CLAUDE_CODE_SESSION_ID` | `claude --resume "$RESUME_SESSION_ID" -p "$RESUME_PROMPT" --permission-mode acceptEdits --allowedTools "Bash(decide:*)"` |
| `codex` | `CODEX_THREAD_ID` | `codex exec resume -c sandbox_mode=workspace-write -c sandbox_workspace_write.network_access=true "$RESUME_SESSION_ID" "$RESUME_PROMPT"` |
| anything else | `DECISIONS_AGENT` + `DECISIONS_SESSION_ID`, or `decide create --agent NAME --session ID` | the resumer's `--agent-command NAME='…'` |

The agents live in one table, `decisions/agents.py`; adding one there is a few lines. The actor name in the log also defaults to the detected agent.

You install the **resumer** once:
```sh
decide resumer install     # launchd (macOS) / systemd --user (Linux): starts at login, restarted if it dies
decide resumer status      # online? (exit 1 if not)
```
When a decision with a resume target is resolved, or expires undecided, the resumer runs that agent's resume command (above) in the session's `cwd`. While it's open, it also resumes the session when you add your own option or ask for an attribute, to fill the values in (`--no-fill` turns this off). The prompt tells the agent to fill them in and stop, not act yet. If a session is still waiting on the decision with `decide wait`, it hears the request itself, so the resumer leaves it alone (`resume.skipped`). Replace or add one with `--agent-command NAME='…'` (repeatable), or use a single `--command` for every agent. Commands get `RESUME_SESSION_ID`, `RESUME_PROMPT`, `RESUME_CWD`, `RESUME_AGENT` and the `DECISION_*` env vars. An expired decision's prompt tells the agent nobody decided in time, so it should carry on with its own judgement or drop that part (`--no-expired` on `resumer run`/`install` turns this off). A decision whose agent has no command is marked `resume.failed`, so the GUI shows it wasn't resumed. The session can't prompt for permissions, so you decide here, not the agent, what a resumed session is allowed to do. Each step is logged as `resume.started` / `resume.finished` / `resume.failed`, and the output is kept in `~/.local/state/decisions/resume-logs/`.

It's built to be hard to miss:
* **Nothing is lost while it's down.** It reads the event feed from a persisted cursor, so decisions resolved while it (or the laptop) was off are resumed when it comes back, up to `--max-age-days`, default 14.
* **You're told before and after.** It heartbeats every 30s. Each decision with a resume target shows a card: "resumes automatically" / "resumer offline — won't resume automatically" before you decide, then "Resuming…" / "Resumed ✓" / "**Not resumed**" / "**Resume failed**". The same card shows "Asking the agent…" / "Agent filling in" while it fills in what you asked for. The sidebar flags resolved decisions whose agent wasn't resumed.
* **A manual fallback, one click away.** "Copy resume command" copies e.g. `cd '<cwd>' && claude --resume '<session>'` (or `codex resume …`; for other agents, the session ID). "Mark resumed" records that you did it yourself, so the resumer won't also resume it later.

The resumer also **runs goals' proofs** (see Proof above): when you press "Prove it", the resumer on the proof's machine runs the agreed command in the directory it was proposed from, with `GOAL_ID`, `GOAL_TITLE`, `GOAL_URL` and `PROOF_HASH` in its environment, kills it at its timeout, and reports whether it passed. Output is kept next to the resume logs. Only a request that's still waiting is run: one made while the resumer was down for longer than the proof's timeout isn't caught up on. It runs with the resumer's own permissions, so read what you agree to; `--no-proofs` on `resumer run`/`install` turns it off for a machine.

And it **tells the agent when you agree to the proof, and how it went**: `proof.agreed`, `proof.passed` and `proof.failed` resume the session recorded on the proof (`proof.resume`, written by `decide goal proof` and `decide goal ready` from the agent's session, like `decide create`), by the resumer on that session's machine, which needn't be the one the proof ran on. The prompt names the goal and says what to do: do the work and `decide goal ready` (agreed); or, with the outcome, the exit code and the output tail, carry on with the next piece of work (passed), or fix the work, not the proof, and `decide goal ready` again (failed). Each event is resumed once (`resume.*` events on the goal, against its `trigger_seq`), a session still in a `decide goal wait` for that event is left to hear it itself (`resume.skipped`), an agreement isn't passed on once the agent has flagged the goal ready, and after 3 failures in a row the agent isn't woken again (`resume_state: capped`) until the proof passes or is proposed anew, so a stubborn failure can't loop. The resume commands get `GOAL_ID`, `GOAL_TITLE`, `GOAL_URL`, `PROOF_HASH` and `PROOF_RESULT` (`agreed`, `passed` or `failed`) as well.

On a machine that's opted in, it **asks sessions about the commands that failed in them** (see Feedback above). `decide` appends each failure to `~/.local/state/decisions/bumps/<session>.jsonl` (the last 30 are used), which is why the session has to have run on the resumer's machine. It runs the agent's feedback command in the session's `cwd`: `claude --resume "$RESUME_SESSION_ID" --fork-session -p "$RESUME_PROMPT" --allowedTools "Bash(decide:*)"`, or `codex exec fork …`, so the fork can run `decide` and nothing else, and the session itself gets no extra turn. `--feedback-command NAME='…'` sets it for another agent (or replaces a built-in one); an agent without one isn't asked. In the fork, `DECISIONS_SESSION_ID` is the original session, so what it sends is recorded as that session's. The machine's opt-in comes back in the answer to each heartbeat, so ticking the box in Settings takes effect within 30 seconds; `decide resumer install --feedback` opts in a machine that has no token of its own.

For apps, and agents without resumable sessions, webhooks (`decide watch`, `decide listen`) remain the push mechanism.

The skill (`decisions/skill/SKILL.md`) teaches agents all of this. It's a plain `SKILL.md`, so any agent that supports skills can use it. `decide skill install` writes it to `~/.agents/skills/decisions/` (the shared location) and to the skills directory of each agent it finds (`~/.claude/skills/` for Claude Code); `--agent NAME` or `--dir DIR` to choose. For agents without skills, `decide skill show` prints it so you can paste it into their instructions (e.g. `AGENTS.md`).

## Running it on another machine

Run the server on one always-on machine (a home server, a Mac mini, a VPS on your tailnet) and use it from all your others.

**On the server:**
```sh
uv tool install /path/to/decisions          # or: git clone … && uv tool install .
decide server install --host 0.0.0.0        # launchd (macOS) or systemd --user (Linux); generates a token
```
This writes `~/.config/decisions/config.toml` (host, port, db, the admin token, `base_url`), starts the service, and prints a GUI link with the admin token (`http://server:8787/#token=…` logs the browser in once). Create spaces and connect machines from there. The server refuses to listen on a non-loopback address without a token (`--insecure` overrides). Traffic is plain HTTP, so keep it on a trusted network or a tailnet, or put it behind an HTTPS reverse proxy and set `base_url`.

**Or with Docker** (the image is just the server; resumers run on the agent machines):
```sh
printf 'DECISIONS_TOKEN=%s\nDECISIONS_BASE_URL=http://homeserver:8787\n' "$(openssl rand -base64 24)" > .env
docker compose up -d
```
Data lives in the `decisions-data` volume (`/data/decisions.db`). Both variables are required: without a token the server refuses to listen on the network, and without `DECISIONS_BASE_URL` links would point at the container's hostname. Any other setting works as a `DECISIONS_*` env var. Plain `docker build -t decisions . && docker run -p 8787:8787 -v decisions-data:/data --add-host=host.docker.internal:host-gateway -e DECISIONS_TOKEN=… -e DECISIONS_BASE_URL=… decisions` works too. Agents on the Docker machine itself can use `decide watch` against `localhost`: the image sets `callback_host = "host.docker.internal"`, and `decide watch` registers that name instead of `127.0.0.1`.

**On every machine whose agents should use it:** in the GUI, Settings → Machines → *Connect a machine*: name it, pick its space, and run the command it shows there:
```sh
curl -fsSL http://server:8787/install.sh | sh -s -- --token dk_…
```
`decide token create laptop --space work` prints the same command. Each machine gets its own token, so you can roll, move or revoke one without touching the others (`decide token roll/set/revoke`, or the buttons in Settings → Machines). Don't hand out the admin token: it sees every space. The installer:
1. installs `decide` from a wheel the server builds from its own code (via uv, pipx or pip; uv can even fetch Python). From then on the machine follows the server's code by itself (see Upgrading).
2. runs `decide connect <url> --token …`, which checks the token and saves both to `~/.config/decisions/client.toml` (mode 600).
3. runs `decide skill install` (see above).
4. runs `decide resumer install` if `claude` or `codex` is on PATH.

Flags: `--no-resumer`, `--no-skill`. The pieces are also served on their own: `/install/decisions.whl` and `/install/SKILL.md`.

**Resuming happens on the machine where the session ran.** `decide create` records the machine (hostname, or `DECISIONS_MACHINE`) in the resume target. Each machine's resumer heartbeats under its own name and only resumes its own sessions. The decision page shows the status of the resumer on that decision's machine, and Settings lists every machine's resumer.

**Upgrading:** install the new version and restart the server (with Docker: `docker compose build && docker compose up -d`). The database records its schema version, and the server upgrades an older database when it starts, keeping all of its data. If it can't, it leaves the database as it was and refuses to start, saying why. `decide migrate` does the upgrade without starting the server, e.g. to try it on a copy first: `decide migrate --db /path/to/copy.db`. Back the database file up before upgrading.

**An upgrade closes the feedback it addresses.** `decisions/fixed.txt` lists the feedback the code addresses, one per line: its unique id (`fb-…`), then what was done. When the server starts, it marks its own open or reported feedback with a listed id `done`, with that text and the commit it runs as the note. Each piece once: if you open it again, it stays open. Because the id is the same everywhere and travels with an issue reported upstream, this works on every server that had the feedback, however it was installed.

**The machines follow the server.** The server names the code it runs with a build (a hash of the package, in `GET /api/config` and in the name of the wheel it serves), and tells every resumer in the answer to its heartbeat. A resumer whose `decide` was installed from the server and is another build reruns the server's installer for `decide` and the skill, and then restarts itself with the new code, once no resume or proof is in flight. So within a minute of the server being upgraded, every machine with a resumer runs the same code as it, and you only ever upgrade the server. `decide update` does the same by hand, e.g. on a machine without a resumer (`--check` only says whether it's up to date, exit 1 if not). A `decide` that runs from a checkout (`uv tool install --editable .`) isn't touched: Settings → Machines marks it "other version". Settings also names the git commit (hash and message) the server runs and the one each machine's resumer runs (`commit` in `GET /api/config` and `GET /api/resumer`). A checkout asks git. Code without its git history reads `decisions/.commit` (JSON: `hash`, `date`, `subject`), which the server puts in the wheel it serves; write that file before `docker build` to have an image know its commit. The commit isn't part of the build, so a machine can name an older commit than the server and still run the same code. `decide resumer install --no-update` turns it off for a machine.

**The server can follow the repository.** In this repository, `.dash-bored/scripts/remote.py deploy` upgrades a Docker server on another machine over ssh: backup, a trial upgrade of a copy of the backup, and only then a restart. `remote.py follow-setup` makes that machine do the same by itself whenever origin's `main` has new commits (a launchd job that pulls every 2 minutes; `--off` removes it). It only moves forward: a server running commits that aren't pushed yet is left on them until `main` contains them. A commit whose migration fails on the copy isn't deployed, and the server keeps running the previous one. Together with the above: push to `main`, and the server and every machine run it a few minutes later.

**What needs which direction:** `decide wait`, the resumer and the GUI only make outbound connections to the server. Webhooks (`decide watch`/`listen`, your own) need the server to reach the receiver. `decide watch` against a remote server listens on all interfaces and registers the local address it uses to reach the server; override with `--callback-url`.

## Configuration

`decisions.toml` (cwd), `~/.config/decisions/config.toml`, or `$DECISIONS_CONFIG`. Env vars `DECISIONS_<KEY>` override, and CLI flags override those.

```toml
host = "127.0.0.1"
port = 8787
db = "~/.local/share/decisions/decisions.db"
token = ""                 # admin token: sees and manages every space. Set to require Authorization: Bearer <token>
base_url = ""              # public URL for links/ack_url if behind a proxy/tunnel
owner = "me"               # actor name for GUI actions
callback_host = ""         # how the server reaches its own machine for webhooks (Docker: host.docker.internal)
deadline_days = 4          # undecided decisions expire this long after creation (0 = only if they set due_at)
feedback_issues_url = "https://github.com/ArvidSU/decide/issues/new"   # where "Report upstream" opens an issue ("" = no button)

[webhook]
timeout = 10
max_attempts = 25          # 0 = retry forever
backoff_base = 10
backoff_max = 3600
require_ack = false        # default for subscriptions that don't say
refire_interval = 300
```

Spaces and machine tokens live in the database, not here: manage them in Settings or with `decide space` / `decide token` (admin token). A space can set its own `deadline_days` and `color`. Decision IDs stay global (D-12), so they never collide across spaces. With several spaces, the admin token has to name the `space` of what it creates (400 otherwise): in the body, or with `decide create --space` / `DECISIONS_SPACE` from the CLI. With one space, it goes there. A webhook created without a decision (`POST /api/webhooks`) covers the creating token's space. If the admin token creates one without a `space`, it covers every space. If there's no admin token, auth is off and anyone sees everything, but a machine token still routes new decisions to its space.

Answer attribute requests from the CLI with `decide fill D-12 < patches.json` (`[{"id", "attributes"}]`).

Link the issue where a decision is acted on with `decide issue D-12 https://linear.app/…/issue/LIN-42` (`--key` to name it, `--remove` to unlink), or `decide create … --issue URL`. For a goal: `decide goal issue G-3 URL` (same flags), or `decide goal create … --issue URL`.

Goals from the CLI: `decide goal list` (a tree), `decide goal create TITLE --serves G-1 --tag tech`, `decide goal get G-3`, `decide goal set G-3 --status abandoned --note …`, `decide create … --goal G-3`, `decide goal proof G-3 --command ./prove.sh --script prove.sh --success "…"` (run it again after editing a script to record the change; `--remove`), `decide goal ready G-3 --note …`, `decide goal wait G-3` (until it's agreed on, passes or fails), `decide map --focus D-12 --tag tech` and `decide insights --tag tech`.

Client commands (`decide create/get/list/issue/wait/watch/fill/ack/verify/resumer/update/space/token/goal/map/insights/feedback`) find the server from `DECISIONS_URL` / `DECISIONS_TOKEN`, then `~/.config/decisions/client.toml` (written by `decide connect`), then `http://127.0.0.1:8787`. `DECISIONS_ACTOR` names who is acting; `DECISIONS_MACHINE` overrides the machine name.

## License

[MIT](LICENSE) © 2026 ArvidSU.
