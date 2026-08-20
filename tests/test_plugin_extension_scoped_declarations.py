"""同一插件多个实例声明同一标识时的裁决：哪些族去重、哪些族不去重。

扩展有两种彼此正交的「多」：一个插件按配置扇出多个实例，实例之间是各自独立的
行为体；一个插件提供一种新类型，用户要接入多份同类配置时配的是该类型自己的配置。
后者声明的是「本宿主提供这个标识」这件扩展级事实，与该插件建了几个实例无关，
因此同一插件的多个实例声明同一标识只认一次；前者声明的东西各实例各自成立，重复
不构成冲突。

本文件按族固定这条分界：

- 扩展级（去重）：服务实例类型、存储标识、媒体数据源标识、智能体工具名
- 分身级（不去重）：工作流动作、仪表盘、登录认证提供方
"""

from typing import Any, Dict, Iterator, List, Optional

import pytest

from app.agent.tools.base import MoviePilotTool
from app.modules._base.storage import StorageBase
from app.runtime.extensions.declaration import (
    ActionDeclaration,
    AgentToolDeclaration,
    AuthProviderDeclaration,
    DashboardDeclaration,
    MediaSourceDeclaration,
    StorageDeclaration,
)
from app.runtime.extensions.plugin import agent_tool_capabilities, extension_scoped
from app.runtime.extensions.plugin.projection import PluginProjection


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

    def provides_storages(self):
        """返回预置的存储声明。"""
        return self._declarations.get("storages")

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

    def provides_auth_providers(self):
        """返回预置的登录认证提供方声明。"""
        return self._declarations.get("auth_providers")

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


def test_storage_identity_is_extension_scoped():
    """存储后端按标识全局登记、构造时不带实例配置，同标识只认一次。"""
    log = _RecordingLogger()
    projection = _siblings(
        "storages",
        [StorageDeclaration(schema="demo_fs", impl=_DemoStorage)],
        [StorageDeclaration(schema="demo_fs", impl=_DemoStorage)],
        log=log,
    )

    declared = projection.provided_storages()

    assert _counts(declared) == {"DemoPlugin": 1, "DemoPlugin@home": 0}
    assert len(log.warnings) == 1
    assert "存储标识" in log.warnings[0]


def test_distinct_storage_identities_from_siblings_are_kept():
    """不同实例声明不同存储标识各自成立，不受去重影响。"""
    log = _RecordingLogger()
    projection = _siblings(
        "storages",
        [StorageDeclaration(schema="demo_fs", impl=_DemoStorage)],
        [StorageDeclaration(schema="home_fs", impl=_DemoStorage)],
        log=log,
    )

    declared = projection.provided_storages()

    assert _counts(declared) == {"DemoPlugin": 1, "DemoPlugin@home": 1}
    assert log.warnings == []


def test_media_source_identity_is_extension_scoped():
    """媒体数据源声明只承载来源本身的展示信息，同标识重复声明无正当语义。"""
    log = _RecordingLogger()
    projection = _siblings(
        "media_sources",
        [MediaSourceDeclaration(media_source="demosource", name="演示来源")],
        [MediaSourceDeclaration(media_source="demosource", name="演示来源")],
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
        [MediaSourceDeclaration(media_source="demosource", name="演示来源")],
        [MediaSourceDeclaration(media_source="demosource", name="演示来源")],
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


def test_auth_providers_are_not_deduplicated_across_siblings():
    """登录入口的认证由声明它的实例自己完成，多个实例即多个登录入口。"""
    log = _RecordingLogger()
    projection = _siblings(
        "auth_providers",
        [AuthProviderDeclaration(id="demo_login", name="演示登录")],
        [AuthProviderDeclaration(id="demo_login", name="演示登录")],
        log=log,
    )

    declared = projection.provided_auth_providers()

    assert _counts(declared) == {"DemoPlugin": 1, "DemoPlugin@home": 1}
    assert log.warnings == []


def test_auth_provider_entries_carry_their_own_instance_key():
    """两个实例的登录入口各自带实例键，登录流程据此回到正确的实例。"""
    projection = _siblings(
        "auth_providers",
        [AuthProviderDeclaration(id="demo_login", name="演示登录")],
        [AuthProviderDeclaration(id="demo_login", name="演示登录")],
    )

    providers = projection.auth_providers()

    assert [provider["instance_key"] for provider in providers] == [
        "DemoPlugin",
        "DemoPlugin@home",
    ]


def test_instance_precedence_orders_default_instance_first():
    """裁决排序规则：默认实例优先，其余按实例标识升序。"""
    keys = ["Demo@work", "Demo@alt", "Demo"]

    assert sorted(keys, key=extension_scoped.instance_precedence) == [
        "Demo",
        "Demo@alt",
        "Demo@work",
    ]
