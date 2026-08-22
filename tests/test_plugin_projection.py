"""插件能力投影的隔离契约测试。"""

from types import SimpleNamespace

from app.runtime.extensions.projection.plugin import PluginProjection


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

    assert projection.services("Demo") == [{"id": "job", "pid": "Demo"}]
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
        "plugin_id": "Demo",
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

    assert projection.services() == [{"id": "healthy", "pid": "Healthy"}]
    assert errors and "Broken" in errors[0]


def test_projection_builds_federation_and_auth_provider_entries():
    """联邦远程入口和插件认证入口保持既有字段与默认值。"""
    plugin = _Plugin(
        get_render_mode=lambda: ("vue", "dist/assets"),
        get_auth_providers=lambda: [{"id": "demo-login"}],
    )
    projection = PluginProjection(
        {"Demo": plugin},
        remote_entry_factory=lambda plugin_id, path, version: f"/{plugin_id}/{path}",
    )

    assert projection.remotes() == [{
        "id": "Demo",
        "url": "/Demo/dist/assets",
        "name": "测试插件",
        "version": None,
        "remote_key": "Demo",
    }]
    assert projection.auth_providers() == [{
        "id": "demo-login",
        "type": "plugin",
        "plugin_id": "Demo",
        "name": "测试插件",
        "enabled": True,
        "instance_id": "default",
        "instance_key": "Demo",
        "component": "AuthPage",
        "remote": {
            "id": "Demo",
            "url": "/Demo/dist/assets",
            "name": "测试插件",
            "version": None,
            "remote_key": "Demo",
        },
    }]


def test_projection_remote_key_falls_back_to_id_without_a_declared_version():
    """插件未声明 plugin_version 时，remote_key 与 id 取值相同，等价于旧格式。"""
    plugin = _Plugin(get_render_mode=lambda: ("vue", "dist/assets"))
    projection = PluginProjection(
        {"Demo": plugin},
        remote_entry_factory=lambda plugin_id, path, version: f"/{plugin_id}/{path}",
    )

    remote = projection.remotes()[0]
    assert remote["version"] is None
    assert remote["remote_key"] == remote["id"] == "Demo"


def test_projection_remote_key_differs_across_versions_of_the_same_plugin():
    """两个实例绑不同版本时，remote_key 各不相同，联邦远程注册不会撞名。"""
    first = _Plugin(plugin_version="1.0.0", get_render_mode=lambda: ("vue", "dist/assets"))
    second = _Plugin(plugin_version="2.0.0", get_render_mode=lambda: ("vue", "dist/assets"))
    captured_versions = []

    def factory(plugin_id, path, version):
        """记录调用方传入的版本号，并回显一个可辨识的 URL。"""
        captured_versions.append(version)
        return f"/{plugin_id}/{version}/{path}"

    projection = PluginProjection(
        {"Demo": first, "Demo@second": second},
        remote_entry_factory=factory,
    )

    remotes = {item["id"]: item for item in projection.remotes()}
    assert remotes["Demo"]["remote_key"] == "Demo#1.0.0"
    assert remotes["Demo@second"]["remote_key"] == "Demo@second#2.0.0"
    assert remotes["Demo"]["remote_key"] != remotes["Demo@second"]["remote_key"]
    assert remotes["Demo"]["url"] != remotes["Demo@second"]["url"]
    assert captured_versions == ["1.0.0", "2.0.0"]


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
        "instance_id": "default",
        "instance_key": "Demo",
    }]
    assert projection.dashboard_metadata() == [{
        "id": "Demo",
        "name": "状态",
        "key": "status",
        "instance_id": "default",
        "instance_key": "Demo",
    }]


def test_projection_tags_services_with_owning_instance_key():
    """两个实例声明同一服务 id 时，各自的服务项带上归属实例键，互不覆盖。"""
    default_plugin = _Plugin(get_service=lambda: [{"id": "sync"}])
    second_plugin = _Plugin(get_service=lambda: [{"id": "sync"}])
    projection = PluginProjection({"Demo": default_plugin, "Demo@second": second_plugin})

    services = projection.services("Demo")

    assert [service["pid"] for service in services] == ["Demo", "Demo@second"]


def test_projection_adds_instance_fields_to_sidebar_dashboard_and_auth_providers():
    """侧栏、仪表板、认证提供方的既有字段继续填实例键，并补实例标识字段。"""
    second_plugin = _Plugin(
        get_render_mode=lambda: ("vue", "dist"),
        get_sidebar_nav=lambda: [{"key": "settings", "section": "system"}],
        get_dashboard=lambda: ({}, {}, []),
        get_auth_providers=lambda: [{"id": "demo-login"}],
    )
    projection = PluginProjection(
        {"Demo@second": second_plugin},
        remote_entry_factory=lambda plugin_id, path, version: f"/{plugin_id}/{path}",
    )

    sidebar_item = projection.sidebar()[0]
    assert sidebar_item["plugin_id"] == "Demo@second"
    assert sidebar_item["instance_id"] == "second"
    assert sidebar_item["instance_key"] == "Demo@second"

    dashboard_item = projection.dashboard_metadata()[0]
    assert dashboard_item["id"] == "Demo@second"
    assert dashboard_item["instance_id"] == "second"
    assert dashboard_item["instance_key"] == "Demo@second"

    provider = projection.auth_providers()[0]
    assert provider["plugin_id"] == "Demo@second"
    assert provider["instance_id"] == "second"
    assert provider["instance_key"] == "Demo@second"
    assert provider["remote"]["remote_key"] == "Demo@second"
