# Per-Note Continuous Hermes Sessions (CLI lane)

**Goal:** Replace the webhook lane for `@hermes` mentions with direct `hermes chat` CLI dispatch so each note gets one persistent conversation (context survives across mentions), with inactivity-based expiry. No migration/backward compat required — webhook lane is removed outright.

**Architecture:** The watcher gains a third CLI runner (`hermes`) alongside claude/codex, plus a small persisted session map `{note_rel_path → {session_id, last_used}}`. First mention in a note runs `hermes chat -Q -q <prompt>` and captures the printed `session_id:`; later mentions run with `--resume <id>`. Per-note locks serialize runs. Watcher writes the inline blockquote reply itself (same as claude/codex) and pings Telegram via `hermes send`.

**Verified in smoke test (2026-09-07):**
- `hermes chat -Q -q "..." --resume <id>` resumes correctly, prints `↻ Resumed session <id> ...` banner + `session_id: <id>` line + reply text; context (incl. multi-turn) persists.
- ⚠️ `hermes -z` (oneshot) SILENTLY IGNORES `--resume`/`--continue` — never use `-z` here. (Consider reporting upstream.)
- `--continue <name>` resolves by *title*; titles are unset on `-Q -q` sessions unless renamed. We resume by **session_id** (robust), not name.

---

## Design decisions

1. **Session key = vault-relative note path** (e.g. `inbox/some note.md`). No frontmatter (user rejected — note pollution).
2. **State file:** `~/.local/state/obsidian-hermes/sessions.json` — schema is per-agent-nested (future-proof for claude/codex resume, only `hermes` populated for now):
   ```json
   {"inbox/some note.md": {"hermes": {"session_id": "20260907_...", "last_used": 1757254000}}}
   ```
   - Env override: `SESSION_STATE_PATH`. Written atomically (tmp + rename) under a module lock. Re-read from disk before every dispatch (so external appenders — see #5 — are picked up).
3. **Expiry:** `SESSION_TTL_HOURS` env, default `72`. On dispatch: if `now - last_used > TTL`, drop the entry and start a fresh session. No background reaper needed — lazy expiry on next mention. Old hermes sessions in state.db just age out via normal pruning.
4. **Concurrency:** per-session-key `threading.Lock` (dict + guard lock). Second mention for the same note blocks until the first finishes (a resumed session cannot run two turns concurrently). Different notes run in parallel. Claude/codex paths unchanged (no locks — stateless).
5. **Spin-off note inheritance (agent-created notes share the session):** the hermes prompt tells the agent: *"If you create a new note as part of this task and want future mentions there to continue THIS conversation, append an entry to SESSION_STATE_PATH mapping the new note's vault-relative path to session_id {session_id} (read-modify-write the JSON)."* Watcher re-reads state before each dispatch, so the inherited mapping is honored. `last_used` gets refreshed by the watcher on first use. Accepted MVP risk: agent forgets → spin-off starts cold; race on concurrent state-file writes is negligible (single-user, serialized per note).
6. **Reply writing:** watcher writes the blockquote via existing `write_reply()` (hermes joins CLI_RUNNERS). Prompt instructs the agent NOT to write the reply into the note itself (it did under the webhook lane — this reverses that; the reply is stdout). Agent may still edit vault files when the request explicitly asks.
7. **Telegram ping:** after `write_reply`, watcher runs `hermes send telegram <TELEGRAM_CHAT_ID> "<1-line summary>"` (fire-and-forget subprocess, 30s timeout). Summary = first 200 chars of reply + note name + ok/err. Env: `TELEGRAM_CHAT_ID` (default `233267520`), empty = disabled. No thread_id → bare lane, matching the 2026-08-14 decision.
8. **Session titles (nice-to-have, last task):** after creating a new session, run `hermes sessions rename <id> "obsidian: <note name>"` so `hermes sessions list` is readable.
9. **Removed:** `post_webhook()`, `WEBHOOK_URL`, `WEBHOOK_SECRET`, HMAC imports, webhook branch in dispatch, `prompt.txt`. Also run `hermes webhook remove obsidian-mention` at deploy time and drop `platform_toolsets.webhook` note from docs (config key can stay, harmless).

## Env / config changes

`~/.config/obsidian-hermes-watcher.env` (and `example.env`):
- REMOVE: `WEBHOOK_URL`, `WEBHOOK_SECRET`
- ADD: `HERMES_BIN` (default `~/.local/bin/hermes`), `SESSION_STATE_PATH` (default above), `SESSION_TTL_HOURS=72`, `TELEGRAM_CHAT_ID=233267520`, `HERMES_TIMEOUT_SECONDS=1200` (hermes runs can be longer than claude/codex; separate knob from `CLI_TIMEOUT_SECONDS`)

systemd unit: unchanged (env file already referenced).

---

## Tasks

### Task 1: Session store module (in `watcher.py`)
Add near the top:
- `STATE_PATH`, `SESSION_TTL_HOURS`, `HERMES_BIN`, `TELEGRAM_CHAT_ID`, `HERMES_TIMEOUT_SECONDS` env reads.
- `_state_lock = threading.Lock()`
- `load_sessions() -> dict` — read JSON, `{}` on missing/corrupt (log warning on corrupt, keep a `.corrupt` backup copy).
- `save_sessions(d)` — mkdir -p parent, write `path.tmp`, `os.replace`.
- `get_session_id(rel_path) -> str | None` — load fresh, drop entry if expired (and save), return id or None.
- `set_session(rel_path, session_id)` — load fresh, upsert with `last_used=time.time()`, save. Also `touch_session(rel_path)` for refresh-on-use.

**Verify:** unit-test by importing watcher in a venv python and exercising the three functions against a tmp path.

### Task 2: `run_hermes()` runner + output parsing
```python
HERMES_SESSION_LINE = re.compile(r"^session_id:\s*(\S+)\s*$", re.MULTILINE)
HERMES_NOISE = re.compile(r"^(↻ Resumed session\b|session_id:\s)")

def run_hermes(prompt, resume_id=None):
    """Returns (ok, reply_text, session_id|None)."""
    cmd = [HERMES_BIN, "chat", "-Q", "-q", prompt]
    if resume_id:
        cmd += ["--resume", resume_id, "--no-restore-cwd"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           timeout=HERMES_TIMEOUT_SECONDS, cwd=str(VAULT))
    except subprocess.TimeoutExpired:
        return False, f"timed out after {HERMES_TIMEOUT_SECONDS}s", None
    m = HERMES_SESSION_LINE.search(r.stdout or "")
    sid = m.group(1) if m else None
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "unknown error").strip()[-500:], sid
    reply = "\n".join(ln for ln in r.stdout.split("\n") if not HERMES_NOISE.match(ln)).strip()
    return (bool(reply), reply or "empty hermes output", sid)
```
Pitfalls to check during implementation:
- Confirm exact position/format of the `session_id:` line on a FRESH (non-resumed) `-Q -q` run — smoke test only confirmed the resumed shape. Adjust regexes if the banner differs.
- If a stored session_id no longer exists (pruned/deleted), `--resume` may error — on failure containing "not found"/similar, retry ONCE without `--resume` and overwrite the mapping. Log it.
- `cwd=VAULT` + `--no-restore-cwd` keeps every turn rooted in the vault (AGENTS.md, relative paths).

**Verify:** run `run_hermes("say OK")` manually from the venv; then a resume round-trip with a codeword like the smoke test; then delete the test session.

### Task 3: Hermes prompt template
New `HERMES_PROMPT` (separate from CLI_PROMPT):
- Same note-path / request-line / context sections as CLI_PROMPT.
- "Your stdout reply is inserted into the note as a blockquote by the watcher — output ONLY the answer (markdown ok, no preamble). Do NOT edit the mention note to add your reply yourself."
- "You may edit/create vault files when the request explicitly asks."
- New-note session inheritance paragraph (design decision #5) with `{session_id}` and `{state_path}` interpolated. Include exact JSON entry shape and 'read-modify-write, preserve other keys'.
- "Do not write a bare @ hermes tag into any note — use the /done form if you must refer to it." (existing invariant)

### Task 4: Dispatch wiring
In `process_file()` dispatch loop: remove the `if agent == "hermes": payload/post_webhook` branch; hermes now goes down the worker-thread path. In `dispatch_cli()`:
- If `agent == "hermes"`: compute `rel = str(path.relative_to(VAULT))`, acquire per-key lock, `sid = get_session_id(rel)`, build HERMES_PROMPT (session_id placeholder = `sid or "(new session — will be assigned)"`; if new, do a post-run `set_session` with returned sid, then the inheritance instruction references the REAL sid only on resumed turns — acceptable: on the first turn the agent is told 'the watcher records this session automatically; for NEW notes you create, re-read the state file to find this note's session_id'). Simpler alternative (choose during impl): always tell the agent to copy this note's entry from the state file — avoids interpolating sid entirely.
- Run `run_hermes(prompt, sid)`; on success `set_session(rel, returned_sid)` (fresh) or `touch_session(rel)` (resumed).
- `write_reply(path, "hermes", ...)` (unchanged function).
- Telegram ping helper `notify_telegram(ok, path, reply)` via `hermes send telegram ...` subprocess; swallow errors with a log line.
- Per-key lock table: `_note_locks: dict[str, threading.Lock]` + `_note_locks_guard`.

### Task 5: Strip webhook code + env
Remove `post_webhook`, `hmac` import, `WEBHOOK_URL`/`SECRET` constants, the `if not SECRET` startup check, requests import if now unused. Update module docstring + `example.env` + `AGENTS.md` in repo (架构 section: hermes = CLI lane with sessions). Delete `prompt.txt`.

### Task 6: Deploy + live test
1. `./deploy.sh`; `journalctl --user -u obsidian-hermes-watcher -f`.
2. In a scratch vault note: write a mention asking hermes to remember a codeword → expect /ack → /done + blockquote + Telegram ping.
3. Second mention in the same note asking for the codeword → must answer correctly (session reuse; check log line shows resume).
4. Mention in a DIFFERENT note asking for the codeword → must NOT know it (isolation).
5. Ask hermes in the note to create a new spin-off note and register it for the same session; then mention it in the spin-off note asking for the codeword → should answer (inheritance path).
6. Two rapid mentions in one note → verify serialization in logs, both replied.
7. `hermes webhook remove obsidian-mention`; confirm `hermes webhook list` clean.
8. `hermes sessions list` shows `obsidian: <note>` titled sessions.

### Task 7: Docs/skill updates
- Update `~/.hermes/skills/note-taking/obsidian-hermes-mentions/SKILL.md`: new architecture diagram, state file path, TTL, resume-by-id, `-z` pitfall, removal of webhook lane.
- Commit + push repo.

## Risks / open questions
- **Fresh-run output shape** for `session_id:` line unverified (Task 2 pitfall) — first thing to check.
- **Startup latency:** each mention pays full CLI startup (config, memory, skills). Expect noticeably slower first token than webhook lane. Acceptable per discussion.
- **Interim status lines** (fallback-model notices, context-pressure warnings) may appear in `-Q` stdout and end up in the note reply. Observe during live test; add noise regexes if needed.
- **Very long sessions** → compaction inside a CLI run = slow turn. Lazy TTL keeps most sessions short.
- **State file loss** = all notes start cold (no data loss otherwise). It sits outside the ~/.hermes backup repo; acceptable, or move under ~/.hermes/ if we want it backed up (it contains no secrets) — decide at impl time.
- Telegram ping via `hermes send`: verify the subcommand syntax (`hermes send --help`) before wiring.
