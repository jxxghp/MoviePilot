# 04 — Design Patterns

This document defines the structural patterns used across this codebase. When implementing complex features, you are required to use these patterns rather than inventing new abstractions.

---

## 1. Module Pattern (Pluggable Backends)

**When to use:** Adding a new downloader, media server, message channel, storage backend, or any other capability that requires lifecycle management, configuration switches, priority ordering, or independent testing.

**Base class:** `_ModuleBase` in `app/modules/__init__.py`

**Specialized base classes:**
- `_DownloaderBase` — for download clients
- `_MediaServerBase` — for media servers (implied by existing patterns)

**Required methods every module must implement:**

```python
class ExampleModule(_ModuleBase, _DownloaderBase):

    def init_module(self) -> None:
        """模块初始化"""
        super().init_service(service_name=..., service_type=...)

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """返回控制此模块开关的配置项名称和匹配值"""
        return "DOWNLOADER", "example"

    @staticmethod
    def get_name() -> str:
        return "Example"

    @staticmethod
    def get_type() -> ModuleType:
        return ModuleType.Downloader

    @staticmethod
    def get_subtype() -> DownloaderType:
        return DownloaderType.Example

    @staticmethod
    def get_priority() -> int:
        return 1

    def test(self) -> Optional[Tuple[bool, str]]:
        """测试模块连通性"""
        ...

    def stop(self):
        pass
```

**Module directory convention:** `app/modules/<backend_name>/` containing at minimum `__init__.py` (the module class) and the implementation class.

**Module types** are defined in `app/schemas/types.py` as `ModuleType`, `DownloaderType`, `MediaServerType`, `MessageChannel`, `StorageSchema`, `OtherModulesType`. When adding a new category, update these enums.

---

## 2. Chain Orchestration Pattern

**When to use:** Adding a new business workflow that is shared across multiple entrypoints (API endpoint, CLI, agent, scheduler, webhook). Chains coordinate modules, helpers, databases, events, and caches.

**Base class:** `ChainBase` in `app/chain/__init__.py`

**Calling modules from a chain:**

```python
# Preferred: call via run_module / async_run_module
result = self.run_module("method_name", kwarg1=val1, kwarg2=val2)
result = await self.async_run_module("method_name", kwarg1=val1)

# Only use ModuleManager directly when you need to enumerate modules,
# inspect instances, or run health checks.
```

**Chain-to-chain calls:** A chain may call another chain to reuse stable domain logic. Avoid introducing new circular dependencies between chains.

**File convention:** `app/chain/<domain>.py`, class name `<Domain>Chain` (e.g., `DownloadChain`, `SearchChain`, `SubscribeChain`).

---

## 3. Event / Observer Pattern

**When to use:** Triggering cross-cutting reactions (e.g., notifying the media server after a transfer completes, reloading a module after config changes, dispatching user messages to message channels).

**Core classes:** `EventManager` (singleton instance `eventmanager`) and `Event` in `app/runtime/events.py`.

**Registering a handler:**

```python
from app.runtime.events import eventmanager, Event
from app.schemas.types import EventType

@eventmanager.register(EventType.TransferComplete)
def on_transfer_complete(self, event: Event):
    event_data = event.event_data
    ...
```

**Sending an event:**

```python
eventmanager.send_event(EventType.TransferComplete, data_dict)
```

**Event types** are defined as `EventType` and `ChainEventType` enums in `app/schemas/types.py`. Add new event types there when extending the event system.

---

## 4. Repository (Oper) Pattern

**When to use:** Implementing table-oriented persistence behind an Application-owned
Protocol. Never issue SQLAlchemy queries or construct Oper objects directly from
chain, module, endpoint, Agent, scheduler, or workflow code.

**Convention:** Each SQLAlchemy model in `app/db/models/` has a corresponding `<Model>Oper` class in `app/db/oper/<model>.py` — the two packages mirror each other file for file, so the module name carries the entity and the package carries the role.

```
app/db/models/subscribe.py       → app/db/oper/subscribe.py       (SubscribeOper)
app/db/models/systemconfig.py    → app/db/oper/systemconfig.py    (SystemConfigOper)
app/db/models/transferhistory.py → app/db/oper/transferhistory.py (TransferHistoryOper)
```

**Host usage:**

Host entrypoints depend on an Application service or Protocol. The concrete
`app/db/adapters/` implementation creates one operation-scoped Session, adapts the
Protocol with Oper objects, and gives commit/rollback ownership to the Application
command. Oper methods only query, stage, or flush in that caller-owned Session.

```python
from app.application.subscription.write import SubscribeWriter

def create_subscription(writer: SubscribeWriter, identity: dict, payload: dict):
    return writer.add(identity=identity, payload=payload)
```

The explicit Session/Oper composition belongs in `app/db/adapters/`, not at the
host call site:

```python
with SessionFactory() as session:
    command = CreateSubscriptionCommand(
        repository=SubscribeOper(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )
    result = command.execute(identity=identity, payload=payload)
```

The no-Session `SubscribeOper()` facade is legacy plugin ABI only. It may remain
available through the curated SDK/Compat boundary, but new host code and examples
must not copy that form.

---

## 5. Config Reload Pattern

**When to use:** A chain, module, or helper holds a long-lived object that must be rebuilt when specific configuration keys change (e.g., a downloader client reconnects when its host/port changes).

**Mixin:** `ConfigReloadMixin` in `app/runtime/reload.py`

**How it works:**
1. Inherit `ConfigReloadMixin`.
2. Define a `CONFIG_WATCH` class attribute as a set of config key names.
3. Implement `on_config_changed()` — called automatically when any watched key changes.
4. Optionally implement `get_reload_name()` to provide a descriptive name for log messages.

```python
class MyChain(ChainBase, ConfigReloadMixin):

    CONFIG_WATCH = {"DOWNLOADER", "QB_HOST", "QB_PORT"}

    def on_config_changed(self):
        self.init_module()
```

`_ModuleBase` already inherits `ConfigReloadMixin` and calls `init_module()` from `on_config_changed()` by default. Modules typically only need to declare `CONFIG_WATCH`.

---

## 6. Singleton Pattern

**When to use:** Classes that must have exactly one instance shared application-wide (e.g., `EventManager`, `ModuleManager`, `PluginManager`).

**Implementation:** Inherit from `Singleton` in `app/foundation/singleton.py`.

```python
from app.foundation.singleton import Singleton

class MyManager(metaclass=Singleton):
    ...
```

Do not introduce new singletons unless the class genuinely manages global shared state. Prefer dependency injection or parameter passing for everything else.

---

## 7. SystemConfig Pattern

**When to use:** Storing runtime business configuration that is user-editable, persistent across restarts, and not tied to a specific deployment environment.

**Enum:** `SystemConfigKey` in `app/schemas/types.py`

**Host service:** `SystemConfigService` and `get_configured_system_config()` in
`app/application/configuration.py`.

```python
from app.application.configuration import get_configured_system_config
from app.schemas.types import SystemConfigKey

configuration = get_configured_system_config()
value = configuration.get(SystemConfigKey.RssUrls)
configuration.set(SystemConfigKey.RssUrls, ["https://..."])
```

`SystemConfigOper` is the DB/composition implementation and legacy plugin-facing
facade; canonical host consumers do not construct it.

**Rule:** Never use raw string literals as SystemConfig keys. Always add a new entry to the `SystemConfigKey` enum first.

---

## 8. UserConfig Pattern

**When to use:** Per-user settings that must survive across sessions but differ by user.

**Host service:** `UserConfigurationService` and
`get_configured_user_configuration()` in
`app/application/security/userconfig.py`.

The concrete repository uses `UserConfigOper` behind the service boundary. Host
callers do not construct it.

---

## Anti-Patterns to Avoid

| Anti-Pattern | Correct Alternative |
|---|---|
| `module -> chain` coupling | Move orchestration into `chain` and shared logic into its owning canonical package |
| `module -> module` direct calls | Use `chain` to orchestrate cross-module workflows |
| Lower-level module importing a chain or manager | Register a callback/resolver from `app/startup/` or move orchestration to `chain` |
| Raw SQLAlchemy queries or Oper construction in host entrypoints | Define/use an Application Protocol or command; implement it in `app/db/adapters/` with an operation-scoped Session/UoW |
| Raw string keys for SystemConfig | Define and use a `SystemConfigKey` enum entry |
| HTTP requests via `requests` or `httpx` directly | Host code uses `RequestUtils` from `app/adapters/network/http.py`; plugins use `app.sdk.network` |

*Last Updated: 2026-08-27*
