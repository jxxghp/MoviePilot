"""插件声明媒体数据源链路测试：展示信息与实现的完整性校验、source 路由、
实例键归属与旧钩子并存。"""

from typing import Iterator, List, Optional

import pytest

from app.foundation.singleton import Singleton
from app.runtime.deprecation import notices as notices_module
from app.runtime.deprecation import policy as deprecation_policy
from app.runtime.deprecation.notices import DeprecationNotice, DeprecationStage
from app.runtime.extensions.contract.declaration import MediaSourceDeclaration
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.plugin_manager import PluginManager


def _detail(**kwargs):
    """媒体数据源声明用的最小实现桩，回显收到的调用参数。"""
    return kwargs


@pytest.fixture(autouse=True)
def _clean_deprecation_warned() -> Iterator[None]:
    """每个用例前后都清空废弃告警去重记录，避免用例间互相掩盖。"""
    deprecation_policy.reset_warned()
    yield
    deprecation_policy.reset_warned()


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def _set_notice_stage(monkeypatch, key: str, stage: DeprecationStage) -> None:
    """把指定废弃标识的登记替换为指定阶段的副本，其余登记原样保留。"""
    original = notices_module.NOTICES[key]
    updated = dict(notices_module.NOTICES)
    updated[key] = DeprecationNotice(
        key=original.key,
        subject=original.subject,
        stage=stage,
        since=original.since,
        remove_in=original.remove_in,
        replacement=original.replacement,
        reason=original.reason,
    )
    monkeypatch.setattr(notices_module, "NOTICES", updated)
    monkeypatch.setattr(deprecation_policy, "NOTICES", updated)


class _CapableMediaSourcePlugin:
    """声明媒体数据源的最小插件桩，用于直接驱动 PluginProjection。"""

    plugin_name = "数据源插件"

    def __init__(self, enabled=True, declarations=None, raise_error=False):
        self._enabled = enabled
        self._declarations = declarations
        self._raise_error = raise_error

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def provides_media_sources(self):
        """返回声明的媒体数据源，或按需抛出异常模拟插件实现出错。"""
        if self._raise_error:
            raise RuntimeError("声明媒体数据源时出错")
        return self._declarations


def test_projection_accepts_valid_declaration():
    """契约合规的声明应被接受，字段原样保留。"""
    plugin = _CapableMediaSourcePlugin(
        declarations=[
            MediaSourceDeclaration(
                media_source="acme.video",
                name="Acme Video",
                media_types=("电影", "电视剧"),
                methods={"media_detail": _detail},
            )
        ]
    )
    projection = PluginProjection({"DemoSource": plugin})

    declared = projection.provided_media_sources()

    assert len(declared["DemoSource"]) == 1
    accepted = declared["DemoSource"][0]
    assert accepted.media_source == "acme.video"
    assert accepted.name == "Acme Video"


def test_projection_accepts_bare_dict_without_wrapper():
    """插件直接交出描述字典而不包 MediaSourceDeclaration 的兼容写法应被接受。"""
    raw = {
        "name": "Acme Video",
        "media_source": "acme.video",
        "methods": {"media_detail": _detail},
    }
    plugin = _CapableMediaSourcePlugin(declarations=[raw])
    projection = PluginProjection({"DemoSource": plugin})

    declared = projection.provided_media_sources()

    assert declared["DemoSource"] == [raw]


@pytest.mark.parametrize(
    "declaration",
    [
        MediaSourceDeclaration(
            media_source="acme.video", name="", methods={"media_detail": _detail}
        ),
        MediaSourceDeclaration(
            media_source="", name="Acme Video", methods={"media_detail": _detail}
        ),
        MediaSourceDeclaration(
            media_source="Not Valid!!", name="Acme Video", methods={"media_detail": _detail}
        ),
        MediaSourceDeclaration(
            media_source="acme.video",
            name="Acme Video",
            media_types=(1, 2),
            methods={"media_detail": _detail},
        ),
        MediaSourceDeclaration(
            media_source="acme.video", name="Acme Video", methods={"media_detail": None}
        ),
    ],
    ids=[
        "name_empty",
        "media_source_empty",
        "media_source_invalid",
        "media_types_not_str",
        "method_not_callable",
    ],
)
def test_projection_rejects_declaration_violations(declaration):
    """不合契约的声明必须被拒绝：名称缺失、标识缺失、标识非法、media_types 非字符串序列。"""
    plugin = _CapableMediaSourcePlugin(declarations=[declaration])
    projection = PluginProjection({"DemoSource": plugin})

    declared = projection.provided_media_sources()

    assert declared["DemoSource"] == []


def test_projection_partial_rejection_keeps_valid_siblings():
    """同一实例声明多条时，不合契约的条目被跳过，合规的条目照常保留。"""
    plugin = _CapableMediaSourcePlugin(
        declarations=[
            MediaSourceDeclaration(
                media_source="ok.source", name="OK Source", methods={"media_detail": _detail}
            ),
            MediaSourceDeclaration(
                media_source="", name="Bad Source", methods={"media_detail": _detail}
            ),
        ]
    )
    projection = PluginProjection({"DemoSource": plugin})

    declared = projection.provided_media_sources()

    assert len(declared["DemoSource"]) == 1
    assert declared["DemoSource"][0].media_source == "ok.source"


def test_projection_swallows_plugin_exception_without_blocking_others():
    """单个插件声明媒体数据源抛异常时不应影响其它插件的投影结果。"""
    broken = _CapableMediaSourcePlugin(raise_error=True)
    healthy = _CapableMediaSourcePlugin(
        declarations=[
            MediaSourceDeclaration(
                media_source="ok.source", name="OK Source", methods={"media_detail": _detail}
            )
        ]
    )
    projection = PluginProjection({"Broken": broken, "Ok": healthy})

    declared = projection.provided_media_sources()

    assert "Broken" not in declared
    assert declared["Ok"][0].media_source == "ok.source"


def test_media_sources_tag_entries_with_owning_instance_key():
    """两个实例各自声明数据源时，聚合结果按实例键区分，互不覆盖。"""
    default_plugin = _CapableMediaSourcePlugin(
        declarations=[
            MediaSourceDeclaration(
                media_source="default.source",
                name="默认实例",
                methods={"media_detail": _detail},
            )
        ]
    )
    second_plugin = _CapableMediaSourcePlugin(
        declarations=[
            MediaSourceDeclaration(
                media_source="second.source",
                name="第二实例",
                methods={"media_detail": _detail},
            )
        ]
    )
    projection = PluginProjection({"Demo": default_plugin, "Demo@second": second_plugin})

    sources = projection.media_sources()

    by_plugin_id = {item["plugin_id"]: item["media_source"] for item in sources}
    assert by_plugin_id == {"Demo": "default.source", "Demo@second": "second.source"}


class _FakeMediaSourcePlugin:
    """既声明新式数据源又实现旧式钩子的插件桩，用于驱动聚合器完整链路。"""

    plugin_name = "假想数据源插件"

    def __init__(
        self,
        declared: Optional[List[MediaSourceDeclaration]] = None,
        legacy: Optional[list] = None,
        state: bool = True,
    ):
        self._declared = declared or []
        self._legacy = legacy
        self._state = state
        self.legacy_calls: List[int] = []

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._state

    def provides_media_sources(self):
        """返回声明的媒体数据源。"""
        return self._declared

    def get_media_source(self):
        """返回旧式裸描述字典列表并记录调用次数；未配置时返回 None 表示未提供。"""
        if self._legacy is None:
            return None
        self.legacy_calls.append(1)
        return self._legacy


def test_get_media_sources_merges_declared_and_legacy_sources(
    plugin_manager: PluginManager,
) -> None:
    """同一实例的声明式数据源与旧式裸字典数据源应合并到同一份聚合结果中。"""
    plugin = _FakeMediaSourcePlugin(
        declared=[
            MediaSourceDeclaration(
                media_source="new.source", name="新式来源", methods={"media_detail": _detail}
            )
        ],
        legacy=[{"name": "旧式来源", "media_source": "legacy.source"}],
    )
    plugin_manager.running_plugins["Demo"] = plugin

    sources = plugin_manager.get_media_sources("Demo")

    identifiers = {item["media_source"] for item in sources}
    assert identifiers == {"new.source", "legacy.source"}


def test_get_media_sources_emits_deprecation_warning_for_legacy_hook(
    plugin_manager: PluginManager, monkeypatch
) -> None:
    """触达旧式 get_media_source() 时必须触发一次废弃告警，重复触达不重复告警。"""
    emitted = []
    monkeypatch.setattr(deprecation_policy.logger, "warning", emitted.append)
    plugin_manager.running_plugins["Demo"] = _FakeMediaSourcePlugin(
        legacy=[{"name": "旧式来源", "media_source": "legacy.source"}]
    )

    plugin_manager.get_media_sources("Demo")
    plugin_manager.get_media_sources("Demo")

    assert len(emitted) == 1
    assert "get_media_source" in emitted[0]


def test_legacy_hook_stops_at_disabled_stage_and_resumes_via_override(
    plugin_manager: PluginManager, monkeypatch
) -> None:
    """阶段推进到 DISABLED 时旧钩子真的停用；标识列入 DEPRECATION_ENABLED 能恢复。"""
    plugin_manager.running_plugins["Demo"] = _FakeMediaSourcePlugin(
        legacy=[{"name": "旧式来源", "media_source": "legacy.source"}]
    )

    # 阶段一（默认登记）：旧钩子照常生效
    sources = plugin_manager.get_media_sources("Demo")
    assert any(item["media_source"] == "legacy.source" for item in sources)

    # 阶段二：默认停用，旧钩子不再产出条目
    _set_notice_stage(monkeypatch, "plugin.get_media_source", DeprecationStage.DISABLED)
    monkeypatch.setattr(deprecation_policy, "_enabled_keys", frozenset)
    sources = plugin_manager.get_media_sources("Demo")
    assert not any(item["media_source"] == "legacy.source" for item in sources)

    # 阶段二 + 标识列入 DEPRECATION_ENABLED：临时恢复
    monkeypatch.setattr(
        deprecation_policy, "_enabled_keys", lambda: frozenset({"plugin.get_media_source"})
    )
    sources = plugin_manager.get_media_sources("Demo")
    assert any(item["media_source"] == "legacy.source" for item in sources)
