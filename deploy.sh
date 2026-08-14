#!/bin/bash
# Deploy watcher.py to the live location and restart the service.
set -euo pipefail
cd "$(dirname "$0")"
cp watcher.py ~/.hermes/scripts/obsidian-mention-watcher.py
systemctl --user restart obsidian-hermes-watcher
systemctl --user --no-pager status obsidian-hermes-watcher | head -5
