#!/bin/bash
# Deploy watcher.py to the live location and restart the service.
set -euo pipefail
cd "$(dirname "$0")"
cp watcher.py ~/.hermes/scripts/vault-agents-watcher.py
systemctl --user restart vault-agents-watcher
systemctl --user --no-pager status vault-agents-watcher | head -5
