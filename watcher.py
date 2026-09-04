#!/usr/bin/env python3
"""Obsidian agent mention watcher.

Watches the Obsidian vault for edits to *.md files. When a line contains a
bare agent mention (@hermes, @claude, @codex — not .../ack, /done, /err), it:
  1. rewrites the tag -> @<agent>/ack in the note (dedup + visual receipt)
  2. dispatches the request:
       - hermes: POST to the Hermes webhook endpoint (V2 HMAC). Hermes runs
         the agent, writes the inline reply, flips /done, pings Telegram.
       - claude / codex: runs the CLI directly as a subprocess; the WATCHER
         writes the reply blockquote and flips /ack -> /done (or /err).

Dispatch rules ("Enter = send"):
  - A mention followed by ANY further line (even blank / trailing newline)
    is considered finished -> dispatched on the normal debounce.
  - A mention on the very last line of the file (no newline after it) is
    probably still being typed -> held until the whole file has been quiet
    for STABILITY_SECONDS, then dispatched.

Run as a systemd user service. Config via env vars (see unit file):
  VAULT_PATH, WEBHOOK_URL, WEBHOOK_SECRET
Optional:
  CLAUDE_BIN, CODEX_BIN (absolute paths; systemd PATH lacks ~/.npm-global/bin)
  CLI_TIMEOUT_SECONDS (default 600)
"""

import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

VAULT = Path(os.environ.get("VAULT_PATH", os.path.expanduser("~/obsidian-vault")))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "http://localhost:8644/webhooks/obsidian-mention")
SECRET = os.environ.get("WEBHOOK_SECRET", "")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", os.path.expanduser("~/.npm-global/bin/claude"))
CODEX_BIN = os.environ.get("CODEX_BIN", os.path.expanduser("~/.npm-global/bin/codex"))
CLI_TIMEOUT_SECONDS = int(os.environ.get("CLI_TIMEOUT_SECONDS", "600"))
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("obsidian-watcher")

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


def post_webhook(payload: dict) -> bool:
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": ts,
        "X-Webhook-Signature-V2": sig,
    }
    for attempt in range(3):
        try:
            r = requests.post(WEBHOOK_URL, data=body, headers=headers, timeout=15)
            if r.status_code < 300:
                return True
            log.warning("webhook POST %s -> %s: %s", payload.get("mention_id"), r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.warning("webhook POST failed (attempt %d): %s", attempt + 1, e)
        time.sleep(2 * (attempt + 1))
    return False


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
        if agent == "hermes":
            payload = {
                "event_type": "obsidian_mention",
                "mention_id": ev["mention_id"],
                "note_path": path_str,
                "line_number": ev["line_number"],
                "mention_line": ev["acked_line"],
                "context": ev["context"],
            }
            if post_webhook(payload):
                log.info("dispatched %s", ev["mention_id"])
            else:
                log.error("FAILED to dispatch %s — mention is acked but not delivered", ev["mention_id"])
        else:
            worker = threading.Thread(
                target=dispatch_cli,
                args=(agent, path, ev["acked_line"], ev["context"], ev["mention_id"]),
                daemon=True,
            )
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
    if not SECRET:
        log.error("WEBHOOK_SECRET not set")
        sys.exit(1)
    if not VAULT.is_dir():
        log.error("vault not found: %s", VAULT)
        sys.exit(1)
    log.info("watching %s (agents: %s; debounce %.0fs, trailing-line stability %.0fs)",
             VAULT, ", ".join(AGENTS), DEBOUNCE_SECONDS, STABILITY_SECONDS)
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
