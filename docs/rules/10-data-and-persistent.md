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
| `PluginIdentity` | Installed physical-plugin source binding and payload provenance |
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

Each model has a corresponding file under `app/db/oper/` containing the data access
class, mirroring `app/db/models/` one-for-one. Do not write SQLAlchemy queries
directly in chain, module, or endpoint code.

| Oper Class | File |
|---|---|
| `AgentChatOper` | `oper/agentchat.py` |
| `AgentTaskOper` | `oper/agenttask.py` |
| `DownloadFailureOper` | `oper/downloadfailure.py` |
| `DownloadHistoryOper` | `oper/downloadhistory.py` |
| `MediaServerOper` | `oper/mediaserver.py` |
| `MessageOper` | `oper/message.py` |
| `PluginDataOper` | `oper/plugindata.py` |
| `PluginIdentityOper` | `oper/pluginidentity.py` |
| `SiteOper` | `oper/site.py` |
| `SubscribeHistoryOper` | `oper/subscribehistory.py` |
| `SubscribeOper` | `oper/subscribe.py` |
| `SystemConfigOper` | `oper/systemconfig.py` |
| `TransferHistoryOper` | `oper/transferhistory.py` |
| `TransferPendingOper` | `oper/transferpending.py` |
| `UserConfigOper` | `oper/userconfig.py` |
| `UserOper` | `oper/user.py` |
| `WorkflowOper` | `oper/workflow.py` |

Import by module (`from app.db.oper.subscribe import SubscribeOper`) — that is the
preferred form in this repository. `app/db/oper/__init__.py` also resolves class
names lazily for callers that only want a name, but it deliberately does not
eagerly re-export: several tests isolate a single Oper by stubbing it in
`sys.modules`, and an eager re-export would pull in the other fifteen and bypass
the stub.

Oper classes accept and return persistence values. Turning a `MediaInfo` or
`MetaBase` into a row is business logic and lives in `app/application/`.

Application owns use-case commands and persistence Protocols, but does not import
`app.db`, SQLAlchemy, Session or Oper. Concrete persistence is used in
`app/db/adapters/`: adapters implement those Protocols with explicit Session,
UnitOfWork and Oper objects. `app/startup/composition/` creates and injects the
adapters; it does not retain reusable repository implementations.

### Transaction ownership ratchet

- `tests/fixtures/architecture/transaction-debt-baseline.json` records formal
  decorators in concrete files under `app/db/models/`. Their count is zero and
  must remain zero. Model/Base code may not import `app.db.decorators`; legacy
  Model transaction shells have been removed and must not be recreated.
- Every Model method with a `db` parameter requires an explicit `Session` or
  `AsyncSession`. The parameter may not default to `None`, accept displaced
  business arguments, create a Session, or call `commit()` / `rollback()`.
- `Base.create/get/update/delete/list/truncate` and their async forms are plain
  explicit-session primitives. They only query or stage changes in the caller's
  transaction; they never own transaction lifecycle.
- Host Oper code routes optional-session entry points through
  `_execute_sync_query` / `_execute_async_query` / `_execute_*_write`. Plugins
  access host persistence through Oper or a curated SDK contract, never by
  importing `app.db.models`.
- The public `db_query`, `db_update`, `async_db_query`, and `async_db_update`
  exports remain available only for plugin-owned database functions. They are
  forbidden on host Model/Base methods.
- Oper receives a caller-owned Session and may query, add, update, delete, or
  flush. A composable Oper method must not create its own Session and must not
  commit or roll back.
- API, Scheduler, Agent and Chain consume an injected Application Port; they do
  not import or create a Session. The concrete `app/db/adapters/` implementation
  creates the Session and adapts it through `app/db/uow.py`. Application command
  code decides when the injected UoW commits or rolls back; events, scheduling
  refresh, reports and other external effects run only after a successful commit.
- A synchronous Session is private to one worker thread. An AsyncSession is
  private to one asyncio task/operation; neither may be stored in a process
  singleton or reused by concurrent work.
- Subscription creation is the reference slice:
  `app/application/subscription/write.py` owns the command and persistence Port,
  `app/db/adapters/subscription.py` creates an exclusive Session and adapts Oper/UoW,
  and `app/startup/composition/subscription.py` only wires scopes and post-commit
  callbacks. `SubscribeOper.stage_add()` only queries, adds and flushes. Preserve
  `SubscribeOper.add()` only for legacy SDK callers; new host code must not use
  that auto-commit compatibility path.
- The same rule applies to `SiteMutationCommand`, history/workflow commands,
  `AgentChatService.delete()`, and `DeletePluginDataCommand`: bind the repository
  and UoW to one request/operation Session. Legacy plugin-facing Oper methods may
  remain temporarily, but a new endpoint or startup workflow must call `stage_*`.
- Site reads and standalone writes use `TransactionalSiteRepository`; request
  mutation uses `SessionSiteRepository` bound to the endpoint AsyncSession and
  the request UoW. Both adapters must finish deep DTO projection inside their
  Session. `SiteOper` remains a DB-internal table DAO; plugin compatibility is
  exposed only by the SDK Legacy/Compat mapping.
- User create/update/delete is an aggregate command owned by
  `app/application/security/user.py`. It uses one request AsyncSession/UoW, locks
  active superusers before destructive changes, and rejects removal of the last
  enabled superuser. Query ports return frozen user/auth snapshots rather than
  ORM rows.
- The database is the final user-identity guard: `user.name` is unique;
  `UserConfig.username` cascades on user rename/delete; `PassKey.user_id`
  cascades on user delete; `(UserConfig.username, UserConfig.key)` is unique and
  non-null. A schema change to any of these constraints requires a replay-safe
  migration that repairs legacy duplicates/orphans before creating constraints.
- DownloadHistory and TransferHistory are projected into deeply frozen DTOs
  within the adapter Session. Their typed query/write ports use short
  Session/UoW scopes; TransferHistory additionally exposes a staging port for
  durable settlement inside the caller-owned transaction. Canonical callers
  must not receive raw history ORM rows or Oper objects.

### Durable post-commit side effects

Business mutations that must survive process interruption stage their durable
intent through `OutboxStager` in the same Session/UoW as the business row.
`OutboxDispatchStore` owns separate short transactions for claim, complete and
retry; a business Session must never call those self-committing operations.
`app/db/adapters/outbox.py` implements both roles, and startup composition
supplies the stager, store factory, transaction scope and topic handlers.

Immediate delivery and the dispatcher both claim before execution. Claim is
atomic and complete/retry is fenced by the claimed attempt, so an expired owner
cannot settle a newer lease. `PostCommitResult` separately reports the committed
business value plus completed and pending effects; a post-commit failure cannot
be represented as a rollback of already committed business data.

This boundary is at-least-once, not exactly-once. If an external sink succeeds
and the process stops before complete is persisted, the intent can be replayed.
Event payloads and the host correlation context therefore carry the stable
event key, and consumers that support deduplication should use it. Legacy
notification plugins retain their existing method signature and remain an
at-least-once boundary where duplicate provider delivery is possible. The
dispatcher records bounded retries or dead-letter state.
The shared data-maintenance policy controls bounded terminal-history cleanup,
with user-configurable 30-day
completed and 90-day dead-letter defaults; `0` disables either cleanup. It must
not delete pending or leased processing rows. The `app/runtime/tasks.py`
TaskRegistry is only the owner for in-process work and bounded shutdown waiting;
it is not a durable queue or a replacement for an Outbox/persistent task table.

All append-only or snapshot history owned by the host must participate in the
shared `DATA_CLEANUP_ENABLE` policy when it has a safe time boundary:

- `message`, `downloadhistory` and orphaned `downloadfiles`, `siteuserdata`,
  `transferhistory`, `downloadfailure`, and `subscribehistory` use their own
  user-configurable retention periods.
- `agentchat` removes only expired sessions not referenced by an `agenttask`;
  `agenttaskrun` removes only expired terminal runs that are neither running nor
  the task's current `last_run_id`.
- `outboxmessage` has separate completed and dead-letter retention periods;
  pending and processing intents are recovery state and are never age-deleted.

`transferpending` and `plugininstallation` are recovery queues/journals rather
than history. Their age is not proof that they are disposable, so generic
retention cleanup must not delete them. Current-state tables keyed by a user,
site, plugin, workflow, passkey, or media-library item are likewise outside
time-based cleanup; their owning mutation lifecycle must replace or delete them.

Run `./.venv/bin/python scripts/architecture/baseline.py --check-host` after
persistence changes. A deliberate debt reduction may refresh the low-water mark
with `--write-host`; never refresh it to accept newly introduced debt.

**Canonical explicit-session Oper conventions:**

```python
with SessionFactory() as session:
    oper = SubscribeOper(session)
    subscribe = oper.get(sid=1)       # Query in caller-owned Session
    subscribes = oper.list()          # List in caller-owned Session
    oper.stage_add(Subscribe(...))    # Stage only; caller-owned UoW commits
```

The following no-Session form is legacy plugin ABI only and must not be copied
into host code:

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

**Host service:** `SystemConfigService` and `get_configured_system_config()` in
`app/application/configuration.py`. `SystemConfigOper` is used behind the
composition/persistence boundary and remains available for legacy plugin ABI.

```python
from app.application.configuration import get_configured_system_config
from app.schemas.types import SystemConfigKey

configuration = get_configured_system_config()

# Read
rss_urls = configuration.get(SystemConfigKey.RssUrls)

# Write
configuration.set(SystemConfigKey.RssUrls, ["https://example.com/rss"])
```

**Rule:** Never use raw string literals as `SystemConfig` keys. Always define a new `SystemConfigKey` enum entry first. Raw string key lookups are not searchable and cannot be refactored safely.

---

## UserConfig — Per-User Configuration

**Purpose:** Settings that differ per user account. Host callers use the configured
`UserConfigurationService`; its concrete repository adapts `UserConfigOper` behind
the persistence boundary.

```python
from app.application.security.userconfig import get_configured_user_configuration

configuration = get_configured_user_configuration()
value = configuration.get(username="alice", key="notification_enabled")
configuration.set(username="alice", key="notification_enabled", value=True)
```

The no-Session `UserConfigOper()` form is legacy plugin ABI only and must not be
copied into host code.

`TransactionalUserConfigurationRepository` stages a set in a short transaction
and publishes the process snapshot only after commit. User rename/delete is
first completed by the user aggregate transaction through database cascades;
post-commit publication then acquires the write lock and reloads the database
fact source so concurrent set/rename/delete operations converge on committed
state. If publication fails, the repository reloads that fact source instead of
rolling back or hiding the already committed user mutation. Reads and published
JSON values are copied so callers cannot mutate shared cache state.

---

## Settings / Environment Configuration

**Purpose:** Deployment-level, environment-level, and startup-time configuration such as ports, paths, proxies, switches, API keys, and third-party service addresses.

**Location:** `ConfigModel` and `Settings` in `app/runtime/config.py`

These values are read from environment variables (or `.moviepilot.env`) at startup and are immutable at runtime. They are not stored in the database.

**Access:**

```python
from app.runtime.config import settings

host = settings.QB_HOST
port = settings.QB_PORT
```

---

## Caching

### FileCache / AsyncFileCache

**Location:** `app/runtime/cache.py`

Used to cache expensive external API responses to disk. Cache entries have a configurable TTL.

```python
from app.runtime.cache import FileCache, fresh

cache = FileCache(cache_name="tmdb", ttl=3600)

@fresh(cache=cache, key_func=lambda tmdb_id: f"movie_{tmdb_id}")
def get_movie_detail(tmdb_id: int) -> dict:
    return self._tmdb_client.get_movie(tmdb_id)
```

### Redis (Optional)

When `REDIS_HOST` is configured, `app/modules/redis/` provides a distributed cache backend. Prefer `FileCache` for single-node deployments.

Security-sensitive one-shot state uses `AtomicCacheBackend`, not a concrete
Redis helper. Its strict `store()` surfaces backend write failures and
`consume()` atomically returns-and-removes a value. Both Memory and Redis
backends implement this contract; Passkey receives the capability from startup
through `PasskeyChallengeCache`, so an authentication or registration challenge
can be accepted only once without Application knowing the configured backend.

---

## Data Lifecycle Rules

- **TransferHistory:** Records are inserted after every successful file transfer. Do not delete records without user confirmation.
- **DownloadHistory:** Records are inserted when a download task is added. Linked `DownloadFiles` records track individual files within a torrent. Host query/write callers use frozen DTOs and the typed DownloadHistory port; ORM rows remain inside the adapter Session.
- **SystemConfig:** Values may be read and written freely at runtime. Changes to watched config keys trigger `on_config_changed()` on registered classes via `ConfigReloadMixin`.
- **MediaServerItem:** This is a cache of the remote media server library. It is refreshed on media server sync events and can be safely cleared and rebuilt.

---

## Sensitive Data Handling

- Never log database record contents that include personal data (user credentials, passkeys, API tokens).
- `settings.API_TOKEN` and other secret fields must not be included in log output or API responses.
- The `config list --show-secrets` flag exists specifically to gate secret visibility in the CLI.

*Last Updated: 2026-08-28*
