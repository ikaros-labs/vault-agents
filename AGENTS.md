# vault-agents — agent guide

Watcher daemon that turns `@hermes` / `@claude` / `@codex` mentions in an Obsidian vault into agent runs with inline replies. Python entrypoint (`watcher.py`) plus `vault_agents_note_runtime.py`, packaged with `pyproject.toml`.

For installing on a new host, follow [INSTALL_FOR_AGENTS.md](INSTALL_FOR_AGENTS.md).

> Renamed from `obsidian-hermes` (2026-09) — it serves any agent, not just Hermes. All deployed artifacts (unit, script, venv, env file, state dir) migrated to the new names 2026-09-07.

## Layout

| File | Purpose |
|---|---|
| `watcher.py` | Watcher orchestration: inotify (Python watchdog), debounce, mention regex, ack/done tag flips, direct CLI dispatch (`hermes chat` / `claude -p` / `codex exec`) in worker threads, per-note hermes session store, Telegram ping via `hermes send` |
| `vault_agents_note_runtime.py` | Markdown parsing, request identity, serialized note updates, and per-path scheduling |
| `tests/test_watcher.py` | Regression tests using temporary notes and mocked subprocesses |
| `install.sh` / `install.py` | Repeatable per-user package and service installation; `--check`, `--no-start` |
| `pyproject.toml` | Python package metadata, dependency range, and CLI entrypoint |
| `INSTALL_FOR_AGENTS.md` | Agent-facing installation and operations runbook |
| `deploy.sh` | Copies both Python modules to the live path + restarts the systemd user unit |
| `vault-agents-watcher.service` | systemd user unit template |
| `example.env` | Template for the env file (VAULT_PATH + optional overrides) |

## Hermes per-note sessions (v3, 2026-09-07)

`@hermes` runs `hermes chat -Q -q <prompt>` (first mention in a note) or
`hermes chat -Q -q <prompt> --resume <session_id> --no-restore-cwd` (later
mentions), cwd=vault. The note→session map lives at
`~/.local/state/vault-agents/sessions.json` (env `SESSION_STATE_PATH`),
schema `{rel_note_path: {agent: {session_id, last_used}}}` — nested per-agent
so claude/codex resume can be added later without migration. Lazy TTL expiry
(`SESSION_TTL_HOURS`, default 72) on read. Per-note write locks cover Hermes
turns (new acknowledgements wait until the turn finishes); session locks also
serialize inherited notes sharing a conversation. Independent sessions run in
parallel. Fresh sessions
get renamed `obsidian: <note name>` for a readable `hermes sessions list`.
Spin-off inheritance: Hermes emits `VAULT_INHERIT: ["inbox/new-note.md"]`
in stdout. The watcher validates and registers paths after the turn returns;
only the watcher writes session JSON.
Stale session_id (pruned/deleted) → detected from stderr, auto-retries fresh.
Telegram summary ping goes via `hermes send -t telegram:<TELEGRAM_CHAT_ID>`
(bare lane, no thread). The webhook lane was REMOVED in v3.

**Critical pitfall:** `hermes -z` (oneshot) silently ignores
`--resume`/`--continue` — it must be `hermes chat -Q -q`. Verified 2026-09-07.

## Source of truth vs live deployment

Edit **here**, then run `./deploy.sh`. Never edit the live copy directly.

New installs use `~/.local/share/vault-agents/venv/` and a generated systemd unit.
`deploy.sh` detects that layout; otherwise it retains the legacy copy workflow below.
The checked-in service file is an installer template, not directly copyable.

- Legacy live script: `~/.hermes/scripts/vault-agents-watcher.py`
- Venv: `~/.hermes/venvs/vault-agents/` (watchdog; requests no longer needed)
- Unit: `systemctl --user status vault-agents-watcher` / `journalctl --user -u vault-agents-watcher`
- Secrets: `~/.config/vault-agents-watcher.env` (deliberately OUTSIDE `~/.hermes`, which is a git-pushed backup repo)

## Behavior invariants (don't break these)

- **Enter-as-send:** a mention with any line below it dispatches after the 3s debounce; a mention on the very last line (no trailing newline) is held until the file is quiet for 8s (`STABILITY_SECONDS`). Prevents acking half-typed sentences during Obsidian Sync bursts.
- Regex matches bare `@<agent>` word-boundary only — not `/ack|/done|/err` suffixed, not `-` suffixed (npm scoped packages), not inside fenced code blocks or `inline code` spans (masked before matching).
- Ack is written in-file FIRST, then dispatch. Sync-collision guard: file is re-read just before the ack write; abort + reschedule if changed.
- Lifecycle: bare tag → `/ack` → `/done` (or `/err`; retry = edit back to the bare tag).
- Reply writing: the WATCHER writes the blockquote for claude/codex and for hermes FAILURES (`/err`); on success the hermes agent writes its own result into the vault and flips /ack itself (v3.1) — watcher only safety-net-flips a forgotten /ack to /done. Hermes stdout = Telegram summary only.
- Ignores dot-dirs, `.git`, `.obsidian`, `.trash`, `templates/`.
- Initial scan on startup dispatches immediately (catches mentions written while down).

## Pitfalls

- **Never write a bare mention tag in prose in any vault note or doc that lands in the vault** — it triggers the watcher. Use the `/done`-suffixed form or a fenced code block. (This repo is outside the vault, so its README is safe.)
- systemd PATH is bare — that's why CLAUDE_BIN/CODEX_BIN/HERMES_BIN env vars exist.
- Codex auth: `codex login --with-api-key` → `~/.codex/auth.json`; Claude: ANTHROPIC_API_KEY in the env file. Hermes uses its own `~/.hermes` config.
- No sudo on the target host — hence Python watchdog venv rather than apt inotify-tools.
- Same-note hermes mentions queue behind the per-note lock — two rapid mentions both reply, second waits for the first.

## Testing a change

1. Run `python -m unittest discover -s tests -v`, then `./deploy.sh`
2. Edit a vault note: flip an existing `/done` tag back to the bare tag, save.
3. Watch `journalctl --user -u vault-agents-watcher -f` for pickup/ack/dispatch.
4. For session continuity: mention with a fact in one save, ask for it back in a second mention, verify the reply and `sessions.json`.
