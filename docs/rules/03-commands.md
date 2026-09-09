# 03 — Commands

This document is the project command reference, not an exhaustive shell allowlist. Prefer these commands and their documented variants. Standard inspection, Git, GitHub, and focused verification commands may also be used when necessary, scoped to the current task, and allowed by the active workflow and maintainer authorization. Do not assume destructive or environment-specific flags.

---

## Development Environment Setup

```bash
# Create the locked development/test environment (standalone checkout)
uv sync --locked

# Create a runtime-only environment
uv sync --locked --no-dev --no-install-project
```

Shared workspaces may map the environment to an explicit absolute path as documented in `docs/development-setup.md`. Choose the environment before running these commands; a runtime environment and an isolated test environment need not be the same directory.

---

## Dependency Management

```bash
# Verify that project metadata and lock agree
uv lock --check

# Update the lock after editing pyproject.toml
uv lock

# Verify the installed environment against the locked project
uv sync --locked --offline --inexact --no-dev --check
```

**Rules:**
- Runtime dependencies belong in `[project].dependencies` in `pyproject.toml`.
- Test, coverage, lint, and explicit build tooling belong in `[dependency-groups].dev`.
- Commit the updated `uv.lock`; do not maintain or generate main-program requirements files.
- `uv pip check` is diagnostic only because unmaintained third-party metadata may name a compatible superseded distribution.
- Use uv 0.12.5+ and Python 3.14+; prefer the latest stable uv release.

---

## Testing

```bash
# Run a specific test file
uv run --locked --no-sync pytest tests/test_xxx.py

# Run all tests
uv run --locked --no-sync pytest

# Run tests with verbose output
uv run --locked --no-sync pytest -v tests/test_xxx.py

# Run a specific test function
uv run --locked --no-sync pytest tests/test_xxx.py::test_function_name
```

**Rules:**
- Contributor preparation runs related tests and expands to the full suite for the shared-impact triggers in `docs/testing.md`. Maintainer execution arrangements follow `AGENTS.md`.
- For documentation tasks, report actual text/structure checks and documentation contract tests separately from unrun product tests. Do not claim checks that were not executed.

---

## Static Analysis

```bash
# Run pylint on the application package
uv run --locked --no-sync pylint app/

# Run pylint on a specific module
uv run --locked --no-sync pylint app/chain/download.py
```

**Rules:**
- After Python code changes, ensure no new error-level issues are introduced.
- Use the changed-file selection and preparation arrangement in `AGENTS.md`; the workflow's configured changed-file result is the gate, while the full application report is advisory for ordinary scoped changes.

---

## Security Scan

```bash
uv export --quiet --locked --no-dev --no-emit-project \
  --output-file /tmp/moviepilot-audit-requirements.txt
uvx --from pip-audit pip-audit \
  --require-hashes --disable-pip --strict --progress-spinner off \
  --requirement /tmp/moviepilot-audit-requirements.txt
```

**Rules:**
- Run after runtime dependency changes; the release workflow enforces the same audit before publishing images.
- Any Python vulnerability reported by this audit blocks publishing until the dependency or explicit audit policy is updated.

---

## Local CLI — Service Management

```bash
moviepilot start
moviepilot start --timeout 60
moviepilot stop
moviepilot stop --timeout 30 --force
moviepilot restart
moviepilot restart --start-timeout 60 --stop-timeout 30
moviepilot status
moviepilot version
moviepilot doctor
moviepilot doctor --json
moviepilot doctor --fix
moviepilot doctor --deep
moviepilot doctor --json --fix
moviepilot start --safe
```

```bash
moviepilot logs
moviepilot logs --lines 100
moviepilot logs --stdio
moviepilot logs --frontend
moviepilot logs --follow
moviepilot logs --frontend --follow
moviepilot logs --stdio --follow
```

---

## Local CLI — Installation and Setup

```bash
# One-line bootstrap installer
curl -fsSL https://raw.githubusercontent.com/jxxghp/MoviePilot/v3/scripts/bootstrap-local.sh | bash

# Install backend dependencies
moviepilot install deps
moviepilot install deps --python python3.12
moviepilot install deps --venv /path/to/venv
moviepilot install deps --recreate

# Install frontend release
moviepilot install frontend
moviepilot install frontend --version latest
moviepilot install frontend --version v3.0.0

# Install resource files
moviepilot install resources

# Initialize local config
moviepilot init
moviepilot init --wizard
moviepilot init --force-token
moviepilot init --superuser admin --superuser-password 'ChangeMe123!'

# All-in-one setup
moviepilot setup
moviepilot setup --wizard
moviepilot setup --recreate
moviepilot setup --superuser admin --superuser-password 'ChangeMe123!'

# Uninstall
moviepilot uninstall
```

---

## Local CLI — Update

```bash
moviepilot update backend
moviepilot update backend --ref latest
moviepilot update backend --ref v3.0.0

moviepilot update frontend
moviepilot update frontend --frontend-version latest

moviepilot update all
moviepilot update all --ref latest --frontend-version latest
moviepilot update all --skip-resources
```

`MOVIEPILOT_AUTO_UPDATE` is a boolean (default `false`): `true` enables the background Release check and version reminders, and `false` disables application checks and reminders. `AUTO_UPDATE_RESOURCE` independently enables resource checks and reminders; the scheduled service exists when either switch is enabled and checks only enabled targets. The scheduler hot-reloads this switch. `MOVIEPILOT_UPDATE_DEV` is an independent boolean (default `false`) that enables development-branch updates during `start/restart`. Legacy `dev`/`release` values of `MOVIEPILOT_AUTO_UPDATE` normalize to `true`; legacy `dev` also preserves Dev tracking when the new switch is not explicitly configured.

---

## Local CLI — Startup on Boot

```bash
moviepilot startup status
moviepilot startup enable
moviepilot startup disable
moviepilot startup enable --venv /path/to/venv
```

---

## Local CLI — Configuration

```bash
moviepilot config path
moviepilot config list
moviepilot config list --show-secrets
moviepilot config get PORT
moviepilot config set PORT 3001
moviepilot config keys
moviepilot config keys DB_
moviepilot config keys --show-current
moviepilot config describe PORT
moviepilot config describe API_TOKEN --show-secrets
```

---

## Local CLI — Tools and Scheduler

```bash
# List all MCP tools
moviepilot tool list

# Show tool parameters
moviepilot tool show moviepilot_api
moviepilot tool show agent_task

# Run a tool directly
moviepilot tool run moviepilot_api operation_id=scheduler.list
moviepilot tool run moviepilot_api operation_id=media.search 'query={"title":"流浪地球","type":"media"}'

# List scheduled tasks
moviepilot scheduler list

# Immediately run a scheduled task
moviepilot scheduler run subscribe_refresh
```

**Tool catalog rule:** MoviePilot business operations use `moviepilot_api` with a
fixed `operation_id`; old business tool names are not registered. Structured
`query`, `body`, arrays, and booleans are passed as JSON values in `key=value`
arguments. Media operations use the complete `media_source` + `media_id` pair
returned by media search. Built-in sources use `MediaSource` constants; plugins
may register a schema-valid extension identifier. A
source-owned tool such as `query_episode_schedule` may retain its native ID
parameter because its schema and implementation are single-source.

---

## Local CLI — Agent

```bash
moviepilot agent "Help me analyze the last search failure"
moviepilot agent --user-id admin "Check the current downloader configuration"
moviepilot agent --session cli-debug-1 "Why was the last transfer not triggered?"
moviepilot agent --new-session "Summarize any obvious problems with the current system config"
```

**Prerequisites:** `AI_AGENT_ENABLE` must be set to true, and LLM provider settings (`LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`) must be configured.

---

## Docker CLI — Doctor

```bash
docker exec -it <container> moviepilot doctor
docker exec -it <container> moviepilot doctor --json
docker run --rm --entrypoint python -v <config-dir>:/config <image> -m app.cli doctor
```

---

## Local CLI — Help Discovery

```bash
moviepilot --help
moviepilot help
moviepilot commands
moviepilot help install
moviepilot help init
moviepilot help setup
moviepilot help update
moviepilot help agent
moviepilot help config
moviepilot help tool
moviepilot help scheduler
```

---

## Site Adapter Capture — macOS / Linux

```bash
# Run from a MoviePilot source checkout and reuse its virtual environment
bash scripts/collect-site-adapter.sh
```

**Rules:**
- The default collector asks only for the site HTTPS address, opens an isolated local Chrome/Edge profile, and reads the completed search page after the user confirms.
- Users must not be asked to inspect HTML or copy Cookie/User-Agent values in the default flow. `--manual-cookie` is an advanced fallback only.
- Run only the collector shipped with a trusted local MoviePilot source checkout or installation package. Do not pipe a remote branch script into a shell.
- Never put a Cookie or other credential in command arguments or shell history.
- Feature Request attachments are public. Review all four files in the generated ZIP before attaching it, and never attach raw HTML, HAR, or browser network archives.

---

## Plugin Market Release Default

```bash
# Run after activating the project virtual environment
python -m scripts.generate_plugin_market_default \
  --wiki-file /path/to/MoviePilot-Wiki/plugin.md \
  --config-file app/runtime/config.py
```

**Rules:**
- The Wiki document must contain exactly one `plugin-market-repos:start/end` marker pair.
- The marked list must be nonempty and include `jxxghp/MoviePilot-Plugins`.
- This command rewrites only `ConfigModel.PLUGIN_MARKET`; inspect the resulting diff before committing or packaging.

*Last Updated: 2026-08-19*
