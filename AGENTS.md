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
* **Required Constraints:** All code changes must pass the relevant pytest tests and pylint checks. Dependency changes require a current `uv.lock`, locked environment verification, and a passing manual Safety scan.

### Testing
* **Primary Reference:** `docs/testing.md`
* **Required Constraints:** pytest is the only runner; `tests/conftest.py` isolates each run to a temporary `CONFIG_DIR`. Tests must not touch the real database, network, or external services (TMDB, LLM catalogs, downloaders, media servers, MP server) — mock at the boundary or replay recorded responses; the bar is zero real outbound traffic. Tests must restore any process-level state they stub (`sys.modules`, singletons, caches, settings). New tests must be pytest-native (function + `assert` + fixtures); do not add new `unittest.TestCase`. Convert existing `TestCase` files to pytest-native opportunistically when you modify them. Before opening a PR to `v3` that changes product code, test infrastructure, dependencies, or runtime behavior, run the full suite locally (`uv run --locked --no-sync python tests/run.py`) with zero real network calls. The changed path must pass; any unrelated failure must be reported and reproduced against the current `upstream/v3` baseline instead of silently expanding the PR. Documentation-only changes use applicable text and structure checks; the `.github/workflows/test.yml` gate still runs the full suite on every PR/push to `v3`.

### Commands and Development Workflow
* **Primary Reference:** `docs/rules/03-commands.md`
* **Required Constraints:** Use that file as the project command reference. Other standard inspection, Git, GitHub, and focused verification commands are allowed when they are necessary, scoped, and consistent with current authorization.

---

## Canonical Package Ownership

The historical `app/core`, `app/helper`, and `app/utils` directories are compatibility-only virtual import roots. Never add physical Python source there and never use those imports from host code. Choose an owner by responsibility, not by whether a function is "shared" or has historically been called a helper.

The legacy roots have no physical directories in the source tree. Current images and update flows write site resources only to `app/application/site/`; plugin imports under `app.helper.*` are resolved exclusively by the exact runtime compatibility manifest.

| Package | Owns | Must Not Own | Representative Files |
|---|---|---|---|
| `app/foundation/` | 无状态、无配置和无 I/O 的底层机制：反射/动态导入、加密、DOM、身份、集合、单例、文本、URL 和版本比较 | `settings`、DB/SystemConfig、网络请求、运行日志、MoviePilot 业务规则、旧导入路径 | `reflection.py`, `crypto.py`, `collections.py`, `text.py`, `url.py` |
| `app/domain/` | Pure MoviePilot business semantics and models for media, recognition, sites, and torrents | Persistence, global settings reads, network/filesystem clients, Rust imports, service discovery, process lifecycle | `context.py`, `media.py`, `metainfo.py`, `scraper.py`, `meta/` |
| `app/runtime/` | 进程级运行机制和策略：配置、事件、完整日志、缓存契约/内存行为、托管资源门面、并发、调度、限流、本地化、GC 和重启状态 | 具体外部产品、业务流程、Redis/文件缓存实现 | `config.py`, `events.py`, `log.py`, `cache.py`, `managed_resources.py`, `thread.py`, `state.py` |
| `app/runtime/extensions/` | 模块、插件、配置化服务和托管资源实现的发现、注册与生命周期适配 | 通用反射机制、插件公开 API、无关业务流程 | `module_manager.py`, `plugin_manager.py`, `managed_resource_adapter.py`, `service_registry.py` |
| `app/adapters/network/` | HTTP、浏览器、DNS、Cloudflare 和 IP 等通用网络技术适配 | RSS/站点业务编排、身份认证策略、命名外部产品流程 | `http.py`, `browser.py`, `doh.py`, `ip.py` |
| `app/adapters/cache/` | Redis 与文件缓存等具体持久化实现 | 缓存协议、装饰器和进程内缓存策略 | `backends.py`, `redis.py` |
| `app/adapters/system/` | 操作系统、文件、进程、标准流、包/资源安装、显示和 Rust 加速适配 | 业务规则、进程重启决策 | `host.py`, `display/`, `stdio.py`, `package.py`, `resource.py`, `rust.py`, `fsproxy.py` |
| `app/adapters/external/` | CookieCloud、插件市场、OCR、IP 归属和 MoviePilot Server 等命名外部生态 | 通用 HTTP/DNS/文件机制或可复用领域语义 | `market.py`, `server.py`, `cookiecloud.py`, `ocr.py`, `location.py`, `wechat_crypt.py` |
| `app/application/` | 读取配置/持久化状态的聚焦应用服务和服务族规则 | 多领域 Chain 编排、底层通用机制、通用传输协议 | `recognition.py`, `filter.py`, `filter_rules.py`, `notification.py`, `mediaserver.py`, `rss.py`, `site/sites.*` |
| `app/application/messaging/` | 消息渲染/路由、交互和 Agent 到消息桥接：`interaction.py` 通用交互契约和视图工具；`router.py` 统一交互优先级和回调分发；`site.py`/`subscribe.py`/`skill.py` 对应命令的会话、输入解析和视图；`media.py` 媒体交互状态（业务工作流仍由 `MediaInteractionChain` 执行）；`plugin.py` 插件输入接管和插件按钮回调；`agent.py` Agent 选择状态、回调协议和 WebAgent 消息桥接；`message.py` 通知渲染、模板和队列。不作为推荐给插件直接使用的公开 SDK | 认证策略、通用 HTTP、服务发现、仅端点使用的 Web Push 行为 | `message.py`, `interaction.py`, `router.py`, `agent.py` |
| `app/application/security/` | 认证、授权、Cookie、Passkey、OTP/二次认证、路径/URL 安全、SSRF 和签名策略 | 通用 URL 解析、进程运行策略、普通业务校验 | `access.py`, `auth.py`, `cookie.py`, `passkey.py`, `otp.py`, `twofactor.py`, `url.py` |
| `app/chain/` | Reusable use-case orchestration across modules, services, Oper classes, events, and caches; chains reach modules only through `run_module` dispatch on method-name contracts | Transport schemas, backend-specific protocol details, generic primitives, direct imports of module internals (classes, exceptions, constants) | `media.py`, `download.py`, `subscribe.py`, `transfer.py` |
| `app/startup/` | Composition root: inject providers/adapters, order initialization and shutdown, decide restart/lifecycle policy | Reusable business rules or adapter implementation details | `lifecycle.py`, `domain_initializer.py`, `cache_initializer.py`, `managed_resources_initializer.py`, `modules_initializer.py` |
| `app/sdk/` | Deliberately curated stable imports for new plugins | Canonical implementation logic or host-internal dependencies | `browser.py`, `cache.py`, `logging.py`, `media.py`, `network.py`, `services.py` |
| `app/runtime/compat/` | 仅依赖标准库的精确旧导入路由、资源前置扫描和 DEBUG 诊断 | 业务实现、通配猜测、目标模块的提前导入 | `manifest.py`, `imports.py`, `resource_imports.py`, `diagnostics.py` |

容易误分的三个边界必须按实际职责判断：`application/rss.py` 同时承担 Feed/种子语义、站点规则和浏览器回退，不是单纯 HTTP 传输；`application/site/sites.*` 及 `user.sites.v3.bin` 共同构成站点目录、认证和索引应用能力，只有下载安装机制留在 `adapters/system/resource.py`；`foundation/crypto.py` 只提供无状态 RSA/摘要/AES 算法，认证、签名、令牌和二次验证策略仍属于 `application/security/`。

### Placement Decision Order

Use these questions in order before creating or moving a module:

1. Is it generic, free of MoviePilot state and I/O? Put it in `foundation`.
2. Is it a pure core MoviePilot rule/model that is independent of a configured service boundary? Put it in `domain`.
3. Is it process-wide runtime policy or a contract used by adapters? Put it in `runtime`.
4. Does it discover or manage modules/plugins/service implementations? Put it in `runtime/extensions`.
5. Does it perform configured network, cache, OS/process, file, package/resource, stdio, or Rust I/O? Put it under the matching `adapters` technical boundary.
6. Does it implement a named external product/ecosystem workflow? Put it in `adapters/external`.
7. Does it own authentication, authorization, signing, SSRF, URL/path safety, OTP, passkeys, or two-factor behavior? Put it in `application/security`.
8. Does it read persisted user configuration, coordinate one bounded capability, or normalize/match one service family? Put it in `application`.
9. Does it coordinate several modules/services/Oper classes for one use case? Put it in `chain`.
10. Is it public to plugins or only preserving an old path? Curate it in `sdk` or map it in `runtime/compat`; do not move implementation there.

### Enforced Split Examples

These decisions are architectural constraints, not naming suggestions:

* Cache contracts, memory backends, decorators, and proxies stay in `app/runtime/cache.py`; Redis and filesystem implementations stay in `app/adapters/cache/backends.py`. Startup registers concrete factories before decorated business modules are imported. Legacy `app.core.cache` resolves to the complete `app.sdk.cache` facade.
* The complete logging runtime stays in `app/runtime/log.py`: policy, console/plugin routing, async rotating file output, and shutdown. `app.runtime.config` supplies the resolved settings and log path. `runtime/log.py` remains a dependency leaf with no `app.*` imports. Plugins use `app.sdk.logging`; legacy `app.log` resolves to that SDK facade.
* Recognition parsing stays pure in `app/domain/meta/` and `app/domain/metainfo.py`. `app/application/recognition.py` reads `SystemConfigOper`; `app/startup/domain_initializer.py` injects rules, extension policy, source defaults, TMDB image construction, and the optional Rust accelerator.
* Kodi-style NFO reading and metadata document generation are one domain capability and stay together in `app/domain/scraper.py`; a separate `domain/nfo.py` must not be recreated.
* `app/application/mediaserver.py` is the single media-server service capability module. It owns configured service discovery together with Provider ID normalization and music-library matching, while reusing generic identity rules from `app/domain/media.py`.
* Configured notification-service discovery belongs in `app/application/notification.py`. Web Push subscription and manual-send HTTP behavior stays in `app/api/endpoints/message.py`; it is not a reusable messaging capability module.
* `app/adapters/system/resource.py` detects/downloads/installs resources and returns whether installation occurred. Only `app/startup/modules_initializer.py` may decide to restart the process afterward.
* Process memory/GC policy belongs in `app/runtime/gc.py`; external IP-location APIs belong in `app/adapters/external/location.py`.
* Security implementation filenames use package-context nouns: `app/application/security/url.py` and `app/application/security/twofactor.py`. Historical `app.utils.security` and `app.helper.twofa` remain compatibility mappings only.

Foundation modules do not emit runtime logs. They return documented fallback values or raise according to their public contract; application callers decide whether a failure is operationally relevant and log it from the owning upper layer.

Any ownership move must update canonical host imports, `app/runtime/compat/manifest.py`, curated SDK exports when applicable, `docs/rules/05-architecture.md`, and `tests/test_architecture_dependencies.py`. Run that architecture test before broader tests; it rejects physical legacy sources, forbidden upward dependencies, retired canonical filenames, and import cycles.

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
| Canonical module ownership or import path | `docs/rules/05-architecture.md`, `app/runtime/compat/manifest.py`, SDK exports when public, architecture/compatibility tests |

---

## Primary Entry Point

For the full documentation map and cross-references, refer to:

**[Documentation Hub Index](./docs/rules/README.md)**

*Last Updated: 2026-08-19*
