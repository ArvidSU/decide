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

### Goals: why you're asking

Every decision is a step toward a goal, and goals serve larger goals, from the business vision down to technical goals. Linking them builds a map the user analyzes later: which options tend to be dead ends, and which decisions led to a pivot. So before creating a decision:

1. **Link it to its goal.** Run `decide goal list` (a tree of the active goals, `G-<n>`) and pass the goal the decision serves: `"goals": ["G-7"]` in the body, or `decide create … --goal G-7`. Pick the most specific goal that fits. A decision may serve more than one.
2. **Create a goal only when none fits** and the goal is clear from the task: `decide goal create "Self-serve data import" --serves G-2 -q`. Always say which larger goal it `--serves`, and tag it with its topic the way the existing goals are tagged (e.g. `--tag tech`, `--tag product`, `--tag business`), so people can filter the map to what concerns them. Don't invent visions or strategy; if you don't know why the work matters, leave the decision unlinked and say so in `context`.
3. **Learn from the history.** Run `decide insights` and check whether any option you're about to propose was a dead end before (picked and later reversed, picked for a goal that was then abandoned, or ruled out repeatedly). Still include it if it's genuinely right this time, but say in `context` what happened last time and why this time is different. `decide map --focus D-12` shows what led to a decision and what it led to; add `--tag tech` to either command to stay within a topic.

To revisit an earlier decision, **reopen and re-decide it** rather than creating a new one, so the change of mind is recorded where the analysis can see it. When you finish the work a goal describes, don't mark it achieved yourself: prove it (below), or tell the user it may be done.

### Proving a goal is achieved

When you take on a goal whose outcome can be checked by a command, agree with the user up front on what success looks like, then let them verify it with one click:

1. **Propose the proof early**, before doing the work, from the project directory on the machine where it should run:
   ```sh
   decide goal proof G-7 --command "./scripts/prove-upgrade.sh" --script scripts/prove-upgrade.sh \
     --success "A fresh EC2 instance runs the new version within 10 minutes of the release images being available" --timeout 1800
   ```
   The command must exit 0 only if the goal is really achieved, and test the outcome end to end (start the instance, trigger the release, watch it upgrade), not that your code ran. Make it safe to run more than once, clean up after itself, and print what it checked: the user sees the output. Pass every file it runs as `--script`, so the user can read it. `--success` says in plain words what passing means.
2. **Tell the user to agree** on the goal's page (the link is printed). You can't agree yourself. Wait with `decide goal wait G-7 --on proof.agreed,proof.removed` (in the background, like `decide wait`).
3. **Do the work.** If you have to change the command or a script, run `decide goal proof G-7` again (it rereads the scripts; give only what changes) and say why. The change is logged, and the user has to agree again: a script that differs from what was agreed on is not run.
4. **When you believe the goal is achieved**, run the command yourself first. If it passes: `decide goal ready G-7 --note "what you did"`. That turns on the user's **Prove it** button; never ask for the proof yourself. Then end your turn (or, if the user is still around and you have more to do meanwhile, `decide goal wait G-7 --on proof.passed,proof.failed` in the background).
5. **You hear the result either way.** `decide goal proof` and `decide goal ready` record your session (as `decide create` does), so when the proof has run, the user's resumer resumes you with the result, the same way it does for a decision; a session in `decide goal wait` hears it there instead. `proof.passed` → the goal is marked achieved; continue with the next piece of work the user asked for, or end your turn. `proof.failed` → the output is printed: fix the work (not the proof, unless it was wrong, and then say so), run the command yourself, and `decide goal ready G-7` again. Then end your turn: you'll be resumed with the next result. You're resumed for at most 3 failures in a row (the prompt counts them); after that, until the proof passes or is proposed anew, the user has to come to you.

The proof runs on this machine through the user's resumer, so check `decide resumer status`. Don't weaken a proof to make it pass.

### Decisions that depend on each other

The user's attention is the scarce resource. Never make them decide something that another open decision could make moot or change. Before creating a decision, run `decide list` and check whether it hangs on one that's still open (yours or another agent's). If it does, in order of preference:

1. **Ask in order.** Wait for the other one to be decided, then create yours with that outcome in its context. This is the default.
2. **Merge.** If two choices only make sense together, make them one decision whose options are the combinations ("Redis + redis-py", "Redis + valkey-py", "HTTP cache").
3. **Declare the dependency**, when asking now saves real time (e.g. you'll be gone when the first is decided):
   ```json
   "depends_on": [{"decision": "D-12", "when": ["redis"], "note": "Only matters if we run Redis"}]
   ```
   or `decide create … --depends-on D-12=redis`. Your decision stays locked, out of the user's way, until D-12 is decided, and its deadline starts then. With `when`, it only applies if D-12 picks one of those options. Otherwise it's **cancelled for you** (`decision.cancelled` by actor `dependency`; the note says why), so you stop that branch. Leave out `when` if any outcome of D-12 just changes the trade-offs. If you need to revise your options once it unlocks, also wait for `decision.unblocked`.

Say in `context` how the decisions relate, so the user sees it while deciding the upstream one.

## 2. Wait — without blocking yourself

Run this **in the background** if your agent can (e.g. Claude Code's `run_in_background: true`), so you are re-invoked when it exits and can keep working meanwhile. Otherwise run it in the foreground; it has no time limit:

```sh
decide wait D-12 --on decision.resolved,decision.cancelled,decision.expired,attributes.requested,options.suggested
```

It exits `0` whenever something you're waiting for happens. Read the first output line to see what happened:
- `event: decision.resolved` → go to step 4.
- `event: decision.cancelled` → the user closed it on purpose, or (actor `dependency`) a decision it depended on went the other way: stop that part, and tell the user.
- `event: decision.expired` → nobody decided before the deadline, so treat it as not important enough to block on. Continue with your own best judgement where the task needs an answer, or drop that part. Say what you chose and why. Don't create the same decision again unless the user asks.
- `event: attributes.requested` or `event: options.suggested` → go to step 3.

Any non-zero exit is a real error (`124` = the timeout you set). While the decision is still open, the output ends with a `to keep waiting: decide wait …` line. Run exactly that line next, again in the background. While you wait, the user sees your actor name in the decision's Listening panel. For the full JSON, use `decide get D-12 --json`.

**If the user says they're leaving or closing the session, don't start `decide wait`.** Just confirm the decision exists and that the resumer is online, then end your turn.

**If the session ends before the user decides, nothing extra is needed.** Inside Claude Code (`CLAUDE_CODE_SESSION_ID`) or Codex (`CODEX_THREAD_ID`), `decide create` records this session and the current directory as the decision's resume target. The user's resumer service then resumes this session headlessly (`claude --resume …` / `codex exec resume …`) with the outcome as soon as they decide, even days later. In another agent, pass your session ID yourself if you know it: `decide create --agent <name> --session <id> …` (the user's resumer needs an `--agent-command` for that agent); otherwise rely on `decide wait`. Check that this machine's resumer is running with `decide resumer status` (it only resumes sessions from the machine it runs on). If it's offline, tell the user: the GUI shows a "Copy resume command" button so they can resume you by hand, but nothing will happen automatically. Don't try to start background watchers yourself (`nohup … &`); they die with your session and are usually blocked by permissions anyway.

When you're resumed, your prompt starts with `[decisions] D-12 "…" was resolved by …`. Continue with step 4. If it says `… expired`, handle it like `event: decision.expired` above. If it says `… is still open, and the user asked you to fill in …`, do step 3 and end your turn: don't start `decide wait`, you'll be resumed again when it's decided. If it starts with `[decisions] G-7 "…"` it's a goal's proof result (see "Proving a goal is achieved"): it says whether the proof passed or failed, the exit code and the output, and what to do; don't start `decide goal wait` afterwards. The resumed session is headless and has limited permissions (by default it can edit files in the project and run `decide`). If you need something it doesn't allow, say so in your final message instead of failing silently.

## 3. Fill in what the user asked for

`event: attributes.requested` means the user wants another attribute on the options. `event: options.suggested` means none of your options fit and the user added their own (its `suggested_by` is set): all you have is its label, and maybe a description or `url`. Either way, `pending_attribute_requests` lists each key with its description and the option IDs missing it, and `decide get D-12` prints them. Fill them in, best effort. For the user's own option, look it up as you did your own: every attribute the others have, plus `description` and `url` if you find them (a fill patch can set those too).

```sh
decide fill D-12 --json fill.json
```

where `fill.json` contains `[{"id": "redis", "attributes": {"risk": "medium"}}, {"id": "http", "attributes": {"risk": "low"}}]`.

Use the same value conventions as in step 1. **Mark values you estimated or inferred** with `attribute_meta`, so the GUI shows them as "≈ value" and the user knows what to double-check: `{"id": "redis", "attributes": {"price_sek": 11990}, "attribute_meta": {"price_sek": {"estimate": true, "source": "from memory, Nov 2026"}}}`. You can also set `attribute_meta` when creating options. If you genuinely can't determine a value, don't guess: mark it `"attribute_meta": {"<key>": {"unknown": true, "note": "why"}}`, so the user isn't left waiting for it.

Then run the `to keep waiting: …` command from the previous `decide wait` output, in the background.

## 4. Act on the decision

`decide get D-12 --json` returns `resolution.selected` (option IDs), `selected_options`, `resolution.note`, and the `goals` it serves. The note is the user's rationale or extra instructions; follow it. Where you act on the decision (commit, PR, Linear comment, a written summary), reference it as `[D-12: title](url)`.

If the work is tracked in an issue (Linear, Jira, GitHub, …), whether you created it or found it, link it back so the decision shows where it's being carried out:

```sh
decide issue D-12 https://linear.app/acme/issue/LIN-42
```

Link the issue for the work itself, not the one the question came from (that goes in `links`). You can link it as soon as it exists, before the decision is made. When an issue tracks the work toward a goal, link it to the goal the same way: `decide goal issue G-7 https://linear.app/acme/issue/LIN-40` (or `decide goal create … --issue URL`).

The user may revise a decision later (another `decision.resolved`, or `decision.reopened`). The latest resolution is authoritative. If `outdated_by` is non-empty, a decision this one depends on changed after it was made. Check with the user before acting on it. A revision may skip notifying you (e.g. a typo fix), so check `decide get` before acting on an older one.
