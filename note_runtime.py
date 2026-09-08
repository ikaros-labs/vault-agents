"""Markdown requests, serialized note edits, and one scheduler per vault."""
from dataclasses import dataclass
from pathlib import Path
import logging
import re
import threading

log = logging.getLogger('vault-agents')
MENTION_RE = re.compile(r'(?<![\w/])@(hermes|claude|codex)(?![-/\w])', re.I)
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def keyed_lock(key):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def mask_inline_code(text):
    runs = list(re.finditer(r'`+', text))
    chars = list(text)
    i = 0
    while i < len(runs):
        start = runs[i]
        end = next((j for j in range(i + 1, len(runs))
                    if len(runs[j][0]) == len(start[0])), None)
        if end is None:
            i += 1
            continue
        chars[start.start():runs[end].end()] = ' ' * (runs[end].end() - start.start())
        i = end + 1
    return ''.join(chars)


def find_mentions(lines):
    fence = None
    visible = []
    for line in lines:
        match = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= fence[1] and not match[2].strip():
                fence = None
            visible.append(' ' * len(line))
        elif match and (match[1][0] != '`' or '`' not in match[2]):
            fence = (match[1][0], len(match[1]))
            visible.append(' ' * len(line))
        else:
            visible.append(line)
    raw = '\n'.join(visible)
    flat = mask_inline_code(raw)
    masked = ''.join('\n' if c == '\n' else flat[i] for i, c in enumerate(raw)).split('\n')
    for i, line in enumerate(masked):
        for mention in MENTION_RE.finditer(line):
            yield i, mention


@dataclass(frozen=True)
class Request:
    agent: str
    mention_id: str
    line: str
    context: str

    @property
    def marker(self):
        return f'<!-- vault-agent:{self.mention_id} -->'


def update_note(path: Path, transform):
    """Serialize watcher writers; retry detected external edits before committing.

    External editors do not honor our lock: this is optimistic conflict detection,
    not a filesystem compare-and-swap. Never claim exactly-once dispatch from it.
    """
    with keyed_lock(('note', str(path))):
        for _ in range(3):
            before = path.read_text(encoding='utf-8')
            after = transform(before)
            if after is None:
                return False
            if after == before:
                return True
            if path.read_text(encoding='utf-8') != before:
                continue
            path.write_text(after, encoding='utf-8')
            return True
    log.warning('note kept changing; update deferred: %s', path)
    return False


class Scheduler:
    """One cancellable deadline per path; stale callbacks cannot process notes."""
    def __init__(self, callback):
        self.callback = callback
        self.lock = threading.RLock()
        self.entries = {}
        self.closed = False

    def schedule(self, path, delay):
        path = str(path)
        with self.lock:
            if self.closed:
                return
            previous = self.entries.pop(path, None)
            if previous:
                previous[1].cancel()
            token = object()
            timer = threading.Timer(delay, self.fire, (path, token))
            timer.daemon = True
            self.entries[path] = (token, timer)
            timer.start()

    def fire(self, path, token):
        with keyed_lock(('note', path)):
            with self.lock:
                entry = self.entries.get(path)
                if not entry or entry[0] is not token:
                    return
                del self.entries[path]
            if Path(path).exists():
                self.callback(Path(path))

    def close(self):
        with self.lock:
            self.closed = True
            for _, timer in self.entries.values():
                timer.cancel()
            self.entries.clear()
