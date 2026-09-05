# obsidian-hermes — agent guide

Watcher daemon that turns `@hermes` / `@claude` / `@codex` mentions in an Obsidian vault into agent runs with inline replies. Single-file Python (`watcher.py`), no build step.

## Layout

| File | Purpose |
|---|---|
| `watcher.py` | The whole watcher: inotify (Python watchdog), debounce, mention regex, ack/done tag flips, Hermes webhook POST (HMAC V2), direct `claude -p` / `codex exec` CLI dispatch in worker threads |
| `deploy.sh` | Copies `watcher.py` to the live path + restarts the systemd user unit |
| `obsidian-hermes-watcher.service` | systemd user unit template |
| `example.env` | Template for the env file (VAULT_PATH, WEBHOOK_URL, WEBHOOK_SECRET, CLAUDE_BIN, CODEX_BIN) |
| `prompt.txt` | Webhook subscription prompt for the Hermes gateway route |

## Source of truth vs live deployment

Edit **here**, then run `./deploy.sh`. Never edit the live copy directly.

- Live script: `~/.hermes/scripts/obsidian-mention-watcher.py`
- Venv: `~/.hermes/venvs/obsidian-watcher/` (watchdog + requests)
- Unit: `systemctl --user status obsidian-hermes-watcher` / `journalctl --user -u obsidian-hermes-watcher`
- Secrets: `~/.config/obsidian-hermes-watcher.env` (deliberately OUTSIDE `~/.hermes`, which is a git-pushed backup repo)

## Behavior invariants (don't break these)

- **Enter-as-send:** a mention with any line below it dispatches after the 3s debounce; a mention on the very last line (no trailing newline) is held until the file is quiet for 8s (`STABILITY_SECONDS`). Prevents acking half-typed sentences during Obsidian Sync bursts.
- Regex matches bare `@<agent>` word-boundary only — not `/ack|/done|/err` suffixed, not `-` suffixed (npm scoped packages), not inside fenced code blocks or `inline code` spans (masked before matching).
- Ack is written in-file FIRST, then dispatch. Sync-collision guard: file is re-read just before the ack write; abort + reschedule if changed.
- Lifecycle: bare tag → `/ack` → `/done` (or `/err`; retry = edit back to the bare tag).
- HMAC: `X-Webhook-Signature-V2` = HMAC-SHA256 of `"<unix_ts>.<body>"`, plus `X-Webhook-Timestamp`, ±300s window.
- Ignores dot-dirs, `.git`, `.obsidian`, `.trash`, `templates/`.
- Initial scan on startup dispatches immediately (catches mentions written while down).

## Pitfalls

- **Never write a bare mention tag in prose in any vault note or doc that lands in the vault** — it triggers the watcher. Use the `/done`-suffixed form or a fenced code block. (This repo is outside the vault, so its README is safe.)
- systemd PATH lacks `~/.npm-global/bin` — that's why CLAUDE_BIN/CODEX_BIN env vars exist.
- Codex auth: `codex login --with-api-key` → `~/.codex/auth.json`; Claude: ANTHROPIC_API_KEY in the env file.
- No sudo on the target host — hence Python watchdog venv rather than apt inotify-tools.

## Testing a change

1. `./deploy.sh`
2. Edit a vault note: flip an existing `/done` tag back to the bare tag, save.
3. Watch `journalctl --user -u obsidian-hermes-watcher -f` for pickup/ack/dispatch.
