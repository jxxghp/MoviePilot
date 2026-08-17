"""插件能力投影的隔离契约测试。"""

from types import SimpleNamespace

from app.runtime.extensions.plugin.projection import PluginProjection


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
