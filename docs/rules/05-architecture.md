# 05 - Architecture and Modules

This document defines the package boundaries, dependency directions, persistence
and extension contracts, and machine-enforced architecture gates for the
MoviePilot V3 backend.

## Core Direction

```text
Entrypoints (app/api, app/command, app/monitor, app/scheduler, app/agent, app/workflow, app/doctor)
    -> Application & Use-Case Orchestration (app/application, app/chain)
        -> Pure Domain Rules (app/domain, depending only on app/foundation and app/schemas)
            -> Stateless Primitives (app/foundation)
```

- `app/foundation` holds stateless, MoviePilot-agnostic Python primitives with
  zero `app.*` imports and no runtime logging.
- `app/domain` holds pure MoviePilot business rules, media metadata parsing,
  immutable provider projections and offline vocabularies with no I/O, runtime
  config, cache, HTTP or database access (importing only `app/foundation` and
  pure value contracts in `app/schemas`).
- `app/application` owns typed use cases, frozen DTOs/snapshots, and persistence
  or technical `Protocol` ports (`app/api/endpoints/plugin.py` and other HTTP
  controllers stay thin transport adapters over application services such as
  `app/application/plugin/routes.py`).
- `app/chain` coordinates cross-module workflows shared across API, scheduler,
  CLI, Agent and events, reaching pluggable modules exclusively via
  `run_module` method-name dispatch.
- `app/adapters` implements concrete cache, HTTP, browser, web routing/middleware,
  observability, OS/process, filesystem, Rust acceleration and third-party
  vendor protocols.
- `app/db` owns SQLAlchemy models, entity `Oper` classes and short-session
  repository/UoW adapters implementing `app/application` persistence ports.
- `app/runtime` owns process-wide settings, event bus, task registry, cache
  contracts, bounded executors, stop state and module/plugin extension runtimes,
  plus the lazy `app/runtime/compat` import overlay.
- `app/startup` is the sole composition root wiring concrete adapters, DB
  repositories, caches and lifecycle components into `HostRuntime`.
- `app/sdk` curates the explicit public ABI exported to plugins (`_PluginBase`,
  typed event snapshots, media/service contracts and narrow facades). Host
  canonical implementations never import `app/sdk` or `app/runtime/compat`.

## Canonical Migrated Packages

| Package | Responsibility and Ownership Boundary |
| --- | --- |
| `app/foundation/` | Generic reflection, collection/text/time utilities and pure helpers independent of MoviePilot state |
| `app/domain/` | Pure business rules — `meta/` and `metainfo.py` (title/release recognition), `projection/` (immutable TMDB/Douban/Bangumi/AniList to `MediaInfo` mappers imported directly by host callers instead of legacy `MediaInfo` setters), media identity source policy (`media.py`), music release/disc/cover rules (`music.py`), torrent identity and episode file selection (`torrent.py`), search collection rules (`search.py`), `classification/`, `plugin.py` and `chat.py` |
| `app/application/` | Typed use-case services, deeply frozen snapshots and port `Protocol` definitions (`subscription/`, `history/`, `transfer/`, `download/`, `search/`, `site/`, `plugin/`, `security/`, `server/`, `chain/`, `outbox.py`, `agent.py`, `workflow.py`, `scheduling.py`, `commands.py`, `system.py`, `network.py`, `notification.py`, `mediaserver.py`, `rss.py`, `rules.py`) |
| `app/chain/` | Shared workflow orchestrators (`subscribe/`, `download/`, `transfer/`, `search/`, `media/` same-named packages plus focused chains and `_recognition.py`, `_messaging.py`, `_interaction.py`, `_music.py` mixins) |
| `app/modules/` | Pluggable downloaders, media servers, metadata/lyrics sources, indexers, notifications and `filemanager` storages (shared boilerplate templates live in `app/modules/_base/`) |
| `app/adapters/` | Technical I/O boundaries — `cache/backends.py` (Redis/file), `network/` (HTTP, browser, WebPush), `web/` (dynamic plugin route registry, correlation, priority, security and monitoring middleware/endpoints), `observability/` (telemetry exporters), `system/` (plugin package/dependency/health managers, resource installer, `rust.py` `moviepilot_rust` bridge, `code/`, display, timeout-guarded `fsproxy.py`) and `external/` (market transport, WeChat crypto and vendor clients) |
| `app/db/` | `models/` (SQLAlchemy entities, `load_all_models()` explicit registration and `_identity.py` mapper normalization), `oper/` (entity data access), and `adapters/` (short-UoW repository, staging, `chain.py` settlement and Outbox implementations) |
| `app/runtime/` | `config.py` / `settings.py`, `log.py` (dependency leaf), `cache.py`, `stop.py` (`StopState`), `tasks.py` (`TaskRegistry`), `execution.py` (`OwnedThreadPoolExecutor`), `correlation.py`, `topology.py`, `event/` bus owners, `extensions/` (`module/`, `plugin/`, `resource.py`) and `compat/` |
| `app/agent/` | Session manager, lifecycle, background tasks, `MoviePilotAgent` orchestrator, `app/agent/graph.py` (`AgentGraphCache` / `GraphResources` owner), shell/terminal owners and `llm/` (`provider.py` facade over `catalog`, `discovery`, `auth`, `session`, `runtime`, plus `helper.py`) |
| `app/startup/` | `composition/` (`HostRuntime` wiring where `startup/composition/server.py` owns MoviePilot Server report/share composition, `startup/composition/chain.py` owns `ChainRuntimeContext`, wallpaper and legacy transfer-command composition, and `app/startup/composition/outbox.py` owns replay handlers) and `initializers/` (ordered normal/safe-mode startup and bounded shutdown hooks) |
| `app/sdk/` | Curated plugin-facing contracts (`plugin/`, `media`, `services`, `scheduler.py`, and private `_legacy/` compatibility shims) |

## Placement Decision Order

1. Generic, stateless, independent of MoviePilot state and free of I/O -> `app/foundation`
2. Pure MoviePilot business rule, metadata parser or provider projection -> `app/domain`
3. Shared data contract or value schema -> `app/schemas`
4. Focused application use case or port definition -> `app/application` (`messaging/` for message rendering, routing and interaction, and `site/` as the sole home for site resources)
5. Multi-module workflow shared across entrypoints -> `app/chain`
6. Process-wide config, events, logging, cache policy, bounded executors, task registry or stop state -> `app/runtime` (`extensions/` for module, plugin and managed-resource runtimes)
7. Concrete cache, network, browser, web transport, observability, OS/process, filesystem or Rust bridge I/O -> matching technical package under `app/adapters` (`external/` for named third-party ecosystems)
8. Public plugin contract or legacy import preservation -> curate in `app/sdk` or map lazily in `app/runtime/compat/manifest.py`

### Naming and Package Root Discipline

- Do not create generic `common`, `helper` or `utils` buckets, and never recreate
  physical files under retired legacy roots (`app/core`, `app/helper`,
  `app/utils`).
- New production Python module filenames use a single lowercase word. When a
  topic needs multiple files, create a topic package with single-word child
  modules (for example `runtime/event/{registry,binding,dispatch,errors}.py` or
  `application/subscription/{contract,delete,identity}.py`). Multiword paths
  require machine-verified discovery identifiers or a recorded path-specific
  reason. Test files keep descriptive `test_<behavior>.py` names.
- Package roots (`__init__.py`), including `app/schemas` and `app/db`, stay
  documentation-only or expose only the verified stable facade lazily (such as
  `TransferChain`, `SubscribeChain`, `DownloadChain`, `SearchChain`,
  `MediaChain`, while full ORM registration runs explicitly via
  `app.db.models.load_all_models()`). Canonical host callers import concrete
  child owners directly rather than re-exporting internal classes or ports from
  package roots.
- Legacy module paths belong in `app/runtime/compat/manifest.py`. New canonical
  modules never re-export old managers, helpers or `Oper` classes just to
  preserve historical imports.

## Existing Chain, Module and DB Layers

### Chain and Scheduler Layer

- Runtime shutdown state is owned by `StopState` in `app/runtime/stop.py`
  (`global_vars` stop properties on `app/runtime/config.py` remain a legacy
  plugin facade only).
- Chains coordinate modules, application services, injected persistence ports,
  events and caches while keeping the chain-to-chain static graph acyclic and
  free of HTTP request objects or vendor SDKs (`qbittorrentapi`,
  `transmission_rpc`, `smbclient`).
- Chains invoke modules exclusively through `run_module` method-name dispatch
  (`app/runtime/extensions/module/dispatcher.py` and `module/contracts.py`), and
  direct imports of `app.modules` internals are forbidden. New chains and tests
  inject `ChainRuntimeContext` (`app/application/chain/context.py`), while
  no-argument `Chain()` remains supported via the startup provider.
- `app/scheduler/` never constructs business `Chain` instances directly —
  `app/startup/initializers/scheduler.py` injects callable-only
  `SchedulerServices` and the `AgentTask` repository before lifecycle start.
- Underscore files in `app/chain/` (`_recognition.py`, `_messaging.py`,
  `_interaction.py`, `_music.py`) are feature mixins declaring minimal host
  contracts via `app/chain/_contracts.py` rather than standalone chains.
- Same-named orchestration packages (`transfer/`, `subscribe/`, `download/`,
  `search/`, `media/`) keep class identity and event wrappers on `facade.py` and
  isolate queue, planning, execution, settlement and cache adapters in
  single-word owner modules, and retired single-file monoliths must not return.

### Module and Agent Layer

- `app/modules/` implementations must not import other modules or chains
  directly — cross-module coordination belongs in a chain, and shared value
  types live in `app/schemas` (such as `MediaServerField` and
  `MediaServerQueryOptions`, see [Media Server Query Contract](../media-server-query.md)).
- `app/modules/_base/` (`downloader.py`, `mediaserver.py`, `notification.py`)
  holds only shared connection, reconnect, auth and command-registration
  templates typed by `TService`, never per-provider business logic.
- Strict file existence checks use `get_file_item_strict(storage, path)`
  returning `FileItemQueryResult(item=...)` (`None` means unsupported provider,
  whereas `item=None` proves absence and storage/query errors raise). Destructive
  cleanup never infers absence from nullable `get_file_item`.
- Interactive management for notification channels, storages and LLM providers
  shares one generic pass-through pattern (`channel_manage`, `storage_manage`,
  `provider_manage` over `NotificationAction` / `StorageAction` /
  `LlmProviderAction` and `ManageRequest` on
  `POST /api/v1/{notification,storage,llm}/manage`, with the named OAuth
  redirect callback `GET /api/v1/llm/provider-auth/callback/{provider_id}` as
  the sole route exception). Chains and endpoints never hard-code
  target-specific parameter schemas.
- `app.agent` and `app.agent.llm` package roots contain no implementation or
  eager exports. Host callers import concrete owners (`orchestrator.py`,
  `graph.py`, `llm/helper.py`, `llm/provider.py`, `tools/base.py`,
  `tools/manager.py`), while historical plugin paths
  (`app.agent.llm.LLMHelper`, `app.helper.llm`) resolve through exact
  `SYMBOL_ALIASES`. Models created by `LLMHelper.get_llm()` own dedicated HTTP
  clients closed via `LLMHelper.close_llm()`.

### DB, Repository, Outbox and Durable Transfer Layer

- SQLAlchemy models live in `app/db/models/` and mirror `app/db/oper/`
  one-for-one, except the verified `oper/site.py` (`Passkey`, `SiteIcon`,
  `SiteStatistic`, `SiteUserData`) and `oper/agenttask.py` (`AgentTaskRun`)
  consolidations. Every schema change requires an Alembic revision under
  `database/versions/`.
- Canonical Application, Chain, Agent, API, Workflow and Startup code accesses
  persistence through Application-owned `Protocol` ports (`site/contract.py`,
  `subscription/contract.py`, `history/`, `security/user.py`, `workflow.py`,
  `download/failures.py`), never importing ORM models or `Oper` classes directly.
  DB adapters project ORM rows into deeply frozen snapshots before closing short
  `AsyncSession`/UoW scopes, and Session-reusing repositories stage within a
  caller-owned UoW without committing it.
- `Oper` classes handle persistence primitives and column-type coercion, while
  translating `MediaInfo` / `MetaBase` into row patches belongs in
  `app/application/`. Mapper-level invariants (`app/db/models/_identity.py`
  normalizing `media_source` / `media_id` via `app/schemas/media.py`) ensure
  `app/db` has zero dependency on `app/domain`.
- **Durable post-commit Outbox** — `app/application/outbox.py` separates
  in-transaction `OutboxStager` from short-transaction `OutboxDispatchStore`
  (implemented in `app/db/adapters/outbox.py`, wired by
  `app/startup/composition/subscription.py` and validated replay handlers in
  `app/startup/composition/outbox.py`). Delivery is at-least-once with fenced
  lease claims, bounded per-topic recovery workers (up to 20 via `ThreadHelper`),
  and per-handler broadcast expansion (`event.handler.<sha256>`). `TaskRegistry`
  (`app/runtime/tasks.py`) is strictly an in-process cancellation/shutdown
  registry and never a substitute for durable persistence.
- **Durable Transfer admission, step execution and atomic settlement** —
  `app/application/transfer/` (`models.py`, `workflow.py`, `execution.py`) and
  `app/db/adapters/transfer/` (`admission.py`, `execution.py`) manage
  `accepted -> provider_pending -> planned` CAS checkpoints and lease-fenced
  worker claims in short UoW scopes without using the Outbox as an execution
  queue. All external filesystem I/O runs outside database transactions after
  checkpoint commit in `app/modules/filemanager/`, any step whose side effect
  cannot be proven enters `manual_review` rather than replaying blindly, and
  terminal history rows, pending-task cleanup and Outbox intents commit
  atomically through `app/db/adapters/chain.py` (never emitting events
  synchronously inside worker callbacks). History rows carrying a
  `transfer_task_id` serve as recovery receipts and are exempt from ordinary
  history retention cleanup, `JobManager` display rows stay separate from live
  lease receipts, and legacy `app.db.transferpending_oper` callers route solely
  through `app/sdk/_legacy/transferpending.py`.

## Composition, Concurrency and Compatibility Boundaries

- Startup registers cache backends before business modules load, and one-shot
  security state (such as `PasskeyChallengeCache`) uses `AtomicCacheBackend`
  `store/consume` without exposing concrete Redis classes to Application code.
- Host code accesses process-wide managers and settings through explicit runtime
  accessors (`get_module_manager()`, `get_plugin_manager()`, `get_scheduler()`,
  `get_configured_system_config()`) rather than class-shaped compatibility
  facades or `SystemConfigOper`.
- Process-owned synchronous worker pools use `OwnedThreadPoolExecutor` in
  `app/runtime/execution.py` for bounded shutdown. Lifecycle `stop()` hooks
  follow a boolean convergence contract — when shutdown times out or returns
  `False`, the component retains its live owner handle and reports
  non-convergence upward instead of blocking unbounded `join()` or clearing
  handles prematurely.
- `app/runtime/compat` stores string mappings and resolves aliases lazily
  without eager canonical imports. Migrated symbols on surviving modules use
  post-loader allowlisted `SYMBOL_ALIASES` that are omitted from physical
  `__all__` (`from ... import *` is unsupported for migrated symbols).
- `app/startup/initializers/plugins.py` backs up stale legacy
  `app/plugins/__init__.py` files left by in-UI upgrades to
  `__init__.py.legacy-bak` and restores the clean namespace entrypoint before
  plugin assembly. `app/plugins/**` holds runtime plugin copies and is excluded
  from host refactors and dependency baselines.
- Host code uses canonical paths only (`app.core`, `app.helper`, `app.utils` and
  `app.log` are virtual paths reserved for `app/plugins/` and compatibility
  tests), and delayed imports must not be used to disguise module dependency
  cycles.

### Architecture, Complexity and Concurrency Ratchet Gates

`tests/fixtures/architecture/dependency-baseline.json` records generated module
edges, SCCs and `direct_egress` fingerprints across `app` (excluding
`app/plugins/**`), whereas human-reviewed policy lives separately in
`tests/fixtures/architecture/dependency-policy.json` (`--write-host` never edits
policy).

- **Cycle policy** — zero canonical host cycles are permitted outside the single
  `contained_vendor` 29-module TMDB vendored SCC, whose member set may not grow.
- **Direct Adapter imports** — runtime edges from `app.application` or
  `app.chain` to `app.adapters.*` are frozen as `temporary_debt` with explicit
  removal owners and an empty target state, and new or replacement edges fail.
- **Direct egress** — raw network transports, registered SDKs and protocol calls
  are fingerprinted without line numbers and classified as `temporary_debt` or
  exact `approved_exception` (`owner: "$source"`). Wildcard egress imports and
  edge growth fail independently.
- **Complexity and concurrency ratchets** — `scripts/architecture/complexity.py`
  enforces non-growing Ruff `C901 <= 15` and `PLR1702 <= 5` ratchets, and
  `scripts/architecture/concurrency.py` tracks lexical-owner baselines for
  threads, processes, executors and async task spawns so unmanaged concurrency
  primitives cannot be added silently.

## Permitted Call Directions

| Direction | Status |
| --- | --- |
| `entrypoint -> chain / application / injected persistence Port` | Allowed according to workflow complexity |
| `chain -> module (via run_module only) / application / injected Port / canonical capability` | Allowed, while direct `chain -> module`, `chain -> Oper` and `chain -> concrete adapter` imports are forbidden |
| `chain -> agent implementation` | Forbidden — chains use `app/application/agent.py` (providers registered in `app/startup/initializers/agent.py`) |
| `agent.tools -> api / scheduler / command` | Forbidden — tools use `app/application/plugin/routes.py`, `plugin/folders.py`, `scheduling.py` and `commands.py` |
| `api -> factory` | Forbidden — route registration is injected into `app/application/plugin/routes.py` by startup |
| `api / chain -> app.workflow` | Forbidden — consumers use `app/application/workflow.py` |
| `application -> domain / runtime contract` | Allowed |
| `application -> DB / Oper / concrete adapter` | Forbidden — define a `Protocol` in Application and inject via startup |
| `db.adapters -> application persistence Protocol / db.oper / UoW` | Allowed (dependency inversion) |
| `module -> canonical capability / Application persistence Port` | Allowed, while new direct `Oper` imports are forbidden |
| `module -> module / chain` | Forbidden for new code |
| `adapter -> application / runtime.extensions / sdk / compat` | Forbidden |
| `domain -> runtime / adapter / application / DB` | Forbidden (`domain` imports only `foundation` and `schemas`) |
| `foundation -> other app packages` | Forbidden |
| `canonical implementation -> sdk / compat` | Forbidden |
| `compat -> canonical implementation at module import time` | Forbidden |
| Any import creating an unapproved module cycle | Forbidden by SCC policy |

Event producer and consumer facts are collected solely by
`scripts/architecture/event_facts.py` on proven `EventManager` receivers
(ignoring `app/plugins/**`). Consumer admission is governed separately — every current consumer fingerprint, owner, classification and concrete reason must match `tests/fixtures/architecture/runtime-contract-policy.json` (`--write-host` never modifies policy, and only configuration-driven workflow registration is permitted as a dynamic consumer). Event validation diagnostics log model names and error counts without raw credential-bearing payloads, and SDK `snapshot()` deep-copies typed models so mutations cannot leak back into live events.

## Key File Locations

| Path | Purpose |
| --- | --- |
| `app/startup/composition/` & `initializers/` | `HostRuntime` wiring (`outbox.py`, `plugin.py`, `server.py`, `chain.py`, `subscription.py`, `system.py`) and ordered normal/safe-mode lifecycle manifests (`lifecycle/components.py`) |
| `app/runtime/compat/manifest.py` | Lazy string-mapped legacy module and `SYMBOL_ALIASES` manifest |
| `app/sdk/` & `app/sdk/plugin/` | Curated plugin ABI (`base.py`, `manager.py`, `scheduler.py`, `media`, `services`, `_legacy/`) |
| `app/runtime/extensions/module/` | `manager.py` discovery, `dispatcher.py` `run_module` execution and `contracts.py` method families |
| `app/runtime/extensions/plugin/` | `manager.py` ABI facade, `runtime.py` aggregate, `gate.py` reentrant lock, `monitor.py`, `projection.py`, `dependency.py`, `storage.py`, `system.py` |
| `app/application/plugin/` | Market `catalog.py`, `install.py`, dynamic `routes.py`, `folders.py`, `release.py`, `rating.py` and `runtime.py` port |
| `app/adapters/external/plugin/client.py` & `system/plugin/` | `PluginMarketTransport`, `PluginPackageManager`, `PluginDependencyInstaller` and `PluginRuntimeHealth` |
| `app/runtime/event/` & `events.py` | `EventManager` facade plus `registry.py`, `binding.py`, `dispatch.py`, `errors.py` and SDK `snapshot.py` |
| `app/application/outbox.py` & `db/adapters/outbox.py` | Transaction-scoped `OutboxStager`, short-UoW `OutboxDispatchStore` and fenced recovery |
| `app/application/transfer/` & `db/adapters/transfer/` | Durable transfer admission (`admission.py`), step execution (`execution.py`), atomic settlement (`db/adapters/chain.py`) and lease-fenced execution ports |
| `app/application/subscription/` & `db/adapters/subscription.py` | Frozen subscription/history DTOs, media translation (`write.py`) and short-session/caller-Session repositories |
| `app/application/history/` & `db/adapters/history/` | Frozen download/transfer history snapshots and query/write/staging ports |
| `app/application/search/session.py` & `chain/search/` | Page-checkpoint search session ports, `SearchChain` facade and shared media/music execution |
| `app/application/agent.py` & `app/agent/` | Agent facade, `AgentManager`, `graph.py` resource cache, session/task/lifecycle/shell/terminal owners and `llm/` (`provider.py`, `helper.py`) |
| `app/application/system.py` & `network.py` | System settings/update/log/Wiki use cases and SSRF-guarded network-test catalog |

Run `tests/test_architecture_dependencies.py` after any ownership, package-root
or import change.
