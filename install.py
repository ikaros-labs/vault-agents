#!/usr/bin/env python3
"""Install the checkout into an isolated, per-user systemd service."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def quote(value):
    """Quote a literal value for systemd (not a shell)."""
    value = str(value)
    if any(c in value for c in '\n\r\0'):
        raise ValueError("Paths must not contain newlines or NUL characters")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, help='Existing vault directory (required on first install)')
    parser.add_argument('--no-start', action='store_true', help='Install without enabling or restarting the service')
    parser.add_argument('--check', action='store_true', help='Read-only prerequisite check; do not install')
    args = parser.parse_args()
    if sys.platform != 'linux' or sys.version_info < (3, 10):
        parser.error('Linux and Python 3.10+ are required')
    if not shutil.which('systemctl'):
        parser.error('systemctl is required (Linux with systemd user services)')
    home = Path.home()
    # Keep the existing config and state locations for seamless upgrades.
    config = home / '.config/vault-agents-watcher.env'
    target = home / '.local/share/vault-agents'
    unit = home / '.config/systemd/user/vault-agents-watcher.service'
    vault = args.vault.expanduser().resolve() if args.vault else None
    if vault and (not vault.is_dir() or not os.access(vault, os.R_OK | os.W_OK | os.X_OK)):
        parser.error('--vault must be an existing readable/writable directory')
    if not config.exists() and not vault:
        parser.error('--vault /absolute/path/to/vault is required on first install')
    if config.exists() and vault:
        parser.error(f'Configuration already exists at {config}; edit VAULT_PATH there, then rerun without --vault')
    binaries = {name: shutil.which(name) for name in ('hermes', 'claude', 'codex')}
    if not config.exists() and not any(binaries.values()):
        parser.error('Install and authenticate at least one of hermes, claude, or codex first')
    for path in (target, config, unit, vault):
        if path is not None:
            quote(path)  # Validate before any filesystem changes.
    if not args.no_start:
        result = subprocess.run(['systemctl', '--user', 'show-environment'], capture_output=True)
        if result.returncode:
            parser.error('No systemd user manager available; use --no-start to prepare files only')
    print(f'Application: {target}\nConfiguration: {config}\nService: {unit}')
    print('Detected CLIs: ' + ', '.join(k for k, v in binaries.items() if v))
    if config.exists():
        print('Existing configuration will be preserved; CLI paths and authentication must be verified separately.')
    if args.check:
        print('Prerequisites OK. This does not test provider authentication or run agents.')
        return
    target.mkdir(parents=True, exist_ok=True)
    venv = target / 'venv'
    if shutil.which('uv'):
        if not (venv / 'bin/python').exists():
            run('uv', 'venv', '--python', sys.executable, venv)
        run('uv', 'pip', 'install', '--python', venv / 'bin/python', ROOT)
    else:
        if not (venv / 'bin/python').exists():
            run(sys.executable, '-m', 'venv', venv)
        run(venv / 'bin/python', '-m', 'pip', 'install', ROOT)
    run(venv / 'bin/python', '-c', 'import watcher, vault_agents_note_runtime')
    if not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write('# Managed initially by install.sh; edits are preserved on upgrades.\n')
            stream.write(f'VAULT_PATH={quote(vault)}\nTELEGRAM_CHAT_ID=\n')
            for name, binary in binaries.items():
                if binary:
                    stream.write(f'{name.upper()}_BIN={quote(binary)}\n')
    unit.parent.mkdir(parents=True, exist_ok=True)
    # Unit specifiers and ExecStart environment substitutions must remain literal.
    executable = quote(venv / 'bin/vault-agents').replace('%', '%%').replace('$', '$$')
    environment = '%h/.config/vault-agents-watcher.env'
    content = (ROOT / 'vault-agents-watcher.service').read_text()
    content = content.replace('@ENVIRONMENT_FILE@', environment).replace('@EXEC_START@', executable)
    temporary = unit.with_suffix('.service.tmp')
    temporary.write_text(content)
    temporary.replace(unit)
    if args.no_start:
        print('Installed. To activate: systemctl --user daemon-reload && systemctl --user enable --now vault-agents-watcher')
        print('If already running, use systemctl --user restart vault-agents-watcher after daemon-reload.')
    else:
        run('systemctl', '--user', 'daemon-reload')
        run('systemctl', '--user', 'enable', 'vault-agents-watcher')
        run('systemctl', '--user', 'restart', 'vault-agents-watcher')
        run('systemctl', '--user', 'is-active', 'vault-agents-watcher')
        print('Installed and started. Inspect logs: journalctl --user -u vault-agents-watcher -n 30')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'Installation failed: {exc}', file=sys.stderr)
        sys.exit(1)
