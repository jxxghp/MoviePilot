# 10 — Data and Persistent Management

## Database Models

**Location:** `app/db/models/`

Models are SQLAlchemy declarative classes. Each model maps to one database table.

| Model | Table Domain |
|---|---|
| `Subscribe` | Media subscriptions |
| `SubscribeHistory` | Completed subscription records |
| `TransferHistory` | File transfer history |
| `DownloadHistory` / `DownloadFiles` | Download task history and file list |
| `MediaServerItem` | Media server library item cache |
| `SystemConfig` | Runtime key-value configuration store |
| `UserConfig` | Per-user configuration store |
| `User` | User accounts |
| `Site` / `SiteIcon` / `SiteStatistic` / `SiteUserData` | Torrent site records and statistics |
| `Message` | Message log |
| `PluginData` | Plugin-persisted data |
| `PassKey` | Passkey authentication records |
| `Workflow` | Workflow definitions |

---

## Alembic Migrations

**Location:** `database/versions/`

**Rule:** Any change to a SQLAlchemy model schema (adding a column, renaming a column, changing a column type, adding a table, removing a table) **requires a new Alembic migration script**. Never update models without a corresponding migration.

**Generating a migration:**

```bash
# Auto-generate from model diff
alembic revision --autogenerate -m "describe the change"

# Create a blank migration for manual SQL
alembic revision -m "describe the change"
```

**Review the auto-generated migration before committing** — auto-generation can miss nullable changes, index modifications, or SQLite-incompatible operations.

---

## Data Access Layer (Oper Pattern)

**Location:** `app/db/`

Each model has a corresponding `*_oper.py` file containing the data access class. Do not write SQLAlchemy queries directly in chain, module, or endpoint code.

| Oper Class | File |
|---|---|
| `SubscribeOper` | `subscribe_oper.py` |
| `SystemConfigOper` | `systemconfig_oper.py` |
| `TransferHistoryOper` | `transferhistory_oper.py` |
| `DownloadHistoryOper` | `downloadhistory_oper.py` |
| `MediaServerOper` | `mediaserver_oper.py` |
| `UserOper` | `user_oper.py` |
| `UserConfigOper` | `userconfig_oper.py` |
| `MessageOper` | `message_oper.py` |
| `SiteOper` | `site_oper.py` |
| `PluginDataOper` | `plugindata_oper.py` |
| `WorkflowOper` | `workflow_oper.py` |

**Standard Oper method conventions:**

```python
oper = SubscribeOper()
subscribe = oper.get(sid=1)           # Get by primary key or filter
subscribes = oper.list()              # List all
oper.add(Subscribe(...))              # Insert
oper.update(sid=1, name="New Name")   # Update by key
oper.delete(sid=1)                    # Delete by key
```

---

## SystemConfig — Runtime Configuration

**Purpose:** Runtime business configuration that is user-editable, persisted in the database, and survives application restarts.

**Enum:** `SystemConfigKey` in `app/schemas/types.py`

**Oper:** `SystemConfigOper` in `app/db/systemconfig_oper.py`

```python
from app.schemas.types import SystemConfigKey
from app.db.systemconfig_oper import SystemConfigOper

oper = SystemConfigOper()

# Read
rss_urls = oper.get(SystemConfigKey.RssUrls)

# Write
oper.set(SystemConfigKey.RssUrls, ["https://example.com/rss"])
```

**Rule:** Never use raw string literals as `SystemConfig` keys. Always define a new `SystemConfigKey` enum entry first. Raw string key lookups are not searchable and cannot be refactored safely.

---

## UserConfig — Per-User Configuration

**Purpose:** Settings that differ per user account. Uses `UserConfigOper`.

```python
from app.db.userconfig_oper import UserConfigOper

oper = UserConfigOper()
value = oper.get(user_id=1, key="notification_enabled")
oper.set(user_id=1, key="notification_enabled", value=True)
```

---

## Settings / Environment Configuration

**Purpose:** Deployment-level, environment-level, and startup-time configuration such as ports, paths, proxies, switches, API keys, and third-party service addresses.

**Location:** `ConfigModel` and `Settings` in `app/core/config.py`

These values are read from environment variables (or `.moviepilot.env`) at startup and are immutable at runtime. They are not stored in the database.

**Access:**

```python
from app.core.config import settings

host = settings.QB_HOST
port = settings.QB_PORT
```

---

## Caching

### FileCache / AsyncFileCache

**Location:** `app/core/cache.py`

Used to cache expensive external API responses to disk. Cache entries have a configurable TTL.

```python
from app.core.cache import FileCache, fresh

cache = FileCache(cache_name="tmdb", ttl=3600)

@fresh(cache=cache, key_func=lambda tmdb_id: f"movie_{tmdb_id}")
def get_movie_detail(tmdb_id: int) -> dict:
    return self._tmdb_client.get_movie(tmdb_id)
```

### Redis (Optional)

When `REDIS_HOST` is configured, `app/modules/redis/` provides a distributed cache backend. Prefer `FileCache` for single-node deployments.

---

## Data Lifecycle Rules

- **TransferHistory:** Records are inserted after every successful file transfer. Do not delete records without user confirmation.
- **DownloadHistory:** Records are inserted when a download task is added. Linked `DownloadFiles` records track individual files within a torrent.
- **SystemConfig:** Values may be read and written freely at runtime. Changes to watched config keys trigger `on_config_changed()` on registered classes via `ConfigReloadMixin`.
- **MediaServerItem:** This is a cache of the remote media server library. It is refreshed on media server sync events and can be safely cleared and rebuilt.

---

## Sensitive Data Handling

- Never log database record contents that include personal data (user credentials, passkeys, API tokens).
- `settings.API_TOKEN` and other secret fields must not be included in log output or API responses.
- The `config list --show-secrets` flag exists specifically to gate secret visibility in the CLI.

*Last Updated: 2026-05-25*
