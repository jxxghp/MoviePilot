"""Agent 助手形象扩展契约的后端测试：投影规整、接口鉴权、默认值设置与用户私有数据上限。"""

import asyncio
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.web.security.access import verify_token
from app.api.endpoints import plugin as plugin_endpoint
from app.api.endpoints import user as user_endpoint
from app.application.security.userconfig import (
    AGENT_PET_STATE_MAX_BYTES,
    UserConfigurationService,
    UserConfigurationValueTooLargeError,
)
from app.application.settings.contract import ALL_SETTING_SPECS
from app.runtime.config import Settings, settings
from app.runtime.extensions.plugin.manager import PluginManager
from app.runtime.extensions.plugin.projection import PluginProjection
from app.schemas.plugin import PluginAgentPet


class _Plugin(SimpleNamespace):
    """提供可配置 hook 的最小运行态插件替身。"""

    def __init__(self, enabled=True, render=("vue", "dist/assets"), **attrs):
        """保存启用状态、渲染模式与 hook 实现。"""
        attrs.setdefault("plugin_name", "形象插件")
        super().__init__(**attrs)
        self._enabled = enabled
        self._render = render

    def get_state(self):
        """返回预设启用状态。"""
        return self._enabled

    def get_render_mode(self):
        """返回预设渲染模式和联邦产物目录。"""
        return self._render


class _Log:
    """收集投影日志，便于断言非法声明被记录。"""

    def __init__(self):
        """初始化各级别日志列表。"""
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def warning(self, message):
        """记录警告。"""
        self.warnings.append(message)

    def error(self, message):
        """记录错误。"""
        self.errors.append(message)


def _projection(plugins, log=None):
    """使用真实 remoteEntry 路径构造方式创建投影。"""
    return PluginProjection(
        plugins,
        log=log or _Log(),
        remote_entry_factory=PluginManager.get_plugin_remote_entry,
    )


def test_agent_pets_projects_defaults_and_preview_url_next_to_remote_entry():
    """合法声明补齐默认值，相对预览图与头像解析到与 remoteEntry 相同的静态目录并带版本，缺省为空。"""
    plugin = _Plugin(
        plugin_version="1.0.0",
        get_agent_pets=lambda: [
            {
                "key": "chibi",
                "name": "看板娘",
                "mode": "stage",
                "preview": "./img/preview.png",
                "avatar": "img/avatar.png",
            },
            {
                "key": "sprite_1",
                "name": " 精灵 ",
                "description": "传统吉祥物",
                "component": "./SpritePet",
                "api_version": 2,
                "preview": "https://example.com/p.png",
                "avatar": "data:image/png;base64,AAAA",
                "random_actions": ["wave", "jump", "wave"],
            },
            {"key": "plain", "name": "无预览"},
        ],
    )
    projection = _projection({"PetDemo": plugin})

    pets = projection.agent_pets()
    remote_url = projection.remotes()[0]["url"]

    assert remote_url == "/plugin/file/petdemo/dist/assets/remoteEntry.js?v=1.0.0"
    assert pets == [
        {
            "plugin_id": "PetDemo",
            "source_plugin_id": "PetDemo",
            "plugin_name": "形象插件",
            "key": "chibi",
            "name": "看板娘",
            "description": None,
            "mode": "stage",
            "component": "AgentPet",
            "api_version": 1,
            "preview_url": "/plugin/file/petdemo/dist/assets/img/preview.png?v=1.0.0",
            "avatar_url": "/plugin/file/petdemo/dist/assets/img/avatar.png?v=1.0.0",
            "bubbles": "host",
            "random_actions": None,
        },
        {
            "plugin_id": "PetDemo",
            "source_plugin_id": "PetDemo",
            "plugin_name": "形象插件",
            "key": "sprite_1",
            "name": "精灵",
            "description": "传统吉祥物",
            "mode": "renderer",
            "component": "SpritePet",
            "api_version": 2,
            "preview_url": "https://example.com/p.png",
            "avatar_url": "data:image/png;base64,AAAA",
            "bubbles": None,
            "random_actions": ["wave", "jump"],
        },
        {
            "plugin_id": "PetDemo",
            "source_plugin_id": "PetDemo",
            "plugin_name": "形象插件",
            "key": "plain",
            "name": "无预览",
            "description": None,
            "mode": "renderer",
            "component": "AgentPet",
            "api_version": 1,
            "preview_url": None,
            "avatar_url": None,
            "bubbles": None,
            "random_actions": None,
        },
    ]
    # 投影结果必须能被接口 schema 无损承载
    assert [PluginAgentPet(**pet).model_dump() for pet in pets] == pets


def test_agent_pets_skips_disabled_non_vue_and_unimplemented_plugins():
    """停用、非 Vue 渲染或未实现钩子的插件不出现在形象列表中。"""
    declared = lambda: [{"key": "pet", "name": "形象"}]  # noqa: E731
    projection = _projection({
        "Disabled": _Plugin(enabled=False, get_agent_pets=declared),
        "Vuetify": _Plugin(render=("vuetify", None), get_agent_pets=declared),
        "NoHook": _Plugin(),
        "Enabled": _Plugin(get_agent_pets=declared),
    })

    assert [pet["plugin_id"] for pet in projection.agent_pets()] == ["Enabled"]


@pytest.mark.parametrize(
    "item",
    [
        {"key": "Bad-Key", "name": "大写"},
        {"key": "a" * 33, "name": "过长"},
        {"key": "has space", "name": "空格"},
        {"name": "缺 key"},
        {"key": "noname"},
        {"key": "mode", "name": "未知模式", "mode": "overlay"},
        {"key": "bubble", "name": "未知气泡", "mode": "stage", "bubbles": "both"},
        {"key": "ver", "name": "版本", "api_version": "1"},
        {"key": "ver0", "name": "版本", "api_version": 0},
        {"key": "trav", "name": "越界", "preview": "../secret.png"},
        {"key": "abs", "name": "根路径", "preview": "/etc/passwd"},
        {"key": "js", "name": "脚本", "preview": "javascript:alert(1)"},
        {"key": "avatar_trav", "name": "头像越界", "avatar": "img/../../secret.png"},
        {"key": "avatar_abs", "name": "头像根路径", "avatar": "/etc/passwd"},
        {"key": "avatar_ftp", "name": "头像协议", "avatar": "ftp://example.com/a.png"},
        {"key": "avatar_type", "name": "头像类型", "avatar": 1},
        {"key": "acts", "name": "动作", "random_actions": "wave"},
        "not-a-dict",
    ],
)
def test_agent_pets_drops_invalid_items_with_warning(item):
    """任一字段非法的声明整项丢弃并记 warning，同插件其他合法项不受影响。"""
    log = _Log()
    projection = _projection(
        {"Demo": _Plugin(get_agent_pets=lambda: [item, {"key": "ok", "name": "正常"}])},
        log=log,
    )

    assert [pet["key"] for pet in projection.agent_pets()] == ["ok"]
    assert log.warnings and "Demo" in log.warnings[0]


def test_agent_pets_deduplicates_keys_within_plugin_only():
    """同一插件重复 key 只保留首项，不同插件可使用相同 key。"""
    log = _Log()
    projection = _projection(
        {
            "Alpha": _Plugin(get_agent_pets=lambda: [
                {"key": "pet", "name": "第一"},
                {"key": "pet", "name": "第二"},
            ]),
            "Beta": _Plugin(get_agent_pets=lambda: [{"key": "pet", "name": "别家"}]),
        },
        log=log,
    )

    pets = projection.agent_pets()

    assert [(pet["plugin_id"], pet["name"]) for pet in pets] == [("Alpha", "第一"), ("Beta", "别家")]
    assert any("重复" in message for message in log.warnings)


def test_agent_pets_instance_keeps_instance_id_and_source_identity():
    """插件分身沿用实例 ID 作为 plugin_id 与预览 URL 路径，source_plugin_id 指向源插件。"""
    plugin = _Plugin(
        plugin_source_id="PetDemo",
        get_agent_pets=lambda: [
            {"key": "pet", "name": "形象", "preview": "preview.png", "avatar": "avatar.png"}
        ],
    )

    pet = _projection({"PetDemoWork": plugin}).agent_pets()[0]

    assert pet["plugin_id"] == "PetDemoWork"
    assert pet["source_plugin_id"] == "PetDemo"
    assert pet["preview_url"] == "/plugin/file/petdemowork/dist/assets/preview.png"
    assert pet["avatar_url"] == "/plugin/file/petdemowork/dist/assets/avatar.png"


def test_agent_pets_isolates_hook_failure_and_non_list_result():
    """单个插件钩子抛错或返回非列表只记日志，不阻断其他插件。"""

    def fail():
        """模拟插件钩子异常。"""
        raise RuntimeError("broken")

    log = _Log()
    projection = _projection(
        {
            "Broken": _Plugin(get_agent_pets=fail),
            "Mapping": _Plugin(get_agent_pets=lambda: {"key": "pet", "name": "错误结构"}),
            "Healthy": _Plugin(get_agent_pets=lambda: [{"key": "pet", "name": "形象"}]),
        },
        log=log,
    )

    assert [pet["plugin_id"] for pet in projection.agent_pets()] == ["Healthy"]
    assert any("Broken" in message for message in log.errors)
    assert any("Mapping" in message for message in log.warnings)


def test_agent_pets_endpoint_requires_token_and_precedes_dynamic_plugin_routes(monkeypatch):
    """接口使用 verify_token 鉴权，且注册在 /{plugin_id} 动态路由之前，返回管理器投影。"""
    signature = inspect.signature(plugin_endpoint.plugin_agent_pets)
    assert signature.parameters["_"].default.dependency is verify_token

    paths = [route.path for route in plugin_endpoint.router.routes]
    assert paths.index("/agent_pets") < paths.index("/{plugin_id}")

    expected = [{"plugin_id": "Demo", "key": "pet"}]
    monkeypatch.setattr(
        plugin_endpoint,
        "get_plugin_manager",
        lambda: SimpleNamespace(get_plugin_agent_pets=lambda: expected),
    )
    assert plugin_endpoint.plugin_agent_pets(_=None) is expected


def test_default_agent_pet_setting_is_declared_and_exposed_to_logged_in_users(monkeypatch):
    """AI_AGENT_PET 进入设置模型与设置目录，并随登录用户全局设置下发给前端。"""
    from app.api.endpoints.system import get_user_global_setting

    assert Settings.model_fields["AI_AGENT_PET"].default == ""
    spec = ALL_SETTING_SPECS["AI_AGENT_PET"]
    assert spec.group == "ai_agent"
    assert spec.description

    monkeypatch.setattr(settings, "AI_AGENT_PET", "PetDemo:chibi")
    runtime = SimpleNamespace(system=SimpleNamespace(user_global=AsyncMock(return_value={})))
    response = asyncio.run(get_user_global_setting(_=None, runtime=runtime))

    assert response.data["AI_AGENT_PET"] == "PetDemo:chibi"


class _Repository:
    """记录写入的用户配置仓库替身。"""

    def __init__(self):
        """初始化写入记录。"""
        self.writes: list[tuple[str, str, object]] = []

    def set(self, username, key, value):
        """记录写入。"""
        self.writes.append((username, key, value))


class _Executor:
    """同步执行数据库操作的异步执行端口替身。"""

    async def run(self, operation):
        """直接执行操作。"""
        return operation()


def _state_of_size(size: int) -> dict:
    """构造紧凑 JSON 序列化后恰为指定字节数的形象数据。"""
    overhead = len(json.dumps({"p": ""}, separators=(",", ":")).encode("utf-8"))
    return {"p": "x" * (size - overhead)}


def test_agent_pet_state_prefix_is_accepted_up_to_size_limit():
    """AgentPetState.* 前缀在 16KB 以内允许写入，AgentPet 选择与其他 key 不受限制。"""
    repository = _Repository()
    service = UserConfigurationService(repository, async_executor=_Executor())
    at_limit = _state_of_size(AGENT_PET_STATE_MAX_BYTES)
    large = _state_of_size(AGENT_PET_STATE_MAX_BYTES + 1)

    asyncio.run(service.async_set("alice", "AgentPetState.PetDemo.chibi", at_limit))
    service.set("alice", "AgentPet", {"plugin_id": "PetDemo", "key": "chibi"})
    service.set("alice", "Dashboard", large)

    assert [write[1] for write in repository.writes] == [
        "AgentPetState.PetDemo.chibi",
        "AgentPet",
        "Dashboard",
    ]


def test_agent_pet_state_over_limit_is_rejected_without_writing():
    """AgentPetState.* 序列化超过 16KB 时同步与异步写入均被拒绝且不落库。"""
    repository = _Repository()
    service = UserConfigurationService(repository, async_executor=_Executor())
    too_large = _state_of_size(AGENT_PET_STATE_MAX_BYTES + 1)

    with pytest.raises(UserConfigurationValueTooLargeError):
        service.set("alice", "AgentPetState.PetDemo.chibi", too_large)
    with pytest.raises(UserConfigurationValueTooLargeError):
        asyncio.run(service.async_set("alice", "AgentPetState.PetDemo.chibi", too_large))

    assert not repository.writes


def test_user_config_endpoint_reports_oversized_agent_pet_state(monkeypatch):
    """用户配置接口把超限写入转换为失败响应，而不是 500。"""
    repository = _Repository()
    service = UserConfigurationService(repository, async_executor=_Executor())
    monkeypatch.setattr(user_endpoint, "get_configured_user_configuration", lambda: service)
    user = SimpleNamespace(name="alice")

    rejected = asyncio.run(user_endpoint.set_config(
        key="AgentPetState.PetDemo.chibi",
        value=_state_of_size(AGENT_PET_STATE_MAX_BYTES + 1),
        current_user=user,
    ))
    accepted = asyncio.run(user_endpoint.set_config(key="AgentPet", value="builtin", current_user=user))

    assert rejected.success is False
    assert "16384" in rejected.message
    assert accepted.success is True
    assert repository.writes == [("alice", "AgentPet", "builtin")]
