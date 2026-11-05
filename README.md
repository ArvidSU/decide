# Decisions

A general-purpose decision log. Agents and external apps **create decisions over REST**; you **resolve them in a fast GUI** by filtering out what isn't viable and selecting one or more options; a **webhook fires** so the requester can act. Every change is recorded in an **immutable, hash-chained log** for provenance.

It doesn't care what is being decided: a jacket to buy, the next feature to build, a database to use. Options carry arbitrary attributes; you can ask the requester for more attributes and it fills them in, best effort.

Zero dependencies: Python ≥ 3.11 stdlib + SQLite + a vanilla-JS GUI.

```sh
python3 -m decisions serve            # GUI + API on http://127.0.0.1:8787
python3 examples/seed.py              # optional: demo decisions
python3 -m unittest discover tests    # tests
uv tool install --editable .          # puts `decide` on PATH
decide skill install                  # teach your agents (Claude Code, Codex, anything reading ~/.agents/skills)
```

## Concepts

| | |
|---|---|
| **Space** | A hard boundary between contexts: `work`, `private`, later maybe one shared with someone. Every decision, webhook and event belongs to exactly one. Each space has its own tokens: an agent using a space's token creates decisions there automatically and can't see any other space. Other spaces' decisions look the same as missing ones (404). The admin `token` sees every space. Without any `[spaces.*]` config, everything is in `default` and the GUI doesn't show spaces at all. Use tags for topics inside a space. |
| **Decision** | `D-12`: title, context (markdown), source, options, `min_select`/`max_select` (default: pick exactly one; `"max_select": null` = any number), links, tags, metadata, `due_at`. Status `open` → `resolved` / `cancelled` / `expired`. Nothing is locked: you can edit, re-resolve or reopen at any time; every change is logged. |
| **Deadline** | Every open decision has a `due_at`, by default 4 days after it's created (`deadline_days`). If nobody decides by then, it **expires**: status `expired`, event `decision.expired` (actor `deadline`). That's distinct from `cancelled`, which someone does on purpose. If nobody took action, it wasn't very important. Its waiters, webhooks and resumer are notified, the same as for a resolution. Extend it with `PATCH {"due_at": …}` or the GUI's +1 day / +4 days. Reopening an expired decision starts a new deadline. |
| **Option** | `label`, `description`, `url`, `image`, `attributes` (any JSON). The ID is yours (e.g. a SKU) or auto-assigned (`1`, `2`, …). |
| **Attribute defs** | Optional hints per attribute key: `label`, `type` (`number`/`boolean`/`enum`/`tags`/`text`/`url`), `unit`, `better` (`lower`/`higher`), `description`. Without them the GUI infers types from the values. |
| **Attribute request** | You ask for a new attribute in the GUI. The decision's `pending_attribute_requests` lists which options are missing it, and an `attributes.requested` webhook fires. The requester PATCHes the values in. |
| **Attribute meta** | Per-option provenance for values: `attribute_meta: {"price": {"estimate": true, "source": "…", "note": "…"}}`. Estimates show as "≈ value" in the GUI and "~value" in markdown. |
| **Listening** | The decision page shows who will hear about it: agents long-polling right now (by `X-Actor`) and webhooks. `GET /api/decisions/{id}/listeners`. Click the eye next to a listener to resolve without notifying it (global webhooks always hear). |
| **Exclusion** | Ruling an option out (✕, or "Exclude N hidden" to turn current filters into exclusions). It's persisted with a reason, so the log shows *why* options were dropped. |
| **Event log** | Append-only and SHA-256 hash-chained, with one chain per space, so a space's history can be verified or shared without the others. SQLite triggers reject UPDATE/DELETE. `GET /api/events/verify` checks each chain the token can see. |
| **Reference** | Click the `D-12` chip to copy `[D-12: title](url)` for Slack / Linear. "Copy summary" copies a markdown table of options and the outcome. |

## GUI

* Sidebar: what's waiting on you and when the next one expires, then Open (most urgent first, with a countdown) / Resolved / Expired / Cancelled, search. The tab title shows the open count.
* Open decisions show how long is left and what happens at the deadline. The card turns amber under a day and red under 6 hours.
* Per-attribute filters: number ranges, yes/no, value chips, text contains, plus free-text search. "Keep options with unknown values" stops best-effort gaps from hiding options. Short lists without attributes skip the filters.
* Best value per attribute is highlighted when `better` is set. Click column headers to sort. Table or card view (cards when options have images).
* Keyboard: `j`/`k` move, `space` select, `x` rule out, `/` search, `⌘/Ctrl+Enter` decide (listed under the options). Alt-click ✕ to give a reason. Undo is offered after every exclusion.
* Resolved, expired and cancelled decisions open as a read-only record: the outcome, rationale, every option considered and the log. Reopen to change one. "Next" jumps to the most urgent open decision.
* Live: new options and attribute values appear as the agent adds them.
* ⚙ Settings: global webhooks, delivery policy, log integrity.

## API

Base: `http://127.0.0.1:8787/api`. JSON in and out. Send `X-Actor: <name>` so the log records who did what. If a token is configured, send `Authorization: Bearer <token>`.

| Method & path | |
|---|---|
| `POST /decisions` | Create. Body: `title`, `context`, `source`, `options[]`, `attributes{}`, `min_select`, `max_select`, `links[]`, `tags[]`, `metadata{}`, `due_at` (ISO 8601; default now + the space's `deadline_days`), `idempotency_key` (per space), `webhooks[]`, `space` (default: the token's space, or `default` for the admin token). Returns 201, or 200 with the existing decision if `idempotency_key` matches. |
| `GET /decisions?status=open&q=&tag=&source=&space=` | List (`status`: `open`, `resolved`, `cancelled`, `expired`, `all`), limited to the token's spaces |
| `GET /decisions/{id}` | Full decision incl. options, resolution, `selected_options`, `pending_attribute_requests` |
| `PATCH /decisions/{id}` | Edit any field (`metadata` merges; `null` removes a key) |
| `GET /decisions/{id}/markdown` | Markdown summary |
| `POST /decisions/{id}/options` | Add one, a list, or `{options: [...]}` |
| `PATCH /decisions/{id}/options` | Bulk patch `[{id, attributes: {...}}]`; attributes merge, `null` deletes |
| `PATCH` / `DELETE /decisions/{id}/options/{oid}` | Edit / remove one option |
| `POST /decisions/{id}/exclude` · `/include` | `{option_ids, reason?}` |
| `POST /decisions/{id}/attribute-requests` | `{key, description}` (normally done from the GUI) |
| `POST /decisions/{id}/resolve` | `{selected: [ids], note?, silence?: [listener ids]}`. `silence` skips notifying those listeners, e.g. when fixing a typo: `resumer`, `poll:<actor>`, or the id of a webhook on this decision. Global webhooks always hear. Recorded as `silenced` on the event. |
| `POST /decisions/{id}/reopen` · `/cancel` · `/comments` | |
| `GET /decisions/{id}/wait?timeout=60&types=…&after=…` | Long-poll until resolved/cancelled/expired (or other event types). Returns immediately if already in one of those states. |
| `GET /decisions/{id}/events` · `GET /events?after=N&wait=30` | Event log; the global feed can be long-polled instead of using webhooks |
| `GET /events/verify` | Verify each space's hash chain: `{ok, events, chains: {space: {ok, events, head, broken_at?}}}` (`*` = webhooks covering every space) |
| `POST /decisions/{id}/webhooks` · `GET/POST /webhooks` · `PATCH/DELETE /webhooks/{id}` | Per-decision or global subscriptions. A global one covers the token's space, or `space` if given; from the admin token without `space`, every space |
| `GET /deliveries?decision=` · `GET /deliveries/{id}/attempts` | Delivery status and immutable attempt log |
| `POST /deliveries/{id}/ack` · `POST /decisions/{id}/ack` · `POST /deliveries/{id}/redeliver` | |

### Webhooks

Subscription: `{url, secret?, events?, require_ack?, max_attempts?, refire_interval?}`. Default events: `decision.resolved`, `decision.reopened`, `decision.cancelled`, `decision.expired`, `attributes.requested`. Use `["*"]` or `["decision.*"]` for more. If you omit `secret`, one is generated; it is returned only in the creating response.

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
decide wait D-12 --on decision.resolved,decision.expired,attributes.requested   # also wake for attribute requests
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
When a decision with a resume target is resolved, or expires undecided, the resumer runs that agent's resume command (above) in the session's `cwd`. Replace or add one with `--agent-command NAME='…'` (repeatable), or use a single `--command` for every agent. Commands get `RESUME_SESSION_ID`, `RESUME_PROMPT`, `RESUME_CWD`, `RESUME_AGENT` and the `DECISION_*` env vars. An expired decision's prompt tells the agent nobody decided in time, so it should carry on with its own judgement or drop that part (`--no-expired` on `resumer run`/`install` turns this off). A decision whose agent has no command is marked `resume.failed`, so the GUI shows it wasn't resumed. The session can't prompt for permissions, so you decide here, not the agent, what a resumed session is allowed to do. Each step is logged as `resume.started` / `resume.finished` / `resume.failed`, and the output is kept in `~/.local/state/decisions/resume-logs/`.

It's built to be hard to miss:
* **Nothing is lost while it's down.** It reads the event feed from a persisted cursor, so decisions resolved while it (or the laptop) was off are resumed when it comes back, up to `--max-age-days`, default 14.
* **You're told before and after.** It heartbeats every 30s. Each decision with a resume target shows a card: "resumes automatically" / "resumer offline — won't resume automatically" before you decide, then "Resuming…" / "Resumed ✓" / "**Not resumed**" / "**Resume failed**". The sidebar flags resolved decisions whose agent wasn't resumed.
* **A manual fallback, one click away.** "Copy resume command" copies e.g. `cd '<cwd>' && claude --resume '<session>'` (or `codex resume …`; for other agents, the session ID). "Mark resumed" records that you did it yourself, so the resumer won't also resume it later.

For apps, and agents without resumable sessions, webhooks (`decide watch`, `decide listen`) remain the push mechanism.

The skill (`decisions/skill/SKILL.md`) teaches agents all of this. It's a plain `SKILL.md`, so any agent that supports skills can use it. `decide skill install` writes it to `~/.agents/skills/decisions/` (the shared location) and to the skills directory of each agent it finds (`~/.claude/skills/` for Claude Code); `--agent NAME` or `--dir DIR` to choose. For agents without skills, `decide skill show` prints it so you can paste it into their instructions (e.g. `AGENTS.md`).

## Running it on another machine

Run the server on one always-on machine (a home server, a Mac mini, a VPS on your tailnet) and use it from all your others.

**On the server:**
```sh
uv tool install /path/to/decisions          # or: git clone … && uv tool install .
decide server install --host 0.0.0.0        # launchd (macOS) or systemd --user (Linux); generates a token
```
This writes `~/.config/decisions/config.toml` (host, port, db, token, `base_url`), starts the service, and prints the two things you need: a GUI link with the token (`http://server:8787/#token=…` logs the browser in once), and the setup command for other machines. The server refuses to listen on a non-loopback address without a token (`--insecure` overrides). Traffic is plain HTTP, so keep it on a trusted network or a tailnet, or put it behind an HTTPS reverse proxy and set `base_url`.

**Or with Docker** (the image is just the server; resumers run on the agent machines):
```sh
printf 'DECISIONS_TOKEN=%s\nDECISIONS_BASE_URL=http://homeserver:8787\n' "$(openssl rand -base64 24)" > .env
docker compose up -d
```
Data lives in the `decisions-data` volume (`/data/decisions.db`). Both variables are required: without a token the server refuses to listen on the network, and without `DECISIONS_BASE_URL` links would point at the container's hostname. Any other setting works as a `DECISIONS_*` env var. Plain `docker build -t decisions . && docker run -p 8787:8787 -v decisions-data:/data --add-host=host.docker.internal:host-gateway -e DECISIONS_TOKEN=… -e DECISIONS_BASE_URL=… decisions` works too. Agents on the Docker machine itself can use `decide watch` against `localhost`: the image sets `callback_host = "host.docker.internal"`, and `decide watch` registers that name instead of `127.0.0.1`.

**On every machine whose agents should use it:**
```sh
curl -fsSL http://server:8787/install.sh | sh -s -- --token <token>
```
The command is also in the GUI under ⚙ Settings → *Connect a machine*. The installer:
1. installs `decide` from a wheel the server builds from its own code (via uv, pipx or pip; uv can even fetch Python). Re-run it to update after you update the server.
2. runs `decide connect <url> --token …`, which checks the token and saves both to `~/.config/decisions/client.toml` (mode 600).
3. runs `decide skill install` (see above).
4. runs `decide resumer install` if `claude` or `codex` is on PATH.

Flags: `--no-resumer`, `--no-skill`. The pieces are also served on their own: `/install/decisions.whl` and `/install/SKILL.md`.

**Resuming happens on the machine where the session ran.** `decide create` records the machine (hostname, or `DECISIONS_MACHINE`) in the resume target. Each machine's resumer heartbeats under its own name and only resumes its own sessions. The decision page shows the status of the resumer on that decision's machine, and Settings lists every machine's resumer.

**What needs which direction:** `decide wait`, the resumer and the GUI only make outbound connections to the server. Webhooks (`decide watch`/`listen`, your own) need the server to reach the receiver. `decide watch` against a remote server listens on all interfaces and registers the local address it uses to reach the server; override with `--callback-url`.

## Configuration

`decisions.toml` (cwd), `~/.config/decisions/config.toml`, or `$DECISIONS_CONFIG`. Env vars `DECISIONS_<KEY>` override, and CLI flags override those.

```toml
host = "127.0.0.1"
port = 8787
db = "~/.local/share/decisions/decisions.db"
token = ""                 # admin token: sees every space. Set to require Authorization: Bearer <token>
base_url = ""              # public URL for links/ack_url if behind a proxy/tunnel
owner = "me"               # actor name for GUI actions
callback_host = ""         # how the server reaches its own machine for webhooks (Docker: host.docker.internal)
deadline_days = 4          # undecided decisions expire this long after creation (0 = only if they set due_at)

[webhook]
timeout = 10
max_attempts = 25          # 0 = retry forever
backoff_base = 10
backoff_max = 3600
require_ack = false        # default for subscriptions that don't say
refire_interval = 300

# Spaces (optional). Decision IDs stay global (D-12), so they never collide across spaces.
[spaces.work]
tokens = ["…"]             # agents with this token see and create only in `work`
deadline_days = 4          # optional, overrides the server-wide value

[spaces.private]
tokens = ["…"]
deadline_days = 30
```

Each token belongs to one space. The admin token creates in `default` unless the body names a `space`, and `decide create --space` / `DECISIONS_SPACE` do the same from the CLI. A webhook created without a decision (`POST /api/webhooks`) covers the creating token's space. If the admin token creates one without a `space`, it covers every space. If there's no admin token, auth is off and anyone sees everything, but a space token still routes new decisions to its space.

Answer attribute requests from the CLI with `decide fill D-12 < patches.json` (`[{"id", "attributes"}]`).

Client commands (`decide create/get/list/wait/watch/fill/ack/verify/resumer`) find the server from `DECISIONS_URL` / `DECISIONS_TOKEN`, then `~/.config/decisions/client.toml` (written by `decide connect`), then `http://127.0.0.1:8787`. `DECISIONS_ACTOR` names who is acting; `DECISIONS_MACHINE` overrides the machine name.
