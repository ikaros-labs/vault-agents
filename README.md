# obsidian-hermes

Tag AI agents inside any Obsidian note. Write `@hermes <request>` (or `@claude` / `@codex`) on any line, save, and within seconds the agent acknowledges it in-place, does the work, and replies inline in the note — turning your vault into a first-class communication channel with [Hermes Agent](https://github.com/NousResearch/hermes-agent), [Claude Code](https://code.claude.com), and [Codex CLI](https://github.com/openai/codex).

`@hermes` mentions get **per-note continuous sessions**: every mention in a note is another turn in the same conversation, so the agent remembers earlier context. Sessions expire after a period of inactivity (default 72 h).

```
note save
  → watcher daemon (inotify via Python watchdog)
  → "Enter = send": mentions still being typed on the last line are held
  → flips bare @<agent> → @<agent>/ack in the note   (dedup + visual receipt)
  → dispatch (all agents run as CLI subprocesses in worker threads):
      @hermes → `hermes chat -Q -q` — resumes the note's session if one
                exists; the AGENT writes its result into the vault itself
                (inline reply, edits, new notes) and flips /done; stdout
                becomes a short Telegram notification
      @claude → runs `claude -p`; watcher writes the inline reply
      @codex  → runs `codex exec`; watcher writes the inline reply
  → inline reply below the mention:  > 🤖 **<agent>** (date): ...
  → tag flips to /done (or /err on failure — edit back to bare tag to retry)
```

## Mention lifecycle

| Tag | Meaning |
|---|---|
| `@hermes <request>` | New request (triggers the watcher) |
| `@hermes/ack …` | Seen — agent run dispatched |
| `@hermes/done …` | Answered — result is in the note |
| `@hermes/err …` | Failed — reason below; edit back to the bare tag to retry |

Requests are free-form: research, drafting, restructuring, reminders (the agent can schedule cron jobs), calendar events — anything the agent can do. The note is provided as context, and for hermes the whole prior conversation of that note too.

## Per-note sessions (hermes only)

- Mapping lives in `~/.local/state/obsidian-hermes/sessions.json`:
  `{"<note path>": {"hermes": {"session_id": "...", "last_used": <epoch>}}}`
- First mention in a note starts a session; later mentions run with `--resume <id>`.
- Lazy TTL expiry: a mention after `SESSION_TTL_HOURS` (default 72) of inactivity starts fresh.
- Same-note runs are serialized with a per-note lock; different notes run in parallel.
- Sessions are titled `obsidian: <note name>` — inspect with `hermes sessions list` / `export`.
- If the agent creates a spin-off note, it can register it in the state file so mentions there continue the same conversation.
- Force a fresh session: delete the note's entry from the state file.
- claude/codex stay stateless one-shots (the state schema is per-agent-nested, so adding resume for them later needs no migration).

## Requirements

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) installed and configured (`hermes chat` working) — no gateway/webhook needed
- Optionally `claude` / `codex` CLIs for those agents
- Linux with systemd (user services) — the watcher uses inotify
- Python 3.10+ and [uv](https://docs.astral.sh/uv/) (or any venv tooling)

## Setup

### 1. Install the watcher

```bash
uv venv ~/.hermes/venvs/obsidian-watcher
uv pip install --python ~/.hermes/venvs/obsidian-watcher/bin/python watchdog

cp watcher.py ~/.hermes/scripts/obsidian-mention-watcher.py
cp example.env ~/.config/obsidian-hermes-watcher.env   # then edit: vault path (+ optional overrides)
chmod 600 ~/.config/obsidian-hermes-watcher.env
cp obsidian-hermes-watcher.service ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now obsidian-hermes-watcher
```

### 2. Configure (env file)

| Var | Default | Purpose |
|---|---|---|
| `VAULT_PATH` | `~/obsidian-vault` | Vault root to watch |
| `HERMES_BIN` | `~/.local/bin/hermes` | hermes binary (systemd PATH is bare) |
| `CLAUDE_BIN` / `CODEX_BIN` | `~/.npm-global/bin/…` | optional CLI agents |
| `ANTHROPIC_API_KEY` | — | auth for `claude -p` |
| `HERMES_TIMEOUT_SECONDS` | `1200` | per-run cap for hermes |
| `CLI_TIMEOUT_SECONDS` | `600` | per-run cap for claude/codex |
| `SESSION_STATE_PATH` | `~/.local/state/obsidian-hermes/sessions.json` | note→session map |
| `SESSION_TTL_HOURS` | `72` | session inactivity expiry |
| `TELEGRAM_CHAT_ID` | — | chat for summary pings via `hermes send` (empty = off) |

### 3. Test

Drop `@hermes say hi` into a scratch note, save, and watch the tag flip to `/ack` then `/done` with an inline reply. Mention again in the same note — it remembers the first exchange.

## Behavior details

- Watches `**/*.md` under the vault; ignores dot-directories (`.git`, `.obsidian`, `.trash`) and `templates/`
- 3-second debounce after the last write, so it doesn't fire mid-typing on editor autosave; a mention on the file's very last line (no newline after it) is held until the file is quiet for ~8 s ("Enter = send")
- Mentions inside fenced code blocks and `inline code` are ignored — that's how you write the literal tag in docs without triggering it
- The ack rewrite happens **before** dispatch, so a mention can never dispatch twice
- On startup the watcher scans the whole vault for pending bare mentions (catches anything written while it was down)
- Works with Obsidian Sync / mobile: the watcher fires when sync lands the file on the host
- Hermes success path: the agent flips the tag itself; the watcher safety-net-flips a forgotten `/ack` → `/done`. Failure path: the watcher writes the ⚠️ `/err` blockquote (the agent may have died before touching the note)

## Troubleshooting

```bash
systemctl --user status obsidian-hermes-watcher
journalctl --user -u obsidian-hermes-watcher -n 50
cat ~/.local/state/obsidian-hermes/sessions.json     # note → session map
hermes sessions list                                  # look for "obsidian: <note>"
```

To re-trigger a mention, edit its tag back to bare `@hermes` and save.

### Pitfalls we hit so you don't have to

- **`hermes -z` (oneshot) silently ignores `--resume`/`--continue`** — it always creates a fresh session. Session continuity requires `hermes chat -Q -q … --resume <id>`.
- **`-Q` prints `session_id:` to stderr, not stdout** — stdout is only the reply text.
- **Never write the bare tag in prose in any vault note** — including docs about this feature. Use a code fence or write `@hermes/done`. (Yes, this repo's watcher triggered on its own documentation. Twice.)
- **Telegram forum-topic delivery:** if pings land outside your topics, your `thread_id` is probably stale — topics get recreated and keep their old IDs in config. Probe with raw Bot API `sendMessage` to find the live ID.

## Deploying changes

The live copy runs from `~/.hermes/scripts/`. After editing `watcher.py` here:

```bash
./deploy.sh
```

## License

MIT
