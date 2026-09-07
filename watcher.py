#!/usr/bin/env python3
"""Obsidian agent mention watcher.

Watches the Obsidian vault for edits to *.md files. When a line contains a
bare agent mention (@hermes, @claude, @codex — not .../ack, /done, /err), it:
  1. rewrites the tag -> @<agent>/ack in the note (dedup + visual receipt)
  2. dispatches the request by running the agent CLI as a subprocess in a
     worker thread; the WATCHER writes the reply blockquote and flips
     /ack -> /done (or /err).

hermes gets PER-NOTE CONTINUOUS SESSIONS: the first mention in a note starts
a session (`hermes chat -Q -q`), later mentions resume it (`--resume <id>`),
so conversation context persists within a note. Mapping note -> session_id
lives in SESSION_STATE_PATH with lazy TTL expiry. claude/codex stay
stateless (one-shot per mention).

Dispatch rules ("Enter = send"):
  - A mention followed by ANY further line (even blank / trailing newline)
    is considered finished -> dispatched on the normal debounce.
  - A mention on the very last line of the file (no newline after it) is
    probably still being typed -> held until the whole file has been quiet
    for STABILITY_SECONDS, then dispatched.

Run as a systemd user service. Config via env vars (see unit file):
  VAULT_PATH
Optional:
  CLAUDE_BIN, CODEX_BIN, HERMES_BIN (absolute paths; systemd PATH is bare)
  CLI_TIMEOUT_SECONDS (default 600), HERMES_TIMEOUT_SECONDS (default 1200)
  SESSION_STATE_PATH (default ~/.local/state/vault-agents/sessions.json)
  SESSION_TTL_HOURS (default 72)
  TELEGRAM_CHAT_ID (default 233267520; empty string disables pings)
"""

import datetime
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

VAULT = Path(os.environ.get("VAULT_PATH", os.path.expanduser("~/obsidian-vault")))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", os.path.expanduser("~/.npm-global/bin/claude"))
CODEX_BIN = os.environ.get("CODEX_BIN", os.path.expanduser("~/.npm-global/bin/codex"))
HERMES_BIN = os.environ.get("HERMES_BIN", os.path.expanduser("~/.local/bin/hermes"))
CLI_TIMEOUT_SECONDS = int(os.environ.get("CLI_TIMEOUT_SECONDS", "600"))
HERMES_TIMEOUT_SECONDS = int(os.environ.get("HERMES_TIMEOUT_SECONDS", "1200"))
SESSION_STATE_PATH = Path(os.environ.get(
    "SESSION_STATE_PATH",
    os.path.expanduser("~/.local/state/vault-agents/sessions.json"),
))
SESSION_TTL_HOURS = float(os.environ.get("SESSION_TTL_HOURS", "72"))
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "233267520")
DEBOUNCE_SECONDS = 3.0
STABILITY_SECONDS = 8.0  # quiet time required for a trailing-line mention
CONTEXT_LINES = 20
IGNORE_DIRS = {".git", ".obsidian", ".trash", "templates"}

AGENTS = ("hermes", "claude", "codex")
# Bare @<agent>: word boundary before, not followed by /ack|/done|/err, a word
# char, or a hyphen (avoids e.g. @claude-plugins-official).
MENTION_RE = re.compile(r"(?<![\w/])@(hermes|claude|codex)(?![-/\w])", re.IGNORECASE)
# Inline code spans — mentions inside `backticks` are documentation, not requests.
INLINE_CODE_RE = re.compile(r"`[^`]*`")

CLI_PROMPT = """You are answering a mention inside the user's Obsidian note.
Note file: {note_path}
The request line: {mention_line}

Surrounding note context:
---
{context}
---

Answer the request on the mention line, using the note context. Your entire
stdout reply will be inserted into the note as a quoted reply, so respond
with ONLY the answer text (markdown ok, no preamble, no meta commentary).
Be concise unless the task demands length. Do not edit any files in the
vault yourself unless the request explicitly asks you to modify a file."""

HERMES_PROMPT = """A mention of you was found in an Obsidian note.
This conversation is the PERSISTENT session for this note — earlier mentions
in the same note are earlier turns in this conversation.

Note: {note_path}
Mention line: {mention_line}

Context around the mention:
---
{context}
---

You are a COLLABORATOR in this vault, not a reply bot. Act like a thoughtful
human assistant who was tagged in a doc: understand the intent, do the work,
and leave the vault better organized than you found it.

1. Load the 'obsidian' skill. Read the FULL note to understand what the
   mention is part of (a todo item, a draft, a question, a list, a heading).
2. Do what the request asks — research, drafting, restructuring, reminders
   (cronjob tool), calendar events, file edits, anything you can do.
3. Choose the output form and placement with YOUR OWN JUDGMENT:
   - Short factual answer -> write it inline right where the mention is.
   - Substantial output (research, long drafts) -> create a NEW note (in
     inbox/ unless context clearly says otherwise) and link it with a
     [[wikilink]] from where the mention was.
   - Mention attached to a task (e.g. a todo line ending in "research") ->
     do the work in a result note and put the [[wikilink]] on the task line.
   - Match the vault's style and AGENTS.md conventions in anything you write.
4. MECHANICS: unlike claude/codex, YOU own the note. Write your result into
   the vault yourself (inline reply below the mention, an edit, a new note —
   whatever fits per rule 3). An inline reply should be a blockquote in the
   established style: `> 🤖 **hermes** (YYYY-MM-DD HH:MM):` followed by
   quoted lines. Then flip the mention's tag suffix from /ack to /done
   yourself. You may also rewrite the mention fragment entirely (e.g.
   replace it with a [[wikilink]] on a todo line) — the preferred,
   human-like outcome; in that case no /done tag is needed. Either way:
   after a successful run the note must contain no /ack-suffixed tag.
5. If you CREATE a note and future mentions in it should continue THIS
   conversation, register it: in the JSON file {state_path}, copy this
   note's entry (key "{rel_path}") to a new key with the new note's
   vault-relative path (keep all other keys intact).
6. HARD RULES:
   - NEVER write a bare agent tag ("@" + agent name, no suffix) into any
     vault note — it retriggers the watcher. Use the /done form or a
     `code span`.
   - Never git commit the vault.
7. Your stdout is NOT inserted into the note — it is sent to the user's
   Telegram as a notification. End with 1-3 lines: what you did and where
   the result lives."""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("vault-agents")

# Trailing-line mentions waiting for the file to go quiet:
#   path_str -> (full_text_sha1, first_seen_monotonic)
_pending = {}
_pending_timers = {}
_pending_lock = threading.Lock()


def is_ignored(path: Path) -> bool:
    try:
        rel = path.relative_to(VAULT)
    except ValueError:
        return True
    return any(part in IGNORE_DIRS or part.startswith(".") for part in rel.parts[:-1]) or path.name.startswith(".")


def mask_inline_code(line: str) -> str:
    """Blank out `inline code` spans so mentions inside them don't match."""
    return INLINE_CODE_RE.sub(lambda s: "`" + "·" * (len(s.group()) - 2) + "`", line)


def ack_line(line: str) -> str:
    """Flip the first real (non-code-span) bare mention to /ack."""
    m = MENTION_RE.search(mask_inline_code(line))
    if not m:
        return line
    return line[: m.start()] + f"@{m.group(1).lower()}/ack" + line[m.end():]


def find_mentions(lines):
    """Yield (line_index, line, agent) for bare mentions outside code fences."""
    in_fence = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = MENTION_RE.search(mask_inline_code(line))
        if m:
            yield i, line, m.group(1).lower()


# ------------------------------------------------------- hermes session store
#
# SESSION_STATE_PATH maps vault-relative note path -> per-agent session info:
#   {"inbox/note.md": {"hermes": {"session_id": "...", "last_used": 1757250000}}}
# Only "hermes" is populated today; schema is nested per-agent so claude/codex
# resume can be added without migration. Re-read from disk on every access so
# entries appended externally (e.g. by the agent registering a spin-off note)
# are honored. Lazy TTL expiry on read.

_state_lock = threading.Lock()


def _load_sessions() -> dict:
    try:
        return json.loads(SESSION_STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        log.warning("session state unreadable (%s) — backing up and starting fresh", e)
        try:
            SESSION_STATE_PATH.replace(SESSION_STATE_PATH.with_suffix(".json.corrupt"))
        except OSError:
            pass
        return {}


def _save_sessions(state: dict):
    SESSION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSION_STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, SESSION_STATE_PATH)


def get_session_id(rel_path: str, agent: str = "hermes"):
    """Return a live session_id for the note, or None (missing/expired)."""
    with _state_lock:
        state = _load_sessions()
        entry = state.get(rel_path, {}).get(agent)
        if not entry:
            return None
        if time.time() - entry.get("last_used", 0) > SESSION_TTL_HOURS * 3600:
            log.info("session for %s expired (> %.0fh) — starting fresh", rel_path, SESSION_TTL_HOURS)
            del state[rel_path][agent]
            if not state[rel_path]:
                del state[rel_path]
            _save_sessions(state)
            return None
        return entry.get("session_id")


def set_session(rel_path: str, session_id: str, agent: str = "hermes"):
    with _state_lock:
        state = _load_sessions()
        state.setdefault(rel_path, {})[agent] = {
            "session_id": session_id,
            "last_used": time.time(),
        }
        _save_sessions(state)


# Per-note locks: a resumed session must not run two turns concurrently.
_note_locks: dict = {}
_note_locks_guard = threading.Lock()


def _note_lock(rel_path: str) -> threading.Lock:
    with _note_locks_guard:
        return _note_locks.setdefault(rel_path, threading.Lock())


# ---------------------------------------------------------------- CLI agents

def run_claude(prompt: str):
    """Returns (ok, reply_text)."""
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--output-format", "json", "--max-turns", "15",
             "--allowedTools", "Read,Bash", "--fallback-model", "haiku"],
            capture_output=True, text=True, encoding="utf-8",
            timeout=CLI_TIMEOUT_SECONDS, cwd=str(VAULT),
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {CLI_TIMEOUT_SECONDS}s"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "unknown error").strip()[-500:]
    try:
        d = json.loads(r.stdout)
        if d.get("subtype") != "success":
            return False, f"claude returned {d.get('subtype')}"
        return True, (d.get("result") or "").strip()
    except (json.JSONDecodeError, TypeError):
        return False, "unparseable claude output: " + r.stdout.strip()[-300:]


_CODEX_NOISE = re.compile(
    r"^(\[\d{4}-\d{2}-\d{2}T[^\]]*\]|OpenAI Codex|-{8,}|workdir:|model:|provider:"
    r"|approval:|sandbox:|reasoning|tokens used:?|To continue|codex$|user$|thinking$)",
)


def run_codex(prompt: str):
    """Returns (ok, reply_text). Vault is a git repo, so codex runs in it."""
    try:
        r = subprocess.run(
            [CODEX_BIN, "exec", "--sandbox", "danger-full-access", prompt],
            capture_output=True, text=True, encoding="utf-8",
            timeout=CLI_TIMEOUT_SECONDS, cwd=str(VAULT),
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {CLI_TIMEOUT_SECONDS}s"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "unknown error").strip()[-500:]
    # codex exec prints a header + the final agent message; strip the noise
    lines = [ln for ln in r.stdout.split("\n") if not _CODEX_NOISE.match(ln.strip())]
    reply = "\n".join(lines).strip()
    return (True, reply) if reply else (False, "empty codex output")


CLI_RUNNERS = {"claude": run_claude, "codex": run_codex}

HERMES_SESSION_LINE = re.compile(r"^session_id:\s*(\S+)\s*$", re.MULTILINE)
HERMES_NOISE = re.compile(r"^(↻ Resumed session\b|session_id:\s)")
HERMES_STALE_SESSION = re.compile(r"(session .* not found|no session|could not resume)", re.IGNORECASE)


def run_hermes(prompt: str, resume_id=None):
    """Run one hermes turn. Returns (ok, reply_text, session_id_or_None).

    NOTE: `hermes -z` silently ignores --resume/--continue (verified
    2026-09-07) — must be `hermes chat -Q -q`.
    """
    cmd = [HERMES_BIN, "chat", "-Q", "-q", prompt]
    if resume_id:
        cmd += ["--resume", resume_id, "--no-restore-cwd"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           timeout=HERMES_TIMEOUT_SECONDS, cwd=str(VAULT))
    except subprocess.TimeoutExpired:
        return False, f"timed out after {HERMES_TIMEOUT_SECONDS}s", None
    out = r.stdout or ""
    err = r.stderr or ""
    # `session_id:` and the resume banner are printed to STDERR by -Q
    # (stdout carries only the reply text). Verified 2026-09-07.
    m = HERMES_SESSION_LINE.search(err) or HERMES_SESSION_LINE.search(out)
    sid = m.group(1) if m else None
    if r.returncode != 0:
        err_msg = (err or out or "unknown error").strip()
        if resume_id and HERMES_STALE_SESSION.search(err_msg):
            log.warning("stale session %s — retrying with a fresh session", resume_id)
            return run_hermes(prompt, resume_id=None)
        return False, err_msg[-500:], sid
    reply = "\n".join(ln for ln in out.split("\n") if not HERMES_NOISE.match(ln)).strip()
    return (bool(reply), reply or "empty hermes output", sid)


def notify_telegram(ok: bool, path: Path, reply: str):
    """Fire-and-forget Telegram ping via `hermes send` (no LLM involved)."""
    if not TELEGRAM_CHAT_ID:
        return
    status = "✅" if ok else "⚠️"
    summary = " ".join(reply.split())[:200]
    msg = f"{status} obsidian · {path.stem}\n{summary}"
    try:
        subprocess.run(
            [HERMES_BIN, "send", "-q", "-t", f"telegram:{TELEGRAM_CHAT_ID}", msg],
            capture_output=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("telegram ping failed: %s", e)


def finalize_hermes_tag(path: Path, acked_line: str) -> None:
    """Safety net: if the /ack tag from this run still sits in the note
    (agent finished but forgot to flip or rewrite it), flip it to /done.
    The agent owns the note content; we only guarantee no /ack is left."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    lines = text.split("\n")
    try:
        idx = lines.index(acked_line)
    except ValueError:
        return  # agent rewrote/removed the line — nothing to do
    lines[idx] = re.sub(r"@hermes/ack", "@hermes/done", lines[idx], count=1, flags=re.IGNORECASE)
    try:
        path.write_text("\n".join(lines), encoding="utf-8")
        log.info("safety-net: flipped forgotten /ack to /done in %s", path)
    except OSError as e:
        log.error("finalize_hermes_tag: cannot write %s: %s", path, e)


def dispatch_hermes(path: Path, acked_line: str, context: str, mention_id: str):
    """Run hermes with per-note session continuity.

    The agent writes its own result into the vault (inline reply, edits,
    new notes) and flips /ack itself — stdout is only the Telegram summary.
    On failure the watcher writes the /err blockquote (agent may have died
    before touching the note)."""
    rel = str(path.relative_to(VAULT))
    request_line = re.sub(r"@hermes/ack", "", acked_line, count=1, flags=re.IGNORECASE).strip()
    prompt = HERMES_PROMPT.format(
        note_path=str(path), mention_line=request_line, context=context,
        state_path=str(SESSION_STATE_PATH), rel_path=rel,
    )
    with _note_lock(rel):
        sid = get_session_id(rel)
        log.info("[hermes] %s running for %s (session=%s)", mention_id, rel, sid or "new")
        t0 = time.monotonic()
        ok, reply, out_sid = run_hermes(prompt, resume_id=sid)
        log.info("[hermes] %s finished ok=%s in %.0fs (session=%s)",
                 mention_id, ok, time.monotonic() - t0, out_sid or sid)
        final_sid = out_sid or sid
        if final_sid:
            fresh = final_sid != sid
            set_session(rel, final_sid)  # upsert also refreshes last_used
            if fresh:
                subprocess.run(
                    [HERMES_BIN, "sessions", "rename", final_sid, f"obsidian: {path.stem}"],
                    capture_output=True, timeout=30,
                )
    if ok:
        finalize_hermes_tag(path, acked_line)
    else:
        write_reply(path, "hermes", acked_line, ok, reply)
    notify_telegram(ok, path, reply)


def _now_stamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def write_reply(path: Path, agent: str, acked_line: str, ok: bool, reply: str):
    """Insert a blockquote reply under the acked mention line and flip its tag.

    Locates the line by content (not index) so concurrent edits/sync can't
    misplace the write. Retries once if the file changed underneath us.
    """
    status = "done" if ok else "err"
    emoji = "🤖" if ok else "⚠️"
    body = reply if ok else f"agent run failed: {reply}"
    quoted = "\n".join(f"> {ln}" if ln.strip() else ">" for ln in body.split("\n"))
    block = f"> {emoji} **{agent}** ({_now_stamp()}):\n{quoted}"

    for attempt in range(2):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            log.error("write_reply: cannot read %s: %s", path, e)
            return False
        lines = text.split("\n")
        try:
            idx = lines.index(acked_line)
        except ValueError:
            log.warning("write_reply: acked line not found in %s (edited away?)", path)
            return False
        lines[idx] = re.sub(rf"@{agent}/ack", f"@{agent}/{status}", lines[idx], count=1, flags=re.IGNORECASE)
        lines.insert(idx + 1, block)
        try:
            # crude optimistic lock: re-read and compare before writing
            if path.read_text(encoding="utf-8") != text:
                log.info("write_reply: %s changed mid-write, retrying", path)
                time.sleep(1.0)
                continue
            path.write_text("\n".join(lines), encoding="utf-8")
            return True
        except OSError as e:
            log.error("write_reply: cannot write %s: %s", path, e)
            return False
    return False


def dispatch_cli(agent: str, path: Path, acked_line: str, context: str, mention_id: str):
    """Run a CLI agent in a worker thread and write its reply back."""
    request_line = re.sub(rf"@{agent}/ack", "", acked_line, count=1, flags=re.IGNORECASE).strip()
    prompt = CLI_PROMPT.format(note_path=str(path), mention_line=request_line, context=context)
    log.info("[%s] %s running for %s", agent, mention_id, path)
    t0 = time.monotonic()
    ok, reply = CLI_RUNNERS[agent](prompt)
    log.info("[%s] %s finished ok=%s in %.0fs", agent, mention_id, ok, time.monotonic() - t0)
    write_reply(path, agent, acked_line, ok, reply)


# ------------------------------------------------------------------ core

def _clear_pending(path_str: str):
    with _pending_lock:
        _pending.pop(path_str, None)
        t = _pending_timers.pop(path_str, None)
        if t:
            t.cancel()


def _schedule_recheck(path_str: str, delay: float):
    with _pending_lock:
        t = _pending_timers.pop(path_str, None)
        if t:
            t.cancel()
        timer = threading.Timer(delay, _recheck, args=(path_str,))
        timer.daemon = True
        _pending_timers[path_str] = timer
        timer.start()


def _recheck(path_str: str):
    with _pending_lock:
        _pending_timers.pop(path_str, None)
    p = Path(path_str)
    if p.exists():
        process_file(p)


def process_file(path: Path, assume_finished: bool = False):
    path_str = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        log.warning("cannot read %s: %s", path, e)
        return
    lines = text.split("\n")
    hits = list(find_mentions(lines))
    if not hits:
        _clear_pending(path_str)
        return

    last_line_idx = len(lines) - 1
    ready, trailing = [], []
    for idx, line, agent in hits:
        if assume_finished or idx < last_line_idx:
            ready.append((idx, line, agent))
        else:
            trailing.append((idx, line, agent))

    if trailing and not ready:
        text_hash = hashlib.sha1(text.encode()).hexdigest()
        now = time.monotonic()
        with _pending_lock:
            prev = _pending.get(path_str)
        if prev and prev[0] == text_hash:
            if now - prev[1] >= STABILITY_SECONDS:
                log.info("trailing mention in %s stable for %.0fs -> dispatching", path, now - prev[1])
                ready.extend(trailing)
                trailing = []
                _clear_pending(path_str)
            else:
                _schedule_recheck(path_str, STABILITY_SECONDS - (now - prev[1]) + 0.5)
                return
        else:
            with _pending_lock:
                _pending[path_str] = (text_hash, now)
            log.info("trailing mention in %s — holding for %.0fs of quiet", path, STABILITY_SECONDS)
            _schedule_recheck(path_str, STABILITY_SECONDS + 0.5)
            return
    elif not trailing:
        _clear_pending(path_str)

    if not ready:
        return

    # Ack the ready mentions (atomic-ish dedup), then dispatch each.
    events = []
    for idx, line, agent in ready:
        mention_id = hashlib.sha1(f"{path}:{idx}:{line}:{time.time()}".encode()).hexdigest()[:10]
        acked = ack_line(line)
        lines[idx] = acked
        lo, hi = max(0, idx - CONTEXT_LINES), min(len(lines), idx + CONTEXT_LINES + 1)
        events.append({
            "agent": agent,
            "mention_id": mention_id,
            "acked_line": acked,
            "line_number": idx + 1,
            "context": "\n".join(lines[lo:hi]),
        })

    # Sync-collision guard: verify the file hasn't changed since we read it.
    try:
        current = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        log.warning("re-read failed for %s: %s — aborting ack", path, e)
        return
    if current != text:
        log.info("%s changed while deciding — re-evaluating", path)
        _schedule_recheck(path_str, DEBOUNCE_SECONDS)
        return

    try:
        path.write_text("\n".join(lines), encoding="utf-8")
    except OSError as e:
        log.error("cannot write ack to %s: %s — skipping dispatch to avoid dup risk", path, e)
        return

    for ev in events:
        agent = ev["agent"]
        log.info("mention %s (@%s) in %s (line %d)", ev["mention_id"], agent, path, ev["line_number"])
        target = dispatch_hermes if agent == "hermes" else dispatch_cli
        args = ((path, ev["acked_line"], ev["context"], ev["mention_id"])
                if agent == "hermes"
                else (agent, path, ev["acked_line"], ev["context"], ev["mention_id"]))
        worker = threading.Thread(target=target, args=args, daemon=True)
        worker.start()


class Handler(FileSystemEventHandler):
    def __init__(self):
        self._timers = {}
        self._lock = threading.Lock()

    def _schedule(self, path_str: str):
        path = Path(path_str)
        if path.suffix != ".md" or is_ignored(path):
            return
        with self._lock:
            t = self._timers.pop(path_str, None)
            if t:
                t.cancel()
            timer = threading.Timer(DEBOUNCE_SECONDS, self._fire, args=(path_str,))
            timer.daemon = True
            self._timers[path_str] = timer
            timer.start()

    def _fire(self, path_str: str):
        with self._lock:
            self._timers.pop(path_str, None)
        p = Path(path_str)
        if p.exists():
            process_file(p)

    def on_modified(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._schedule(event.dest_path)


def initial_scan():
    """Catch mentions written while the watcher was down (always finished)."""
    count = 0
    for p in VAULT.rglob("*.md"):
        if is_ignored(p):
            continue
        try:
            lines = p.read_text(encoding="utf-8").split("\n")
        except (OSError, UnicodeDecodeError):
            continue
        if any(True for _ in find_mentions(lines)):
            process_file(p, assume_finished=True)
            count += 1
    if count:
        log.info("initial scan: processed %d file(s) with pending mentions", count)


def main():
    if not VAULT.is_dir():
        log.error("vault not found: %s", VAULT)
        sys.exit(1)
    if not os.access(HERMES_BIN, os.X_OK):
        log.error("hermes binary not found/executable: %s", HERMES_BIN)
        sys.exit(1)
    log.info("watching %s (agents: %s; debounce %.0fs, trailing-line stability %.0fs; "
             "hermes sessions: %s, ttl %.0fh)",
             VAULT, ", ".join(AGENTS), DEBOUNCE_SECONDS, STABILITY_SECONDS,
             SESSION_STATE_PATH, SESSION_TTL_HOURS)
    initial_scan()
    observer = Observer()
    observer.schedule(Handler(), str(VAULT), recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(60)
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
