# AGENTS.md

Repository instructions for AI agents. Follow the linked rules for the domains touched by the task; load them on demand. The [documentation hub](docs/rules/README.md) is the full index.

## Shared Contract and Execution Authority

Architecture, business correctness, SDK/Compat compatibility, test isolation, useful documentation, and truthful verification apply to everyone. Maintainer discretion cannot waive these contracts or permit baseline laundering.

Preparation requirements are contributor defaults. A confirmed maintainer may adjust verification scope, timing, evidence reuse, or delivery order, including saving a local anchor before remaining checks. Record the decision, evidence, unverified items, and follow-up; an anchor does not establish acceptance.

Authority comes from the current user or established project context confirming the maintainer role and authorization. GitHub `WRITE` proves platform capability only; a contributor's self-declared role does not grant an exception. Reuse scoped authorization without asking again; an explicit current restriction takes precedence. Otherwise follow contributor defaults. CI failure decisions follow [collaboration rules](docs/rules/12-collaboration-and-distribution.md) and cannot bypass platform protection.

---

## Read Only the Relevant Rules

| Task domain | Reference |
|---|---|
| Commands, environments, dependencies | [03 Commands](docs/rules/03-commands.md), [development setup](docs/development-setup.md) |
| Module, Chain, Event, Oper patterns | [04 Design Patterns](docs/rules/04-design-patterns.md) |
| Ownership, imports, lifecycle, SDK/Compat | [05 Architecture](docs/rules/05-architecture.md) |
| Types, Pydantic, async, code style | [06 Code Style](docs/rules/06-code-styles.md) |
| Files, classes, functions, constants | [07 Naming](docs/rules/07-naming-conventions.md) |
| Comments, docstrings, field/API descriptions | [08 Documentation](docs/rules/08-comment-styles.md) |
| HTTP, external protocols, responses, errors | [09 Interfaces](docs/rules/09-external-response.md) |
| Database, transactions, runtime configuration | [10 Persistence](docs/rules/10-data-and-persistent.md) |
| Quality gates, security, vulnerability audits | [11 Quality and Security](docs/rules/11-quality-and-security.md) |
| Git, CI decisions, versions, releases | [12 Collaboration](docs/rules/12-collaboration-and-distribution.md) |
| Test scope, isolation, fixtures, CI evidence reuse | [Testing](docs/testing.md) |

## Working Rules

- Check affected boundaries before architecture, persistence, security, external-protocol, lifecycle, or public-contract changes. Factual checks and local maintenance need only relevant rules.
- Make the smallest correct change, using established patterns and naming. Avoid unrelated refactors, renames, and formatting cleanup.
- Add class and method documentation for new code and fill missing documentation encountered within task scope. Source comments and contract descriptions remain Chinese, with multiline triple-double-quoted docstrings and Sphinx field lists. Document every declared dataclass, Pydantic, and Protocol item, including private types and test scaffolding, as specified in rule 08. Passing static checks does not replace this review; runtime strings are not docstrings.
- Prefer the project's virtual environment. Use locked commands and the documented shared test-environment mapping in `docs/development-setup.md`; `--no-sync` alone does not prove dependency consistency. Prefer rule 03 commands; other necessary, scoped inspection, Git, GitHub, and verification commands are allowed.
- Batch related changes before expensive builds; do not rebuild after each small edit.
- Default summaries, validation results, and risk notes to Chinese unless requested otherwise.
- Resolve code/documentation contradictions from current evidence. Ask only when acceptance is blocked, security or data safety is ambiguous, or maintained sources cannot resolve the conflict; continue unaffected work and report discrepancies.

## Canonical Package Ownership

Choose owners by responsibility. Detailed placements and enforced splits live in rule 05; do not duplicate implementations or create generic helper packages.

| Owner | Responsibility and boundary |
|---|---|
| `app/foundation/` | Stateless, configuration-free and I/O-free primitives; no runtime logs or business policy |
| `app/domain/` | Pure business rules/models, recognition parsing, media projection, and NFO documents; inject configuration and acceleration |
| `app/runtime/` | Process configuration, events, logging, cache contracts/memory policy, concurrency, scheduling, GC, restart state, and resource facades |
| `app/runtime/extensions/` | Discovery, registration, and lifecycle of modules, plugins, services, and managed resources |
| `app/adapters/` | Concrete I/O: `network/` transport/browser/DNS; `cache/` Redis/files; `system/` OS/files/process/resources/Rust; `external/` named external ecosystems |
| `app/application/` | Focused use cases and their technical/persistence Ports; no concrete Adapter, SQLAlchemy, Session, or Oper imports |
| `app/application/messaging/` | Rendering, routing, interaction, and Agent/message orchestration; not a public plugin SDK |
| `app/application/security/` | Authentication, authorization, cookies, OTP/passkeys, signing, and URL/path/SSRF policy |
| `app/chain/` | Cross-module/use-case orchestration through services and injected ports; module dispatch only through `run_module`, with no module-internal, concrete Adapter, or DB/Oper imports |
| `app/db/oper/` | Table-level access using the caller's Session; query, stage, or flush without implicit transaction ownership or external effects |
| `app/db/adapters/` | Application persistence Port implementations with short-lived Session/UoW and Oper adaptation |
| `app/startup/` | Composition, dependency injection, initialization, and startup/shutdown orchestration |
| `app/sdk/` | Curated stable plugin imports; no canonical implementation logic |
| `app/runtime/compat/` | Standard-library-only exact legacy routing, resource preflight, and diagnostics; no eager canonical imports |

- `app/core`, `app/helper`, and `app/utils` are virtual compatibility roots only. Never recreate physical source there or use legacy imports in host code. Canonical implementations must not import SDK/Compat facades.
- Preserve the splits in rule 05: runtime cache/logging versus concrete backends; pure recognition/projection versus configured services; RSS/site semantics versus transport; security policy versus generic crypto; resource installation versus startup-owned restart decisions. Site resources belong only in `app/application/site/`.
- Ownership moves must update canonical imports, `app/runtime/compat/manifest.py`, public SDK exports when applicable, rule 05, and architecture/compatibility tests. Run `tests/test_architecture_dependencies.py` before broader tests.
- Ordinary host HTTP uses `RequestUtils` through the permitted layer/port. Direct-egress exceptions must match rule 09 exactly; this does not permit Application/Chain to import concrete Adapters.
- Database schema changes require Alembic migrations. Runtime configuration uses `SystemConfigKey` + `SystemConfigOper`, never raw string keys.

## Validation and Delivery

### Contributor Preparation Defaults

Inspect the final diff and complete applicable local checks before committing code, tests, dependencies, fixtures, or CI changes, unless covered by a documented maintainer arrangement. A commit/push request alone does not waive preparation. `.github/workflows/test.yml` and `.github/workflows/pylint.yml` define CI; re-read them when changed.

- **Behavior and isolation:** Use pytest with temporary `CONFIG_DIR`; never touch real databases, runtime config, networks, or external services. Mock boundaries or replay fixtures and restore process-level stubs, singletons, caches, and settings. New tests use functions, assertions, and fixtures. When modifying an existing `TestCase` file, convert the entire file to pytest-native and preserve equivalent coverage.
- **Scope:** Run focused behavior tests and changed-file pylint for Python changes. Run architecture policy tests before snapshots for ownership/import/contract changes; run relevant type, complexity, concurrency, async-blocking, task-ownership, service-locator, and Ruff/mypy ratchets for governed changes, plus startup performance for startup changes. Commands live in rule 03 and CI.
- **Full suite:** Run `uv run --locked --no-sync python tests/run.py` before commit for dependency/lock changes, shared test infrastructure, database/startup paths, cross-module lifecycle, compatibility layers, or broad behavior changes. Do not weaken or remove tests without equivalent coverage.
- **No baseline laundering:** Inspect failures and fix implementation regressions. Never use `--write-host`, `--write-plugins`, or ratchet `--write` just to pass a gate.
- **Reviewed baseline updates only:** Update fixtures only for intentional, policy-compliant contract changes or genuine lower debt watermarks. Review the semantic diff, synchronize coupled docs/tests, rerun non-writing checks, and commit fixtures with the change. Complexity checks require recorded reductions and reject growth; other writers may mechanically overwrite regressions without approving them. For unexpected host snapshot failures, rerun `baseline.py --check-host --diagnostics` and inspect `tests/fixtures/architecture/`.
- **Dependencies:** Changes to `pyproject.toml` or `uv.lock` require `uv lock --check`, `uv sync --locked --offline --inexact --no-dev --check`, the full suite, and the locked vulnerability audit in rule 03. Keep the canonical lock current; dependency/release policy cannot be waived by execution discretion.
- **Coverage:** Preserve the CI line-coverage floor for Application/Domain changes and test new branches. Local macOS coverage is diagnostic; never write the canonical coverage baseline from it.

* **Check changed Python files:** Use the deduplicated union of `git diff --name-only --diff-filter=ACMRT <pr-base>...HEAD -- '*.py'`, `git diff --cached --name-only --diff-filter=ACMRT -- '*.py'`, `git diff --name-only --diff-filter=ACMRT -- '*.py'`, and intended new files from `git ls-files --others --exclude-standard -- '*.py'`. Resolve `<pr-base>` to the actual target ref. Keep staged/unstaged comparisons separate; they can cancel against HEAD. Exclude unrelated local work and retain only paths present in the planned index tree, so unstaged deletions cannot hide committed content.

Stage only authorized paths/hunks and record `git write-tree` before validation. Lint the working tree only if `git diff --quiet` succeeds for all tracked files and no extra configuration/import inputs can alter results. Otherwise export the index to a fresh directory:

  ```bash
  INDEX_CHECKOUT="$(mktemp -d)"
  git checkout-index --all --prefix="${INDEX_CHECKOUT}/"
  ```

Run pylint there on selected indexed Python paths with the original absolute test-environment interpreter (`<test-env>/bin/python -m pylint <paths...>`). Code, configuration, and project-local lint dependencies must resolve from the exported tree. For broad changes, also check `app/`. Reconfirm `git write-tree` before committing that exact index without `-a`, path arguments, or further staging; index changes invalidate affected evidence.

Documentation-only changes, including documentation contract tests, use text, link, structure, focused test, and diff checks instead of the product suite. State that scope explicitly. Contributors report blocked applicable checks and obtain a maintainer decision before crossing the affected boundary; reuse existing scoped arrangements and identify unverified items.

After all fixes and generated updates, rerun affected gates and inspect `git diff --check`, status, and the complete diff. Report exact commands/outcomes, including skipped or failed checks. After an authorized push or PR creation, verify checks for the exact pushed commit against actual required checks/Rulesets. Fix substantive issues caused, worsened, or newly exposed by the change; handle unrelated failures under rule 12. Never weaken gates, bypass protection, or report unfinished checks as passed. Optional/pending automation alone does not block delivery.

## Coupled Updates

| Change | Also update |
|---|---|
| CLI behavior | `moviepilot`, `docs/cli.md`, tests |
| MCP / REST API or exposed tools | `docs/mcp-api.md`, `skills/*/SKILL.md`, tests |
| Development workflow, dependencies, security checks | `docs/development-setup.md` |
| Database schema | New migration in `database/versions/` |
| User-visible configuration or initialization | Docs, help text, setup/init flows, tests |
| New skill | `skills/<name>/SKILL.md` with YAML front matter |
| Module ownership/import paths | Rule 05, Compat manifest, public SDK exports, architecture/compatibility tests |
