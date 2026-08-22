# 05 - Architecture and Modules

## Directory Model

MoviePilot keeps the established product packages such as `app/application`,
`app/agent`, `app/modules`, `app/db`, `app/api`, `app/startup` and
`app/workflow` in their canonical locations. The historical `app/core`,
`app/helper` and `app/utils` roots are virtual compatibility packages only;
physical Python sources must not be recreated there. Use-case orchestration is
now part of `app/application/orchestration/`.

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
| `app/application/subscription/` | Subscription use cases: `write.py` owns media-to-row translation and the write port; `contract.py` owns shared metadata/media-key projection; query, mutation, deletion, identity and search stay in their single-word modules |
| `app/application/search/` | Search state and later search-plan use cases |
| `app/application/download/` | Download task querying/control and later submission use cases |
| `app/application/music/` | Multi-source music catalog orchestration |
| `app/application/orchestration/` | Processing chains and their dispatch primitives; `context.py` owns the injectable runtime context and its no-argument compatibility provider |
| `app/application/plugin/` | Plugin market catalog, installation command, runtime port, folder operations and dynamic-route use cases; filenames remain single words (`catalog.py`, `install.py`, `runtime.py`, `folders.py`, `routes.py`) |
| `app/application/server/` | MoviePilot Server reporting and sharing use cases; local data readers and transport callbacks are injected by startup |
| `app/application/site/` | Configured site catalog, authentication level and index-resource capability; the generated extension and its data bundle stay together here |
| `app/application/messaging/` | Message rendering/routing, interactions and the Agent-to-message bridge: `interaction.py` shared interaction contracts and view helpers; `router.py` unified interaction priority and callback dispatch; `site.py`/`subscribe.py`/`skill.py` per-command sessions, input parsing and views; `media.py` media interaction state while the business workflow stays in `MediaInteractionChain`; `plugin.py` plugin input capture and plugin button callbacks; `agent.py` agent choice state, callback protocol and WebAgent bridge; `message.py` notification rendering, templates and queue. Not a public SDK recommended for direct plugin use |
| `app/application/security/` | Authentication, authorization, cookies, passkeys, OTP/two-factor, path/URL safety, SSRF and signing policy |

Application services may use domain rules, runtime contracts, Oper classes and
adapters. Multi-domain workflows belong to `app/application/orchestration/`
package. `Chain`, `Service` and `Manager` remain class patterns; they do not
create additional top-level directory categories.

### Runtime boundaries

`app/runtime` spans far more modules than a per-file ownership table can track,
and such a table silently rots: it drifts from the tree and starts assigning
responsibilities to files that no longer exist. Placement is therefore decided
by a rule, and the rule is what this section fixes.

**Criterion D — a directory exists because *when its members run* and *who may
import them* differ, not because they share a topic word.**

Ask in order. The first hit decides placement; parallel answers are not allowed.

| # | Question | Hit → |
|---|---|---|
| D1 | Would deleting it break plugin code that is already written? | `extensions/contract/` when the host still routes every caller there; `compat/` when only already-published plugins still import it |
| D2 | Is its shape a port slot — registered by the composition root, resolved by extensions? | `hostports/` |
| D3 | At which moment of the extension lifecycle does it run? | discovery and loading → `extensions/lifecycle/`; registration → `extensions/admission/`; held state → `extensions/registry/`; query → `extensions/projection/` |
| D4 | None of the above | Process-level mechanism; stays flat at the runtime root |

Tie-break: when two lifecycle phases both claim a module, it belongs to the
earliest one. The D3 row lists the four directories in that chronological
order, so the earlier claimant is the one listed first.

A hit on D1 or D2 only decides placement when the target directory's own rule
admits the module. `contract/` admits a module when every public symbol it
declares reaches extension authors through the SDK; `hostports/` admits one
protocol plus one module-level `HostPort` instance. A module that hits the
question but fails the rule falls through to the next question.

Directories follow from the criterion, not from subject matter:

| Path | Admitted by | Contents |
|---|---|---|
| `app/runtime/*.py` (flat) | D4 | Process-level mechanisms owned by the process, not by any extension: deployment configuration, logging, cache, event facade, scheduling, threading, execution, rate limiting, process and reload state |
| `app/runtime/hostports/` | D2 | Port slots only. Each module declares one protocol plus one module-level `HostPort` instance; `port.py` holds the generic. Every slot is injected in one place, by `app/startup/hostport_initializer.py` |
| `app/runtime/extensions/` | D3 | Module, plugin, configured-service and managed-resource discovery, registration and lifecycle adapters, split by lifecycle phase. Flat at this level: the host-internal service-configuration substrate that every phase reads and that runs at none of them, plus the modules named below that a gate or a hard-coded string holds in place |
| `app/runtime/extensions/contract/` | D1 | Declaration types, distribution and hook probing, instance identity and the configuration-schema subset. Every symbol here is handed to extension authors through the SDK, and the package imports nothing from `app/runtime` |
| `app/runtime/extensions/admission/` | D3 registration | Declaration contract checks, extension-scoped deduplication, instance selection, service-instance requirement shape checks and registration arbitration. A declaration that breaks its contract is rejected at registration, never at call time |
| `app/runtime/extensions/registry/` | D3 held state | Registries that keep admitted extensions by coordinate and reclaim their entries. They only store and hand back registration results |
| `app/runtime/extensions/projection/` | D3 query | Views and dispatch paths aggregated from a registration snapshot; a projection never changes what is registered |
| `app/runtime/extensions/lifecycle/` | D3 discovery and loading | Manifest discovery, versioned plugin source layout, plugin persistence directory layout, the Capability Runtime adapters that materialize/start/stop host modules and managed resources, and the persistence and external-system ports the loader resolves |
| `app/runtime/compat/` | D1 | Exact legacy import routing, resource preflight scanning and DEBUG diagnostics, plus modules the host itself no longer calls and only already-published plugins still import. `manifest.py` stays standard-library-only so the baseline script can load it without importing the host |

`plugin_manager.py` and `module_manager.py` stay flat in
`app/runtime/extensions/`. They belong to the discovery-and-loading phase, but
five hard-coded names in `scripts/sdk/exports.py`, one `__module__` assertion
and fourteen patch-target strings in tests all spell their current path, and
every one of those is a string match that stays green when it is wrong.

`service_config.py` stays flat for a structural reason: the host-internal
service-family landing table, the injected configuration readers and the single
fan-out implementation are read from `admission/`, `registry/`, `projection/`
and `lifecycle/` alike, and run at none of those moments. A substrate every
phase imports belongs below the phase directories, not inside one of them.

`service_registry.py` stays flat for a gate reason. It hits D1 — `app.helper.service`
aliases this exact module and both of its public symbols are SDK exports — but
`contract/` cannot take it. Modules under the plugin-component roots may not
declare `__all__`, and this module's `__all__` is what makes
`scripts/sdk/exports.py` require `ServiceConfigHelper` from `app.sdk.services`:
without it `public_surface()` falls back to symbols defined in the module and
the re-exported class silently drops out of the required-export list. It also
imports `module_manager`, which would make the frozen contract package depend on
the manager it is supposed to be independent of.

Directories inside `app/runtime/extensions/` are scanned by `rglob` from
`PLUGIN_COMPONENT_ROOTS` in `tests/test_architecture_dependencies.py`. Moving a
file into or out of one of them changes what the gate covers without changing
the assertion, so any such move must be validated by injecting one deliberate
violation and confirming the gate turns red.

A file name never repeats the phase its directory already states: the
registration check for storage declarations is `admission/storage.py`, the
registry that holds them is `registry/storage.py`.

`app/runtime/config.py` does not move. Three workflow paths under `.github/`
and three assertions in `tests/test_plugin_market_default.py` name it literally.

`topology.py` and `observability/` are admitted by D4: process topology policy is
read by startup and by offline diagnostics alike, and the metric contracts are a
no-op-capable facade the process owns. Neither belongs to any extension, so
neither enters `extensions/`.

`app/startup/` is the composition root and is not nested under runtime; lower
runtime modules must not import it. It publishes its frozen, slotted
`HostRuntime` through FastAPI `app.state`. API dependencies narrow that object to
a domain runtime — `AgentChatRuntime`, for example — instead of adding a string
key to a global service map; a legacy registry may delegate the same object while
its domain migrates, but must not construct a second set of service instances.
API, Scheduler and Chain deployment values are exposed as frozen snapshots from
`HostRuntime.configuration`, so a canonical caller must not add a fresh direct
`settings` import for a field an existing snapshot already carries.

`app.schemas` and `app.db` are compatibility facades, not implementation
dependency hubs. Host code imports concrete schema submodules; the schema root
resolves its generated export manifest lazily for plugins and legacy callers.
DB internals import `base`, `decorators`, `engine`, `session`, concrete models
and Oper modules directly. `app.db.models.load_all_models()` is the explicit
composition entry used before metadata creation or migration; importing one
model must not import every table.

### Composition-root boundaries

`app/startup/` remains the established composition root and is not nested under
runtime. It injects providers and callbacks, orders initialization/shutdown and
decides restart policy. Lower-level runtime modules must not import startup.

Criterion D cannot place a file inside `app/startup/`. Its four questions ask
what a module means to extension authors, and the composition root holds no
extension: D1–D3 miss every member and all of them fall through to D4,
"process-level mechanism, stays flat at the root". One answer for every file is
not a decision procedure, and it is exactly where the *de facto* rule — "a new
file defaults to the top level" — came from. That default held for every file
added between 2024-09 and 2026-08-16, and it left three unrelated shapes
indistinguishable by path.

**Criterion S — a directory under `app/startup/` exists because *what the caller
does with its members* differs, not because they share a topic word.**

Ask in order. The first hit decides placement; parallel answers are not allowed.

| # | Question | Hit → |
|---|---|---|
| S1 | Does it decide *when* other members run — order, dependencies, timeout budget, failure policy, normal/safe-mode scope? | `lifecycle/` |
| S2 | Does the caller *perform* it, once, at a moment the composition root names, and never read it again? | flat `*_initializer.py` at the startup root |
| S3 | Does the caller *read* it as a table — the caller picks the moment, may read it again, and an engine that must not know the entries executes them later? | `bindings/` |
| S4 | Does the caller *build* it — a port implementation, or the frozen type of the runtime those implementations are assembled into — and hand the object to another layer that then holds it for the life of the process? | `ports/` |
| S5 | None of the above | There is no fifth class. Extend this criterion before landing the file; it must not default to the top level |

Tie-break: S1 > S2 > S3 > S4 — the end that decides moments wins.
`command_initializer.py` both pushes a table into the command hub at import time
(S3-shaped) and exposes `init_command`/`stop_command` (S2); it hits S2 and stays
flat, while the table it pushes hits S3 and lives in `bindings/`.

Decidability check when S2 and S3 both look like a hit: **is it called a second
time in the same process?** An action runs once per lifecycle moment — a restart
is a new process or a new `lifespan`. A binding is re-read on the consumer's
schedule: `builtin_commands()` on every `restart_command()`, `build_host_jobs()`
on every `Scheduler().init()`, `build_database_governance()` on every backup.

Decidability check when S2 and S4 both look like a hit: **is the module read
again after that one call?** An initialization action is dead once its moment
has passed — nothing imports it afterwards. A port module keeps being read: the
object it defines is held and called for the life of the process, and its types
annotate the layers that hold it. `ports/subscription.py` exposes one
registration verb next to its writer, and stays a port module because
`modules_initializer.py` also constructs `TransactionalSubscribeWriter` from it.

| Path | Admitted by | Contents |
|---|---|---|
| `app/startup/lifecycle/` | S1 | `components.py` declares the normal/safe-mode manifest, ordering, dependencies, timeout budgets and failure policy; `__init__.py` is the `lifespan` that executes it. Nothing here binds a business domain |
| `app/startup/*_initializer.py` (flat) | S2 | One initialization-action family per module. Every public symbol is a verb bound to a moment, and deleting its lifecycle entry makes the module dead. The top level admits nothing else |
| `app/startup/bindings/` | S3 | Command word, job id and database dialect bound to concrete business chains and infrastructure. Knows every business domain; the engines that consume it know none. One binding family per module, or a subpackage when it needs more than one |
| `app/startup/ports/` | S4 | `context.py` declares the shape — request-scoped repository/transaction factory protocols and the frozen `HostRuntime` that groups them by domain; every other module implements one application-declared port over SQLAlchemy and Oper classes. This is the only place in the host allowed to depend on `app/db` and `app/application` at once: the port is declared in application, landed in db, and only the composition root sees both ends |

`app/runtime/hostports/` and `app/startup/ports/` are different ends of the same
word. The runtime package holds *slots* — one protocol plus one module-level
`HostPort` instance that extensions resolve. The startup package holds
*implementations* the composition root constructs and hands out; nothing resolves
them by name.

The fifteen `*_initializer.py` modules deliberately stay flat. They are one class
with one shape, and a subdirectory for them would only restate `_initializer`.
Eleven are invoked from `lifecycle/`, three (`agent`, `hostport`,
`managed_resources`) from `modules_initializer.py`, and `database_initializer.py`
from `app/main.py` before the ASGI application exists. That is a difference in
*which* moment, not in *what the caller does*, so criterion S does not split on
it — and `lifecycle/components.py` is already the one place the moments are
declared.

`bindings/database.py` and `database_initializer.py` are two modules on purpose.
The first picks the backup backend by dialect and assembles the governance
facade; the second owns table creation and Alembic migration. Keeping them apart
keeps Alembic and `load_all_models()` off `app/cli.py`, which builds the
governance facade to take one backup without ever starting the application.

`tests/test_architecture_dependencies.py` gates all four rows: the top level
admits only `*_initializer.py`, no subpackage may contain one, and the set of
subpackages is closed — a fourth one turns the gate red until criterion S is
extended to admit it.

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
- `app/db/`
- `app/doctor/`
- `app/modules/`
- `app/monitor/`
- `app/plugins/`（扩展的安装挂载点，纯数据目录：不放任何宿主源码，连
  `__init__.py` 都没有，`app.plugins` 是命名空间包）
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
    Sole exception: `_PluginBase` itself. It is not a re-export of an
    implementation living elsewhere — it *is* the extension ABI, and its only
    other possible home, `app/plugins/`, must stay a pure data directory so a
    container volume can cover it. Its public surface is pinned by
    `SDK_PLUGIN_BASE_SURFACE` in `app/sdk/_exports.py`.

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

`app/application/orchestration/` implements use cases shared by API, CLI, Agent, scheduler and other
entrypoints. Chains may coordinate modules, application services, Oper classes,
events and caches. New chain-to-chain dependencies are allowed only while the
static graph remains acyclic. Backend protocol details and HTTP request objects
do not belong here. Chains interact with modules exclusively through
`run_module` dispatch on method-name contracts; direct imports of module
internals (classes, exceptions, constants) are forbidden, so every module stays
pluggable and a chain never names a concrete module implementation.
The dispatch algorithm belongs to
`app/runtime/extensions/projection/dispatcher.py`; `ChainBase` remains the
compatibility facade. New chains and tests inject the minimal
`ChainRuntimeContext` from `app/application/orchestration/context.py`. No-argument
`Chain()` remains supported through the startup-configured compatibility
provider. High-frequency string methods are classified in
`module/contracts.py`; unknown third-party plugin methods retain the frozen
legacy aggregation contract, while the architecture baseline records every
literal method and call site.

Underscore-prefixed files in `app/application/orchestration/` are feature-domain mixins for
`ChainBase` and concrete chains, not chains themselves: `_recognition.py`
(`RecognitionMixin`), `_messaging.py` (`MessageProcessingMixin` /
`NotificationMixin`), `_interaction.py` (`InteractionChainMixin`, the shared
slash-command delegation for `remote_list` / `parse_callback` /
`handle_callback_interaction` / `handle_text_interaction`), `_music.py`
(`MusicSubscribeMixin`, the music single/album subscribe domain mixed into
`SubscribeChain`) and `_transfer.py` (TransferChain feature mixins). Shared
subscription metadata and media-key construction belongs to
`app.application.subscription.contract`; `app.application.orchestration.subscribe` keeps the old helper
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

`app.modules.medialibrary` is the media library filesystem module: it organises
files into the library and resolves library files back from the standard library
layout. Its capability entrypoint is
`app.modules.medialibrary:MediaLibraryModule`. Storage backends and the transfer
handler live outside this package and must not be reached through it; the
historical `app.modules.filemanager` path and the `FileManagerModule` class name
stay resolvable through `app/runtime/compat/manifest.py`.

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
`app/application/` — see `application/subscription/write.py` and `application/history.py`
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
  canonical 模块不得为兼容而反向 import `app.runtime.compat`。命名空间包同样适用：
  `app.plugins` 没有 `__init__.py`，兼容 Finder 直接给它挂叠加层。
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
| `agent.tools -> api / scheduler / command` | Forbidden; tools use `app/application/plugin/routes.py`, `plugin/folders.py`, `scheduling.py` and `commands.py` application services |
| `api -> factory` | Forbidden; the FastAPI route adapter is injected into `app/application/plugin/routes.py` by the composition root after creation |
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
| `app/application/subscription/write.py` | Subscription media translation and sync/async write-port orchestration |
| `app/application/scheduling.py` | Runtime scheduler facade for Agent tools and endpoints; `Scheduler` class registered by `app/startup/scheduler_initializer.py` |
| `app/application/commands.py` | Command registry facade for Agent tools and endpoints; `Command` class registered by `app/startup/command_initializer.py` |
| `app/application/orchestration/agent.py` | `AgentChain(ChainBase)`: the chain-layer entry for Agent sessions; Agent runtime stays in `app/agent/` |
| `app/runtime/config.py` | `ConfigModel`, `Settings` and deployment configuration |
| `app/runtime/topology.py` | Single-worker full-runtime policy and safe-mode topology validation |
| `app/runtime/events.py` | `EventManager`/`Event` compatibility facade and global `eventmanager` identity |
| `app/runtime/event/registry.py` | Event subscriptions, enable/disable state and dispatch snapshots |
| `app/runtime/event/binding.py` | Explicit module/plugin/host handler resolvers; unresolved classes are diagnosed and skipped, never implicitly constructed by the bus |
| `app/runtime/event/dispatch.py` | Chain/broadcast ordering, concurrency, target-plugin filtering and isolated delivery |
| `app/runtime/event/errors.py` | Handler failure notification and non-recursive `SystemError` downgrade policy |
| `app/runtime/extensions/projection/dispatcher.py` | Plugin-first invocation, short-circuit, list merge, signature relay and sync/async execution |
| `app/runtime/extensions/contract/module_method.py` | High-frequency method families and frozen legacy fallback contract |
| `app/application/orchestration/context.py` | Injectable Chain dependencies and no-argument compatibility provider |
| `app/startup/lifecycle/components.py` | Declarative normal/safe-mode lifecycle manifest, ordering and timeout budgets |
| `app/runtime/extensions/module_manager.py` | Module discovery and lifecycle |
| `app/runtime/extensions/plugin_manager.py` | Plugin discovery and lifecycle |
| `app/runtime/extensions/projection/plugin.py` | Plugin commands, APIs, services, modules and actions projected from a running-registry snapshot |
| `app/runtime/extensions/lifecycle/storage.py` | Injected plugin configuration/data persistence port; runtime code does not import DB Oper classes |
| `app/application/plugin/catalog.py` | Plugin-market mapping, concurrent collection, generation merge and source/version deduplication |
| `app/application/plugin/install.py` | Compatibility, package installation, reporting, installed-list persistence and runtime reload command |
| `app/application/plugin/routes.py` | Dynamic plugin-route registry protocol and registration/removal use cases; plugin response payloads remain raw unless the plugin chooses its own envelope |
| `app/application/plugin/folders.py` | Plugin-folder cleanup use case, compatible with current dictionary and legacy list storage shapes |
| `app/application/plugin/runtime.py` | Plugin runtime port consumed by API, Agent and Workflow; the concrete `PluginManager` is registered only by startup |
| `app/application/module.py` | Host module runtime port consumed by entrypoints; the concrete `ModuleManager` is registered only by startup |
| `app/application/scheduling.py` | Scheduler runtime port consumed by API/Agent/application commands |
| `app/runtime/scheduler.py` | Scheduled-job declaration types and the generic execution engine: job-state registry, trigger expansion, sync/async/subprocess execution, progress convergence and listing. Knows no business domain; failure notices leave through a host-overridable hook |
| `app/scheduler/` | Scheduling composition root: `composition.py` assembles the engine with the host manifest, `agent_tasks.py`/`workflows.py`/`plugins.py` own one registration path each. The three paths differ in trigger timing, lifecycle and failure semantics and are deliberately not merged |
| `app/startup/bindings/scheduling/` | Host business job manifest expressed as data (`manifest.py`) plus the system jobs the host implements itself (`systemjobs.py`). This is where knowledge of every business domain lives |
| `app/startup/bindings/builtin_commands.py` | Built-in command words bound to business chains and scheduled-job ids; business chains materialize on first execution, not at registration |
| `app/startup/bindings/database.py` | Backup backend selected by dialect and the assembled database-governance facade; carries no Alembic or model-loading dependency |
| `app/application/orchestration/scheduler.py` | `SchedulerChain`: table cleanup, `scheduler_job`/`clear_cache` broadcast and system-message forwarding for scheduled jobs |
| `app/application/server/report.py` | Server reporting use cases over injected local readers and transport callbacks |
| `app/application/server/share.py` | Server sharing use cases over injected repositories and transport callbacks |
| `app/adapters/external/plugin/client.py` | Plugin-market read adapter and cache-refresh boundary |
| `app/adapters/system/plugin/package.py` | Plugin package installation adapter |
| `app/adapters/system/plugin/dependency.py` | Plugin dependency inspection and installation adapter |
| `app/runtime/extensions/lifecycle/managed_resource_adapter.py` | Data-only managed-resource registry and sync/async lifecycle adapters |
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
(`qbittorrentapi`, `transmission_rpc`) imports inside `app/application/orchestration`.

*Last Updated: 2026-08-18*
