#!/usr/bin/env python3
"""Obsidian @hermes mention watcher.

Watches the Obsidian vault for edits to *.md files. When a line contains a
bare `@hermes` mention (not @hermes/ack or @hermes/done), it:
  1. rewrites `@hermes` -> `@hermes/ack` in the note (dedup + visual receipt)
  2. POSTs {note_path, mention_line, mention_id, context} to the Hermes
     webhook endpoint with a generic V2 HMAC signature.

Run as a systemd user service. Config via env vars (see unit file):
  VAULT_PATH, WEBHOOK_URL, WEBHOOK_SECRET
"""

import hashlib
import hmac
import json
import logging
import os
import re
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
DEBOUNCE_SECONDS = 3.0
CONTEXT_LINES = 20
IGNORE_DIRS = {".git", ".obsidian", ".trash", "templates"}

# Bare @hermes: word boundary before, not followed by /ack or /done or a word char
MENTION_RE = re.compile(r"(?<![\w/])@hermes(?!/)(?!\w)", re.IGNORECASE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("obsidian-watcher")


def is_ignored(path: Path) -> bool:
    try:
        rel = path.relative_to(VAULT)
    except ValueError:
        return True
    return any(part in IGNORE_DIRS or part.startswith(".") for part in rel.parts[:-1]) or path.name.startswith(".")


def find_mentions(lines):
    """Yield (line_index, line) for bare @hermes mentions outside code fences."""
    in_fence = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if MENTION_RE.search(line):
            yield i, line


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


def process_file(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        log.warning("cannot read %s: %s", path, e)
        return
    lines = text.split("\n")
    hits = list(find_mentions(lines))
    if not hits:
        return

    # Ack all bare mentions first (atomic-ish dedup), then POST each.
    events = []
    for idx, line in hits:
        mention_id = hashlib.sha1(f"{path}:{idx}:{line}:{time.time()}".encode()).hexdigest()[:10]
        acked = MENTION_RE.sub("@hermes/ack", line, count=1)
        lines[idx] = acked
        lo, hi = max(0, idx - CONTEXT_LINES), min(len(lines), idx + CONTEXT_LINES + 1)
        events.append({
            "event_type": "obsidian_mention",
            "mention_id": mention_id,
            "note_path": str(path),
            "line_number": idx + 1,
            "mention_line": acked,
            "context": "\n".join(lines[lo:hi]),
        })

    try:
        path.write_text("\n".join(lines), encoding="utf-8")
    except OSError as e:
        log.error("cannot write ack to %s: %s — skipping POST to avoid dup risk", path, e)
        return

    for ev in events:
        log.info("mention %s in %s (line %d)", ev["mention_id"], path, ev["line_number"])
        if post_webhook(ev):
            log.info("dispatched %s", ev["mention_id"])
        else:
            log.error("FAILED to dispatch %s — mention is acked but not delivered", ev["mention_id"])


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
    """Catch mentions written while the watcher was down."""
    count = 0
    for p in VAULT.rglob("*.md"):
        if is_ignored(p):
            continue
        try:
            lines = p.read_text(encoding="utf-8").split("\n")
        except (OSError, UnicodeDecodeError):
            continue
        if any(True for _ in find_mentions(lines)):
            process_file(p)
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
    log.info("watching %s -> %s", VAULT, WEBHOOK_URL)
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
