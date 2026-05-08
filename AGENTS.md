# MoviePilot AI Agent Guide

This file defines the default behavior for AI agents working in the MoviePilot repository. Unless a deeper directory provides another `AGENTS.md`, these rules apply to the entire repo.

## 1. Project Scope

- This repository contains the MoviePilot backend, CLI, MCP/API, Docker assets, and AI skills.
- The backend is based on FastAPI, with most code under `app/`.
- Frontend source code is not in this repository. The frontend source repository is `MoviePilot-Frontend`.
- This repository also includes the local CLI, database migrations, developer docs, tests, Docker scripts, and AI skills.

## 2. Working Principles

- Read the relevant implementation, tests, and docs before changing code. Do not infer behavior from directory names alone.
- Prefer the smallest correct change. Reuse existing functions, patterns, and naming whenever possible.
- Do not perform unrelated large refactors, mass renames, or formatting-only cleanup.
- Before adding a new abstraction, check whether it is actually reusable. If the logic fits well inside an existing function, class, or flow, keep it there.
- The worktree may contain user changes. Do not revert, overwrite, or reorganize changes you do not fully understand.
- Default to writing conclusions, validation results, and risk notes in Chinese unless the user asks otherwise.

## 3. Key Directories

- `app/api/endpoints/`: HTTP entrypoints. Handles auth, parameters, responses, and simple CRUD.
- `app/chain/`: Business orchestration layer for search, recognition, subscriptions, downloads, messaging flows, and similar use cases.
- `app/modules/`: Dynamically loaded system modules. Encapsulates pluggable downloaders, media servers, message channels, and other backend capabilities.
- `app/helper/`: Reusable low-level helper logic. Not a place for full business orchestration.
- `app/core/config.py`: Environment variables, deployment parameters, and startup-level settings.
- `app/schemas/types.py`: Shared enums and types such as `SystemConfigKey` and module categories.
- `app/db/`: Database models, sessions, and `*_oper.py` data access wrappers.
- `moviepilot`: Local CLI entrypoint and help text.
- `database/versions/`: Alembic migration scripts.
- `docs/`: CLI, MCP/API, and development workflow documentation.
- `skills/`: AI agent skills and related scripts.
- `tests/`: Pytest tests and a few manual test scripts.
- `config/`, `.moviepilot.env`, and `*.db`: Local config or runtime data. Do not modify or commit them unless the user explicitly asks for it.

## 4. Layering And Access Boundaries

### API / Endpoint Layer

- Endpoints should only handle HTTP concerns: auth, parameter parsing, response models, streaming adaptation, and simple input validation.
- Simple list, detail, toggle, settings read/write, and pure CRUD endpoints may directly call `app/db/` or an existing `helper`.
- If the logic coordinates multiple modules, triggers events, touches caches, or combines search, recognition, subscription, or download workflows, move it into `chain`.
- Prefer adding new endpoints to an existing domain file. Create a new endpoint file only when introducing a new top-level resource domain.
- After adding a new endpoint, register it in `app/api/apiv1.py`.

### Chain Layer

- `chain` is the business orchestration layer shared by API, CLI, message interaction, agents, schedulers, and similar entrypoints.
- `chain` is responsible for composing `module`, `helper`, `db`, events, caches, and other stable `chain` capabilities.
- Inside `chain`, prefer calling module capabilities through `run_module()` or `async_run_module()`. Only use `ModuleManager` or similar helpers directly when you truly need to enumerate modules, inspect instances, or run health checks.
- `chain` should focus on use cases and workflows. It should not hold low-level protocol details, HTTP request objects, or page-specific parameter assembly.
- Before adding a new `chain`, ask whether this is a reusable business use case shared by multiple entrypoints, or a flow that coordinates multiple modules or resources. If it is just short logic for one endpoint, do not create a new `chain`.
- `chain` may call other `chain` classes when reusing stable domain logic, but avoid introducing new circular dependencies.

### Module Layer

- `module` is the pluggable capability layer discovered and loaded by `ModuleManager`.
- Put logic in `module` when it represents a new downloader, media server, message channel, recognition backend, filtering backend, file-management backend, or any other capability that needs lifecycle management, priority, configuration switches, or independent testing.
- New modules should follow the existing base-class contract and implement or align with `init_module()`, `init_setting()`, `get_name()`, `get_type()`, `get_subtype()`, `get_priority()`, `test()`, and `stop()`.
- A `module` should focus on one backend or one capability implementation. It should return domain results, not HTTP responses, and should not depend on endpoint auth or FastAPI request objects.
- `chain -> module` is the intended main direction. The repository contains a small number of historical `module -> chain` usages. Do not expand that pattern in new code. If a module needs shared business logic, prefer moving that logic up into `chain` or down into `helper`.
- Do not add direct `module -> module` coupling for new code. Cross-module orchestration should be handled by `chain`.

### Helper Layer

- `helper` is for reusable low-level support logic such as path handling, config aggregation, site index loading, protocol wrappers, rate limiting, cache helpers, and page parsing.
- Add a new `helper` only when the logic is reused in multiple places, or when it is clearly a standalone low-level concern.
- If logic is used only by a single `chain` or a single `module`, prefer keeping it in the original file instead of turning `helper` into a dumping ground.
- If the code needs configuration switches, runtime loading, priorities, independent test entrypoints, or multi-implementation dispatch, it is probably a `module`, not a `helper`.
- `helper` must not become another orchestration layer. Full business workflows still belong in `chain`.

### Preferred Call Directions

- Preferred direction: `endpoint/CLI/agent/command -> chain -> module/helper/db`
- Allowed direction: `chain -> chain`, as long as the reused logic is stable and does not introduce cycles.
- Cautious direction: `endpoint -> db/model/oper/helper`, only for simple queries, simple CRUD, or input normalization.
- Avoid for new code: `module -> chain`, `module -> module`, `helper -> chain`, `helper -> endpoint`.

## 5. Where New Capabilities Should Go

- Scenario: adding a new business workflow such as search, recognition, subscription, download orchestration, or message interaction.
  Action: prefer `app/chain/` so API, CLI, agents, and schedulers can share the same orchestration logic.
- Scenario: adding a new downloader, media server, message channel, or other pluggable backend integration.
  Action: put it in `app/modules/`. If this introduces a new module category or subtype, also check `app/schemas/types.py` and related schemas.
- Scenario: adding a new public HTTP API.
  Action: put it in `app/api/endpoints/`, register it in `app/api/apiv1.py`, and add auth, schemas, docs, and tests. Move complex logic into `chain`.
- Scenario: adding a new low-level utility, parser, config reader, or protocol wrapper.
  Action: put it in `app/helper/`, but only if it is not a one-off implementation and not a full business use case.
- Scenario: adding a deployment-level, environment-level, or startup-time config such as ports, paths, proxies, switches, keys, or third-party service addresses.
  Action: put it in `ConfigModel` or `Settings` inside `app/core/config.py`.
- Scenario: adding a runtime business config, user-editable rule, or persistent system option.
  Action: prefer `SystemConfigKey` plus `SystemConfigOper`. Do not scatter raw string keys.
- Scenario: a config change should automatically reload a long-lived object.
  Action: add `CONFIG_WATCH`, `on_config_changed()`, and `get_reload_name()` where appropriate on the related `chain`, `module`, `helper`, or manager class.
- Scenario: adding a few dozen lines of private logic inside one `chain` or `module`.
  Action: prefer a private function or private method in the same file. Do not create a new `helper` by default.

## 6. Code And Comment Requirements

- Preserve the existing code style. Do not introduce a new abstraction layer without a clear payoff.
- The repository already uses short docstrings for many public classes and methods. For new public classes and methods, follow the local style of the surrounding file.
- Comments and docstrings should default to Chinese. If the surrounding file is already consistently in English, match the local style.
- Comments should explain why the code is written that way and what non-obvious constraints exist, such as edge cases, compatibility reasons, call ordering, cache or reload semantics, and external system limitations.
- Do not write line-by-line translation comments. Do not comment obvious assignments, branches, or straightforward calls.
- For complex notes, place the comment above the code block instead of using long end-of-line comments.
- When changing code, update or remove stale comments so the documentation stays aligned with the implementation.
- Do not add TODO or FIXME without context. Only keep one if it is genuinely useful and cannot be addressed as part of the current task.
- Do not add noisy comments like "change starts here", "change ends here", or "this is important".

## 7. Dependency And Environment Conventions

- Target Python version is `3.11+`. Current CI uses Python `3.12`.
- The dependency source file is `requirements.in`.
- `requirements.txt` is the lock file generated by `pip-compile requirements.in`. Do not maintain it manually.
- Install dependencies with `pip install -r requirements.txt`.
- When adding or upgrading dependencies:
  1. Update `requirements.in`
  2. Run `pip-compile requirements.in`
  3. Run the relevant tests and security checks

## 8. Coupled Updates

- When fixing a bug, prefer adding a test that reproduces it. When adding a feature, prefer the smallest useful test coverage.
- When changing CLI behavior, also check and update `moviepilot`, `docs/cli.md`, and related tests.
- When changing MCP or REST API behavior, exposed tools, or AI interaction behavior, also check and update `docs/mcp-api.md`, related `skills/*/SKILL.md` files or scripts, and related tests.
- When changing development workflow, dependency management, or security-check procedures, also update `docs/development-setup.md`.
- When changing database structure, add an Alembic migration under `database/versions/`. Do not update models without a migration.
- When changing user-visible config, defaults, or initialization flow, also check related docs, help text, setup or init flows, and tests.
- When adding a new skill, follow the existing `skills/<name>/SKILL.md` structure, keep the YAML front matter, and prefer script paths relative to the `SKILL.md` file.

## 9. Validation Requirements

- Run at least the tests directly related to the change, for example `pytest tests/test_xxx.py`.
- If the change affects common modules, startup flow, CLI, or agent runtime behavior, expand the validation scope.
- After Python code changes, at minimum ensure the change does not introduce new error-level issues in `pylint app/`.
- When changing CLI behavior, validate the relevant help output such as `moviepilot help` or the specific subcommand help.
- When changing dependencies, also run `pip-compile requirements.in` and `safety check -r requirements.txt --policy-file=safety.policy.yml`.
- If the task only changes documentation, explicitly say that tests were not run. Do not claim checks that were not executed.

## 10. Commit And Release Conventions

- Only create a commit when the user explicitly asks for one.
- Prefer Conventional Commits such as `feat: ...`, `fix: ...`, and `docs: ...`.
- This is not just stylistic. The release workflow uses Conventional Commits to categorize changelog entries.
- Do not casually change version numbers, release settings, or Docker release flow unless the task explicitly involves them.

## 11. Output Requirements

- Result summaries should focus on three things: what changed, how it was validated, and what risks remain.
- Do not write vague summaries. Do not describe unexecuted checks as completed.
- If there is compatibility impact, config migration risk, or user-data risk, call it out explicitly.
