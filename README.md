# obsidian-hermes

Tag AI agents inside any Obsidian note. Write `@hermes <request>` (or `@claude` / `@codex`) on any line, save, and within seconds the agent acknowledges it in-place, does the work, and replies inline in the note — turning your vault into a first-class communication channel with [Hermes Agent](https://github.com/NousResearch/hermes-agent), [Claude Code](https://code.claude.com), and [Codex CLI](https://github.com/openai/codex).

```
note save
  → watcher daemon (inotify via Python watchdog)
  → "Enter = send": mentions still being typed on the last line are held
  → flips bare @<agent> → @<agent>/ack in the note   (dedup + visual receipt)
  → dispatch:
      @hermes → POST to Hermes webhook (HMAC-signed) → agent run replies inline,
                flips /done, pings your chat platform
      @claude → runs `claude -p` directly; watcher writes the inline reply
      @codex  → runs `codex exec` directly; watcher writes the inline reply
  → inline reply below the mention:  > 🤖 **<agent>** (date): ...
  → tag flips to /done (or /err on failure — edit back to bare tag to retry)
```

## Mention lifecycle

| Tag | Meaning |
|---|---|
| `@hermes <request>` | New request (triggers the watcher) |
| `@hermes/ack …` | Seen — agent run dispatched |
| `@hermes/done …` | Answered — reply is below the line |

Requests are free-form: research, drafting, reminders (the agent can schedule cron jobs), calendar events — anything the agent can do. The full note is provided as context.

## Requirements

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) with the gateway running and the webhook platform enabled
- Linux with systemd (user services) — the watcher uses inotify
- Python 3.10+ and [uv](https://docs.astral.sh/uv/) (or any venv tooling)

## Setup

### 1. Enable the Hermes webhook platform (if not already)

```bash
hermes config set platforms.webhook.enabled true
hermes config set platforms.webhook.extra.port 8644
hermes config set platforms.webhook.extra.secret "$(openssl rand -hex 32)"
# restart the gateway afterwards
```

### 2. Create the webhook subscription

```bash
hermes webhook subscribe obsidian-mention \
  --prompt "$(cat prompt.txt)" \
  --description "Obsidian vault @hermes mention -> agent run, inline reply + chat ping" \
  --skills "obsidian" \
  --deliver telegram \
  --deliver-chat-id "<your chat id>"
```

Note the returned per-route secret — the watcher signs its POSTs with it.

Give webhook agent runs file/terminal access (the default webhook toolset is web-only). In `~/.hermes/config.yaml`:

```yaml
platform_toolsets:
  webhook:
    - hermes-cli
```

(Must be a real YAML list — `hermes config set` writes lists as strings; edit the file.)

### 3. Install the watcher

```bash
uv venv ~/.hermes/venvs/obsidian-watcher
uv pip install --python ~/.hermes/venvs/obsidian-watcher/bin/python watchdog requests

cp watcher.py ~/.hermes/scripts/obsidian-mention-watcher.py
cp example.env ~/.config/obsidian-hermes-watcher.env   # then edit: vault path, webhook URL, secret
chmod 600 ~/.config/obsidian-hermes-watcher.env
cp obsidian-hermes-watcher.service ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now obsidian-hermes-watcher
```

### 4. Test

Drop `@hermes say hi` into a scratch note, save, and watch the tag flip to `/ack` then `/done` with an inline reply.

## Behavior details

- Watches `**/*.md` under the vault; ignores dot-directories (`.git`, `.obsidian`, `.trash`) and `templates/`
- 3-second debounce after the last write, so it doesn't fire mid-typing on editor autosave
- Mentions inside fenced code blocks are ignored — that's how you write the literal tag in docs without triggering it
- The ack rewrite happens **before** the POST, so a mention can never dispatch twice; if delivery fails after 3 retries it stays `/ack` (check the journal)
- On startup the watcher scans the whole vault for pending bare mentions (catches anything written while it was down)
- Works with Obsidian Sync / mobile: the watcher fires when sync lands the file on the host

## Troubleshooting

```bash
systemctl --user status obsidian-hermes-watcher
journalctl --user -u obsidian-hermes-watcher -n 50
curl http://localhost:8644/health          # webhook listener up?
grep obsidian-mention ~/.hermes/logs/gateway.log | tail
```

To re-trigger a mention, edit its tag back to bare `@hermes` and save.

### Pitfalls we hit so you don't have to

- **Never write the bare tag in prose in any vault note** — including docs about this feature. Use a code fence or write `@hermes/done`. (Yes, this repo's watcher triggered on its own documentation. Twice.)
- **Telegram forum-topic delivery:** if pings land outside your topics, your `thread_id` is probably stale — topics get recreated and keep their old IDs in config. Probe with raw Bot API `sendMessage` to find the live ID.
- Webhook agent runs use Hermes' standard gateway timeout (`agent.gateway_timeout`, default 30 min of *inactivity* — resets on every tool call), so long research tasks are fine. Each webhook delivery runs in its own isolated agent session.

## Deploying changes

The live copy runs from `~/.hermes/scripts/`. After editing `watcher.py` here:

```bash
./deploy.sh
```

## License

MIT
