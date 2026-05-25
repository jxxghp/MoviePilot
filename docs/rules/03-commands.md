# 03 — Commands

Only suggest or execute commands that appear in this document. Do not assume standard tool defaults, global flags, or operating-system-specific behavior unless explicitly listed here.

---

## Development Environment Setup

```bash
# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate          # macOS / Linux
.\venv\Scripts\activate           # Windows

# Install pip-tools
pip install pip-tools

# Install project dependencies
pip install -r requirements.txt
```

---

## Dependency Management

```bash
# Compile requirements.txt from requirements.in (full recompile)
pip-compile requirements.in

# Upgrade a single package without touching others
pip-compile --upgrade-package <package-name> requirements.in

# Install from the generated lock file
pip install -r requirements.txt
```

**Rules:**
- Always edit `requirements.in` to add or change dependencies.
- Never edit `requirements.txt` manually — it is a generated lock file.
- After any change to `requirements.in`, re-run `pip-compile requirements.in` and commit both files together.

---

## Testing

```bash
# Run a specific test file
pytest tests/test_xxx.py

# Run all tests
pytest

# Run tests with verbose output
pytest -v tests/test_xxx.py

# Run a specific test function
pytest tests/test_xxx.py::test_function_name
```

**Rules:**
- Run at minimum the tests directly related to the change.
- If the change affects common modules, startup flow, CLI, or agent runtime behavior, expand the scope to the full test suite.
- If the task only changes documentation, state explicitly that tests were not run. Do not claim checks that were not executed.

---

## Static Analysis

```bash
# Run pylint on the application package
pylint app/

# Run pylint on a specific module
pylint app/chain/download.py
```

**Rules:**
- After Python code changes, ensure no new error-level issues are introduced.
- Warning-level issues in new code should be minimized but are not an absolute gate.

---

## Security Scan

```bash
# Run safety check against the lock file
safety check -r requirements.txt --policy-file=safety.policy.yml

# Save report to file
safety check -r requirements.txt --policy-file=safety.policy.yml > safety_report.txt
```

**Rules:**
- Run after every change to `requirements.txt`.
- No new high-severity vulnerabilities may be introduced.

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
curl -fsSL https://raw.githubusercontent.com/jxxghp/MoviePilot/v2/scripts/bootstrap-local.sh | bash

# Install backend dependencies
moviepilot install deps
moviepilot install deps --python python3.11
moviepilot install deps --venv /path/to/venv
moviepilot install deps --recreate

# Install frontend release
moviepilot install frontend
moviepilot install frontend --version latest
moviepilot install frontend --version v2.9.31

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
moviepilot update backend --ref v2.9.31

moviepilot update frontend
moviepilot update frontend --frontend-version latest

moviepilot update all
moviepilot update all --ref latest --frontend-version latest
moviepilot update all --skip-resources
```

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
moviepilot tool show query_schedulers
moviepilot tool show search_torrents

# Run a tool directly
moviepilot tool run query_schedulers
moviepilot tool run search_torrents media_type=movie tmdb_id=12345

# List scheduled tasks
moviepilot scheduler list

# Immediately run a scheduled task
moviepilot scheduler run subscribe_refresh
```

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

*Last Updated: 2026-05-25*
