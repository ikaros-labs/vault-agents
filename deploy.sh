#!/bin/bash
# Deploy the watcher modules to the live location and restart the service.
set -euo pipefail
cd "$(dirname "$0")"
if [[ -x "$HOME/.local/share/vault-agents/venv/bin/vault-agents" ]]; then
    exec ./install.sh
fi
python3 -m py_compile watcher.py vault_agents_note_runtime.py
cp vault_agents_note_runtime.py ~/.hermes/scripts/vault_agents_note_runtime.py
cp watcher.py ~/.hermes/scripts/vault-agents-watcher.py
systemctl --user restart vault-agents-watcher
systemctl --user --no-pager status vault-agents-watcher | head -5
