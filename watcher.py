#!/usr/bin/env python3
"""Obsidian agent mention watcher.

Watches the Obsidian vault for edits to *.md files. When a line contains a
bare agent mention (@hermes, @claude, @codex — not .../ack, /done, /err), it:
  1. rewrites the tag -> @<agent>/ack in the note (dedup + visual receipt)
  2. dispatches the request by running the agent CLI as a subprocess in a
     worker thread; the watcher completes claude/codex replies and failures.
     Hermes writes its own results, with a watcher tag-completion safety net.

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
  TELEGRAM_CHAT_ID (default empty; disables pings)
"""

import datetime
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from vault_agents_note_runtime import MENTION_RE, Request, Scheduler, find_mentions, keyed_lock, update_note

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
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DEBOUNCE_SECONDS = 3.0
STABILITY_SECONDS = 8.0  # quiet time required for a trailing-line mention
CONTEXT_LINES = 20
IGNORE_DIRS = {".git", ".obsidian", ".trash", "templates"}

AGENTS = ("hermes", "claude", "codex")
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
   change only THIS request's tag (the /ack on the request line above).
   Other pending requests must remain untouched.
5. If you CREATE notes that should inherit this conversation, emit one stdout
   line: VAULT_INHERIT: ["inbox/new-note.md"] (a JSON array of relative paths).
   The watcher registers these AFTER your session is saved. Never edit the
   session state JSON yourself.
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

# Pending trailing notes retain a text snapshot until their next scan clears it.
_pending = {}
_completions = {}


def is_ignored(path: Path) -> bool:
    try:
        rel = path.relative_to(VAULT)
    except ValueError:
        return True
    return any(part in IGNORE_DIRS or part.startswith(".") for part in rel.parts[:-1]) or path.name.startswith(".")


# ------------------------------------------------------- hermes session store
#
# SESSION_STATE_PATH maps vault-relative note path -> per-agent session info:
#   {"inbox/note.md": {"hermes": {"session_id": "...", "last_used": 1757250000}}}
# Only "hermes" is populated today; schema is nested per-agent so claude/codex
# resume can be added without migration. The watcher is the sole writer;
# inheritance directives are applied after the turn finishes. Lazy TTL expiry.

_state_lock = threading.Lock()


def _load_sessions() -> dict:
    try:
        state = json.loads(SESSION_STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("session state must be an object")
        for note, agents in state.items():
            if not isinstance(agents, dict):
                raise ValueError(f"invalid session agents for {note}")
            for entry in agents.values():
                if (not isinstance(entry, dict) or not isinstance(entry.get("session_id"), str)
                        or not isinstance(entry.get("last_used"), (int, float))):
                    raise ValueError(f"invalid session entry for {note}")
        return state
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
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


def run_hermes_request(path: Path, request: Request):
    rel = str(path.relative_to(VAULT))
    prompt = HERMES_PROMPT.format(
        note_path=str(path), mention_line=request.line, context=request.context,
    )
    # Hermes edits the note itself: hold the watcher write lock for its turn.
    # A shared session lock also covers inherited notes with different paths.
    with keyed_lock(("note", str(path))):
        sid = get_session_id(rel)
        with keyed_lock(("session", sid or rel)):
            ok, reply, out_sid = run_hermes(prompt, resume_id=sid)
            final_sid = out_sid or sid
            summary, inherited = [], []
            for line in reply.splitlines():
                if line.startswith("VAULT_INHERIT:"):
                    try:
                        paths = json.loads(line.partition(":")[2])
                        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                            raise ValueError("expected an array of note paths")
                        inherited.extend(paths)
                    except (ValueError, TypeError) as e:
                        log.warning("invalid inheritance directive: %s", e)
                else:
                    summary.append(line)
            if final_sid:
                try:
                    register_sessions(rel, final_sid, inherited if ok else [])
                except (OSError, ValueError, TypeError):
                    log.exception("could not save session for %s", rel)
                if final_sid != sid:
                    try:
                        subprocess.run(
                            [HERMES_BIN, "sessions", "rename", final_sid, f"obsidian: {path.stem}"],
                            capture_output=True, timeout=30, check=True,
                        )
                    except (OSError, subprocess.SubprocessError):
                        log.exception("could not name session %s", final_sid)
            return ok, "\n".join(summary)


def register_sessions(rel, sid, inherited):
    paths = [rel]
    for candidate in inherited:
        path = (VAULT / candidate).resolve()
        try:
            relative = path.relative_to(VAULT.resolve())
        except ValueError:
            log.warning("inheritance path outside vault: %s", candidate)
            continue
        if path.suffix == ".md" and path.is_file() and not is_ignored(VAULT / relative):
            paths.append(str(relative))
    with _state_lock:
        state = _load_sessions()
        for path in paths:
            state.setdefault(path, {})["hermes"] = {"session_id": sid, "last_used": time.time()}
        _save_sessions(state)


def _now_stamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def complete_request(path: Path, request: Request, ok: bool, reply: str):
    status = "done" if ok else "err"
    ack_re = re.compile(rf"(?<![\w/])@{request.agent}/ack(?![-/\w])", re.I)
    body = reply if ok else f"agent run failed: {reply}"
    quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in body.splitlines())
    block = f"> {'🤖' if ok else '⚠️'} **{request.agent}** ({_now_stamp()}):\n{quoted}"

    def transform(text):
        lines = text.split("\n")
        # Primary anchor: the exact line as written at ack time + which /ack
        # occurrence on it is ours (survives multiple mentions per line).
        anchor = None
        if request.acked_line in lines:
            idx = lines.index(request.acked_line)
            matches = list(ack_re.finditer(lines[idx]))
            if len(matches) > request.ordinal:
                anchor = (idx, matches[request.ordinal])
        if anchor is None:
            # Line edited mid-run: fall back to the first remaining /ack tag
            # for this agent anywhere in the note. Ambiguous only if several
            # same-agent requests are in flight AND their lines were edited.
            anchor = next(((i, m) for i, line in enumerate(lines)
                           for m in [ack_re.search(line)] if m), None)
        if anchor is None:
            # Hermes may replace its request with a link. Never guess another tag.
            if request.agent == "hermes" and ok:
                return text
            # Preserve results even if a user deleted the request anchor.
            return text + f"\n\n> Request {request.mention_id} (original request tag removed)\n" + block + "\n"
        idx, match = anchor
        lines[idx] = lines[idx][:match.start()] + f"@{request.agent}/{status}" + lines[idx][match.end():]
        if request.agent != "hermes" or not ok:
            lines.insert(idx + 1, block)
        return "\n".join(lines)

    return update_note(path, transform)


def dispatch_request(path: Path, request: Request):
    log.info("[%s] %s running for %s", request.agent, request.mention_id, path)
    try:
        if request.agent == "hermes":
            ok, reply = run_hermes_request(path, request)
        else:
            prompt = CLI_PROMPT.format(note_path=str(path), mention_line=request.line, context=request.context)
            ok, reply = CLI_RUNNERS[request.agent](prompt)
    except Exception as e:
        log.exception("request %s failed", request.mention_id)
        ok, reply = False, f"{type(e).__name__}: {e}"
    with keyed_lock(("note", str(path))):
        _completions.setdefault(str(path), []).append((request, ok, reply))
        flush_completions(path)
    if request.agent == "hermes":
        notify_telegram(ok, path, reply)


def flush_completions(path: Path):
    """Retain results across transient edit conflicts without rerunning agents."""
    pending = _completions.get(str(path), [])
    remaining = []
    for request, ok, reply in pending:
        try:
            if complete_request(path, request, ok, reply):
                continue
        except (OSError, UnicodeDecodeError):
            log.exception("could not complete request %s", request.mention_id)
        remaining.append((request, ok, reply))
    if remaining:
        _completions[str(path)] = remaining
        scheduler.schedule(path, DEBOUNCE_SECONDS)
    else:
        _completions.pop(str(path), None)


def process_file(path: Path, assume_finished: bool = False):
    with keyed_lock(("note", str(path))):
        try:
            flush_completions(path)
            _process_file(path, assume_finished)
        except (OSError, UnicodeDecodeError):
            log.exception("cannot process %s", path)


def quote_safe_mentions(text: str) -> str:
    """Prompt excerpts must not plant new requests when quoted into a note."""
    return MENTION_RE.sub(lambda match: f"@{match[1]}/done", text)


def _process_file(path: Path, assume_finished: bool):
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    hits = list(find_mentions(lines))
    if not hits:
        _pending.pop(str(path), None)
        return
    now = time.monotonic()
    previous_text, since = _pending.get(str(path), (None, now))
    if text != previous_text:
        since = now
    _pending[str(path)] = (text, since)
    ready = [(idx, match) for idx, match in hits
             if assume_finished or idx < len(lines) - 1 or now - since >= STABILITY_SECONDS]
    prompt_lines = lines.copy()
    by_line = {}
    for idx, match in ready:
        by_line.setdefault(idx, []).append(match)
    pending = []
    for idx, match in reversed(ready):
        agent = match[1].lower()
        # Remove this request's tag; make other bare tags safe to quote, including
        # trailing requests that have not been acknowledged yet. Use an immutable
        # snapshot so another request's ack never enters the prompt.
        line = prompt_lines[idx][:match.start()] + prompt_lines[idx][match.end():]
        context = "\n".join(prompt_lines[max(0, idx-CONTEXT_LINES):idx+CONTEXT_LINES+1])
        lines[idx] = lines[idx][:match.start()] + f"@{agent}/ack" + lines[idx][match.end():]
        # Every ack rewrite inserts exactly "/ack" (4 chars); acks to our right
        # don't shift us, earlier ones on this line shift us right by 4 each.
        final_start = match.start() + 4 * sum(1 for m in by_line[idx] if m.start() < match.start())
        pending.append((idx, agent, final_start,
                        quote_safe_mentions(line).strip(), quote_safe_mentions(context)))
    requests = []
    for idx, agent, final_start, line, context in pending:
        acked = lines[idx]
        ack_re = re.compile(rf"(?<![\w/])@{agent}/ack(?![-/\w])", re.I)
        ordinal = sum(1 for m in ack_re.finditer(acked) if m.start() < final_start)
        requests.append(Request(agent, uuid.uuid4().hex, line, context, acked, ordinal))
    if ready:
        if not update_note(path, lambda current: "\n".join(lines) if current == text else None):
            scheduler.schedule(path, DEBOUNCE_SECONDS)
            return
        # Our acknowledgement does not reset the trailing mention's quiet clock.
        _pending[str(path)] = ("\n".join(lines), since)
        for request in reversed(requests):
            threading.Thread(target=dispatch_request, args=(path, request), daemon=True).start()
    if len(ready) < len(hits):
        scheduler.schedule(path, max(0.1, STABILITY_SECONDS - (now - since)))


scheduler = Scheduler(process_file)


class Handler(FileSystemEventHandler):
    def _schedule(self, path_str: str):
        path = Path(path_str)
        if path.suffix == ".md" and not is_ignored(path):
            scheduler.schedule(path, DEBOUNCE_SECONDS)

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
    if not any(os.access(binary, os.X_OK) for binary in (HERMES_BIN, CLAUDE_BIN, CODEX_BIN)):
        log.error("no agent CLI found; configure HERMES_BIN, CLAUDE_BIN, or CODEX_BIN")
        sys.exit(1)
    log.info("watching %s (agents: %s; debounce %.0fs, trailing-line stability %.0fs; "
             "hermes sessions: %s, ttl %.0fh)",
             VAULT, ", ".join(AGENTS), DEBOUNCE_SECONDS, STABILITY_SECONDS,
             SESSION_STATE_PATH, SESSION_TTL_HOURS)
    observer = Observer()
    observer.schedule(Handler(), str(VAULT), recursive=True)
    observer.start()
    try:
        initial_scan()
        while True:
            time.sleep(60)
    finally:
        scheduler.close()
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
