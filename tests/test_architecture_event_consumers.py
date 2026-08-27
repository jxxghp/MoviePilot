"""Event consumer AST collector tests."""

from pathlib import Path

from scripts.architecture.event_consumers import collect_event_consumers

EVENT_MEMBERS = {
    "EventType": ("Alpha", "Beta", "Gamma"),
    "ChainEventType": ("Delta", "Epsilon"),
}


def _collect(
    tmp_path: Path,
    source: str,
    *,
    module_name: str = "app.sample",
) -> tuple[dict[str, list[dict]], list[dict]]:
    """从单个临时宿主模块收集 Event consumer。"""
    path = tmp_path / f"{module_name.replace('.', '_')}.py"
    path.write_text(source, encoding="utf-8")
    return collect_event_consumers({module_name: path}, EVENT_MEMBERS)


def _stable_locations(locations: list[dict]) -> list[dict]:
    """移除仅用于诊断的行号，比较稳定 consumer identity。"""
    return [{key: value for key, value in item.items() if key != "line"} for item in locations]


def test_collect_event_consumers_resolves_canonical_instances_and_aliases(
    tmp_path: Path,
) -> None:
    """Canonical singleton、构造器、模块和简单赋值别名都应可证明。"""
    static, dynamic = _collect(
        tmp_path,
        '''
from app.runtime.events import EventManager as Bus, eventmanager as global_bus
from app.schemas.types import EventType as Events, ChainEventType as ChainEvents
import app.runtime.events as runtime_events
import app.schemas.types as schema_types

alias = global_bus
constructed = Bus()
existing = Bus.get_existing_instance()

def listener(event):
    pass

@alias.register(Events.Alpha, priority=3)
def decorated(event):
    pass

@global_bus.register(etype=ChainEvents.Epsilon)
def keyword_decorated(event):
    pass

constructed.add_event_listener(
    event_type=ChainEvents.Delta,
    handler=listener,
    priority=7,
)
runtime_events.eventmanager.add_event_listener(
    schema_types.EventType.Beta,
    listener,
)
existing.add_event_listener(Events.Gamma, listener, 11)
''',
    )

    assert dynamic == []
    assert _stable_locations(static["EventType.Alpha"]) == [
        {
            "caller": "app.sample",
            "handler": "decorated",
            "identity": "decorator|decorated|3",
            "priority": "3",
            "registration_kind": "decorator",
        }
    ]
    assert _stable_locations(static["EventType.Beta"]) == [
        {
            "caller": "app.sample",
            "handler": "listener",
            "identity": "listener|listener|<default>",
            "priority": "<default>",
            "registration_kind": "listener",
        }
    ]
    assert static["EventType.Gamma"][0]["priority"] == "11"
    assert static["ChainEventType.Delta"][0]["priority"] == "7"
    assert static["ChainEventType.Epsilon"][0]["handler"] == "keyword_decorated"


def test_collect_event_consumers_only_counts_applied_register_decorators(
    tmp_path: Path,
) -> None:
    """register 只有装饰声明或立即应用到 handler 时才形成 consumer。"""
    static, dynamic = _collect(
        tmp_path,
        '''
from app.runtime.events import eventmanager
from app.schemas.types import EventType

unused = eventmanager.register(EventType.Alpha)
used = eventmanager.register(EventType.Beta, priority=4)

@used
def via_alias(event):
    pass

def immediate(event):
    pass

eventmanager.register(EventType.Gamma, priority=8)(immediate)
''',
    )

    assert dynamic == []
    assert "EventType.Alpha" not in static
    assert static["EventType.Beta"][0]["handler"] == "via_alias"
    assert static["EventType.Beta"][0]["registration_kind"] == "decorator"
    assert static["EventType.Gamma"][0]["handler"] == "immediate"
    assert static["EventType.Gamma"][0]["registration_kind"] == "decorator"


def test_collect_event_consumers_expands_enum_and_partial_dynamic_lists(
    tmp_path: Path,
) -> None:
    """register 的 enum/list 静态成员和未知成员必须分别保留。"""
    static, dynamic = _collect(
        tmp_path,
        '''
from app.runtime.events import eventmanager
from app.schemas.types import EventType, ChainEventType

runtime_event = EventType("runtime")
selected = [EventType.Alpha, runtime_event, ChainEventType.Delta]

@eventmanager.register(EventType)
def all_broadcast(event):
    pass

@eventmanager.register(selected, priority=5)
def selected_events(event):
    pass
''',
    )

    assert set(static) == {
        "ChainEventType.Delta",
        "EventType.Alpha",
        "EventType.Beta",
        "EventType.Gamma",
    }
    assert [item["handler"] for item in static["EventType.Alpha"]] == [
        "all_broadcast",
        "selected_events",
    ]
    assert _stable_locations(dynamic) == [
        {
            "caller": "app.sample",
            "handler": "selected_events",
            "identity": "decorator|selected_events|5",
            "priority": "5",
            "registration_kind": "decorator",
        }
    ]


def test_collect_event_consumers_ignores_unknown_receivers_and_internal_delegation(
    tmp_path: Path,
) -> None:
    """同名 API 和 EventManager 实现内部转调都不是独立 consumer 声明。"""
    static, dynamic = _collect(
        tmp_path,
        '''
from app.schemas.types import EventType

registry.register(EventType.Alpha)
registry.add_event_listener(EventType.Beta, handler)

class EventManager:
    def register(self, event):
        self.add_event_listener(event, handler)
''',
        module_name="app.runtime.events",
    )

    assert static == {}
    assert dynamic == []


def test_collect_event_consumers_respects_shadowing_and_rebinding(
    tmp_path: Path,
) -> None:
    """参数遮蔽、局部重绑和模块最终重绑不得伪造 manager provenance。"""
    static, dynamic = _collect(
        tmp_path,
        '''
from app.runtime.events import eventmanager as bus
from app.schemas.types import EventType

@bus.register(EventType.Alpha)
def before_rebind(event):
    pass

def listener(event):
    pass

def shadow(bus):
    bus.add_event_listener(EventType.Beta, listener)

def local_alias():
    local = original_bus
    local.add_event_listener(EventType.Beta, listener)
    local = object()
    local.add_event_listener(EventType.Gamma, listener)

def final_global_binding():
    bus.add_event_listener(EventType.Gamma, listener)

original_bus = bus
bus = object()
''',
    )

    assert dynamic == []
    assert set(static) == {"EventType.Alpha", "EventType.Beta"}
    assert static["EventType.Alpha"][0]["handler"] == "before_rebind"
    assert static["EventType.Beta"][0]["handler"] == "listener"


def test_collect_event_consumers_handles_type_checking_runtime_else(
    tmp_path: Path,
) -> None:
    """TYPE_CHECKING body 只用于类型，else 才是运行期 provenance。"""
    static, dynamic = _collect(
        tmp_path,
        '''
from typing import TYPE_CHECKING
from app.schemas.types import EventType

if TYPE_CHECKING:
    from app.runtime.events import eventmanager as type_bus
else:
    from app.runtime.events import eventmanager as runtime_bus

def listener(event):
    pass

type_bus.add_event_listener(EventType.Alpha, listener)
runtime_bus.add_event_listener(EventType.Beta, listener)
''',
    )

    assert dynamic == []
    assert set(static) == {"EventType.Beta"}


def test_collect_event_consumers_requires_canonical_type_checking_provenance(
    tmp_path: Path,
) -> None:
    """只有未遮蔽的 typing.TYPE_CHECKING 才能裁剪运行期分支。"""
    static, dynamic = _collect(
        tmp_path,
        '''
from typing import TYPE_CHECKING as TC
from app.runtime.events import eventmanager
from app.schemas.types import EventType

def type_only(event):
    pass

def runtime_only(event):
    pass

def shadowed_only(event):
    pass

def rebound_only(event):
    pass

if TC:
    eventmanager.add_event_listener(EventType.Alpha, type_only)
else:
    eventmanager.add_event_listener(EventType.Beta, runtime_only)

def shadowed(TC):
    if TC:
        eventmanager.add_event_listener(EventType.Gamma, shadowed_only)

TYPE_CHECKING = True
if TYPE_CHECKING:
    eventmanager.add_event_listener(EventType.Alpha, rebound_only)
''',
    )

    assert dynamic == []
    assert set(static) == {
        "EventType.Alpha",
        "EventType.Beta",
        "EventType.Gamma",
    }
    assert [item["handler"] for item in static["EventType.Alpha"]] == [
        "rebound_only"
    ]
    assert static["EventType.Beta"][0]["handler"] == "runtime_only"
    assert static["EventType.Gamma"][0]["handler"] == "shadowed_only"


def test_collect_event_consumers_respects_compound_scope_targets(
    tmp_path: Path,
) -> None:
    """循环、异常和推导式目标必须遮蔽同名 EventManager alias。"""
    static, dynamic = _collect(
        tmp_path,
        '''
from app.runtime.events import eventmanager as bus
from app.schemas.types import EventType

def listener(event):
    pass

def scoped(values):
    local = bus
    for local in values:
        local.add_event_listener(EventType.Alpha, listener)
    local.add_event_listener(EventType.Beta, listener)

    try:
        raise RuntimeError
    except RuntimeError as bus:
        bus.add_event_listener(EventType.Alpha, listener)

    [bus.add_event_listener(EventType.Beta, listener) for bus in values]

bus.add_event_listener(EventType.Gamma, listener)
''',
    )

    assert dynamic == []
    assert set(static) == {"EventType.Gamma"}


def test_collect_event_consumers_merges_try_exception_entry_states(
    tmp_path: Path,
) -> None:
    """异常 handler 必须从 try 内所有可能抛出点的合并状态开始。"""
    static, dynamic = _collect(
        tmp_path,
        '''
from app.runtime.events import eventmanager
from app.schemas.types import EventType

class Other:
    def add_event_listener(self, *args):
        pass

def listener(event):
    pass

bus = eventmanager
try:
    bus = Other()
    raise RuntimeError
except RuntimeError:
    bus.add_event_listener(EventType.Alpha, listener)

bus = eventmanager
try:
    raise RuntimeError
except RuntimeError:
    bus.add_event_listener(EventType.Beta, listener)
''',
    )

    assert dynamic == []
    assert set(static) == {"EventType.Beta"}


def test_collect_event_consumers_models_while_with_match_and_delete_bindings(
    tmp_path: Path,
) -> None:
    """复合语句目标和 del 必须遵循 Python 的绑定及路径语义。"""
    static, dynamic = _collect(
        tmp_path,
        '''
from contextlib import nullcontext
from app.runtime.events import eventmanager as bus
from app.schemas.types import EventType

global_bus = bus

class Other:
    def add_event_listener(self, *args):
        pass

def listener(event):
    pass

def scoped(context):
    bus = Other()
    while False:
        bus = global_bus
    bus.add_event_listener(EventType.Alpha, listener)

    bus = global_bus
    with nullcontext(Other()) as bus:
        bus.add_event_listener(EventType.Alpha, listener)
    bus.add_event_listener(EventType.Alpha, listener)

    bus = global_bus
    match Other():
        case bus:
            bus.add_event_listener(EventType.Alpha, listener)

    bus = global_bus
    while context:
        bus.add_event_listener(EventType.Beta, listener)
        break
    with nullcontext():
        bus.add_event_listener(EventType.Gamma, listener)

async def async_scoped(context):
    bus = global_bus
    async with context as bus:
        bus.add_event_listener(EventType.Alpha, listener)

def deleted_name_is_local():
    global_bus.add_event_listener(EventType.Beta, listener)
    bus.add_event_listener(EventType.Alpha, listener)
    del bus
''',
    )

    assert dynamic == []
    assert set(static) == {"EventType.Beta", "EventType.Gamma"}
    assert len(static["EventType.Beta"]) == 2


def test_collect_event_consumers_writes_walrus_to_containing_scope(
    tmp_path: Path,
) -> None:
    """推导式海象绑定属于包含函数，普通外层 alias 仍可读取。"""
    static, dynamic = _collect(
        tmp_path,
        '''
from app.runtime.events import eventmanager as bus
from app.schemas.types import EventType

global_bus = bus

def listener(event):
    pass

def rebound(values):
    [(bus := object()) for value in values]
    bus.add_event_listener(EventType.Alpha, listener)

def retained(values):
    bus = global_bus
    [bus.add_event_listener(EventType.Beta, listener) for value in values]

def eager_iterable():
    [value for value in ((bus := global_bus),)]
    bus.add_event_listener(EventType.Gamma, listener)
''',
    )

    assert dynamic == []
    assert set(static) == {"EventType.Beta", "EventType.Gamma"}


def test_collect_event_consumers_uses_late_bound_function_closures(
    tmp_path: Path,
) -> None:
    """嵌套函数和 lambda 必须读取包含函数的最终 cell provenance。"""
    static, dynamic = _collect(
        tmp_path,
        '''
from app.runtime.events import eventmanager as global_bus
from app.schemas.types import EventType

def listener(event):
    pass

def rebound():
    bus = global_bus
    def nested():
        bus.add_event_listener(EventType.Alpha, listener)
    callback = lambda: bus.add_event_listener(EventType.Alpha, listener)
    bus = object()
    return nested, callback

def retained():
    bus = global_bus
    def nested():
        bus.add_event_listener(EventType.Beta, listener)
    callback = lambda: bus.add_event_listener(EventType.Gamma, listener)
    return nested, callback
''',
    )

    assert dynamic == []
    assert set(static) == {"EventType.Beta", "EventType.Gamma"}


def test_collect_event_consumers_validates_decorator_factory_application(
    tmp_path: Path,
) -> None:
    """保存后的 factory 仅在合法单参数应用时形成 consumer。"""
    static, dynamic = _collect(
        tmp_path,
        '''
from app.runtime.events import eventmanager
from app.schemas.types import EventType

def positional(event):
    pass

def keyword(event):
    pass

factory = eventmanager.register(EventType.Beta, priority=4)
factory(positional)
eventmanager.register(EventType.Gamma)(f=keyword)

unused = eventmanager.register(EventType.Alpha)
unused()
unused(handler=positional)
unused(positional, keyword)
unused(positional, f=keyword)
''',
    )

    assert dynamic == []
    assert set(static) == {"EventType.Beta", "EventType.Gamma"}
    assert static["EventType.Beta"][0]["handler"] == "positional"
    assert static["EventType.Gamma"][0]["handler"] == "keyword"


def test_collect_event_consumers_validates_real_event_manager_signatures(
    tmp_path: Path,
) -> None:
    """两种注册 API 只接受各自真实且完整的参数绑定。"""
    static, dynamic = _collect(
        tmp_path,
        '''
from app.runtime.events import eventmanager
from app.schemas.types import EventType

def listener(event):
    pass

eventmanager.add_event_listener(
    event_type=EventType.Alpha,
    handler=listener,
    priority=3,
)

@eventmanager.register(etype=EventType.Beta, priority=4)
def decorated(event):
    pass

eventmanager.add_event_listener(etype=EventType.Gamma, handler=listener)
eventmanager.add_event_listener(EventType.Gamma)
eventmanager.add_event_listener(
    EventType.Gamma,
    listener,
    handler=listener,
)
eventmanager.add_event_listener(EventType.Gamma, listener, unknown=True)

@eventmanager.register(event_type=EventType.Gamma)
def wrong_keyword(event):
    pass

@eventmanager.register()
def missing_event(event):
    pass
''',
    )

    assert dynamic == []
    assert set(static) == {"EventType.Alpha", "EventType.Beta"}
    assert static["EventType.Alpha"][0]["priority"] == "3"
    assert static["EventType.Beta"][0]["priority"] == "4"


def test_collect_event_consumers_keeps_only_proven_dynamic_registrations(
    tmp_path: Path,
) -> None:
    """只有 receiver 已证明而事件值未知时才进入 dynamic facts。"""
    static, dynamic = _collect(
        tmp_path,
        '''
from app.runtime.events import eventmanager
from app.schemas.types import EventType

def register_runtime(name, handler):
    event_type = EventType(name)
    eventmanager.add_event_listener(event_type=event_type, handler=handler)
    unrelated.add_event_listener(event_type, handler)
    unrelated.register(event_type)(handler)
''',
    )

    assert static == {}
    assert len(dynamic) == 1
    assert dynamic[0]["caller"] == "app.sample"
    assert dynamic[0]["handler"] == "handler"
    assert dynamic[0]["registration_kind"] == "listener"


def test_collect_event_consumers_excludes_plugin_modules(tmp_path: Path) -> None:
    """即使调用方误传插件模块，collector 也不得读取其注册事实。"""
    host_path = tmp_path / "host.py"
    plugin_path = tmp_path / "plugin.py"
    source = '''
from app.runtime.events import eventmanager
from app.schemas.types import EventType

@eventmanager.register(EventType.Alpha)
def handler(event):
    pass
'''
    host_path.write_text(source, encoding="utf-8")
    plugin_path.write_text(source.replace("Alpha", "Beta"), encoding="utf-8")

    static, dynamic = collect_event_consumers(
        {
            "app.host": host_path,
            "app.plugins.sample": plugin_path,
        },
        EVENT_MEMBERS,
    )

    assert dynamic == []
    assert set(static) == {"EventType.Alpha"}
