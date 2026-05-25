# 05 — Architecture and Modules

## Layer Overview

The application is structured as four distinct layers. Each layer has a defined responsibility, and dependency may only flow in permitted directions.

```
┌──────────────────────────────────────────────────┐
│  Entrypoints                                     │
│  (API Endpoints / CLI / Agent / Scheduler /      │
│   Webhook / Message Interaction)                 │
└────────────────────┬─────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────┐
│  Chain Layer  (app/chain/)                       │
│  Business orchestration: search, download,       │
│  subscribe, transfer, message, recommend, etc.   │
└──────┬──────────────┬───────────────┬────────────┘
       │              │               │
       ▼              ▼               ▼
┌────────────┐  ┌──────────┐  ┌────────────────┐
│  Module    │  │  Helper  │  │  DB / Oper     │
│  Layer     │  │  Layer   │  │  Layer         │
│ (app/      │  │ (app/    │  │ (app/db/)      │
│  modules/) │  │  helper/)│  │                │
└────────────┘  └──────────┘  └────────────────┘
```

---

## Layer Responsibilities and Boundaries

### Entrypoint Layer

**Directories:** `app/api/endpoints/`, `moviepilot` (CLI), `app/agent/`, scheduler callbacks, webhook handlers, message interactions.

**Responsibilities:**
- HTTP concerns: authentication, parameter parsing, response model serialization, streaming adaptation, simple input validation.
- Simple list, detail, toggle, settings read/write, and pure CRUD endpoints may call `app/db/` or a helper directly.
- Any logic that coordinates multiple modules, triggers events, touches caches, or combines workflows must be moved into `chain`.

**Rules:**
- Prefer adding new endpoints to an existing domain file. Create a new endpoint file only when introducing a new top-level resource domain.
- After adding a new endpoint, register it in `app/api/apiv1.py`.
- Endpoints must not contain business logic that belongs in `chain`.

---

### Chain Layer

**Directory:** `app/chain/`

**Responsibilities:**
- Business orchestration shared by API, CLI, agent, scheduler, and other entrypoints.
- Composes module capabilities, helpers, database access, events, and caches.
- Focuses on use cases and workflows.

**Rules:**
- Call module capabilities via `run_module()` or `async_run_module()`. Use `ModuleManager` directly only when enumerating, inspecting, or running health checks.
- Do not hold low-level protocol details, HTTP request objects, or page-specific parameter assembly.
- Before creating a new chain file, verify the workflow is genuinely reused across multiple entrypoints, or coordinates multiple modules. If it is short logic for a single endpoint, keep it in the endpoint.
- Chain-to-chain calls are allowed when reusing stable domain logic. Avoid introducing new circular dependencies.

---

### Module Layer

**Directory:** `app/modules/`

**Responsibilities:**
- Pluggable capability implementations: downloaders, media servers, message channels, metadata sources, storage backends, subtitle backends, filter backends, etc.
- Manages lifecycle (init, stop), configuration switches, priority ordering, and independent testability.

**Module categories (defined in `app/schemas/types.py`):**

| Enum | Examples |
|---|---|
| `ModuleType.Downloader` | qBittorrent, Transmission, rTorrent |
| `ModuleType.MediaServer` | Emby, Jellyfin, Plex, TrimMedia, Zspace, Ugreen |
| `ModuleType.MessageChannel` | Telegram, WeChat, Feishu, Slack, Discord |
| `ModuleType.MetaData` | TMDB, TheTVDB, Douban, Bangumi, Fanart |
| `ModuleType.Indexer` | Site-specific torrent indexers |
| `ModuleType.Storage` | Alist, rclone, u115, local storage |

**Rules:**
- A module must focus on one backend or one capability. It returns domain result objects, not HTTP responses, and must not depend on FastAPI request objects or endpoint auth.
- Do not add direct `module → module` coupling for new code. Cross-module orchestration must go through `chain`.
- Do not expand the historical `module → chain` usage pattern. If a module needs shared business logic, move that logic into `chain` or down into `helper`.

---

### Helper Layer

**Directory:** `app/helper/`

**Responsibilities:**
- Reusable low-level support: path handling, config aggregation, site index loading, protocol wrappers, rate limiting, cache utilities, page parsing, notification helpers.

**Rules:**
- Add a new helper only when the logic is reused in multiple places, or it is clearly a standalone low-level concern.
- If logic is used only by a single chain or module, keep it in the original file. Do not turn `helper` into a dumping ground.
- If the code needs configuration switches, runtime loading, priorities, or multi-implementation dispatch, it is a `module`, not a `helper`.
- `helper` must not contain full business workflows.

---

### DB / Oper Layer

**Directory:** `app/db/`

**Responsibilities:**
- SQLAlchemy models under `app/db/models/`.
- Data access wrappers (`*_oper.py`) that encapsulate all database queries.

**Rules:**
- Never issue SQLAlchemy queries directly from chain, module, or endpoint code. Always use the corresponding `*_oper.py` class.
- Any schema change requires a new Alembic migration under `database/versions/`.

---

## Permitted Call Directions

| Direction | Status |
|---|---|
| `endpoint / CLI / agent / scheduler → chain` | ✅ Preferred |
| `endpoint / CLI / agent / scheduler → db / helper` | ✅ Allowed for simple CRUD and input normalization only |
| `chain → chain` | ✅ Allowed when reusing stable, non-circular domain logic |
| `chain → module` | ✅ Via `run_module()` / `async_run_module()` |
| `chain → helper` | ✅ Allowed |
| `chain → db` | ✅ Via `*_oper.py` classes |
| `module → chain` | ⚠️ Exists in legacy code; do not expand in new code |
| `module → module` | ❌ Forbidden in new code |
| `helper → chain` | ❌ Forbidden |
| `helper → endpoint` | ❌ Forbidden |

---

## Key File Locations

| Path | Purpose |
|---|---|
| `app/api/apiv1.py` | API router registration — register new endpoints here |
| `app/core/config.py` | `ConfigModel` and `Settings` — all deployment/env-level config |
| `app/schemas/types.py` | `SystemConfigKey`, `EventType`, `ModuleType`, and all shared enums |
| `app/core/module.py` | `ModuleManager` — discovers and manages module instances |
| `app/core/plugin.py` | `PluginManager` — discovers and manages plugin instances |
| `app/core/event.py` | `EventManager` + `Event` — the application event bus |
| `app/core/context.py` | `Context`, `MediaInfo`, `TorrentInfo` — shared domain context objects |
| `app/main.py` | Application startup and FastAPI instance |
| `database/versions/` | Alembic migration scripts |

---

## Where New Capabilities Go

| Scenario | Action |
|---|---|
| New business workflow shared by multiple entrypoints | `app/chain/` |
| New downloader, media server, message channel, or storage backend | `app/modules/<backend>/` |
| New public HTTP API endpoint | `app/api/endpoints/`, register in `app/api/apiv1.py` |
| New low-level utility reused in multiple places | `app/helper/` |
| New deployment/env/startup config (ports, paths, API keys) | `ConfigModel` in `app/core/config.py` |
| New runtime business config, user-editable rule, or persistent system option | `SystemConfigKey` + `SystemConfigOper` |
| Config change should reload a long-lived object | Add `CONFIG_WATCH` + `on_config_changed()` to the relevant class |
| Few dozen lines of private logic in one chain or module | Private function in the same file; do not create a new helper |
| New module category or subtype | Also update `app/schemas/types.py` |

*Last Updated: 2026-05-25*
