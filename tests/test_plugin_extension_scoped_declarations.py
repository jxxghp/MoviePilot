"""同一插件多个实例声明同一标识时的裁决：哪些族去重、哪些族不去重。

扩展有两种彼此正交的「多」：一个插件按配置扇出多个实例，实例之间是各自独立的
行为体；一个插件提供一种新类型，用户要接入多份同类配置时配的是该类型自己的配置。
后者声明的是「本宿主提供这个标识」这件扩展级事实，与该插件建了几个实例无关，
因此同一插件的多个实例声明同一标识只认一次；前者声明的东西各实例各自成立，重复
不构成冲突。

本文件按族固定这条分界：

- 扩展级（去重）：服务实例类型、存储标识、媒体数据源标识、智能体工具名
- 分身级（不去重）：工作流动作、仪表盘、分身级旧钩子声明的登录入口
"""

import ast
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

from app.agent.tools.base import MoviePilotTool
from app.modules._base.storage import StorageBase
from app.sdk.extension import _PluginBase
from app.runtime.extensions.contract.declaration import (
    ActionDeclaration,
    AgentToolDeclaration,
    DashboardDeclaration,
    MediaSourceDeclaration,
    ModuleDeclaration,
    ServiceInstanceDeclaration,
)
from app.runtime.extensions.admission import agent_tool as agent_tool_capabilities, extension_scoped
from app.runtime.extensions.projection import plugin as projection_module
from app.runtime.extensions.projection.plugin import PluginProjection
from app.schemas.notification import ChannelCapabilities


class _DemoStorage(StorageBase):
    """契约合规的存储后端桩，全部抽象方法均已落地。"""

    def init_storage(self):
        """无需建立任何连接。"""

    def check(self) -> bool:
        """存储始终可用。"""
        return True

    def list(self, fileitem):
        """返回空列表。"""
        return []

    def create_folder(self, fileitem, name):
        """不提供实际创建。"""
        return None

    def get_folder(self, path):
        """不提供实际查询。"""
        return None

    def get_item(self, path):
        """不提供实际查询。"""
        return None

    def delete(self, fileitem) -> bool:
        """不提供实际删除。"""
        return True

    def rename(self, fileitem, name) -> bool:
        """不提供实际重命名。"""
        return True

    def download(self, fileitem, path=None):
        """不提供实际下载。"""
        return None

    def upload(self, fileitem, path, new_name=None):
        """不提供实际上传。"""
        return None

    def detail(self, fileitem):
        """不提供实际查询。"""
        return None

    def copy(self, fileitem, path, new_name) -> bool:
        """不提供实际复制。"""
        return True

    def move(self, fileitem, path, new_name) -> bool:
        """不提供实际移动。"""
        return True

    def link(self, fileitem, target_file) -> bool:
        """不提供实际硬链接。"""
        return True

    def softlink(self, fileitem, target_file) -> bool:
        """不提供实际软链接。"""
        return True

    def usage(self):
        """不提供实际用量查询。"""
        return None


class _DemoTool(MoviePilotTool):
    """契约合规的智能体工具桩。"""

    name: str = "demo_tool"
    description: str = "A demo tool."

    async def run(self, **kwargs) -> str:
        """返回固定结果。"""
        return "ok"


class _OtherTool(_DemoTool):
    """工具名不同的智能体工具桩。"""

    name: str = "other_tool"
    description: str = "Another demo tool."


def _demo_detail(**kwargs):
    """媒体数据源声明用的最小实现桩。"""
    return None


def _demo_action(context, **kwargs):
    """工作流动作实现桩，原样返回上下文。"""
    return True, context


class _DeclaringPlugin:
    """按钩子名交出预置声明的插件桩。"""

    plugin_name = "声明插件"

    def __init__(self, **declarations: Any):
        """保存各钩子的声明列表。"""
        self._declarations = declarations

    def get_state(self) -> bool:
        """插件桩始终处于启用状态。"""
        return True

    def get_name(self) -> str:
        """返回插件展示名。"""
        return self.plugin_name

    def provides_service_instances(self):
        """返回预置的服务实例类型声明。"""
        return self._declarations.get("service_instances")

    def provides_media_sources(self):
        """返回预置的媒体数据源声明。"""
        return self._declarations.get("media_sources")

    def provides_agent_tools(self):
        """返回预置的智能体工具声明。"""
        return self._declarations.get("agent_tools")

    def provides_actions(self):
        """返回预置的工作流动作声明。"""
        return self._declarations.get("actions")

    def provides_dashboards(self):
        """返回预置的仪表盘声明。"""
        return self._declarations.get("dashboards")

    def get_auth_providers(self):
        """返回预置的分身级登录入口描述。"""
        return self._declarations.get("auth_providers")

    def provides_modules(self):
        """返回预置的模块方法表声明。"""
        return self._declarations.get("modules")

    def provides_channel_capabilities(self):
        """返回预置的消息渠道能力声明。"""
        return self._declarations.get("channel_capabilities")

    def get_dashboard(self, key=None, **kwargs):
        """仪表盘取用钩子，元信息投影只检查它是否存在。"""
        return None


class _RecordingLogger:
    """记录告警与错误文本的日志端口替身。"""

    def __init__(self):
        self.warnings: List[str] = []
        self.errors: List[str] = []

    def warning(self, message: str) -> None:
        """记录一条告警。"""
        self.warnings.append(message)

    def error(self, message: str) -> None:
        """记录一条错误。"""
        self.errors.append(message)

    def info(self, message: str) -> None:
        """记录一条信息，用例不关心其内容。"""


@pytest.fixture(autouse=True)
def _clean_extension_scoped_warnings() -> Iterator[None]:
    """每个用例前后都清空扩展级去重告警记录，避免用例间互相掩盖。"""
    extension_scoped._extension_scoped_warnings_seen.clear()
    yield
    extension_scoped._extension_scoped_warnings_seen.clear()


@pytest.fixture(autouse=True)
def _isolate_agent_tool_base() -> Iterator[None]:
    """快照并复原智能体工具基类注入状态，避免测试间相互污染。"""
    original = agent_tool_capabilities._agent_tool_base
    agent_tool_capabilities.configure_agent_tool_base(MoviePilotTool)
    try:
        yield
    finally:
        agent_tool_capabilities._agent_tool_base = original


def _siblings(
    hook: str, first: List[Any], second: List[Any], log: Optional[_RecordingLogger] = None
) -> PluginProjection:
    """构造由默认实例与一个分身组成的能力投影服务。

    :param hook: 声明钩子的短名，与 `_DeclaringPlugin` 的构造参数对应
    :param first: 默认实例的声明列表
    :param second: 分身实例的声明列表
    :param log: 日志端口
    :return: 能力投影服务
    """
    running = {
        "DemoPlugin": _DeclaringPlugin(**{hook: first}),
        "DemoPlugin@home": _DeclaringPlugin(**{hook: second}),
    }
    return PluginProjection(running, log) if log else PluginProjection(running)


def _counts(declared: Dict[str, List[Any]]) -> Dict[str, int]:
    """把投影结果压成实例键到条目数的映射。

    :param declared: 实例键到声明列表的映射
    :return: 实例键到条目数的映射
    """
    return {key: len(items) for key, items in declared.items()}


def _storage_type(storage_type):
    """构造一条存储类型的服务实例声明。

    :param storage_type: 存储标识，同时是类型标识
    :return: 服务实例声明
    """
    return ServiceInstanceDeclaration(
        capability="storage",
        type=storage_type,
        name=storage_type,
        impl=_DemoStorage,
    )


def test_storage_identity_is_extension_scoped():
    """存储类型按标识全局登记、构造时不带实例配置，同标识只认一次。"""
    log = _RecordingLogger()
    projection = _siblings(
        "service_instances",
        [_storage_type("demo_fs")],
        [_storage_type("demo_fs")],
        log=log,
    )

    declared = projection.provided_service_instances()

    assert _counts(declared) == {"DemoPlugin": 1, "DemoPlugin@home": 0}
    assert len(log.warnings) == 1
    assert "服务实例类型" in log.warnings[0]


def test_distinct_storage_identities_from_siblings_are_kept():
    """不同实例声明不同存储标识各自成立，不受去重影响。"""
    log = _RecordingLogger()
    projection = _siblings(
        "service_instances",
        [_storage_type("demo_fs")],
        [_storage_type("home_fs")],
        log=log,
    )

    declared = projection.provided_service_instances()

    assert _counts(declared) == {"DemoPlugin": 1, "DemoPlugin@home": 1}
    assert log.warnings == []


def test_media_source_identity_is_extension_scoped():
    """媒体数据源声明只承载来源本身的展示信息，同标识重复声明无正当语义。"""
    log = _RecordingLogger()
    projection = _siblings(
        "media_sources",
        [MediaSourceDeclaration(
            media_source="demosource",
            name="演示来源",
            methods={"media_detail": _demo_detail},
        )],
        [MediaSourceDeclaration(
            media_source="demosource",
            name="演示来源",
            methods={"media_detail": _demo_detail},
        )],
        log=log,
    )

    declared = projection.provided_media_sources()

    assert _counts(declared) == {"DemoPlugin": 1, "DemoPlugin@home": 0}
    assert len(log.warnings) == 1
    assert "媒体数据源标识" in log.warnings[0]


def test_media_source_list_carries_one_entry_per_identity():
    """去重后的来源列表里同一标识只出现一次，归属胜出的实例。"""
    projection = _siblings(
        "media_sources",
        [MediaSourceDeclaration(
            media_source="demosource",
            name="演示来源",
            methods={"media_detail": _demo_detail},
        )],
        [MediaSourceDeclaration(
            media_source="demosource",
            name="演示来源",
            methods={"media_detail": _demo_detail},
        )],
    )

    sources = projection.media_sources()

    assert [source["media_source"] for source in sources] == ["demosource"]
    assert sources[0]["plugin_id"] == "DemoPlugin"


def test_agent_tool_name_is_extension_scoped():
    """工具在智能体侧按工具名寻址，重名会被判为身份歧义，故同名只认一次。"""
    log = _RecordingLogger()
    projection = _siblings(
        "agent_tools",
        [AgentToolDeclaration(impl=_DemoTool)],
        [AgentToolDeclaration(impl=_DemoTool)],
        log=log,
    )

    declared = projection.provided_agent_tools()

    assert _counts(declared) == {"DemoPlugin": 1, "DemoPlugin@home": 0}
    assert len(log.warnings) == 1
    assert "智能体工具名" in log.warnings[0]
    assert "demo_tool" in log.warnings[0]


def test_distinct_agent_tool_names_from_siblings_are_kept():
    """不同实例声明不同工具名各自成立，智能体两个工具都能用。"""
    projection = _siblings(
        "agent_tools",
        [AgentToolDeclaration(impl=_DemoTool)],
        [AgentToolDeclaration(impl=_OtherTool)],
    )

    declared = projection.provided_agent_tools()

    assert _counts(declared) == {"DemoPlugin": 1, "DemoPlugin@home": 1}


def test_actions_are_not_deduplicated_across_siblings():
    """动作实现绑定在声明它的实例上，同一动作标识由各实例各自提供是分身语义。"""
    log = _RecordingLogger()
    projection = _siblings(
        "actions",
        [ActionDeclaration(action_id="sync", name="同步", impl=_demo_action)],
        [ActionDeclaration(action_id="sync", name="同步", impl=_demo_action)],
        log=log,
    )

    declared = projection.provided_actions()

    assert _counts(declared) == {"DemoPlugin": 1, "DemoPlugin@home": 1}
    assert log.warnings == []


def test_action_groups_stay_per_instance():
    """动作按实例分组产出，调用方据此选择走哪一个实例的实现。"""
    projection = _siblings(
        "actions",
        [ActionDeclaration(action_id="sync", name="同步", impl=_demo_action)],
        [ActionDeclaration(action_id="sync", name="同步", impl=_demo_action)],
    )

    actions = projection.actions()

    assert [group["plugin_id"] for group in actions] == ["DemoPlugin", "DemoPlugin@home"]


def test_dashboards_are_not_deduplicated_across_siblings():
    """仪表盘取用时按实例键加 key 定位，每个实例各有一块是正当配置。"""
    log = _RecordingLogger()
    projection = _siblings(
        "dashboards",
        [DashboardDeclaration(key="overview", name="总览")],
        [DashboardDeclaration(key="overview", name="总览")],
        log=log,
    )

    declared = projection.provided_dashboards()

    assert _counts(declared) == {"DemoPlugin": 1, "DemoPlugin@home": 1}
    assert log.warnings == []


def test_dashboard_metadata_keeps_one_entry_per_instance():
    """两个实例的同 key 仪表盘在元信息里各占一条，靠实例键区分。"""
    projection = _siblings(
        "dashboards",
        [DashboardDeclaration(key="overview", name="总览")],
        [DashboardDeclaration(key="overview", name="总览")],
    )

    metadata = projection.dashboard_metadata()

    assert [item["instance_key"] for item in metadata] == ["DemoPlugin", "DemoPlugin@home"]
    assert {item["key"] for item in metadata} == {"overview"}


def test_legacy_auth_providers_are_not_deduplicated_across_siblings():
    """分身级旧钩子的登录入口由声明它的实例自己完成认证，多个实例即多个入口。

    登录入口本身已并入服务实例族，那条来源按类型登记、按配置扇出，与实例键无关；
    仍留在废弃期里的旧钩子则维持分身级语义不变。
    """
    log = _RecordingLogger()
    projection = _siblings(
        "auth_providers",
        [{"id": "demo_login", "name": "演示登录"}],
        [{"id": "demo_login", "name": "演示登录"}],
        log=log,
    )

    providers = projection.auth_providers()

    assert [provider["instance_key"] for provider in providers] == [
        "DemoPlugin",
        "DemoPlugin@home",
    ]
    assert log.warnings == []


def test_instance_precedence_orders_default_instance_first():
    """裁决排序规则：默认实例优先，其余按实例标识升序。"""
    keys = ["Demo@work", "Demo@alt", "Demo"]

    assert sorted(keys, key=extension_scoped.instance_precedence) == [
        "Demo",
        "Demo@alt",
        "Demo@work",
    ]


def test_modules_are_not_deduplicated_across_siblings():
    """模块方法表挂的是分身的绑定方法，两个分身各挂一份即两套各自成立的实现。"""
    log = _RecordingLogger()
    projection = _siblings(
        "modules",
        [ModuleDeclaration(methods={"media_detail": _demo_detail})],
        [ModuleDeclaration(methods={"media_detail": _demo_detail})],
        log=log,
    )

    declared = projection.provided_modules()

    assert _counts(declared) == {"DemoPlugin": 1, "DemoPlugin@home": 1}
    assert log.warnings == []


def test_module_tables_stay_keyed_by_instance():
    """两个分身的方法表在聚合结果里各占一条，键含实例键因此不会互相覆盖。"""
    projection = _siblings(
        "modules",
        [ModuleDeclaration(methods={"media_detail": _demo_detail})],
        [ModuleDeclaration(methods={"media_detail": _demo_detail})],
    )

    modules = projection.modules()

    assert [key[0] for key in modules] == ["DemoPlugin", "DemoPlugin@home"]


def test_channel_capabilities_are_not_deduplicated_across_siblings():
    """渠道能力登记按分身的生命周期驱动，分身之间不做扩展级去重。"""
    log = _RecordingLogger()
    projection = _siblings(
        "channel_capabilities",
        [ChannelCapabilities(channel="demo_bridge", capabilities=set())],
        [ChannelCapabilities(channel="demo_bridge", capabilities=set())],
        log=log,
    )

    declared = projection.provided_channel_capabilities()

    assert _counts(declared) == {"DemoPlugin": 1, "DemoPlugin@home": 1}
    assert log.warnings == []


def test_channel_capabilities_stay_keyed_by_instance():
    """两个分身各自的渠道能力在聚合结果里按实例键分开列出。"""
    projection = _siblings(
        "channel_capabilities",
        [ChannelCapabilities(channel="demo_bridge", capabilities=set())],
        [ChannelCapabilities(channel="demo_bridge", capabilities=set())],
    )

    assert sorted(projection.channel_capabilities()) == [
        "DemoPlugin",
        "DemoPlugin@home",
    ]


# §7.3 归属表所在文档，表格与代码实际行为须逐族对齐
_ARCHITECTURE_DOC = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "plugin-extension-architecture.md"
)


def _documented_family_levels() -> Dict[str, str]:
    """解析 §7.3 归属表，取出每个声明钩子被文档记为哪一级。

    一行里写了多个钩子时逐个展开；同一钩子出现在多行时各行记法须一致。

    :return: 声明钩子名到级别文案的映射
    """
    levels: Dict[str, str] = {}
    for line in _ARCHITECTURE_DOC.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 4 or cells[2] not in {"扩展级", "分身级"}:
            continue
        for hook in re.findall(r"provides_\w+", cells[1]):
            assert levels.setdefault(hook, cells[2]) == cells[2], (
                f"{hook} 在 §7.3 归属表里被记成了两种级别"
            )
    return levels


def _code_family_levels() -> Dict[str, str]:
    """按投影实现判定每个声明钩子实际落在哪一级。

    走扩展级裁决即扩展级，否则按实例键各自成立即分身级。

    :return: 声明钩子名到级别文案的映射
    """
    source = Path(projection_module.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()
    levels: Dict[str, str] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("provided_"):
            continue
        body = "\n".join(lines[node.lineno - 1:node.end_lineno])
        scoped = any(
            marker in body
            for marker in (
                "elect_extension_scoped",
                "_collect_extension_scoped",
                "_extension_scope",
            )
        )
        levels[node.name.replace("provided_", "provides_", 1)] = (
            "扩展级" if scoped else "分身级"
        )
    return levels


def test_architecture_doc_lists_every_declaration_family():
    """§7.3 归属表不得漏族：基类上的每个 `provides_*` 钩子都要在表里有一行。"""
    documented = set(_documented_family_levels())
    declared = {
        name
        for name in dir(_PluginBase)
        if name.startswith("provides_") and callable(getattr(_PluginBase, name))
    }

    assert declared - documented == set()


def test_architecture_doc_levels_match_projection_behaviour():
    """§7.3 归属表记的级别必须与投影实现的实际行为逐族一致。"""
    documented = _documented_family_levels()
    actual = _code_family_levels()

    assert {
        hook: level for hook, level in documented.items() if hook in actual
    } == actual
