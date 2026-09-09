# 11 — Code Quality and Security

## Testing Requirements

Verification preparation below is the contributor default. Confirmed maintainers may adjust scope, timing, and evidence reuse under `AGENTS.md`; shared correctness, compatibility, isolation, and honest reporting still apply. Full-suite triggers follow `docs/testing.md` and the affected behavior, not a directory name alone.

### What to Run

```bash
# Minimum: run tests directly related to the change
uv run --locked --no-sync pytest tests/test_<domain>.py

# If the change affects common modules, startup flow, CLI, or agent runtime
uv run --locked --no-sync pytest
```

### When to Expand Scope

Use the following as likely shared-impact boundaries. Run the full test suite when changes affect their shared behavior, lifecycle, or compatibility contract:
- `app/runtime/`, `app/adapters/`, or `app/runtime/compat/` - config, events, managers, adapters, and compatibility boundaries
- `app/chain/base.py` — chain base class
- `app/modules/__init__.py` — module base class
- `app/main.py` — application startup
- The CLI entrypoint (`moviepilot`)
- Agent runtime (`app/agent/`)
- Any shared schema in `app/schemas/types.py`

### Honest Reporting

- For documentation tasks, report the actual text/structure checks and documentation contract tests; distinguish these from product tests that were not run.
- Do not claim "all tests pass" unless you ran them.
- Do not describe unexecuted checks as completed.

### Writing New Tests

- When fixing a bug, prefer adding a test that reproduces it first.
- When adding a feature, add at minimum the smallest useful test coverage.
- Test files go in `tests/`, named `test_<domain>.py`.
- Use the patterns established in adjacent test files (fixtures, mock patterns, assertion style).
- Agent-related tests are under `tests/test_agent_*.py`. Integration-style tests may be in `tests/cases/` or `tests/manual/`.

---

## Static Analysis

```bash
# Full application report; use the AGENTS.md selector for changed-file checks
uv run --locked --no-sync pylint app/
```

- Changed-file pylint must pass the workflow's configured check under the applicable preparation arrangement; select the PR-base plus uncommitted union from `AGENTS.md`. The full application report is advisory for ordinary scoped changes.
- Do not introduce error-level issues; handle warning-level findings according to the configured changed-file check rather than treating all warnings as exempt.
- Do not suppress pylint warnings with `# pylint: disable` without a documented reason.

---

## Dependency Security Scan

```bash
uv export --quiet --locked --no-dev --no-emit-project \
  --output-file /tmp/moviepilot-audit-requirements.txt
uvx --from pip-audit pip-audit \
  --require-hashes --disable-pip --strict --progress-spinner off \
  --requirement /tmp/moviepilot-audit-requirements.txt
```

- Run after runtime dependency changes; the release workflow audits the same locked dependency set before publishing images.
- Any Python vulnerability reported by this audit blocks publishing until the dependency or explicit audit policy is updated.
- Release candidates also scan OS and language packages on amd64 and arm64. HIGH or CRITICAL findings with an available fix block publishing; unfixed upstream findings require a separate reachability and impact assessment.
- If upstream has no fix, assess reachability and impact before changing the audit policy; PR documentation alone does not bypass the gate.

---

## Authentication and Authorization

### API Authentication

All REST and MCP API endpoints require authentication. The project supports two mechanisms:

| Method | Format |
|---|---|
| Request header | `X-API-KEY: <api_key>` |
| Query parameter | `?apikey=<api_key>` |

The `API_TOKEN` value in `settings` is the source of truth. It is set at initialization and never exposed in logs or API responses.

### Endpoint Authorization

- API-token authenticated integration endpoints are administrator-level surfaces unless a specific endpoint documents a narrower contract.
- Do not infer user-scoped authorization from a valid `API_TOKEN`; use an explicit user identity dependency when behavior must be scoped to a logged-in user.
- Use the existing FastAPI dependency functions (e.g., `get_current_user`, `get_current_active_superuser`) — check `app/api/endpoints/` for usage patterns.
- Do not add manual token parsing inside endpoint functions. Always use the project's dependency injection.
- Superuser-only operations must explicitly require the superuser dependency.

---

## Input Validation

- Validate user input at the **endpoint layer only**, using Pydantic models.
- Do not duplicate validation logic in chain or module code. Trust that the endpoint has already validated what it passes down.
- For external API responses, validate using Pydantic models or explicit `None` checks before accessing fields.

---

## Secrets Management

- Never hardcode secrets (API keys, passwords, tokens) in source code.
- All secrets are configured via environment variables or `.moviepilot.env` and accessed through `settings`.
- Never log or serialize `settings.API_TOKEN`, `settings.DB_PASSWORD`, or any field with `Secret` in its name.
- Do not commit `.moviepilot.env`, `*.db`, or any file under `config/` — these are local runtime state.

---

## SQL Injection Prevention

- All database access goes through SQLAlchemy ORM via the Oper classes in `app/db/oper/`. No raw SQL string construction.
- If a raw SQL query is ever genuinely necessary, use SQLAlchemy's `text()` with parameterized binds — never string interpolation.

---

## XSS and Injection in Notifications

- When constructing notification messages that include user-provided data (media titles, filenames, usernames), treat those values as untrusted strings.
- Do not render user data in HTML contexts without escaping. Notification channels that render HTML (e.g., Telegram with `parse_mode=HTML`) must escape user-controlled strings.

---

## File Path Security

- Use `pathlib.Path` for all file path operations.
- Never construct file paths by concatenating user-provided strings.
- When transferring files to a user-configured path, verify the destination is within an allowed base directory before writing.

---

## Contributor Pre-Submission Checklist

Before contributor submission, check applicable items below. Maintainer execution arrangements follow `AGENTS.md`; a local anchor does not imply completion or acceptance. The quality contracts remain shared.

- [ ] Related pytest tests pass
- [ ] Changed-file pylint passes for the PR-base plus uncommitted union defined in `AGENTS.md`; use full `pylint app/` for broad Python changes
- [ ] If dependencies changed: the package is in the correct `pyproject.toml` group, `uv.lock` is current, the locked project consistency check and runtime dependency audit pass
- [ ] If CLI behavior changed: `docs/cli.md` and related tests are updated
- [ ] If MCP/API behavior changed: `docs/mcp-api.md` and related skill files are updated
- [ ] If database schema changed: a new Alembic migration exists under `database/versions/`
- [ ] No secrets are included in code, logs, or committed files
- [ ] Public or cross-module contracts and non-obvious business behavior have useful Chinese documentation

*Last Updated: 2026-08-19*
