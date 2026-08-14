# 05 - Architecture and Modules

## Directory Model

MoviePilot keeps the established product packages such as `app/chain`,
`app/agent`, `app/modules`, `app/db`, `app/api`, `app/startup` and
`app/workflow` in their original locations. The historical `app/core`,
`app/helper` and `app/utils` roots are virtual compatibility packages only;
physical Python sources must not be recreated there.

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
| `app/application/*.py` | Audio, directory, downloader, filter, formatting, transfer history, image, media-server, notification, recognition, RSS, storage and torrent application services |
| `app/application/site/` | Configured site catalog, authentication level and index-resource capability; the generated extension and its data bundle stay together here |
| `app/application/messaging/` | Message rendering/routing, interactions and the Agent-to-message bridge |
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
| `app/runtime/state.py` | Process restart and update state |
| `app/runtime/extensions/` | Module, plugin and configured-service discovery/registration/lifecycle |
| `app/runtime/compat/` | Standard-library-only exact legacy import routing and DEBUG diagnostics |

`app/startup/` remains the established composition root and is not nested under
runtime. It injects providers and callbacks, orders initialization/shutdown and
decides restart policy. Lower-level runtime modules must not import startup.

### Adapter boundaries

| Path | Ownership |
|---|---|
| `app/adapters/cache/` | Redis and filesystem cache implementations and Redis clients |
| `app/adapters/network/` | Generic HTTP, browser, DNS, Cloudflare and IP transport mechanisms |
| `app/adapters/system/` | OS/filesystem/process facilities, stdio, display, packages, resources and optional Rust acceleration |
| `app/adapters/external/` | CookieCloud, plugin market, OCR, IP-location providers and MoviePilot Server |

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
| Media | `context.py` owns `Context`, `MediaInfo` and `TorrentInfo`; `media.py` owns source/ID normalization; `scraper.py` owns Kodi-style NFO reading and metadata document generation |
| Recognition | `metainfo.py`, `meta/` and `tokens.py` parse names, paths, release groups, streaming platforms, anime, video and music metadata |
| Site | `site.py` interprets HTML into business states such as logged-in and checked-in; configured catalog/auth/index resources stay in `app/application/site/`, DOM parsing stays in foundation and network access stays in adapters |
| Torrent | Identity/title semantics live in the domain model; configured download/cache/file behavior stays in `app/application/torrent.py` |
| Shared business text | `string.py` contains MoviePilot-specific media/site/torrent normalization; generic text primitives stay in `app/foundation/text.py` |

`app/domain` may depend only on schemas and foundation. It must not read global
settings, access DB/network/filesystem adapters, import Rust, discover services
or initialize process runtime state.

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

## Existing Chain, Module and DB Layers

### Chain layer

`app/chain/` implements use cases shared by API, CLI, Agent, scheduler and other
entrypoints. Chains may coordinate modules, application services, Oper classes,
events and caches. New chain-to-chain dependencies are allowed only while the
static graph remains acyclic. Backend protocol details and HTTP request objects
do not belong here.

### Module layer

`app/modules/` contains pluggable downloaders, media servers, metadata sources,
message channels, indexers and storage providers. New direct module-to-module or
module-to-chain dependencies are forbidden; cross-module orchestration belongs
in a chain. The directory remains unchanged because discovery and plugin code
depend on this established runtime root.

### DB / Oper layer

SQLAlchemy models stay under `app/db/models/`; `*_oper.py` classes encapsulate
queries. Chains, modules, application services and endpoints use Oper classes
instead of issuing SQLAlchemy queries directly. Every schema change requires an
Alembic migration under `database/versions/`.

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
| `chain -> module / application / Oper / canonical capability` | Allowed |
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
| `app/runtime/config.py` | `ConfigModel`, `Settings` and deployment configuration |
| `app/runtime/events.py` | `EventManager`, `Event` and event resolver registration |
| `app/runtime/extensions/module_manager.py` | Module discovery and lifecycle |
| `app/runtime/extensions/plugin_manager.py` | Plugin discovery and lifecycle |
| `app/foundation/reflection.py` | Generic reflection and Python module discovery |
| `app/adapters/network/http.py` | Shared synchronous and asynchronous HTTP clients |
| `app/application/rss.py` | Configured RSS retrieval and parsing |
| `app/application/site/sites.*` | Generated site catalog, authentication and index capability plus its colocated data bundle |
| `app/runtime/cache.py` | Cache contracts, memory backend, decorators and proxies |
| `app/adapters/cache/backends.py` | Redis and filesystem cache adapters |
| `app/adapters/system/resource.py` | Runtime resource detection/download/installation |
| `app/adapters/external/market.py` | Plugin repository discovery and installation |
| `app/application/security/url.py` | URL/path validation, SSRF protection and signed image policy |
| `app/application/mediaserver.py` | Configured media-server discovery and identity matching |
| `app/runtime/compat/manifest.py` | Exact legacy-to-canonical import manifest |
| `app/sdk/` | Stable plugin imports |

Run `tests/test_architecture_dependencies.py` after every ownership or import
change. It rejects physical legacy or retired canonical sources, forbidden
upward dependencies, SDK/compat backreferences and any strongly connected
component containing a migrated module.

*Last Updated: 2026-08-14*
