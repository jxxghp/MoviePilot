"""插件能力投影的隔离契约测试。"""

from types import SimpleNamespace

from app.runtime.extensions.plugin.projection import PluginProjection
from app.schemas.category import ClassificationFieldDefinition
from app.schemas.event import MediaSourceInfo


class _Plugin(SimpleNamespace):
    """提供可配置插件 hook 的最小运行态插件替身。"""

    def __init__(self, enabled=True, **hooks):
        """保存启用状态、插件名称和 hook 实现。"""
        super().__init__(plugin_name=hooks.pop("plugin_name", "测试插件"), **hooks)
        self._enabled = enabled

    def get_state(self):
        """返回预设启用状态。"""
        return self._enabled

    def get_name(self):
        """返回插件展示名称。"""
        return self.plugin_name


def test_projection_preserves_commands_and_api_adaptation():
    """命令补 pid，API 补宿主路径与默认认证方式。"""
    command = {"cmd": "/demo"}
    api = {"path": "/items", "endpoint": object()}
    plugin = _Plugin(
        get_command=lambda: [command],
        get_api=lambda: [api],
    )
    projection = PluginProjection({"Demo": plugin})

    assert projection.commands() == [{"cmd": "/demo", "pid": "Demo"}]
    assert projection.apis() == [{
        "path": "/Demo/items",
        "endpoint": api["endpoint"],
        "auth": "apikey",
    }]
    assert api == {"path": "/items", "endpoint": api["endpoint"]}


def test_projection_filters_disabled_stateful_hooks_but_keeps_api_contract():
    """禁用插件不暴露命令/服务/模块/动作，API 保持历史上的独立注册语义。"""
    plugin = _Plugin(
        enabled=False,
        get_command=lambda: [{"cmd": "/demo"}],
        get_api=lambda: [{"path": "/items"}],
        get_service=lambda: [{"id": "job"}],
        get_module=lambda: {"recognize": object()},
        get_actions=lambda: [{"id": "action"}],
    )
    projection = PluginProjection({"Demo": plugin})

    assert projection.commands() == []
    assert projection.services() == []
    assert projection.modules() == {}
    assert projection.actions() == []
    assert projection.apis() == [{"path": "/Demo/items", "auth": "apikey"}]


def test_projection_preserves_services_modules_actions_and_pid_filter():
    """指定 pid 时只投影目标插件，并保持各 hook 的原始结构。"""
    demo = _Plugin(
        get_service=lambda: [{"id": "job"}],
        get_module=lambda: {"recognize": "handler"},
        get_actions=lambda: [{"id": "action"}],
    )
    other = _Plugin(get_service=lambda: [{"id": "other"}])
    projection = PluginProjection({"Demo": demo, "Other": other})

    assert projection.services("Demo") == [{"id": "job"}]
    assert projection.modules("Demo") == {
        ("Demo", "测试插件"): {"recognize": "handler"}
    }
    assert projection.actions("Demo") == [{
        "plugin_id": "Demo",
        "plugin_name": "测试插件",
        "actions": [{"id": "action"}],
    }]


def test_projection_modules_skips_none_and_non_mapping_declarations():
    """get_module 未返回映射的插件被跳过并记日志，不污染整体模块投影。"""
    errors = []
    log = SimpleNamespace(error=lambda message: errors.append(message))
    projection = PluginProjection(
        {
            "NoDecl": _Plugin(get_module=lambda: None),
            "ListDecl": _Plugin(get_module=lambda: ["not-a-mapping"]),
            "Healthy": _Plugin(get_module=lambda: {"recognize": "handler"}),
        },
        log=log,
    )

    assert projection.modules() == {("Healthy", "测试插件"): {"recognize": "handler"}}
    assert any("ListDecl" in message for message in errors)


def test_projection_collects_enabled_media_source_declarations():
    """只投影启用插件的媒体来源声明，并附带插件 ID 便于诊断。"""
    demo = _Plugin(
        get_media_source=lambda: [{
            "name": "Acme Video",
            "media_source": "acme.video",
            "media_types": ["电影", "电视剧"],
            "classification_fields": [{
                "id": "extensions.acme.video.release_channel",
                "label": "发行渠道",
                "value_type": "enum",
                "operators": ["equals"],
            }],
        }],
    )
    disabled = _Plugin(
        enabled=False,
        get_media_source=lambda: [{"name": "Disabled", "media_source": "disabled"}],
    )
    projection = PluginProjection({"Demo": demo, "Disabled": disabled})

    assert projection.media_sources() == [{
        "name": "Acme Video",
        "media_source": "acme.video",
        "media_types": ["电影", "电视剧"],
        "classification_fields": [{
            "id": "extensions.acme.video.release_channel",
            "label": "发行渠道",
            "value_type": "enum",
            "operators": ["equals"],
        }],
        "plugin_id": "Demo",
    }]


def test_projection_serializes_public_media_source_sdk_model():
    """新 SDK 声明模型应继续出现在既有媒体来源接口中。"""
    source = MediaSourceInfo(
        name="SDK Video",
        media_source="sdk.video",
        media_types=["电影"],
        classification_fields=[
            ClassificationFieldDefinition(
                id="extensions.sdk.video.channel",
                label="渠道",
                value_type="string",
                operators=["equals"],
                media_types=["电影"],
            )
        ],
    )
    projection = PluginProjection(
        {"SdkDemo": _Plugin(get_media_source=lambda: [source])}
    )

    projected = projection.media_sources()

    assert len(projected) == 1
    assert projected[0]["media_source"] == "sdk.video"
    assert projected[0]["plugin_id"] == "SdkDemo"
    assert projected[0]["classification_fields"][0]["id"] == (
        "extensions.sdk.video.channel"
    )


def test_projection_isolates_one_plugin_hook_failure():
    """单个插件 hook 失败只记日志，不阻断其他插件投影。"""
    errors = []
    log = SimpleNamespace(error=lambda message: errors.append(message))

    def fail():
        """模拟插件 hook 抛出异常。"""
        raise RuntimeError("broken")

    projection = PluginProjection(
        {
            "Broken": _Plugin(get_service=fail),
            "Healthy": _Plugin(get_service=lambda: [{"id": "healthy"}]),
        },
        log=log,
    )

    assert projection.services() == [{"id": "healthy"}]
    assert errors and "Broken" in errors[0]


def test_projection_builds_federation_and_auth_provider_entries():
    """联邦远程入口和插件认证入口保持既有字段与默认值。"""
    plugin = _Plugin(
        get_render_mode=lambda: ("vue", "dist/assets"),
        get_auth_providers=lambda: [{"id": "demo-login"}],
    )
    projection = PluginProjection(
        {"Demo": plugin},
        remote_entry_factory=lambda plugin_id, path: f"/{plugin_id}/{path}",
    )

    assert projection.remotes() == [{
        "id": "Demo",
        "url": "/Demo/dist/assets",
        "name": "测试插件",
    }]
    assert projection.auth_providers() == [{
        "id": "demo-login",
        "type": "plugin",
        "plugin_id": "Demo",
        "name": "测试插件",
        "enabled": True,
        "component": "AuthPage",
        "remote": {
            "id": "Demo",
            "url": "/Demo/dist/assets",
            "name": "测试插件",
        },
    }]


def test_projection_exposes_source_identity_for_virtual_frontend_instance():
    """虚拟实例的联邦入口保留实例 URL，并补充共享源码身份。"""
    plugin = _Plugin(
        plugin_source_id="Demo",
        get_render_mode=lambda: ("vue", "dist/assets"),
        get_auth_providers=lambda: [{"id": "demo-login"}],
    )
    projection = PluginProjection(
        {"DemoWork": plugin},
        remote_entry_factory=lambda plugin_id, path: f"/{plugin_id}/{path}",
    )

    assert projection.remotes()[0] == {
        "id": "DemoWork",
        "url": "/DemoWork/dist/assets",
        "name": "测试插件",
        "source_plugin_id": "Demo",
    }
    assert projection.auth_providers()[0]["remote"]["source_plugin_id"] == "Demo"


def test_projection_normalizes_sidebar_and_dashboard_metadata():
    """侧栏和仪表板元数据在投影层完成校验、排序与兼容默认值。"""
    plugin = _Plugin(
        get_render_mode=lambda: ("vue", "dist"),
        get_sidebar_nav=lambda: [{
            "key": "settings",
            "section": "invalid",
            "permission": "invalid",
            "order": "3",
        }],
        get_dashboard=lambda: ({}, {}, []),
        get_dashboard_meta=lambda: [{"name": "状态", "key": "status"}],
    )
    projection = PluginProjection({"Demo": plugin})

    assert projection.sidebar() == [{
        "plugin_id": "Demo",
        "nav_key": "settings",
        "title": "测试插件",
        "icon": "mdi-puzzle",
        "section": "system",
        "permission": None,
        "order": 3,
    }]
    assert projection.dashboard_metadata() == [{
        "id": "Demo",
        "name": "状态",
        "key": "status",
    }]
