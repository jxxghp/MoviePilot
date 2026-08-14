# 05 - Architecture and Modules

## Dependency Model

MoviePilot uses explicit capability packages instead of the historical
`app/core`, `app/helper`, and `app/utils` buckets. Physical Python source must
not be added back under those paths. They exist only as virtual compatibility
packages for installed plugins.

Every migrated capability module and boundary package is required to stay out
of Python-module import cycles. The gate builds the complete application graph
so a cycle through an unmigrated caller is still detected. Startup code is the
composition root: it wires callbacks, resolvers, and adapters into lower-level
managers instead of letting those managers import and instantiate higher-level
services.

```text
Entrypoints / Plugins
        |
        v
API / Agent / CLI / Scheduler / Workflow
        |
        v
Chain orchestration -----> Modules / DB / Services
        |                         |
        +-------------------------+
                    |
                    v
Domain / Platform contracts and state
                    |
                    v
Foundation and infrastructure adapters

Startup composes managers, adapters, diagnostics, and error callbacks.
Compatibility aliases and the plugin SDK are boundary packages, never
dependencies of canonical implementation modules.
```

## Canonical Capability Packages

| Package | Ownership |
|---|---|
| `app/foundation/` | Reusable low-level mechanisms with no MoviePilot business/config dependency: HTTP clients, dynamic module loading, crypto, DOM, identity, URL, version, singleton, text segmentation, and data structures |
| `app/domain/` | Pure business semantics for media, recognition, sites, and torrents; configuration, persistence, and acceleration are injected; detailed below |
| `app/platform/` | Process-wide config, events, complete logging runtime, cache contracts/in-memory policy, execution policy, localization, scheduling, runtime lifecycle, concurrency, GC monitoring, and rate limits |
| `app/infrastructure/` | Configured runtime adapters for Redis/file cache, standard streams, browser, DNS/network, RSS, resources, packages, OS, Rust acceleration, and generated site resources |
| `app/extensions/` | Runtime module, plugin, and service discovery/lifecycle management |
| `app/integrations/` | Concrete product/ecosystem integrations: plugin markets and repositories, CookieCloud, IP-location providers, OCR, and remote MoviePilot service |
| `app/messaging/` | Agent-message bridge, message rendering/routing, and interactions |
| `app/security/` | Authentication, authorization, URL/path safety, SSRF protection, OTP, cookies, passkeys, and two-factor authentication |
| `app/services/` | Focused application services: audio, directory, downloader/media-server/storage selection, notification selection, media-server normalization/matching, persisted recognition/filter rules, formatting, image, torrent I/O, and transfer history |
| `app/agent/skills/` | Agent Skill metadata, market discovery, installation, and local lifecycle; importing it must not initialize the Agent orchestrator |
| `app/sdk/` | Stable, deliberately curated imports for plugin authors |
| `app/compat/` | Standard-library-only legacy import routing and DEBUG diagnostics |

`app/chain/` remains the application orchestration layer and `app/modules/`
remains the collection of pluggable backend implementations. A package name
describes ownership; it does not authorize a dependency cycle. The architecture
gate checks the complete module graph, including imports outside these packages.

### Domain subdomains

`app/domain/` is a business-capability package, not a synonym for every file
whose name mentions media, site, or torrent:

| Subdomain | Modules and ownership |
|---|---|
| Media | `context.py` owns `MediaInfo`, `TorrentInfo`, music models, and use-case context; `media.py` owns source/ID normalization; `scraper.py` owns Kodi-style NFO reading and media metadata document generation |
| Recognition | `metainfo.py`, `meta/`, and `tokens.py` parse names, paths, release groups, streaming platforms, anime, video, and music metadata |
| Site | `site.py` interprets site HTML into business states such as logged-in and checked-in; generic HTTP transport remains foundation and configured browser access remains infrastructure |
| Torrent | Torrent identity and title semantics live in the context/recognition model; downloading, caching, and parsing torrent files lives in `services/torrent.py` |
| Shared business text | `string.py` retains MoviePilot-specific media/site/torrent text normalization pending concern-level extraction; new generic primitives must not be added to it |

Filter-rule meaning is part of the torrent/filter domain, but
`services/filter.py` reads user-persisted rule configuration and is
therefore an application service rather than a pure domain module.

Recognition follows the same boundary: `services/recognition.py` reads
`SystemConfigOper`; `startup/domain_initializer.py` injects live rule providers,
file-extension policy, TMDB image URL construction, source defaults, and the
optional Rust accelerator into pure domain modules.

## Shared File Placement Rule

Before creating a file, first decide which capability package owns it and check
whether an existing domain file already provides that capability. Create a new
file only for a genuinely separate concern and name it according to
`07-naming-conventions.md`.

Do not create generic `common`, `helper`, or `utils` buckets. A reusable function
still needs an owner:

- Generic code that does not read MoviePilot business/config state belongs in `app/foundation/`, including reusable protocol clients and reflection helpers.
- Core media-specific rules belong in `app/domain/`; rules tied specifically to
  configured media-server representations belong in `app/services/mediaserver.py`.
- Configuration-aware runtime resources belong in `app/infrastructure/`; a
  concrete external product or ecosystem belongs in `app/integrations/`.
- Stateful cross-domain behavior belongs in `app/services/` or `app/chain/`.
- Plugin-facing public imports belong in `app/sdk/`; canonical packages are not
  automatically public plugin APIs.

## Entrypoint Layer

**Directories:** `app/api/endpoints/`, `moviepilot` (CLI), `app/agent/`, scheduler
callbacks, webhook handlers, and message interactions.

Responsibilities:

- Handle authentication, parameter parsing, response serialization, streaming,
  and boundary validation.
- Call `app/chain/` for logic that coordinates modules, events, caches, or
  workflows.
- Call an Oper class or focused service directly only for simple CRUD and input
  normalization.

Endpoints must not contain reusable business workflows. Register new API
endpoints in `app/api/apiv1.py`.

## Chain Layer

**Directory:** `app/chain/`

Chains implement use cases shared by API, CLI, agent, scheduler, and other
entrypoints. They may coordinate modules, services, Oper classes, events, and
caches.

- Call module capabilities through `run_module()` or `async_run_module()`.
- Use `ModuleManager` directly only for enumeration, inspection, or health
  checks.
- Chain-to-chain reuse is allowed only while the static dependency graph remains
  acyclic.
- Do not place HTTP request objects or backend-specific protocol details here.

## Module Layer

**Directory:** `app/modules/`

Modules implement pluggable backends such as downloaders, media servers,
metadata sources, message channels, indexers, and storage providers.

- A module focuses on one backend or capability and returns domain results, not
  HTTP responses.
- New direct `module -> module` or `module -> chain` dependencies are forbidden.
- Cross-module orchestration belongs in a chain.
- Shared backend-neutral behavior belongs in its owning canonical package.

Module categories are defined in `app/schemas/types.py`.

## DB / Oper Layer

**Directory:** `app/db/`

SQLAlchemy models live under `app/db/models/`; `*_oper.py` classes encapsulate
queries. Chains, modules, services, and endpoints must use those classes instead
of issuing SQLAlchemy queries directly. Every schema change requires an Alembic
migration under `database/versions/`.

## Composition and Compatibility Boundaries

- `app/startup/` owns process composition. Lower layers expose explicit
  registration or configuration functions for dependencies such as event
  resolvers and error reporters.
- Cache contracts, memory implementations, decorators, and proxies live in
  `app/platform/cache.py`; Redis and file I/O implementations live in
  `app/infrastructure/cache.py`. Startup registers concrete factories before
  importing modules that instantiate cache decorators.
- The complete logging runtime lives in `app/platform/log.py`: policy,
  console/plugin routing, async rotating file output, and shutdown.
  `app.platform.config` supplies the resolved settings and log path.
  `platform/log.py` is enforced as a dependency leaf with no `app.*` imports.
  Foundation modules do not emit runtime logs; their callers decide whether a
  returned fallback or raised error should be logged. Plugins use `app.sdk.logging`;
  legacy `app.log` resolves to that SDK facade.
- Resource adapters only report whether installation succeeded. Process restart
  policy belongs to `app/startup/modules_initializer.py`.
- Configured notification-service discovery lives in
  `app/services/notification.py`. Web Push subscription and manual-send HTTP
  behavior lives directly in `app/api/endpoints/message.py`, not in messaging.
- `app/compat/` may not import canonical MoviePilot implementation modules at
  import time. Its manifest stores strings and resolves aliases lazily.
- Canonical packages may not import `app.compat` or `app.sdk`.
- Host code uses canonical paths. Only `app/plugins/` and compatibility tests
  may use legacy `app.core`, `app.helper`, or `app.utils` paths.
- New plugins use `app.sdk`. In DEBUG mode, legacy plugin imports work but emit
  one actionable warning per plugin and legacy module.
- Delayed imports are not accepted as a way to hide a dependency cycle.

## Permitted Call Directions

| Direction | Status |
|---|---|
| `entrypoint -> chain / service / Oper` | Allowed according to workflow complexity |
| `chain -> module / service / Oper / canonical capability` | Allowed |
| `module -> canonical capability / Oper` | Allowed |
| `module -> module / chain` | Forbidden for new code |
| `canonical implementation -> sdk / compat` | Forbidden |
| `compat -> canonical implementation at module import time` | Forbidden; aliases resolve lazily |
| `foundation -> other app capability packages` | Forbidden |
| Any import that creates a module-level cycle | Forbidden |

## Key File Locations

| Path | Purpose |
|---|---|
| `app/api/apiv1.py` | API router registration |
| `app/platform/config.py` | `ConfigModel`, `Settings`, and deployment configuration |
| `app/platform/events.py` | `EventManager`, `Event`, and event resolver registration |
| `app/extensions/module_manager.py` | Module discovery and lifecycle |
| `app/extensions/plugin_manager.py` | Plugin discovery and lifecycle |
| `app/foundation/module.py` | Generic Python module discovery and dynamic import |
| `app/foundation/http.py` | Shared synchronous and asynchronous HTTP clients |
| `app/infrastructure/rss.py` | Configured RSS retrieval and parsing adapter |
| `app/platform/cache.py` | Cache contracts, memory backend, decorators, and proxies |
| `app/infrastructure/cache.py` | Redis and filesystem cache adapters |
| `app/platform/gc.py` | Process memory observation and garbage-collection policy |
| `app/integrations/market.py` | Plugin repository discovery, compatibility, download, and installation |
| `app/integrations/location.py` | External IP-location provider integration |
| `app/agent/skills/registry.py` | Agent Skill discovery, market, and local lifecycle |
| `app/domain/context.py` | `Context`, `MediaInfo`, and `TorrentInfo` |
| `app/security/url.py` | URL/path validation, SSRF protection, and signed image URL policy |
| `app/services/filter.py` | Persistent user filter-rule lookup and media-context selection |
| `app/services/recognition.py` | Persistent recognition-rule lookup for domain injection |
| `app/services/mediaserver.py` | Configured media-server discovery, Provider ID normalization, and music-library matching |
| `app/startup/` | Runtime composition root |
| `app/compat/manifest.py` | Exact legacy-to-canonical import manifest |
| `app/sdk/` | Stable plugin imports |
| `database/versions/` | Alembic migrations |

## Where New Capabilities Go

| Scenario | Action |
|---|---|
| Shared business workflow | `app/chain/` |
| Stateful focused application behavior | `app/services/` or the owning capability package |
| New backend implementation | `app/modules/<backend>/` or `app/integrations/` |
| New public HTTP endpoint | `app/api/endpoints/`, registered in `app/api/apiv1.py` |
| Generic primitive, protocol client, or reflection mechanism | `app/foundation/` |
| Media-domain parsing or rule | `app/domain/` |
| Configuration-aware network, filesystem, process, feed, or generated resource adapter | `app/infrastructure/` |
| Concrete third-party product or ecosystem integration | `app/integrations/` |
| Deployment/startup setting | `ConfigModel` in `app/platform/config.py` |
| Runtime user-editable option | `SystemConfigKey` plus `SystemConfigOper` |
| New supported plugin API | Curated export in `app/sdk/` with compatibility tests |

Run `tests/test_architecture_dependencies.py` after every ownership or import
change. It rejects physical legacy sources, host legacy imports, implementation
dependencies on SDK/compat, and any strongly connected component containing a
canonical migrated module.

*Last Updated: 2026-08-14*
