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

## Canonical Package Ownership

The historical `app/core`, `app/helper`, and `app/utils` directories are compatibility-only virtual import roots. Never add physical Python source there and never use those imports from host code. Choose an owner by responsibility, not by whether a function is "shared" or has historically been called a helper.

| Package | Owns | Must Not Own | Representative Files |
|---|---|---|---|
| `app/foundation/` | Generic mechanisms that work without MoviePilot business state: HTTP transport, reflection/module loading, crypto, URL/object primitives, and structures | `settings`, DB/SystemConfig, MoviePilot business rules, concrete products, legacy import paths | `http.py`, `module.py`, `url.py`, `structures.py` |
| `app/domain/` | Pure MoviePilot business semantics and models for media, recognition, sites, and torrents | Persistence, global settings reads, network/filesystem clients, Rust imports, service discovery, process lifecycle | `context.py`, `media.py`, `metainfo.py`, `scraper.py`, `meta/` |
| `app/platform/` | Process-wide runtime contracts and policy: configuration, events, complete logging runtime, cache contracts/in-memory behavior, concurrency, scheduling, rate limiting, localization, GC, restart state | Named third-party integrations, DB-backed business services, Redis/file cache adapter implementations | `config.py`, `events.py`, `log.py`, `cache.py`, `thread.py`, `gc.py`, `runtime.py` |
| `app/infrastructure/` | Configured technical adapters to Redis, files and standard streams, OS/process facilities, browser/display, DNS, RSS transport, package/resource installation, Rust, generated site resources | Media/site/torrent business decisions, plugin lifecycle, named ecosystem workflows, process restart policy after an adapter action | `cache.py`, `stdio.py`, `redis.py`, `resource.py`, `rss.py`, `rust.py`, `system.py` |
| `app/integrations/` | Concrete external products and ecosystems, including plugin markets, MoviePilot remote service, CookieCloud, OCR, and IP-location providers | Generic HTTP/DNS/filesystem primitives or reusable domain semantics | `market.py`, `server.py`, `cookiecloud.py`, `ocr.py`, `location.py` |
| `app/extensions/` | Discovery, loading, registration, and lifecycle of runtime modules, plugins, and configured service implementations | Generic module import mechanics, plugin-facing compatibility APIs, unrelated business workflows | `module_manager.py`, `plugin_manager.py`, `service_registry.py` |
| `app/messaging/` | Message rendering/routing, interactions, and Agent-to-message bridging | Authentication policy, generic HTTP clients, configured service discovery, endpoint-only Web Push behavior | `message.py`, `interaction.py`, `agent.py` |
| `app/security/` | Authentication, authorization, cookies, passkeys, OTP/two-factor, path/URL safety, SSRF and signing policy | Generic URL parsing, process runtime policy, ordinary business validation | `access.py`, `auth.py`, `cookie.py`, `passkey.py`, `otp.py`, `twofactor.py`, `url.py` |
| `app/services/` | Focused application services and service-bound rules, including persisted configuration, configured capability discovery, notification selection, and media-server normalization/matching | Multi-domain use-case orchestration, generic primitives, concrete product protocols | `recognition.py`, `filter.py`, `notification.py`, `mediaserver.py`, `history.py`, `image.py`, `torrent.py` |
| `app/chain/` | Reusable use-case orchestration across modules, services, Oper classes, events, and caches | Transport schemas, backend-specific protocol details, generic primitives | `media.py`, `download.py`, `subscribe.py`, `transfer.py` |
| `app/startup/` | Composition root: inject providers/adapters, order initialization and shutdown, decide restart/lifecycle policy | Reusable business rules or adapter implementation details | `lifecycle.py`, `domain_initializer.py`, `cache_initializer.py`, `modules_initializer.py` |
| `app/sdk/` | Deliberately curated stable imports for new plugins | Canonical implementation logic or host-internal dependencies | `cache.py`, `logging.py`, `media.py`, `network.py`, `services.py` |
| `app/compat/` | Standard-library-only exact legacy import routing and DEBUG diagnostics | Business implementation, wildcard alias guessing, eager canonical imports | `manifest.py`, `imports.py`, `diagnostics.py` |

### Placement Decision Order

Use these questions in order before creating or moving a module:

1. Is it generic and independent of MoviePilot state? Put it in `foundation`.
2. Is it a pure core MoviePilot rule/model that is independent of a configured service boundary? Put it in `domain`.
3. Is it process-wide runtime policy or a contract used by adapters? Put it in `platform`.
4. Does it perform configured technical I/O against Redis, files, OS, browser, RSS, or Rust? Put it in `infrastructure`.
5. Does it implement a named external product/ecosystem workflow? Put it in `integrations`.
6. Does it discover or manage modules/plugins/service implementations? Put it in `extensions`.
7. Does it own authentication, authorization, signing, SSRF, URL/path safety, OTP, passkeys, or two-factor behavior? Put it in `security`.
8. Does it read persisted user configuration, coordinate one bounded capability, or normalize/match one service family? Put it in `services`.
9. Does it coordinate several modules/services/Oper classes for one use case? Put it in `chain`.
10. Is it public to plugins or only preserving an old path? Curate it in `sdk` or map it in `compat`; do not move implementation there.

### Enforced Split Examples

These decisions are architectural constraints, not naming suggestions:

* Cache contracts, memory backends, decorators, and proxies stay in `app/platform/cache.py`; Redis and filesystem implementations stay in `app/infrastructure/cache.py`. Startup registers concrete factories before decorated business modules are imported. Legacy `app.core.cache` resolves to the complete `app.sdk.cache` facade.
* The complete logging runtime stays in `app/platform/log.py`: policy, console/plugin routing, async rotating file output, and shutdown. `app.platform.config` supplies the resolved settings and log path. `platform/log.py` remains a dependency leaf with no `app.*` imports. Plugins use `app.sdk.logging`; legacy `app.log` resolves to that SDK facade.
* Recognition parsing stays pure in `app/domain/meta/` and `app/domain/metainfo.py`. `app/services/recognition.py` reads `SystemConfigOper`; `app/startup/domain_initializer.py` injects rules, extension policy, source defaults, TMDB image construction, and the optional Rust accelerator.
* Kodi-style NFO reading and metadata document generation are one domain capability and stay together in `app/domain/scraper.py`; a separate `domain/nfo.py` must not be recreated.
* `app/services/mediaserver.py` is the single media-server service capability module. It owns configured service discovery together with Provider ID normalization and music-library matching, while reusing generic identity rules from `app/domain/media.py`.
* Configured notification-service discovery belongs in `app/services/notification.py`. Web Push subscription and manual-send HTTP behavior stays in `app/api/endpoints/message.py`; it is not a reusable messaging capability module.
* `app/infrastructure/resource.py` detects/downloads/installs resources and returns whether installation occurred. Only `app/startup/modules_initializer.py` may decide to restart the process afterward.
* Process memory/GC policy belongs in `app/platform/gc.py`; external IP-location APIs belong in `app/integrations/location.py`.
* Security implementation filenames use package-context nouns: `app/security/url.py` and `app/security/twofactor.py`. Historical `app.utils.security` and `app.helper.twofa` remain compatibility mappings only.

Foundation modules do not emit runtime logs. They return documented fallback values or raise according to their public contract; application callers decide whether a failure is operationally relevant and log it from the owning upper layer.

Any ownership move must update canonical host imports, `app/compat/manifest.py`, curated SDK exports when applicable, `docs/rules/05-architecture.md`, and `tests/test_architecture_dependencies.py`. Run that architecture test before broader tests; it rejects physical legacy sources, forbidden upward dependencies, retired canonical filenames, and import cycles.

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
| Canonical module ownership or import path | `docs/rules/05-architecture.md`, `app/compat/manifest.py`, SDK exports when public, architecture/compatibility tests |

---

## Primary Entry Point

For the full documentation map and cross-references, refer to:

**[Documentation Hub Index](./docs/rules/README.md)**

*Last Updated: 2026-08-14*
