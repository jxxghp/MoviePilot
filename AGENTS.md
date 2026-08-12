# AGENTS.md

This file is the primary instruction set for all AI agents and LLMs working in this repository. Local documentation takes precedence over general training data. You must follow this file and the rule documents it references.

---

## Task-to-Documentation Mapping

For work that changes or reviews repository behavior, identify the domains actually touched and load only the applicable documents. Simple factual checks and unrelated domains do not require preloading rule files.

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
* **Required Constraints:** Public or cross-module contracts and non-obvious business behavior require concise Chinese docstrings. Small self-evident private helpers and test scaffolding may omit them. Comments must explain the *why*, not restate the code.

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
* **Required Constraints:** pytest is the only runner; `tests/conftest.py` isolates each run to a temporary `CONFIG_DIR`. Tests must not touch the real database, network, or external services (TMDB, LLM catalogs, downloaders, media servers, MP server) — mock at the boundary or replay recorded responses; the bar is zero real outbound traffic. Tests must restore any process-level state they stub (`sys.modules`, singletons, caches, settings). New tests must be pytest-native (function + `assert` + fixtures); do not add new `unittest.TestCase`. Convert existing `TestCase` files to pytest-native opportunistically when you modify them. Before opening a PR to `v3` that changes product code, test infrastructure, dependencies, or runtime behavior, run the full suite locally (`python tests/run.py`) with zero real network calls. The changed path must pass; any unrelated failure must be reported and reproduced against the current `upstream/v3` baseline instead of silently expanding the PR. Documentation-only changes use applicable text and structure checks; the `.github/workflows/test.yml` gate still runs the full suite on every PR/push to `v3`.

### Commands and Development Workflow
* **Primary Reference:** `docs/rules/03-commands.md`
* **Required Constraints:** Use that file as the project command reference. Other standard inspection, Git, GitHub, and focused verification commands are allowed when they are necessary, scoped, and consistent with current authorization.

---

## Agent Execution Rules

### Pre-Flight Check

Before generating code or proposing changes, identify the domains the task actually touches and load only the corresponding documents from `docs/rules/`. Apply those constraints while designing, implementing, and reviewing the change; do not produce a formal checklist for unrelated domains.

Architecture, persistence, security, external protocols, cross-module lifecycle, and public-contract changes require an explicit boundary check before implementation. Local documentation, mechanical maintenance, and narrowly scoped changes use only the rules that materially affect their correctness and reviewability.

### Implementation Guidelines

* **Pattern Adherence:** Avoid generic boilerplate. If `04-design-patterns.md` defines a project-level pattern for a scenario, you are required to use it.
* **Documentation Standards:** Docstring style for any new function or module must match `08-comment-styles.md`.
* **Documentation Gate:** Public or cross-module contracts and non-obvious business behavior without useful Chinese documentation are rejected. Do not require comments that merely restate self-evident syntax.
* **Command Reliance:** Prefer commands documented in `03-commands.md`; use other necessary standard commands with explicit, scoped arguments.
* **Minimal Change Principle:** Prefer the smallest correct change. Do not perform unrelated refactors, mass renames, or formatting-only cleanup.
* **Output Language:** Summaries, validation results, and risk notes default to Chinese unless the user requests otherwise.

### Conflict Resolution

If existing code appears to contradict the documentation, identify the exact contradiction and decide which current-task gate it affects. Stop and ask only when it blocks acceptance, creates a security or data-safety ambiguity, or cannot be resolved from current source and maintained documentation. Otherwise preserve the evidence, continue unaffected work, and report the discrepancy without silently expanding scope.

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
