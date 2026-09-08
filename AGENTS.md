# vault-agents — agent guide

Watcher daemon that turns `@hermes` / `@claude` / `@codex` mentions in an Obsidian vault into agent runs with inline replies. Python entrypoint (`watcher.py`) plus `vault_agents_note_runtime.py`, packaged with `pyproject.toml`.

For installing on a new host, follow [INSTALL_FOR_AGENTS.md](INSTALL_FOR_AGENTS.md).

## Layout

| File | Purpose |
|---|---|
| `watcher.py` | Watcher orchestration: inotify (Python watchdog), debounce, mention regex, ack/done tag flips, direct CLI dispatch (`hermes chat` / `claude -p` / `codex exec`) in worker threads, per-note hermes session store, Telegram ping via `hermes send` |
| `vault_agents_note_runtime.py` | Markdown parsing, request identity, serialized note updates, and per-path scheduling |
| `tests/` | Regression tests using temporary notes/homes and mocked subprocesses |
| `install.sh` / `install.py` | Repeatable per-user package and service installation; `--check`, `--no-start` |
| `pyproject.toml` | Python package metadata, dependency range, and CLI entrypoint |
| `INSTALL_FOR_AGENTS.md` | Agent-facing installation and operations runbook |
| `vault-agents-watcher.service` | systemd user unit template (installer fills placeholders; not directly copyable) |
| `example.env` | Reference for the env file options (VAULT_PATH + optional overrides) |

## Hermes per-note sessions

`@hermes` runs `hermes chat -Q -q <prompt>` (first mention in a note) or
`hermes chat -Q -q <prompt> --resume <session_id> --no-restore-cwd` (later
mentions), cwd=vault. The note→session map lives at
`~/.local/state/vault-agents/sessions.json` (env `SESSION_STATE_PATH`),
schema `{rel_note_path: {agent: {session_id, last_used}}}` — nested per-agent
so claude/codex resume can be added later without migration. Lazy TTL expiry
(`SESSION_TTL_HOURS`, default 72) on read. Per-note write locks cover Hermes
turns (new acknowledgements wait until the turn finishes); session locks also
serialize inherited notes sharing a conversation. Independent sessions run in
parallel. Fresh sessions get renamed `obsidian: <note name>` for a readable
`hermes sessions list`.
Spin-off inheritance: Hermes emits `VAULT_INHERIT: ["inbox/new-note.md"]`
in stdout. The watcher validates and registers paths after the turn returns;
only the watcher writes session JSON.
Stale session_id (pruned/deleted) → detected from stderr, auto-retries fresh.
Telegram summary ping goes via `hermes send -t telegram:<TELEGRAM_CHAT_ID>`;
empty/unset disables pings.

**Critical pitfall:** `hermes -z` (oneshot) silently ignores
`--resume`/`--continue` — it must be `hermes chat -Q -q`.

## Source of truth vs live deployment

Edit **here**, then run `./install.sh`. Never edit the installed copy directly.

The installer is the single supported installation and deployment workflow.

- Installed package and venv: `~/.local/share/vault-agents/venv/`
- Unit: `~/.config/systemd/user/vault-agents-watcher.service`
- Status: `systemctl --user status vault-agents-watcher`
- Logs: `journalctl --user -u vault-agents-watcher`
- Configuration: `~/.config/vault-agents-watcher.env` (private; never commit secrets)

## Behavior invariants (don't break these)

- **Enter-as-send:** a mention with any line below it dispatches after the 3s debounce; a mention on the very last line (no trailing newline) is held until the file is quiet for 8s (`STABILITY_SECONDS`). Prevents acking half-typed sentences during editor/sync write bursts.
- Regex matches bare `@<agent>` word-boundary only — not `/ack|/done|/err` suffixed, not `-` suffixed (npm scoped packages), not inside fenced code blocks or `inline code` spans (masked before matching).
- Ack is written in-file FIRST, then dispatch. Sync-collision guard: file is re-read just before the ack write; abort + reschedule if changed.
- Lifecycle: bare tag → `/ack` → `/done` (or `/err`; retry = edit back to the bare tag).
- No extra content is ever written to notes beyond tag flips and reply blockquotes: completion anchors on the acked line's content and the tag's occurrence index on it; an edited line falls back to the first remaining `/ack` for that agent.
- Reply writing: the WATCHER writes the blockquote for claude/codex and for hermes FAILURES (`/err`); on success the hermes agent writes its own result into the vault and flips /ack itself — the watcher only safety-net-flips a forgotten /ack to /done. Hermes stdout = Telegram summary only.
- All watcher note writes go through the serialized `update_note` path (optimistic re-read retry — not a filesystem compare-and-swap). Completion results are retained in memory across transient write conflicts and retried without rerunning the agent; they do not survive a daemon restart.
- Prompt excerpts are quote-safe: the current request's tag is removed and other bare mentions are `/done`-suffixed before they enter an agent prompt.
- Ignores dot-dirs, `.git`, `.obsidian`, `.trash`, `templates/`.
- Filesystem observation starts before the initial scan; the scan dispatches mentions written while the watcher was down.

## Pitfalls

- **Never write a bare mention tag in prose in any vault note or doc that lands in the vault** — it triggers the watcher. Use the `/done`-suffixed form or a fenced code block. (This repo lives outside the vault, so its README is safe.)
- systemd user services get a bare PATH — that's why CLAUDE_BIN/CODEX_BIN/HERMES_BIN env vars exist.
- Codex auth: `codex login --with-api-key` → `~/.codex/auth.json`; Claude: ANTHROPIC_API_KEY in the env file. Hermes uses its own `~/.hermes` config.
- The watcher uses the Python `watchdog` library rather than inotify-tools, so no root access is ever required.
- Same-note hermes mentions queue behind the per-note lock — two rapid mentions both reply, the second waits for the first.

## Testing a change

1. Run `python -m unittest discover -s tests -v` (needs `watchdog` importable — use the installed venv's python or any env with it), then `./install.sh`.
2. Edit a vault note: flip an existing `/done` tag back to the bare tag, save.
3. Watch `journalctl --user -u vault-agents-watcher -f` for pickup/ack/dispatch.
4. For session continuity: mention with a fact in one save, ask for it back in a second mention, verify the reply and `sessions.json`.
