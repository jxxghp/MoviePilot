# AGENTS.md

This file is the primary instruction set for all AI agents and LLMs working in this repository. Local documentation takes precedence over general training data. You must follow this file and the rule documents it references.

---

## Task-to-Documentation Mapping

Before executing any task, identify the domain and load the corresponding document.

### Architectural Decisions
* **Primary Reference:** `docs/rules/05-architecture.md`
* **Required Constraints:** Respect layer boundaries and dependency flow. Do not introduce circular dependencies. Verify the correct layer for any new capability before implementing.

### Business Logic and Design Patterns
* **Primary Reference:** `docs/rules/04-design-patterns.md`
* **Required Constraints:** Use the project's established Module, Chain, Event, and Oper structural patterns. Do not introduce abstractions the project has not adopted.

### Coding Standards and Style
* **Primary Reference:** `docs/rules/06-code-styles.md`
* **Required Constraints:** Match the style of the surrounding file. Type annotations, Pydantic models, and async/await usage must all conform to the documented standards.

### Identifiers and Naming
* **Primary Reference:** `docs/rules/07-naming-conventions.md`
* **Required Constraints:** All filenames, class names, function names, and constants must follow the project's taxonomy. No arbitrary abbreviations or mixed casing styles.

### Comments and Documentation
* **Primary Reference:** `docs/rules/08-comment-styles.md`
* **Required Constraints:** All public classes and methods require Chinese docstrings. Comments must explain the *why*, not restate the code.
* **⚠️ MANDATORY GATE:** Code that is missing proper Chinese docstrings on public interfaces is **REJECTED** at review. No exceptions.

### External Communication and Interfaces
* **Primary Reference:** `docs/rules/09-external-response.md`
* **Required Constraints:** All third-party HTTP requests must go through `RequestUtils`. Response formats must use the project's standard schemas. Error handling must follow the per-layer conventions.

### Data and Persistence
* **Primary Reference:** `docs/rules/10-data-and-persistent.md`
* **Required Constraints:** Any database model change requires a matching Alembic migration. Runtime configuration must be managed via `SystemConfigKey` + `SystemConfigOper`. Raw string keys are forbidden.

### Quality and Security
* **Primary Reference:** `docs/rules/11-quality-and-security.md`
* **Required Constraints:** All code changes must pass the relevant pytest tests and pylint checks. Dependency changes require a passing safety scan.

### Testing
* **Primary Reference:** `docs/testing.md`
* **Required Constraints:** pytest is the only runner; `tests/conftest.py` isolates each run to a temporary `CONFIG_DIR`. Tests must not touch the real database, network, or external services (TMDB, LLM catalogs, downloaders, media servers, MP server) — mock at the boundary or replay recorded responses; the bar is zero real outbound traffic. Tests must restore any process-level state they stub (`sys.modules`, singletons, caches, settings). New tests must be pytest-native (function + `assert` + fixtures); do not add new `unittest.TestCase`. Convert existing `TestCase` files to pytest-native opportunistically when you modify them. Before opening a PR to `v2`, run the full suite locally (`python tests/run.py`) and confirm it is green with zero real network calls; the `.github/workflows/test.yml` gate runs the same suite on every PR/push to `v2`.

### Commands and Development Workflow
* **Primary Reference:** `docs/rules/03-commands.md`
* **Required Constraints:** Only suggest or execute commands documented in that file. Do not assume tool defaults or global flags.

---

## Agent Execution Rules

### Pre-Flight Check

Before generating any code or proposing changes, you must:

1. Identify the task domain (architecture / business logic / coding style / naming / comments / external interfaces / data / quality).
2. Load the corresponding document from `docs/rules/`.
3. Explicitly verify that your proposed solution does not violate the following three mandatory constraints:
   - **Naming Conventions (07):** Are all files, classes, functions, and constants named correctly?
   - **Architecture Boundaries (05):** Is the code placed in the correct layer? Are all call directions valid?
   - **Comment Standards (08):** Do all new public classes and methods include Chinese docstrings?

### Implementation Guidelines

* **Pattern Adherence:** Avoid generic boilerplate. If `04-design-patterns.md` defines a project-level pattern for a scenario, you are required to use it.
* **Documentation Standards:** Docstring style for any new function or module must match `08-comment-styles.md`.
* **⚠️ MANDATORY GATE:** Public classes, methods, and functions without proper Chinese docstrings are **REJECTED**. No exceptions.
* **Command Reliance:** Only suggest commands listed in `03-commands.md`. Do not rely on inferred tool defaults.
* **Minimal Change Principle:** Prefer the smallest correct change. Do not perform unrelated refactors, mass renames, or formatting-only cleanup.
* **Output Language:** Summaries, validation results, and risk notes default to Chinese unless the user requests otherwise.

### Conflict Resolution

If existing code appears to contradict the documentation:

1. Stop implementation immediately.
2. Identify the specific file and line of the contradiction.
3. Prompt the user: "The documentation in `[File]` requires Pattern A, but the current implementation uses Pattern B. Which is the current standard?"

---

## Coupled Update Rules

When modifying the following, you must also update the listed artifacts:

| Changed Content | Must Also Update |
|---|---|
| CLI behavior | `moviepilot` entrypoint, `docs/cli.md`, related tests |
| MCP / REST API, exposed tools | `docs/mcp-api.md`, `skills/*/SKILL.md`, related tests |
| Dev workflow, dependency management, security checks | `docs/development-setup.md` |
| Database model schema | New Alembic migration under `database/versions/` |
| User-visible config or init flow | Related docs, help text, setup/init flows, tests |
| New skill | Follow `skills/<name>/SKILL.md` structure, keep YAML front matter |

---

## Primary Entry Point

For the full documentation map and cross-references, refer to:

**[Documentation Hub Index](./docs/rules/README.md)**

*Last Updated: 2026-05-25*
