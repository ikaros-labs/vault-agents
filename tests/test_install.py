"""Installer integration tests with isolated homes and mocked external commands."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home with spaces'
        self.home.mkdir()
        self.vault = self.home / 'My Vault'
        self.vault.mkdir()
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        self.log = self.home / 'commands'
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin), COMMAND_LOG=str(self.log))
        self.script('systemctl', '#!/bin/sh\necho "$*" >> "$COMMAND_LOG"\n')
        self.script('codex', '#!/bin/sh\nexit 0\n')
        self.script('uv', f'''#!{sys.executable}
import os, pathlib, sys
with open(os.environ['COMMAND_LOG'], 'a') as f: f.write('uv ' + ' '.join(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'venv':
    folder = pathlib.Path(sys.argv[-1]) / 'bin'
    folder.mkdir(parents=True)
    for name in ('python', 'vault-agents'):
        p = folder / name
        p.write_text('#!/bin/sh\\nexit 0\\n')
        p.chmod(0o755)
''')

    def script(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def install(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'install.py'), *map(str, args)],
                              env=self.env, capture_output=True, text=True)

    def test_fresh_install_and_repeat_preserve_config(self):
        result = self.install('--vault', self.vault)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.home / '.config/vault-agents-watcher.env'
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertIn(f'VAULT_PATH="{self.vault}"', config.read_text())
        self.assertIn('CODEX_BIN=', config.read_text())
        config.write_text(config.read_text() + 'SECRET=keep-me\n')
        expected = config.read_bytes()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_bytes(), expected)
        unit = (self.home / '.config/systemd/user/vault-agents-watcher.service').read_text()
        self.assertIn(f'ExecStart="{self.home}/.local/share/vault-agents/venv/bin/vault-agents"', unit)
        self.assertIn('EnvironmentFile=%h/.config/vault-agents-watcher.env', unit)
        self.assertNotIn('@ENVIRONMENT_FILE@', unit)
        self.assertIn('restart vault-agents-watcher', self.log.read_text())

    def test_check_is_read_only(self):
        result = self.install('--vault', self.vault, '--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / '.local').exists())
        self.assertFalse((self.home / '.config').exists())
        self.assertNotIn('uv ', self.log.read_text())

    def test_no_start_does_not_call_systemctl(self):
        result = self.install('--vault', self.vault, '--no-start')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(line.startswith('uv ') for line in self.log.read_text().splitlines()))

    def test_missing_vault_or_agent_fails_before_install(self):
        self.assertNotEqual(self.install().returncode, 0)
        self.assertNotEqual(self.install('--vault', self.home / 'missing').returncode, 0)
        (self.bin / 'codex').unlink()
        self.assertNotEqual(self.install('--vault', self.vault).returncode, 0)
        self.assertFalse((self.home / '.local').exists())

    def test_failed_package_install_does_not_replace_unit(self):
        unit = self.home / '.config/systemd/user/vault-agents-watcher.service'
        unit.parent.mkdir(parents=True)
        unit.write_text('old unit')
        self.script('uv', '#!/bin/sh\nexit 1\n')
        self.assertNotEqual(self.install('--vault', self.vault).returncode, 0)
        self.assertEqual(unit.read_text(), 'old unit')
        self.assertFalse((self.home / '.config/vault-agents-watcher.env').exists())

    def test_existing_config_rejects_vault_override(self):
        self.assertEqual(self.install('--vault', self.vault).returncode, 0)
        self.assertNotEqual(self.install('--vault', self.vault).returncode, 0)


if __name__ == '__main__':
    unittest.main()
