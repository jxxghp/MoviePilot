# 05 - Architecture and Modules

## Directory Model

MoviePilot keeps the established product packages such as `app/chain`,
`app/agent`, `app/modules`, `app/db`, `app/api`, `app/startup` and
`app/workflow` in their original locations. The historical `app/core`,
`app/helper` and `app/utils` roots are virtual compatibility packages only;
physical Python sources must not be recreated there.

The legacy roots have no physical directories in the source tree. Current
images and update flows write site resources only to `app/application/site/`;
plugin imports under `app.helper.*` are resolved exclusively by the exact
runtime compatibility manifest.

Capabilities migrated out of those legacy roots are organized by technical
responsibility:

```text
Entrypoints / Plugins
        |
        v
API / Agent / CLI / Scheduler / Workflow
        |
        v
Chain orchestration ---------> Application services
        |                              |
        +----------> Modules / DB <----+
                       |
                       v
             Domain / Runtime contracts
                       |
                       v
              Foundation / Adapters

Startup remains the composition root. SDK and compatibility are boundaries,
not dependencies of canonical implementation modules.
```

Directory grouping does not override dependency direction. The architecture
gate builds the complete Python module graph and rejects cycles even when a
cycle passes through an established package that was not moved.

## Canonical Migrated Packages

| Package | Ownership |
|---|---|
| `app/foundation/` | Stateless, config-free and I/O-free primitives: reflection and dynamic import, crypto, DOM parsing, identity, collections, singleton, text conversion/segmentation, URL and version helpers |
| `app/domain/` | Pure MoviePilot business semantics for media, recognition, sites and torrents; live configuration, persistence, transport and acceleration are injected |
| `app/application/` | Focused stateful application services, configured capability selection and service-bound rules |
| `app/runtime/` | Process-wide config, events, complete logging runtime, cache contracts/in-memory policy, execution, localization, scheduling, restart state, concurrency, GC and rate limits |
| `app/adapters/` | Concrete technical I/O and named external ecosystems, split by cache, network, system and external boundaries |
| `app/sdk/` | Stable, deliberately curated imports for plugin authors |

The packages above are the only top-level roots created by the legacy-module
refactor. Existing product roots remain unchanged rather than being moved only
to make the directory tree look symmetrical.

### Application boundaries

| Path | Ownership |
|---|---|
| `app/application/*.py` | Established single-module application services and compatibility facades |
| `app/application/subscription/` | Subscription contracts and write commands: `contract.py` owns shared metadata/media-key projection; `delete.py` and `identity.py` own deletion use cases |
| `app/application/search/` | Search state and later search-plan use cases |
| `app/application/download/` | Download task querying/control and later submission use cases |
| `app/application/music/` | Multi-source music catalog orchestration |
| `app/application/chain/` | Injectable Chain runtime context and compatibility provider |
| `app/application/plugin/` | Plugin market catalog, installation command and dynamic-route port; filenames remain single words (`catalog.py`, `install.py`, `routes.py`) |
| `app/application/server/` | MoviePilot Server reporting and sharing use cases; local data readers and transport callbacks are injected by startup |
| `app/application/site/` | Configured site catalog, authentication level and index-resource capability; the generated extension and its data bundle stay together here |
| `app/application/messaging/` | Message rendering/routing, interactions and the Agent-to-message bridge: `interaction.py` shared interaction contracts and view helpers; `router.py` unified interaction priority and callback dispatch; `site.py`/`subscribe.py`/`skill.py` per-command sessions, input parsing and views; `media.py` media interaction state while the business workflow stays in `MediaInteractionChain`; `plugin.py` plugin input capture and plugin button callbacks; `agent.py` agent choice state, callback protocol and WebAgent bridge; `message.py` notification rendering, templates and queue. Not a public SDK recommended for direct plugin use |
| `app/application/security/` | Authentication, authorization, cookies, passkeys, OTP/two-factor, path/URL safety, SSRF and signing policy |

Application services may use domain rules, runtime contracts, Oper classes and
adapters. Multi-domain workflows still belong in the existing `app/chain/`
package. `Chain`, `Service` and `Manager` remain class patterns; they do not
create additional top-level directory categories.

### Runtime boundaries

| Path | Ownership |
|---|---|
| `app/runtime/config.py` | Deployment configuration and resolved runtime settings |
| `app/runtime/events.py` | Event contracts, dispatch and resolver registration |
| `app/runtime/log.py` | Complete console/plugin/file logging runtime and shutdown |
| `app/runtime/cache.py` | Cache protocols, memory implementations, decorators and proxies |
| `app/runtime/managed_resources.py` | Provider-neutral acquisition, observation and shutdown facade for process-owned optional resources |
| `app/runtime/state.py` | Process restart and update state |
| `app/runtime/extensions/` | Module, plugin, configured-service and managed-resource discovery/registration/lifecycle adapters |
| `app/runtime/compat/` | Standard-library-only exact legacy import routing, resource preflight scanning and DEBUG diagnostics |

`app/startup/` remains the established composition root and is not nested under
runtime. It injects providers and callbacks, orders initialization/shutdown and
decides restart policy. Lower-level runtime modules must not import startup.

`app.schemas` and `app.db` are compatibility facades, not implementation
dependency hubs. Host code imports concrete schema submodules; the schema root
resolves its generated export manifest lazily for plugins and legacy callers.
DB internals import `base`, `decorators`, `engine`, `session`, concrete models
and Oper modules directly. `app.db.models.load_all_models()` is the explicit
composition entry used before metadata creation or migration; importing one
model must not import every table.

### Adapter boundaries

| Path | Ownership |
|---|---|
| `app/adapters/cache/` | Redis and filesystem cache implementations and Redis clients |
| `app/adapters/network/` | Generic HTTP, browser, DNS, Cloudflare and IP transport mechanisms |
| `app/adapters/system/` | OS/filesystem/process facilities, stdio, display, packages, resources and optional Rust acceleration |
| `app/adapters/external/` | CookieCloud, plugin market, OCR, IP-location providers and MoviePilot Server |
| `app/adapters/external/plugin/client.py` | Read-only plugin-market and local-repository client over the established `PluginHelper` implementation |
| `app/adapters/system/plugin/` | Plugin package and dependency I/O (`package.py`, `dependency.py`) |

Generic protocol transport belongs in `adapters/network`; a named product or
ecosystem workflow belongs in `adapters/external`. An adapter may depend on
foundation, domain models, schemas and narrowly required runtime contracts, but
must not import application services, `runtime/extensions`, `runtime/compat` or
the plugin SDK.

RSS is not classified as a transport adapter merely because it uses HTTP. The
current `RssHelper` combines feed parsing, torrent item semantics, configured
site-specific URL discovery and browser fallback, so it belongs to
`app/application/rss.py` and consumes network adapters. Likewise, the generated
site extension owns the configured catalog/authentication/index capability and
lives in `app/application/site/`; only its download and file installation
mechanism remains in `app/adapters/system/resource.py`.

可选的进程级技术资源使用 Managed Resource 合同：实现及其 data-only
`capability.toml` 与适配器同目录，`runtime/extensions` 只解释通用的同步/异步
`start`、`stop` 生命周期，`startup` 负责构建 Capability Runtime。声明必须使用
`on_first_use`，普通启动只发现声明；消费者通过 `app/runtime/managed_resources.py`
显式获取资源。关闭路径先释放消费者，再关闭已初始化 Runtime，未使用的资源不得因关闭而物化。
应用级启动顺序使用 `app/startup/lifecycle/components.py` 的组件描述声明依赖、
normal/safe-mode 范围、start/stop 顺序、超时预算和失败策略。新增进程级资源不得只在
`lifespan()` 中追加过程代码，必须先进入可导出的生命周期清单并补顺序快照测试。
Runtime 关闭后不可逆；完整应用生命周期的再次启动必须由新进程承载，不能在同一解释器中重建局部资源域。
插件需要浏览器时使用 `app.sdk.browser`，由宿主浏览器适配器协调资源，不直接依赖资源实现。
旧插件若直接导入有资源前置条件的第三方包，compat 在插件 import 前递归扫描源码并保守准备资源；
无法精确解析的文件按全部已登记资源降级，最终可导入性仍由 Python loader 判断。

`app/foundation/crypto.py` stays in foundation because it contains only generic
RSA, digest and CryptoJS-compatible AES primitives and has no settings, policy,
I/O or logging. Authentication, token, passkey, signing and two-factor policy
still belongs in `app/application/security/`; callers decide how cryptographic
failures are reported.

### Domain subdomains

`app/domain/` is a business package, not a synonym for every file whose name
mentions media, site or torrent:

| Subdomain | Modules and ownership |
|---|---|
| Media | `context.py` owns `Context`, `MediaInfo` and `TorrentInfo`; `media.py` owns source/ID normalization; `title.py` owns title-candidate and search-keyword rules; `episode.py` owns episode-range display; `scraper.py` owns Kodi-style NFO reading and metadata document generation |
| Recognition | `metainfo.py`, `meta/` and `tokens.py` parse names, paths, release groups, streaming platforms, anime, video and music metadata |
| Site | `site.py` owns site-domain exceptions and interprets HTML into business states such as logged-in and checked-in; configured catalog/auth/index resources stay in `app/application/site/`, generic URL/DOM parsing stays in foundation and network access stays in adapters |
| Torrent | `torrent.py` owns magnet-link semantics; configured download/cache/file behavior stays in `app/application/torrent.py` |

`app/domain` may depend only on schemas and foundation. It must not read global
settings, access DB/network/filesystem adapters, import Rust, discover services
or initialize process runtime state.

`StringUtils` is not a canonical implementation type. Generic text, capacity,
time, URL, DOM, hash and version functions live under `app.foundation`; media
title, episode, site and torrent rules live in their owning domain modules. Host
code must import those implementations directly. `app.sdk.string.StringUtils`
only composes the complete historical static-method surface for plugins, and
both `app.utils.string` and the retired `app.domain.string` resolve to that same
SDK module through the compatibility manifest.

## Established Packages That Stay in Place

The following roots predate this migration and must not be moved or renamed as
part of migrated-capability cleanup:

- `app/agent/`
- `app/api/`
- `app/chain/`
- `app/db/`
- `app/doctor/`
- `app/modules/`
- `app/monitor/`
- `app/plugins/`
- `app/schemas/`
- `app/startup/`
- `app/testing/`
- `app/workflow/`

Necessary canonical import updates are allowed; changing their physical layout
or product responsibilities requires a separate architectural decision.

## Placement Decision Order

Use these questions in order before creating or moving a migrated capability:

1. Is it generic, stateless, independent of MoviePilot state and free of I/O?
   Put it in `app/foundation`.
2. Is it a pure MoviePilot business rule/model? Put it in `app/domain`.
3. Does it read persisted configuration or coordinate one focused configured
   capability? Put it in `app/application`.
4. Is it authentication, authorization, signing, SSRF, URL/path safety, OTP,
   passkey or two-factor policy? Put it in `app/application/security`.
5. Is it message rendering, routing or interaction behavior? Put it in
   `app/application/messaging`.
6. Is it process-wide configuration, events, logging, cache policy, execution,
   scheduling, concurrency, GC or restart state? Put it in `app/runtime`.
7. Does it discover/manage modules, plugins or configured service providers?
   Put it in `app/runtime/extensions`.
8. Does it perform concrete cache, network, OS/process, filesystem, stdio,
   package/resource or Rust I/O? Put it under the matching `app/adapters`
   technical boundary.
9. Does it implement a named external product/ecosystem? Put it in
   `app/adapters/external`.
10. Is it public to plugins or only preserving an old path? Curate it in
    `app/sdk` or map it in `app/runtime/compat`; never move implementation there.

Do not create generic `common`, `helper` or `utils` buckets. Reuse does not erase
ownership.

New production Python module filenames use one lowercase word. When one topic
needs multiple modules, create a topic package and keep each child filename to
one word, for example `runtime/event/{registry,binding,dispatch,errors}.py` or
`application/subscription/{contract,delete,identity}.py`. Established multiword
public import paths may remain as compatibility exceptions after plugin/import
scanning, but they are not templates for new modules. Test filenames continue
to follow pytest's descriptive `test_<behavior>.py` convention.

Legacy module paths belong in `app/runtime/compat/manifest.py`. New
implementation modules must not re-export old managers, helpers or Oper classes
just to preserve imports or tests. A public runtime object whose path or identity
is itself part of the plugin ABI stays at its established path as a thin facade;
new plugin-facing symbols are exported deliberately through `app/sdk` and its
architecture snapshot, not through incidental module globals.

## Existing Chain, Module and DB Layers

### Chain layer

`app/chain/` implements use cases shared by API, CLI, Agent, scheduler and other
entrypoints. Chains may coordinate modules, application services, Oper classes,
events and caches. New chain-to-chain dependencies are allowed only while the
static graph remains acyclic. Backend protocol details and HTTP request objects
do not belong here. Chains interact with modules exclusively through
`run_module` dispatch on method-name contracts; direct imports of module
internals (classes, exceptions, constants) are forbidden, so every module stays
pluggable and a chain never names a concrete module implementation.
The dispatch algorithm belongs to
`app/runtime/extensions/module/dispatcher.py`; `ChainBase` remains the
compatibility facade. New chains and tests inject the minimal
`ChainRuntimeContext` from `app/application/chain/context.py`. No-argument
`Chain()` remains supported through the startup-configured compatibility
provider. High-frequency string methods are classified in
`module/contracts.py`; unknown third-party plugin methods retain the frozen
legacy aggregation contract, while the architecture baseline records every
literal method and call site.

Underscore-prefixed files in `app/chain/` are feature-domain mixins for
`ChainBase` and concrete chains, not chains themselves: `_recognition.py`
(`RecognitionMixin`), `_messaging.py` (`MessageProcessingMixin` /
`NotificationMixin`), `_interaction.py` (`InteractionChainMixin`, the shared
slash-command delegation for `remote_list` / `parse_callback` /
`handle_callback_interaction` / `handle_text_interaction`), `_music.py`
(`MusicSubscribeMixin`, the music single/album subscribe domain mixed into
`SubscribeChain`) and `_transfer.py` (TransferChain feature mixins). Shared
subscription metadata and media-key construction belongs to
`app.application.subscription.contract`; `app.chain.subscribe` keeps the old helper
names only as compatibility forwards and `_music` must not import its concrete
chain owner. A concrete chain that exposes slash-command
interaction inherits `InteractionChainMixin`, injects its handler class via
`_interaction_handler_type` and implements only `_interaction_handler`; it must
not re-export application-layer interaction managers.

### Module layer

`app/modules/` contains pluggable downloaders, media servers, metadata sources,
message channels, indexers and storage providers. New direct module-to-module or
module-to-chain dependencies are forbidden; cross-module orchestration belongs
in a chain. Module internals stay sealed inside the module: shared constants,
exceptions and value domains used by both modules and upper layers live in
`schemas`, and module capabilities are exposed to chains only as dispatched
method names. The directory remains unchanged because discovery and plugin code
depend on this established runtime root.

`app.modules.filemanager` is a lazy compatibility entrypoint. The concrete
`FileManagerModule` implementation lives in `app.modules.filemanager.module`,
while the historical capability path and class module identity remain
`app.modules.filemanager:FileManagerModule`. Storage and transfer-handler
submodules must not import the concrete module implementation through the
package root.

`app/modules/_base/` hosts the shared template base classes for module families
(`downloader.py`, `mediaserver.py`, `notification.py`), each combining the
family mixin with `_ModuleBase` and typed by `TService` (usage:
`class QbittorrentModule(_DownloaderModuleBase[Qbittorrent])`). The base classes
carry only verbatim-duplicated boilerplate — connection test, scheduled
reconnect, torrent-info reading, query-status normalization for downloaders;
authentication, media-exists check, inactive-server handling for media servers;
admin resolution and command registration for message channels — while
subclasses keep the differentiated API calls and override small hooks such as
`_test_connection`, `_test_server` and `_is_inactive`. Discovery already skips
the package (module discovery only enumerates first-level submodules and skips
underscore-prefixed names), so no new exclusion rules are needed; do not grow
this package with per-module business logic.

Channels and storages that need login management or temporary-parameter
initialization follow one generic contract instead of per-target APIs: modules
implement `channel_manage(channel, action, **params)` or
`storage_manage(storage, action, **params)`, route by the requested target
identifier (returning `None` for other targets, accepting both enum members
and plain strings), and interpret actions from the shared
`schemas.types.NotificationAction` / `StorageAction` vocabulary plus opaque
form parameters themselves. All results use the unified
`{"success": bool, "message": ..., "data": ...}` shape.
`NotificationChain.manage_channel` and `StorageChain.manage_storage` forward
transparently and must stay free of any channel/storage-specific names or
logic; new channels or storages adopt the same contract without touching the
chains. The endpoint layer exposes this as two generic endpoints
(`POST /api/v1/notification/manage`, `POST /api/v1/storage/manage`) taking the
common `schemas.ManageRequest` body (`target` + `action` + `params`) and must
never define target-specific names, parameters or response fields — the
frontend supplies them and the endpoint passes them through untouched.

LLM providers follow the same contract: `LLMProviderManager.provider_manage`
dispatches actions from the shared `schemas.types.LlmProviderAction`
vocabulary, seals default-value filling, key sanitization and error rewriting
inside, and the endpoint layer exposes a single `POST /api/v1/llm/manage` with
the same `ManageRequest` body. The only exception is the named OAuth callback
route (`GET /api/v1/llm/provider-auth/callback/{provider_id}`), which stays
named because external browsers redirect to that URL; the endpoint builds the
callback URL from that route name and injects it as an action parameter.

### DB / Oper layer

SQLAlchemy models stay under `app/db/models/`; the data access classes live in
`app/db/oper/` and mirror them one-for-one (`models/subscribe.py` ↔
`oper/subscribe.py`), so a filename carries only the entity and the package name
carries the role. Two verified aggregation exceptions exist: the site family
(`Passkey`, `SiteIcon`, `SiteStatistic`, `SiteUserData`) is consolidated in
`oper/site.py`, and `AgentTaskRun` lives in `oper/agenttask.py`. Chains, modules,
application services and endpoints use Oper
classes instead of issuing SQLAlchemy queries directly. Every schema change
requires an Alembic migration under `database/versions/`.

Oper classes take and return persistence values, not domain objects. Translating
`MediaInfo` / `MetaBase` into a row is business logic and belongs in
`app/application/` — see `application/subscribe.py` and `application/history.py`
for the two write paths. Column-type coercion (numeric year to string, boolean
switches to integers) stays in the Oper because it follows the column, not the
caller.

Invariants that must hold for *every* write are enforced at the mapper rather
than at each call site: `app/db/models/_identity.py` normalizes
`media_source` / `media_id` on `before_insert` / `before_update`, so a new write
path cannot forget them. Identity representation rules themselves
(alias folding, trimming, rejecting zero) live in `app/schemas/media.py`
alongside the two identity mixins; `app/domain/media.py` keeps only source
policy. `app/db` therefore has no dependency on `app/domain`.

## Composition and Compatibility Boundaries

- Startup registers concrete cache factories before decorated business modules
  are imported. Cache contracts remain in `app/runtime/cache.py`; Redis/file
  implementations remain in `app/adapters/cache/backends.py`.
- `app/runtime/log.py` is a dependency leaf with no `app.*` imports. Foundation
  emits no runtime logs; upper-layer owners decide whether failures are
  operationally relevant.
- `app/adapters/system/resource.py` only reports whether installation occurred;
  `app/startup/modules_initializer.py` supplies the loaded site-resource
  versions and decides whether to restart. The adapter never imports the site
  application service.
- Configured notification discovery lives in
  `app/application/notification.py`. Web Push subscription and manual-send HTTP
  behavior stays in `app/api/endpoints/message.py`.
- `app/runtime/compat` stores string mappings and resolves aliases lazily. It may
  not eagerly import canonical MoviePilot modules.
- 已删除的 `app.db.<entity>_oper` 路径继续由精确模块映射提供给旧插件；其中订阅写入、
  整理历史写入和拆分后的用户认证依赖通过 `app.sdk._legacy` 薄门面委托 canonical
  Application/Oper，不把领域对象或 HTTP 依赖重新引回 DB 层。
- 物理模块仍存在但公开符号已经迁走时（例如 `app.domain.media` 的身份原语、
  `app.schemas` 的整理工作项），兼容 Finder 在标准 Loader 执行后叠加白名单符号路由；
  canonical 模块不得为兼容而反向 import `app.runtime.compat`。
- Canonical implementation packages may not import `app/runtime/compat` or
  `app/sdk`.
- Host code uses canonical paths. Only `app/plugins/` and compatibility tests
  may use `app.core`, `app.helper`, `app.utils` or `app.log`.
- New plugins use `app.sdk`. In DEBUG mode, a legacy plugin import remains
  functional and emits one actionable warning per plugin and legacy module.
- Delayed imports are not accepted as a way to hide dependency cycles.

## Permitted Call Directions

| Direction | Status |
|---|---|
| `entrypoint -> chain / application / Oper` | Allowed according to workflow complexity |
| `chain -> module (only via run_module dispatch) / application / Oper / canonical capability` | Allowed; direct `chain -> module` imports forbidden |
| `chain -> agent implementation` | Forbidden; chains reach Agent runtime only through `app/application/agent.py`; `app/startup/agent_initializer.py` registers lightweight providers at import time, and implementations are materialized only when the capability is enabled or first used |
| `agent.tools -> api / scheduler / command` | Forbidden; tools use `app/application/plugins.py`, `scheduling.py` and `commands.py` facades |
| `api -> factory` | Forbidden; the FastAPI instance is injected into `app/application/plugins.py` by the composition root after creation |
| `application -> domain / runtime / adapter / Oper` | Allowed |
| `module -> canonical capability / Oper` | Allowed |
| `module -> module / chain` | Forbidden for new code |
| `adapter -> application / runtime.extensions / sdk / compat` | Forbidden |
| `domain -> runtime / adapter / application / DB` | Forbidden |
| `foundation -> other app packages` | Forbidden |
| `canonical implementation -> sdk / compat` | Forbidden |
| `compat -> canonical implementation at module import time` | Forbidden |
| Any import that creates a module-level cycle | Forbidden |

## Key File Locations

| Path | Purpose |
|---|---|
| `app/application/agent.py` | Agent orchestration facade (`get_agent_manager` / `get_prompt_manager` / capability queries / prompt builders); lightweight providers register through `app/startup/agent_initializer.py`, with no static `application -> agent` edge |
| `app/agent/runtime_loader.py` | Agent-specific capability discovery and canonical entrypoint/service materialization; reuses the generic Capability Runtime while keeping Agent ownership under `app/agent/` |
| `app/application/plugins.py` | Plugin API dynamic route registration/removal; the FastAPI instance is injected by `app/factory.py` after creation |
| `app/application/scheduling.py` | Runtime scheduler facade for Agent tools and endpoints; `Scheduler` class registered by `app/startup/scheduler_initializer.py` |
| `app/application/commands.py` | Command registry facade for Agent tools and endpoints; `Command` class registered by `app/startup/command_initializer.py` |
| `app/chain/agent.py` | `AgentChain(ChainBase)`: the chain-layer entry for Agent sessions; Agent runtime stays in `app/agent/` |
| `app/runtime/config.py` | `ConfigModel`, `Settings` and deployment configuration |
| `app/runtime/events.py` | `EventManager`/`Event` compatibility facade and global `eventmanager` identity |
| `app/runtime/event/registry.py` | Event subscriptions, enable/disable state and dispatch snapshots |
| `app/runtime/event/binding.py` | Explicit module/plugin/host handler resolvers; unresolved classes are diagnosed and skipped, never implicitly constructed by the bus |
| `app/runtime/event/dispatch.py` | Chain/broadcast ordering, concurrency, target-plugin filtering and isolated delivery |
| `app/runtime/event/errors.py` | Handler failure notification and non-recursive `SystemError` downgrade policy |
| `app/runtime/extensions/module/dispatcher.py` | Plugin-first invocation, short-circuit, list merge, signature relay and sync/async execution |
| `app/runtime/extensions/module/contracts.py` | High-frequency method families and frozen legacy fallback contract |
| `app/application/chain/context.py` | Injectable Chain dependencies and no-argument compatibility provider |
| `app/startup/lifecycle/components.py` | Declarative normal/safe-mode lifecycle manifest, ordering and timeout budgets |
| `app/runtime/extensions/module_manager.py` | Module discovery and lifecycle |
| `app/runtime/extensions/plugin_manager.py` | Plugin discovery and lifecycle |
| `app/runtime/extensions/plugin/projection.py` | Plugin commands, APIs, services, modules and actions projected from a running-registry snapshot |
| `app/runtime/extensions/plugin/storage.py` | Injected plugin configuration/data persistence port; runtime code does not import DB Oper classes |
| `app/application/plugin/catalog.py` | Plugin-market mapping, concurrent collection, generation merge and source/version deduplication |
| `app/application/plugin/install.py` | Compatibility, package installation, reporting, installed-list persistence and runtime reload command |
| `app/application/plugin/routes.py` | Dynamic plugin-route registry protocol; plugin response payloads remain raw unless the plugin chooses its own envelope |
| `app/application/server/report.py` | Server reporting use cases over injected local readers and transport callbacks |
| `app/application/server/share.py` | Server sharing use cases over injected repositories and transport callbacks |
| `app/adapters/external/plugin/client.py` | Plugin-market read adapter and cache-refresh boundary |
| `app/adapters/system/plugin/package.py` | Plugin package installation adapter |
| `app/adapters/system/plugin/dependency.py` | Plugin dependency inspection and installation adapter |
| `app/runtime/extensions/managed_resource_adapter.py` | Data-only managed-resource registry and sync/async lifecycle adapters |
| `app/runtime/managed_resources.py` | Lightweight acquisition, state observation and shutdown facade |
| `app/foundation/reflection.py` | Generic reflection and Python module discovery |
| `app/adapters/network/http.py` | Shared synchronous and asynchronous HTTP clients |
| `app/adapters/network/browser.py` | Browser launch facade and browser session implementation |
| `app/adapters/system/display/` | On-first-use virtual display resource and legacy `DisplayHelper` facade |
| `app/application/rss.py` | Configured RSS retrieval and parsing |
| `app/application/site/sites.*` | Generated site catalog, authentication and index capability plus its colocated data bundle |
| `app/runtime/cache.py` | Cache contracts, memory backend, decorators and proxies |
| `app/adapters/cache/backends.py` | Redis and filesystem cache adapters |
| `app/adapters/system/resource.py` | Runtime resource detection/download/installation |
| `app/adapters/system/fsproxy.py` | Timeout-guarded local filesystem operations in a killable subprocess (with colocated `fsworker.py`) |
| `app/adapters/external/wechat_crypt.py` | WeChat enterprise-message XML encryption/decryption protocol |
| `app/application/rules.py` | Rule domain: user rule-group config access (`RuleHelper`), built-in torrent filter rule set and rule parser |
| `app/adapters/external/market.py` | Plugin repository discovery and installation |
| `app/application/security/url.py` | URL/path validation, SSRF protection and signed image policy |
| `app/application/mediaserver.py` | Configured media-server discovery and identity matching |
| `app/runtime/compat/manifest.py` | Exact legacy-to-canonical import manifest |
| `app/sdk/` | Stable plugin imports, including provider-neutral browser launch functions |

Run `tests/test_architecture_dependencies.py` after every ownership or import
change. It rejects physical legacy or retired canonical sources, forbidden
upward dependencies, SDK/compat backreferences, any strongly connected
component containing a migrated module, module-to-module or module-to-chain
imports, entrypoint (`api`/`agent`/`monitor`/`workflow`/`doctor`) imports of
`app.modules` internals, chain imports of `app.modules` internals (chains reach
modules only through `run_module` dispatch), and downloader SDK
(`qbittorrentapi`, `transmission_rpc`) imports inside `app/chain`.

*Last Updated: 2026-08-17*
