"""SDK 公开签名引出的类型必须落在插件的合法 import 面内。

``tests/test_plugin_import_boundary.py`` 只判「插件写下的 import 路径合不合法」。一条 import
路径全都合法的插件仍可能写不出来：SDK 导出了 ``eventmanager``，注册事件处理器要给一个
``EventType``，而这个枚举从 ``app.sdk`` 与 ``app.schemas`` 聚合入口都取不到。边界门禁对此
是绿的——三个参考实现恰好没用到它。承诺面缺口不会被「谁写了什么 import」这个判据发现，
它只能由「承诺面自己引出了什么」来发现。

判据：**一个类型必须能从插件的合法 import 面取到，当且仅当插件只使用 SDK 已导出的符号，
就可能收到该类型的值、或必须提供该类型的值。**

按此判据算一个闭包而不是列一份清单：

1. 种子取 ``app/sdk`` 下非下划线模块各自 ``__all__`` 里的符号，这是当前对外承诺面；
2. 逐个读其公开签名——函数的参数与返回标注、类的公开字段与公开方法（含继承来的与
   ``__init__`` 这个构造契约）标注，导出的是实例时读其类；``self``/``cls`` 不计，
   插件手里已经有那个对象；
3. 签名里出现的本仓类型继续按同样方式展开，直到不再有新类型；
4. 减去已可达的：某个 SDK 门面 ``__all__`` 里的、``SCHEMA_EXPORTS`` 里的、经可达外层类取到
   的嵌套类，以及标准库与第三方类型——后者插件本就可以直接 import。

剩下的差集即缺口。标注一律实读，不与任何写死的名单对拍：写死名单只能证明「和上次一样」，
证明不了「现在没有缺口」。标注解析失败按红处理，解析不了就等于没读到，静默跳过会让门禁
在最需要报警的地方最安静。
"""

import ast
import importlib
import inspect
import sys
import typing
from collections import deque
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
SDK_DIR = PROJECT_ROOT / "app" / "sdk"
SDK_ROOT = "app.sdk"
SCHEMA_FACADE = "app.schemas"
# 绑定实参不计入签名：插件调用方法时已经持有该对象，无须能命名它的类型
BOUND_ARGUMENTS = frozenset({"self", "cls"})
# 门面数量下界，防止扫描范围塌成空集后本门禁静默通过
MINIMUM_FACADE_MODULES = 10


def facade_modules() -> list[str]:
    """列出插件可见的 SDK 门面模块。

    下划线开头的模块与包是 SDK 自己的生成物与内部实现，导入边界门禁不放行，
    因而也不构成对插件的承诺。

    :return: 模块全名列表，按名称排序
    """
    names = []
    for path in sorted(SDK_DIR.rglob("*.py")):
        parts = list(path.relative_to(SDK_DIR).with_suffix("").parts)
        if not any(part.startswith("_") for part in parts):
            names.append(f"{SDK_ROOT}." + ".".join(parts))
    return names


@lru_cache(maxsize=None)
def type_checking_namespace(module_name: str) -> dict:
    """解析一个模块 ``if TYPE_CHECKING:`` 块里导入的名字。

    这些名字运行期不在模块全局里，标注却引用它们；不补进来，``get_type_hints`` 会在
    ``PluginDatabaseHandle`` 这类标注上抛 NameError，签名随之整条读不到。

    :param module_name: 模块全名
    :return: ``{绑定名: 对象}``，源码不可读或无此类导入时为空字典
    """
    namespace: dict = {}
    source_file = getattr(sys.modules.get(module_name), "__file__", None)
    if not source_file or not source_file.endswith(".py"):
        return namespace
    try:
        tree = ast.parse(Path(source_file).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return namespace
    for node in ast.walk(tree):
        test = getattr(node, "test", None) if isinstance(node, ast.If) else None
        if test is None:
            continue
        if (getattr(test, "id", None) or getattr(test, "attr", None)) != "TYPE_CHECKING":
            continue
        for statement in ast.walk(node):
            if not isinstance(statement, ast.ImportFrom) or not statement.module:
                continue
            try:
                origin = importlib.import_module(statement.module)
            except ImportError:
                continue
            for alias in statement.names:
                value = getattr(origin, alias.name, None)
                if value is not None:
                    namespace[alias.asname or alias.name] = value
    return namespace


def is_first_party(obj) -> bool:
    """判断一个对象是否由本仓定义。

    :param obj: 类或函数
    :return: 定义在 ``app`` 包内时为 True
    """
    module = getattr(obj, "__module__", "") or ""
    return module == "app" or module.startswith("app.")


def leaf_types(annotation, seen: set | None = None) -> list[type]:
    """把一个标注拆成其中出现的叶子类型。

    ``Optional[List[MetaParseTrace]]`` 要拆到 ``MetaParseTrace``，泛型容器本身也留下——
    插件收到的既可能是元素也可能是容器。

    :param annotation: 已解析的标注对象
    :param seen: 递归去重用的对象标识集合
    :return: 标注涉及的类型列表
    """
    collected: list[type] = []
    seen = set() if seen is None else seen
    if annotation is None or annotation is type(None) or annotation is Ellipsis:
        return collected
    if isinstance(annotation, str):
        return collected
    if id(annotation) in seen:
        return collected
    seen.add(id(annotation))
    arguments = typing.get_args(annotation)
    if arguments:
        for argument in arguments:
            collected.extend(leaf_types(argument, seen))
        origin = typing.get_origin(annotation)
        if isinstance(origin, type):
            collected.append(origin)
    elif isinstance(annotation, type):
        collected.append(annotation)
    return collected


def resolve_hints(obj, owner_modules: tuple[str, ...], failures: list) -> dict:
    """解析一个类或函数的标注。

    ``localns`` 而不是 ``globalns``：类的标注按 MRO 逐层在各自模块的全局名字空间里求值，
    整体换掉全局名字空间会让基类那一层的 ``ClassVar``、``Annotated`` 反而解析不到。

    :param obj: 类或函数
    :param owner_modules: 提供 ``TYPE_CHECKING`` 名字的模块全名
    :param failures: 解析失败的累积记录
    :return: ``{名字: 标注}``；解析失败时为空字典并记入 failures
    """
    localns: dict = {}
    for module_name in owner_modules:
        localns.update(type_checking_namespace(module_name))
    try:
        return typing.get_type_hints(obj, localns=localns)
    except Exception as error:  # noqa: BLE001 - 任何解析失败都要显形
        failures.append(
            f"{'/'.join(owner_modules)} 的 {getattr(obj, '__qualname__', obj)}："
            f"{type(error).__name__}: {error}"
        )
        return {}


def own_functions(cls: type):
    """产出类体内定义、且由本仓实现的公开函数成员。

    ``__init__`` 一并收下：它名字带下划线却不是内部实现，而是构造契约——插件写
    ``ProgressHelper(ProgressKey.Search)`` 就必须能命名那个枚举。其余下划线成员是内部实现。

    只收本仓实现的：pydantic 与 langchain 会往类体里塞自己的方法，那些不是本仓对插件的
    承诺，读它们的标注只会引入第三方类型噪声。

    :param cls: 待检查的类
    :return: ``(成员名, 函数对象)`` 迭代器
    """
    for member_name, member in vars(cls).items():
        if member_name.startswith("_") and member_name != "__init__":
            continue
        function = member
        if isinstance(function, (staticmethod, classmethod)):
            function = function.__func__
        elif isinstance(function, property):
            function = function.fget
        if inspect.isfunction(function) and is_first_party(function):
            yield member_name, function


def signature_sites(obj, failures: list) -> list[tuple[str, object]]:
    """列出一个类或函数的公开签名位置及其标注。

    :param obj: 类或函数
    :param failures: 解析失败的累积记录
    :return: ``(签名位置描述, 标注)`` 列表
    """
    sites: list[tuple[str, object]] = []
    if inspect.isfunction(obj):
        for name, annotation in resolve_hints(obj, (obj.__module__,), failures).items():
            if name not in BOUND_ARGUMENTS:
                sites.append((f"参数/返回 {name}", annotation))
        return sites
    if not inspect.isclass(obj):
        return sites
    lineage = tuple(cls.__module__ for cls in obj.__mro__ if is_first_party(cls))
    for name, annotation in resolve_hints(obj, lineage, failures).items():
        if not name.startswith("_"):
            sites.append((f"字段 {name}", annotation))
    for cls in obj.__mro__:
        if cls is object or not is_first_party(cls):
            continue
        inherited = "" if cls is obj else f"（继承自 {cls.__qualname__}）"
        for member_name, function in own_functions(cls):
            hints = resolve_hints(function, (cls.__module__,), failures)
            sites.extend(
                (f"方法 {member_name}{inherited} 的 {name}", annotation)
                for name, annotation in hints.items()
                if name not in BOUND_ARGUMENTS
            )
    return sites


def sdk_surface() -> dict[int, str]:
    """建立 SDK 门面承诺的对象到其取用位置的反查表。

    :return: ``{对象标识: 取用位置}``
    """
    surface: dict[int, str] = {}
    for module_name in facade_modules():
        module = importlib.import_module(module_name)
        for name in getattr(module, "__all__", ()):
            surface.setdefault(id(getattr(module, name, None)), f"{module_name}.{name}")
    return surface


def schema_surface() -> dict[int, str]:
    """建立 schema 聚合入口能取到的对象到其取用位置的反查表。

    :return: ``{对象标识: 取用位置}``
    """
    from app.schemas.exports import SCHEMA_EXPORTS

    surface: dict[int, str] = {}
    for name, (module_name, symbol) in SCHEMA_EXPORTS.items():
        try:
            value = getattr(importlib.import_module(module_name), symbol)
        except (ImportError, AttributeError):
            continue
        surface.setdefault(id(value), f"{SCHEMA_FACADE}.{name}")
    return surface


def enclosing_access(obj, surface: dict[int, str]) -> str | None:
    """给出嵌套类经其外层类的取用位置。

    ``IncomingMessage.MessageImage`` 随 ``IncomingMessage`` 一起可达，不构成缺口。

    :param obj: 待检查的类
    :param surface: 已可达对象的反查表
    :return: 取用位置；不是嵌套类或外层类不可达时为 None
    """
    path = getattr(obj, "__qualname__", "").split(".")
    if len(path) < 2:
        return None
    owner_module = sys.modules.get(obj.__module__)
    for depth in range(len(path) - 1, 0, -1):
        target = owner_module
        for part in path[:depth]:
            target = getattr(target, part, None)
            if target is None:
                break
        where = surface.get(id(target)) if target is not None else None
        if where:
            return f"{where}.{'.'.join(path[depth:])}"
    return None


@lru_cache(maxsize=None)
def type_closure() -> tuple[dict, list, int]:
    """按 SDK 承诺面实算类型闭包。

    :return: ``({(模块, 限定名): (类型, 来源链)}, 标注解析失败列表, 种子符号数)``
    """
    failures: list = []
    seeds: list[tuple[str, object]] = []
    for module_name in facade_modules():
        module = importlib.import_module(module_name)
        for name in getattr(module, "__all__", ()):
            value = getattr(module, name)
            target = value if inspect.isclass(value) or inspect.isfunction(value) else type(value)
            seeds.append((f"{module_name}.{name}", target))

    pending = deque(seeds)
    visited: set[int] = set()
    collected: dict[tuple[str, str], tuple] = {}
    while pending:
        label, obj = pending.popleft()
        if id(obj) in visited:
            continue
        visited.add(id(obj))
        for site, annotation in signature_sites(obj, failures):
            for leaf in leaf_types(annotation):
                if not is_first_party(leaf):
                    continue
                key = (leaf.__module__, leaf.__qualname__)
                collected.setdefault(key, (leaf, f"{label} 的 {site}"))
                if id(leaf) not in visited:
                    pending.append((f"{leaf.__module__}.{leaf.__qualname__}", leaf))
    return collected, failures, len(seeds)


def remedy(module_name: str) -> str:
    """给出一个不可达类型的两条补法。

    :param module_name: 该类型所在的模块全名
    :return: 面向作者的改法说明
    """
    facade = (
        "(a) 在 app/sdk/ 下相应门面模块补 import 与 __all__ 条目"
        "（无处安放时新建一个门面模块），再运行 python scripts/sdk/exports.py --write"
    )
    if not module_name.startswith(f"{SCHEMA_FACADE}."):
        return facade
    basename = module_name.rpartition(".")[2]
    return (
        f"{facade}；或 (b) 把 {basename!r} 登记进 scripts/schema/exports.py 的 "
        f"SCHEMA_MODULES，再运行 python scripts/schema/exports.py --write，"
        f"让 {SCHEMA_FACADE} 聚合入口取得到"
    )


def test_sdk_public_signatures_only_reference_reachable_types():
    """SDK 公开签名引出的每个本仓类型都必须落在插件的合法 import 面内。"""
    collected, _failures, _seeds = type_closure()
    surface = {**schema_surface(), **sdk_surface()}
    unreachable = [
        f"{module_name}.{qualname} 不可达；来源：{chain}；补法：{remedy(module_name)}"
        for (module_name, qualname), (obj, chain) in sorted(collected.items())
        if id(obj) not in surface and not enclosing_access(obj, surface)
    ]

    assert not unreachable, "\n".join(
        [
            "[承诺面缺口] SDK 公开签名引用了插件取不到的类型：",
            *unreachable,
            "插件只能 import app.sdk 与 app.schemas 聚合入口；"
            "签名里出现却两处都取不到的类型，等于承诺了一件插件写不出来的事。",
        ]
    )


def test_every_annotation_on_the_sdk_surface_resolves():
    """承诺面上的标注必须全部解析得动。

    解析不了就等于没读到，上一条会因此少算一批类型而静默转绿——缺口最可能藏身的地方
    正是标注写法特殊、求值需要额外名字空间的那些签名。
    """
    _collected, failures, _seeds = type_closure()

    assert not failures, "\n".join(
        [
            "[签名读不动] 以下标注无法解析，闭包因此不完整：",
            *failures,
            "补齐求值所需的名字空间，或修正标注，不要让它静默跳过。",
        ]
    )


def test_the_closure_gate_actually_reads_the_promised_surface():
    """闭包必须真的扫到了门面与签名。

    扫描范围塌成空集时上面两条都会通过——一条不读任何签名的规则和没有规则是一回事。
    """
    modules = facade_modules()
    collected, _failures, seeds = type_closure()

    assert len(modules) >= MINIMUM_FACADE_MODULES, f"只扫到 {len(modules)} 个 SDK 门面模块"
    assert f"{SDK_ROOT}.events" in modules, "事件门面不在扫描范围内"
    assert seeds >= len(modules), f"只取到 {seeds} 个种子符号，门面 __all__ 像是空的"
    assert collected, "闭包为空，签名像是一条都没读到"


def test_a_plugin_can_reach_the_event_enums_through_the_sdk_only():
    """只用合法 import 面就要能注册事件处理器。

    事件枚举取不到时，``eventmanager.register`` 这个已导出的符号对插件是不可用的：
    注册要给一个 ``EventType``，而它既不在 SDK 门面上、也不在 schema 聚合入口的清单里。
    本条按插件的实际写法验一次闭环，而不是只验符号存在。
    """
    from app.sdk.events import eventmanager
    from app.sdk.types import EventType

    received: list = []

    def closure_probe_handler(event):
        """记录收到的事件。"""
        received.append(event)

    eventmanager.register(EventType.PluginAction)(closure_probe_handler)
    try:
        registered = {
            item["handler_identifier"]
            for item in eventmanager.visualize_handlers()
            if item["event_type"] == EventType.PluginAction.value
        }
        assert any(
            "closure_probe_handler" in identifier for identifier in registered
        ), f"按 SDK 出口注册的处理器未登记到事件管理器上，当前登记：{sorted(registered)}"
    finally:
        eventmanager.remove_event_listener(EventType.PluginAction, closure_probe_handler)
