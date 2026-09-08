# Install vault-agents for your user

Read this guide completely before installing. The goal is a working Linux user
service that watches the user's existing Obsidian vault and dispatches their
chosen agent CLI. Read [AGENTS.md](AGENTS.md) for runtime invariants; its host-specific
legacy deployment paths are not prerequisites for new installations.

If you have only this URL, clone https://github.com/ikaros-labs/vault-agents.git
outside the vault. All commands below run from that checkout. Install from this
repository; this guide does not assume a published PyPI distribution.

## 1. Discover the environment

Use existing conversation context and configuration before asking questions.
Establish the vault's absolute path, which installed agent the user wants to use,
and whether they want Telegram notifications (default: off). Ask only for missing
information. Do not create a replacement vault or move existing notes.

Check Linux, Python 3.10+, systemd user services, and available CLIs:

```bash
uname -s
python3 --version
command -v systemctl
command -v uv
command -v hermes
command -v claude
command -v codex
systemctl --user is-active vault-agents-watcher
```

Missing optional CLIs or an inactive, not-yet-installed service are expected.
At least one supported CLI must be installed and authenticated under the same
user that will run the service. Follow that CLI's official setup instructions
if needed. Hermes requires working `hermes chat`; its vault prompt also expects
an Obsidian skill. Claude/Codex can be used without Hermes.

Inspect whether `~/.config/vault-agents-watcher.env` exists without printing its
contents into chat or logs. Reuse existing settings and session state. Never
source this file as a shell script: it is a systemd EnvironmentFile.

Starting the service dispatches **all existing bare mentions**, including those
written while it was down. Explain this before activation. If the requested vault
or permission to run agents on its pending requests is unclear, prepare using
`--no-start` and resolve that ambiguity before starting.

## 2. Install

For a fresh installation:

```bash
./install.sh --vault "/absolute/path/to/vault" --check
./install.sh --vault "/absolute/path/to/vault"
```

For an existing configuration:

```bash
./install.sh --check
./install.sh
```

Commands are noninteractive and exit nonzero on failure. The installer uses `uv`
if available, otherwise `python3 -m venv` and the venv's `pip`. If the distribution
lacks venv support, use an available `uv` installation or have its prerequisite
installed using the host's normal package management process. Do not use `sudo pip`.

`--check` is read-only and checks prerequisites, not authentication or end-to-end
operation. `--no-start` skips user-manager connectivity checks and service actions;
it can be combined with `--check`. Repeated installs preserve the configuration.
If a config already exists, `--vault` is rejected to prevent accidentally changing
which notes an existing service processes.

Installed files:

| Path | Purpose |
|---|---|
| `~/.local/share/vault-agents/venv/` | Isolated Python package and dependencies |
| `~/.config/vault-agents-watcher.env` | Config and optional secrets; new files use mode 600 |
| `~/.config/systemd/user/vault-agents-watcher.service` | Generated user service |
| `~/.local/state/vault-agents/sessions.json` | Existing default Hermes session map, created on use |

These are fixed per-user paths; the installer does not currently honor XDG path
overrides. It installs one vault per OS user. Keep the checkout for updates.

## 3. Configure only what is needed

See [example.env](example.env) and the [README configuration table](README.md#configuration).
Use absolute paths; quote values containing spaces. Shell substitutions, `$HOME`,
and `~` are not expanded in configuration values. The installer discovers absolute
CLI executable paths from its PATH on first installation. Check those paths when
reusing old configuration or moving CLI installations.

CLI authentication must work without interactive prompts. The service does not
source shell startup files: put necessary provider environment variables in the
private env file or use the CLI's existing supported credential storage. Let users
enter secrets locally; do not request that they paste keys into the conversation.
Never commit credentials. Executables using `/usr/bin/env node` may also require
an explicit `PATH` in the env file containing the actual Node installation's bin
directory alongside `/usr/local/bin:/usr/bin:/bin`.

Telegram is optional and requires Hermes configured to send to the user's own
`TELEGRAM_CHAT_ID`. Leave it empty otherwise. Do not invent or reuse someone else's
chat ID. Existing configuration is preserved, including notification settings.

After configuration edits, restart the service. After `--no-start`, activate it:

```bash
systemctl --user daemon-reload
systemctl --user enable vault-agents-watcher
systemctl --user restart vault-agents-watcher
```

A user service normally follows the user's login lifecycle. For a host that must
watch while logged out, inspect `loginctl show-user "$USER" -p Linger`. Enabling
linger (`loginctl enable-linger "$USER"`) changes that lifecycle and may require
host administrator help; do it only when unattended operation is intended.

## 4. Verify the outcome

```bash
systemctl --user is-enabled vault-agents-watcher
systemctl --user is-active vault-agents-watcher
journalctl --user -u vault-agents-watcher -n 30 --no-pager
```

Confirm the logs identify the intended vault and show no startup errors. An
`active` result alone does not establish successful provider authentication.
For an authorized end-to-end test, create a uniquely named scratch Markdown note
in the vault with one request for the chosen agent and a trailing newline:

```text
@codex Reply with exactly: vault-agents is working
```

Use the user's selected agent in place of Codex. Observe the bare tag become
`/ack`, then `/done`, with a reply in the note. Allow for CLI response time; inspect
logs on failure. `/err` includes a failure reply. A tag left at `/ack` after a
restart may need manual retry by restoring the bare tag; do not blindly resubmit
requests that might already have performed work.

For Hermes continuity, send a fact in the scratch note, wait for completion, then
append a second mention asking for it back. Confirm the answer and session reuse.
Only the watcher writes the session map during normal operation.

Never write bare mention tags in explanatory vault notes: use fenced code blocks,
inline code, or `/done` suffixes. Do not turn existing user requests into tests.

Report: installed checkout revision (`git rev-parse HEAD`), vault path, configured
agents, service enabled/active status, smoke-test result or why it wasn't run,
and any remaining action. Do not claim completion if only preflight succeeded.

## Upgrade and rollback

Wait until active requests finish; restart loses in-memory pending results.
Record the current revision, then:

```bash
git pull --ff-only
./install.sh
```

Local edits or diverged history require resolution rather than a forced reset.
The installer upgrades the package before replacing the unit, preserves config
and sessions, and restarts the same service. A failed install exits nonzero;
inspect the error and service state before retrying. Package updates are not
transactional, so a failed update may require reinstalling a known-good revision.
For rollback, check out that recorded revision in a separate checkout and run its
installer. For a revision predating the installer, restore the legacy unit template
from that revision and use its documented deployment procedure.

Legacy `~/.hermes/scripts/` and `~/.hermes/venvs/vault-agents/` files are left in
place during migration. The new unit takes over the same service name; config and
session locations stay the same. Do not start a second watcher for the same vault.

## Remove

When the user requests removal:

```bash
systemctl --user disable --now vault-agents-watcher
rm "$HOME/.config/systemd/user/vault-agents-watcher.service"
systemctl --user daemon-reload
rm -rf "$HOME/.local/share/vault-agents/venv"
```

Keep configuration, session history, the checkout, and vault notes unless the user
also requests deletion of those. On a legacy installation, application files are
under `~/.hermes` instead; inspect the unit before removing it to identify them.

## Troubleshooting

- **No user bus:** run as the intended logged-in user, not root. Use `--no-start`
  for preparation on hosts without an available user manager.
- **CLI not found/auth fails:** verify absolute `*_BIN` paths, executable permissions,
  required interpreter PATH, and credentials available to the service user.
- **No pickup:** check the logged vault path, ignored directories, Markdown extension,
  and code formatting. A final line without a newline waits for 8 seconds of quiet.
- **Install download fails:** inspect proxy/network and Python package index settings;
  the installer needs access to dependencies and the build backend.
- **Service keeps restarting:** inspect the journal; correct invalid configuration
  or unavailable vault paths before restarting again.

Structure inspired by [gbrain's agent installation guide](https://github.com/garrytan/gbrain/blob/master/INSTALL_FOR_AGENTS.md).
