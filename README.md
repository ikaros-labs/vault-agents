# vault-agents

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

- Mapping lives in `~/.local/state/vault-agents/sessions.json`:
  `{"<note path>": {"hermes": {"session_id": "...", "last_used": <epoch>}}}`
- First mention in a note starts a session; later mentions run with `--resume <id>`.
- Lazy TTL expiry: a mention after `SESSION_TTL_HOURS` (default 72) of inactivity starts fresh.
- Watcher writes and new acknowledgements in a note wait while Hermes edits that note.
- Hermes turns are serialized by note and session identity; inherited notes sharing a session cannot run concurrent turns. Independent sessions run in parallel.
- Sessions are titled `obsidian: <note name>` — inspect with `hermes sessions list` / `export`.
- For spin-off notes, Hermes emits `VAULT_INHERIT: ["inbox/new-note.md"]` in its output. The watcher validates the paths and registers them alongside the parent after saving the session. Agents must not edit the state JSON.
- Force a fresh session: delete the note's entry from the state file.
- claude/codex stay stateless one-shots (the state schema is per-agent-nested, so adding resume for them later needs no migration).

## Install

**Delegating to an agent?** Give it this instruction:

> Install vault-agents following https://github.com/ikaros-labs/vault-agents/blob/main/INSTALL_FOR_AGENTS.md

The complete [agent installation guide](INSTALL_FOR_AGENTS.md) covers discovery,
configuration, verification, upgrades, and removal.

Requirements: Linux with systemd user services, Python 3.10+, and at least one
installed and authenticated agent CLI (`hermes`, `claude`, or `codex`). The installer
uses `uv` when available, otherwise Python's `venv` and `pip` (some distributions
require the `python3-venv` package). No root access is needed for the installation.

Clone this repository **outside your vault**, then run:

```bash
git clone https://github.com/ikaros-labs/vault-agents.git
cd vault-agents
./install.sh --vault "/absolute/path/to/your/vault"
```

This installs the Python package and its declared dependencies in
`~/.local/share/vault-agents/venv`, creates a private configuration file,
detects CLI paths, and enables and starts the systemd user service. Starting the
service immediately processes existing bare mentions throughout the vault.
Telegram notifications are disabled until configured.

To check prerequisites first, append `--check`. To install files without starting
or restarting the service, append `--no-start`. `--check` validates local
prerequisites; it does not test provider authentication.

Existing `~/.config/vault-agents-watcher.env` files are preserved byte-for-byte.
For an existing installation, run `./install.sh` without `--vault`; edit that
file directly to change the vault or CLI paths.

### Configuration

| Var | Default | Purpose |
|---|---|---|
| `VAULT_PATH` | `~/obsidian-vault` | Vault root to watch |
| `HERMES_BIN` | `~/.local/bin/hermes` | hermes binary (systemd PATH is bare) |
| `CLAUDE_BIN` / `CODEX_BIN` | `~/.npm-global/bin/…` | optional CLI agents |
| `ANTHROPIC_API_KEY` | — | auth for `claude -p` |
| `HERMES_TIMEOUT_SECONDS` | `1200` | per-run cap for hermes |
| `CLI_TIMEOUT_SECONDS` | `600` | per-run cap for claude/codex |
| `SESSION_STATE_PATH` | `~/.local/state/vault-agents/sessions.json` | note→session map |
| `SESSION_TTL_HOURS` | `72` | session inactivity expiry |
| `TELEGRAM_CHAT_ID` | — | chat for summary pings via `hermes send` (empty = off) |

### Verify

Write a short request for your configured agent in a scratch note, save with a trailing newline, and watch the tag flip to `/ack` then `/done` with an inline reply. For Hermes, mention again in the same note to verify session continuity.

## Behavior details

- Watches `**/*.md` under the vault; ignores dot-directories (`.git`, `.obsidian`, `.trash`) and `templates/`
- 3-second debounce after the last write, so it doesn't fire mid-typing on editor autosave; a mention on the file's very last line (no newline after it) is held until the file is quiet for ~8 s ("Enter = send")
- Mentions inside fenced code blocks and `inline code` are ignored — that's how you write the literal tag in docs without triggering it
- The ack rewrite happens **before** dispatch. Watcher pickup and writes are serialized per note. Nothing extra is written to the note: reply placement anchors on the acked line's content and the tag's position on it (so multiple mentions on one line still resolve correctly); if the line is edited mid-run, completion falls back to the first remaining `/ack` tag for that agent. Prompt excerpts remove the current request tag and suffix other bare mentions so they are safe to quote.
- External editor/sync conflicts are detected by re-reading before writing; this is best-effort, not a filesystem compare-and-swap. Completion conflicts retry without rerunning the agent. Pending results are in memory: a daemon restart still requires manually retrying stranded `/ack` tags.
- On startup the watcher scans the whole vault for pending bare mentions (catches anything written while it was down)
- Works with Obsidian Sync / mobile: the watcher fires when sync lands the file on the host
- Hermes success path: the agent flips the tag itself; the watcher safety-net-flips a forgotten `/ack` → `/done`. Failure path: the watcher writes the ⚠️ `/err` blockquote (the agent may have died before touching the note)

## Troubleshooting

```bash
systemctl --user status vault-agents-watcher
journalctl --user -u vault-agents-watcher -n 50
cat ~/.local/state/vault-agents/sessions.json     # note → session map
hermes sessions list                                  # look for "obsidian: <note>"
```

To re-trigger a mention, edit its tag back to bare `@hermes` and save.

### Pitfalls we hit so you don't have to

- **`hermes -z` (oneshot) silently ignores `--resume`/`--continue`** — it always creates a fresh session. Session continuity requires `hermes chat -Q -q … --resume <id>`.
- **`-Q` prints `session_id:` to stderr, not stdout** — stdout is only the reply text.
- **Never write the bare tag in prose in any vault note** — including docs about this feature. Use a code fence or write `@hermes/done`. (Yes, this repo's watcher triggered on its own documentation. Twice.)
- **Telegram forum-topic delivery:** if pings land outside your topics, your `thread_id` is probably stale — topics get recreated and keep their old IDs in config. Probe with raw Bot API `sendMessage` to find the live ID.

## Deploying changes

After editing the Python sources here, run the regression tests, then:

```bash
./install.sh
```

For updates from upstream, run `git pull --ff-only` and `./install.sh` in the
checkout. A restart interrupts active runs;
wait for pending acknowledgements to complete before upgrading.

See [the agent guide](INSTALL_FOR_AGENTS.md#remove) for removal and rollback.

## Regression tests

With `watchdog` installed in your Python environment:

```bash
python -m unittest discover -s tests -v
```

The tests use temporary notes and mocked agent processes; they do not touch the live vault.

## License

MIT
