#!/bin/bash
# Deploy the watcher modules to the live location and restart the service.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m py_compile watcher.py note_runtime.py
cp note_runtime.py ~/.hermes/scripts/note_runtime.py
cp watcher.py ~/.hermes/scripts/vault-agents-watcher.py
systemctl --user restart vault-agents-watcher
systemctl --user --no-pager status vault-agents-watcher | head -5
