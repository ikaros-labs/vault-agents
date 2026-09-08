import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import watcher as w
from vault_agents_note_runtime import Request, Scheduler, find_mentions, update_note


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in [('VAULT', self.root), ('SESSION_STATE_PATH', self.root / 'sessions.json'),
                            ('TELEGRAM_CHAT_ID', ''), ('_pending', {}), ('_completions', {})]:
            mock = patch.object(w, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.schedule = patch.object(w.scheduler, 'schedule').start()
        self.addCleanup(patch.stopall)
        self.path = self.root / 'note.md'

    def request(self, agent='claude', ident='one', acked_line=None, ordinal=0):
        acked_line = acked_line if acked_line is not None else f'@{agent}/ack request'
        return Request(agent, ident, 'request', 'context', acked_line, ordinal)

    def test_markdown_delimiters(self):
        for text in ['``@hermes example``', '````md\n```python\n@hermes example\n```\n````',
                     '~~~md\n```\n@codex example\n~~~', '`multi\n@claude line`']:
            with self.subTest(text=text):
                self.assertEqual(list(find_mentions(text.split('\n'))), [])
        hits = list(find_mentions(['`unclosed @claude yes', '@codex-no @hermes/done @codex yes']))
        self.assertEqual([match[1].lower() for _, match in hits], ['claude', 'codex'])

    def test_both_mentions_on_one_line_complete_after_edit(self):
        self.path.write_text('@claude first @codex second\n')
        captured = []
        class Worker:
            def __init__(self, target, args, daemon): captured.append(args[1])
            def start(self): pass
        with patch.object(w.threading, 'Thread', Worker):
            w.process_file(self.path)
        self.assertEqual(len(captured), 2)
        self.path.write_text(self.path.read_text().replace('first', 'edited first'))
        for request in captured:
            self.assertTrue(w.complete_request(self.path, request, True, request.agent + ' answer'))
        result = self.path.read_text()
        self.assertIn('@claude/done', result)
        self.assertIn('@codex/done', result)
        self.assertIn('claude answer', result)
        self.assertIn('codex answer', result)

    def test_prompt_excerpts_are_safe_to_echo(self):
        self.path.write_text('@hermes first @claude second\n@codex still typing')
        captured = []
        class Worker:
            def __init__(self, target, args, daemon): captured.append(args[1])
            def start(self): pass
        with patch.object(w.threading, 'Thread', Worker):
            w.process_file(self.path)
        self.assertEqual([r.agent for r in captured], ['hermes', 'claude'])
        for request in captured:
            self.assertFalse(w.MENTION_RE.search(request.line))
            self.assertFalse(w.MENTION_RE.search(request.context))
            self.assertIn('@codex/done still typing', request.context)
        self.assertEqual(captured[0].line, 'first @claude/done second')
        self.assertIn('@codex still typing', self.path.read_text())

    def test_no_marker_written_and_completion_flips_only_its_own_tag(self):
        for agent in ['hermes', 'claude', 'codex']:
            for ok in [True, False]:
                with self.subTest(agent=agent, ok=ok):
                    acked = f'@{agent}/ack task @hermes/ack next'
                    request = self.request(agent, acked_line=acked, ordinal=0)
                    self.path.write_text(acked + '\n')
                    self.assertNotIn('<!--', self.path.read_text())
                    w.complete_request(self.path, request, ok, 'answer')
                    result = self.path.read_text()
                    self.assertIn(f"@{agent}/{'done' if ok else 'err'} task", result)
                    self.assertIn('@hermes/ack next', result)

    def test_same_line_ordinal_targets_second_tag(self):
        acked = '@claude/ack first @claude/ack second'
        request = self.request('claude', acked_line=acked, ordinal=1)
        self.path.write_text(acked + '\n')
        w.complete_request(self.path, request, True, 'answer')
        self.assertEqual(self.path.read_text().split('\n')[0],
                         '@claude/ack first @claude/done second')

    def test_edited_line_falls_back_to_first_ack(self):
        request = self.request('claude', acked_line='@claude/ack original task')
        self.path.write_text('@claude/ack reworded task\n')
        w.complete_request(self.path, request, True, 'answer')
        result = self.path.read_text()
        self.assertIn('@claude/done reworded task', result)
        self.assertIn('answer', result)

    def test_hermes_rewrite_leaves_note_untouched(self):
        request = self.request('hermes')
        self.path.write_text('[[result]]\n')
        w.complete_request(self.path, request, True, 'finished')
        self.assertEqual(self.path.read_text(), '[[result]]\n')

    def test_concurrent_replies_are_preserved(self):
        requests = [self.request('claude', 'one'), self.request('codex', 'two')]
        self.path.write_text('\n'.join(r.acked_line for r in requests))
        barrier = threading.Barrier(2)
        def finish(request):
            barrier.wait()
            w.complete_request(self.path, request, True, request.agent + ' answer')
        threads = [threading.Thread(target=finish, args=(r,)) for r in requests]
        for t in threads: t.start()
        for t in threads: t.join(timeout=3)
        result = self.path.read_text()
        self.assertIn('claude answer', result)
        self.assertIn('codex answer', result)
        self.assertNotIn('/ack', result)

    def test_concurrent_pickup_dispatches_once(self):
        self.path.write_text('@claude request\n')
        captured = []
        with patch.object(w, 'dispatch_request', side_effect=lambda p, r: captured.append(r)):
            threads = [threading.Thread(target=w.process_file, args=(self.path,)) for _ in range(2)]
            for t in threads: t.start()
            for t in threads: t.join(timeout=3)
        self.assertEqual(len(captured), 1)

    def test_worker_exception_finishes_as_error(self):
        request = self.request()
        self.path.write_text(request.acked_line + '\n')
        with patch.dict(w.CLI_RUNNERS, {'claude': lambda _: (_ for _ in ()).throw(FileNotFoundError('missing'))}):
            w.dispatch_request(self.path, request)
        self.assertIn('@claude/err', self.path.read_text())
        self.assertIn('FileNotFoundError', self.path.read_text())

    def test_hermes_rename_failure_does_not_prevent_completion(self):
        request = self.request('hermes')
        self.path.write_text(request.acked_line + '\n')
        with patch.object(w, 'run_hermes', return_value=(True, 'finished', 'sid')), \
             patch.object(w.subprocess, 'run', side_effect=w.subprocess.TimeoutExpired('rename', 30)):
            w.dispatch_request(self.path, request)
        self.assertIn('@hermes/done', self.path.read_text())

    def test_inheritance_is_registered_after_first_turn(self):
        child = self.root / 'child.md'
        child.write_text('result')
        with patch.object(w, 'run_hermes', return_value=(True, 'VAULT_INHERIT: ["child.md", "../escape.md"]\nfinished', 'sid')), \
             patch.object(w.subprocess, 'run'):
            ok, reply = w.run_hermes_request(self.path, self.request('hermes'))
        self.assertTrue(ok)
        self.assertEqual(reply, 'finished')
        state = json.loads(w.SESSION_STATE_PATH.read_text())
        self.assertEqual(set(state), {'note.md', 'child.md'})
        self.assertEqual(state['child.md']['hermes']['session_id'], 'sid')

    def test_inherited_session_turns_do_not_overlap(self):
        child = self.root / 'child.md'
        child.write_text('result')
        w.register_sessions('note.md', 'sid', ['child.md'])
        started = threading.Event()
        release = threading.Event()
        calls = []
        def run(*args, **kwargs):
            calls.append(1)
            started.set()
            self.assertTrue(release.wait(3))
            return True, 'done', 'sid'
        with patch.object(w, 'run_hermes', side_effect=run):
            first = threading.Thread(target=w.run_hermes_request, args=(self.path, self.request('hermes')))
            second = threading.Thread(target=w.run_hermes_request, args=(child, self.request('hermes')))
            first.start()
            self.assertTrue(started.wait(3))
            second.start()
            self.assertEqual(len(calls), 1)
            release.set()
            first.join(3)
            second.join(3)
        self.assertEqual(len(calls), 2)

    def test_completion_conflict_retries_without_rerunning_agent(self):
        request = self.request()
        self.path.write_text(request.acked_line + '\n')
        with patch.dict(w.CLI_RUNNERS, {'claude': lambda _: (True, 'answer')}), \
             patch.object(w, 'complete_request', return_value=False):
            w.dispatch_request(self.path, request)
        self.assertEqual(len(w._completions[str(self.path)]), 1)
        w.process_file(self.path)
        self.assertEqual(w._completions, {})
        self.assertIn('answer', self.path.read_text())
        self.assertIn('@claude/done', self.path.read_text())

    def test_session_save_failure_still_completes(self):
        request = self.request('hermes')
        self.path.write_text(request.acked_line + '\n')
        with patch.object(w, 'run_hermes', return_value=(True, 'finished', 'sid')), \
             patch.object(w, 'register_sessions', side_effect=OSError('disk full')), \
             patch.object(w.subprocess, 'run'):
            w.dispatch_request(self.path, request)
        self.assertIn('@hermes/done', self.path.read_text())

    def test_trailing_clock_restarts_on_edit(self):
        self.path.write_text('@claude first')
        with patch.object(w.threading, 'Thread') as worker, patch.object(w.time, 'monotonic', return_value=0):
            w.process_file(self.path)
        self.path.write_text('@claude edited')
        with patch.object(w.threading, 'Thread') as worker, patch.object(w.time, 'monotonic', return_value=7):
            w.process_file(self.path)
            worker.assert_not_called()
        with patch.object(w.threading, 'Thread') as worker, patch.object(w.time, 'monotonic', return_value=15):
            w.process_file(self.path)
            worker.assert_called_once()

    def test_observation_starts_before_scan(self):
        order = []
        with patch.object(w.os, 'access', return_value=True), \
             patch.object(w, 'Observer') as observer, \
             patch.object(w, 'initial_scan', side_effect=lambda: order.append('scan')), \
             patch.object(w.time, 'sleep', side_effect=KeyboardInterrupt), \
             patch.object(w.scheduler, 'close'):
            observer.return_value.start.side_effect = lambda: order.append('watch')
            with self.assertRaises(KeyboardInterrupt):
                w.main()
        self.assertEqual(order, ['watch', 'scan'])
        observer.return_value.stop.assert_called_once()

    def test_trailing_mention_waits_and_initial_scan_bypasses_wait(self):
        self.path.write_text('@claude request')
        with patch.object(w.threading, 'Thread') as worker:
            w.process_file(self.path)
            worker.assert_not_called()
            self.assertEqual(self.schedule.call_count, 1)
            w.process_file(self.path, assume_finished=True)
            worker.assert_called_once()

    def test_stale_scheduler_callback_is_ignored(self):
        fired = []
        scheduler = Scheduler(fired.append)
        self.addCleanup(scheduler.close)
        self.path.write_text('note')
        with patch('vault_agents_note_runtime.threading.Timer'):
            scheduler.schedule(self.path, 3)
            old = scheduler.entries[str(self.path)][0]
            scheduler.schedule(self.path, 8)
            current = scheduler.entries[str(self.path)][0]
            scheduler.fire(str(self.path), old)
            self.assertEqual(fired, [])
            scheduler.fire(str(self.path), current)
            self.assertEqual(fired, [self.path])

    def test_external_edit_is_reapplied_before_write(self):
        self.path.write_text('initial')
        calls = []
        def transform(text):
            calls.append(text)
            if len(calls) == 1:
                self.path.write_text('external edit')
            return text + ' result'
        self.assertTrue(update_note(self.path, transform))
        self.assertEqual(self.path.read_text(), 'external edit result')


if __name__ == '__main__':
    unittest.main()
