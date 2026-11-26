"""
插件能力投影的隔离契约测试。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.extensions.plugin.contracts import (
    PluginDashboardError,
    PluginNotFoundError,
)
from app.runtime.extensions.plugin.projection import PluginProjection
from app.schemas.category import ClassificationFieldDefinition
from app.schemas.event import MediaSourceInfo


class _Plugin(SimpleNamespace):
    """
    提供可配置插件 hook 的最小运行态插件替身。
    """

    def __init__(self, enabled=True, **hooks):
        """
        保存启用状态、插件名称和 hook 实现。
        """
        super().__init__(plugin_name=hooks.pop("plugin_name", "测试插件"), **hooks)
        self._enabled = enabled

    def get_state(self):
        """
        返回预设启用状态。
        """
        return self._enabled

    def get_name(self):
        """
        返回插件展示名称。
        """
        return self.plugin_name


def test_projection_preserves_commands_and_api_adaptation():
    """
    命令补 pid，API 补宿主路径与默认认证方式。
    """
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
    """
    禁用插件不暴露命令/服务/模块/动作，API 保持历史上的独立注册语义。
    """
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
    """
    指定 pid 时只投影目标插件，并保持各 hook 的原始结构。
    """
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
    """
    get_module 未返回映射的插件被跳过并记日志，不污染整体模块投影。
    """
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
    """
    只投影启用插件的媒体来源声明，并附带插件 ID 便于诊断。
    """
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
    """
    新 SDK 声明模型应继续出现在既有媒体来源接口中。
    """
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
    """
    单个插件 hook 失败只记日志，不阻断其他插件投影。
    """
    errors = []
    log = SimpleNamespace(error=lambda message: errors.append(message))

    def fail():
        """
        模拟插件 hook 抛出异常。
        """
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
    """
    联邦远程入口和插件认证入口保持既有字段与默认值。
    """
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


def test_projection_versions_federation_remote_entry_after_plugin_update():
    """
    插件版本变化时，联邦入口 URL 应生成新的缓存键，并附带 version 字段。
    """
    plugin = _Plugin(
        plugin_version="1.2.3+build 4",
        get_render_mode=lambda: ("vue", "dist/assets"),
    )
    projection = PluginProjection(
        {"Demo": plugin},
        remote_entry_factory=lambda plugin_id, path: f"/{plugin_id}/{path}",
    )

    remote = projection.remotes()[0]
    assert remote["url"] == "/Demo/dist/assets?v=1.2.3%2Bbuild+4"
    assert remote["version"] == "1.2.3+build 4"


def test_projection_exposes_remote_bundle_hash_for_source_and_virtual_instances(tmp_path):
    """
    存在联邦产物目录时，remotes 应输出 remoteEntry.js 的稳定内容哈希并在产物更新后变化。
    """
    assets_dir = tmp_path / "demo" / "dist" / "assets"
    assets_dir.mkdir(parents=True)
    entry_file = assets_dir / "remoteEntry.js"
    entry_file.write_text("export const version = 1;\n", encoding="utf-8")

    host_plugin = _Plugin(
        plugin_version="2.0.0",
        get_render_mode=lambda: ("vue", "dist/assets"),
    )
    clone_plugin = _Plugin(
        plugin_version="2.0.0",
        plugin_source_id="Demo",
        get_render_mode=lambda: ("vue", "dist/assets"),
    )
    projection = PluginProjection(
        {"Demo": host_plugin, "DemoClone": clone_plugin},
        remote_entry_factory=lambda plugin_id, path: f"/api/v1/plugin/file/{plugin_id.lower()}/{path}/remoteEntry.js",
        plugins_root=tmp_path,
    )

    remotes = {item["id"]: item for item in projection.remotes()}
    first_hash = remotes["Demo"]["bundle_hash"]
    assert len(first_hash) == 12
    assert remotes["DemoClone"]["bundle_hash"] == first_hash

    entry_file.write_text("export const version = 2;\n", encoding="utf-8")
    updated_hash = projection.remotes("Demo")[0]["bundle_hash"]
    assert len(updated_hash) == 12
    assert updated_hash != first_hash


@pytest.mark.parametrize(
    ("source_plugin_id", "dist_path"),
    [
        pytest.param(None, "dist/../../outside", id="parent-segment"),
        pytest.param(None, "dist\\assets", id="backslash"),
        pytest.param("../outside", "dist/assets", id="plugin-dir-escape"),
        pytest.param(None, "dist/missing", id="missing-entry"),
        pytest.param(None, "dist/linked", id="entry-symlink-escape"),
    ],
)
def test_projection_omits_bundle_hash_for_unsafe_or_missing_remote_entry(tmp_path, source_plugin_id, dist_path):
    """
    联邦产物路径越界、指向插件目录之外或入口文件不存在时，remotes 仍正常输出但不附带 bundle_hash。
    """
    plugins_root = tmp_path / "plugins"
    (plugins_root / "demo" / "dist" / "assets").mkdir(parents=True)
    (plugins_root / "demo" / "dist" / "assets" / "remoteEntry.js").write_text("export {};\n", encoding="utf-8")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "remoteEntry.js").write_text("export const leaked = true;\n", encoding="utf-8")
    linked_dir = plugins_root / "demo" / "dist" / "linked"
    linked_dir.mkdir(parents=True)
    (linked_dir / "remoteEntry.js").symlink_to(outside_dir / "remoteEntry.js")

    plugin = _Plugin(
        plugin_version="2.0.0",
        plugin_source_id=source_plugin_id,
        get_render_mode=lambda: ("vue", dist_path),
    )
    projection = PluginProjection(
        {"Demo": plugin},
        remote_entry_factory=lambda plugin_id, path: f"/{plugin_id}/{path}",
        plugins_root=plugins_root,
    )

    remote = projection.remotes()[0]
    assert remote["version"] == "2.0.0"
    assert "bundle_hash" not in remote


def test_projection_omits_bundle_hash_when_remote_entry_is_unreadable(tmp_path, monkeypatch):
    """
    读取 remoteEntry.js 失败时不中断 remotes 输出，只省略 bundle_hash。
    """
    assets_dir = tmp_path / "demo" / "dist" / "assets"
    assets_dir.mkdir(parents=True)
    (assets_dir / "remoteEntry.js").write_text("export {};\n", encoding="utf-8")

    def _raise_os_error(_path):
        """
        模拟文件在检查后被移除或无权读取。
        """
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", _raise_os_error)
    projection = PluginProjection(
        {"Demo": _Plugin(plugin_version="2.0.0", get_render_mode=lambda: ("vue", "dist/assets"))},
        remote_entry_factory=lambda plugin_id, path: f"/{plugin_id}/{path}",
        plugins_root=tmp_path,
    )

    remote = projection.remotes()[0]
    assert remote["url"].startswith("/Demo/dist/assets")
    assert "bundle_hash" not in remote


def test_projection_exposes_source_identity_for_virtual_frontend_instance():
    """
    虚拟实例的联邦入口保留实例 URL，并补充共享源码身份。
    """
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
    """
    侧栏和仪表板元数据在投影层完成校验、排序与兼容默认值。
    """
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


def test_projection_widget_metadata_merges_declared_and_dashboard_fallbacks():
    """
    widget_metadata 汇总显式小组件声明，对未覆盖键名的仪表板自动兜底，并隔离异常与非法键。
    """
    errors: list[str] = []
    warnings: list[str] = []
    log = SimpleNamespace(
        error=lambda msg: errors.append(msg),
        warning=lambda msg: warnings.append(msg),
    )

    rich = _Plugin(
        plugin_name="音乐盒",
        get_page=lambda: [],
        get_widgets=lambda: [
            "not-a-dict",
            {"key": "bad key!", "name": "非法键"},
            {
                "key": "main",
                "name": "主卡片",
                "description": " 实时曲目 ",
                "kind": "custom",
            },
            {"key": "main", "name": "重复键被忽略"},
            {"key": "unknown_kind", "kind": "unsupported"},
        ],
        get_dashboard=lambda: ({}, {}, []),
        get_dashboard_meta=lambda: [
            "invalid-meta",
            {"key": "main", "name": "已被显式声明覆盖"},
            {"key": "bad/key", "name": "非法仪表盘键"},
            {"key": "extra", "name": "附加仪表盘"},
        ],
    )
    broken_widgets = _Plugin(
        get_widgets=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        get_dashboard=lambda: ({}, {}, []),
        get_dashboard_meta=lambda: (_ for _ in ()).throw(RuntimeError("meta-boom")),
    )
    non_list_widgets = _Plugin(
        get_widgets=lambda: "invalid-return",
        get_dashboard=lambda: ({}, {}, []),
    )
    disabled = _Plugin(
        enabled=False,
        get_widgets=lambda: [{"key": "hidden", "name": "隐藏"}],
    )

    projection = PluginProjection(
        {
            "Rich": rich,
            "Broken": broken_widgets,
            "NonList": non_list_widgets,
            "Disabled": disabled,
        },
        log=log,
    )

    items = projection.widget_metadata()
    by_pair = {(item["plugin_id"], item["key"]): item for item in items}

    assert ("Disabled", "hidden") not in by_pair
    assert by_pair[("Rich", "main")] == {
        "plugin_id": "Rich",
        "plugin_name": "音乐盒",
        "key": "main",
        "name": "主卡片",
        "description": "实时曲目",
        "kind": "custom",
        "source": "widget",
        "has_page": True,
    }
    assert by_pair[("Rich", "unknown_kind")]["kind"] == "metrics"
    assert by_pair[("Rich", "extra")] == {
        "plugin_id": "Rich",
        "plugin_name": "音乐盒",
        "key": "extra",
        "name": "附加仪表盘",
        "description": None,
        "kind": "metrics",
        "source": "dashboard",
        "has_page": True,
    }
    assert by_pair[("NonList", "")]["source"] == "dashboard"
    assert warnings and "bad key!" in warnings[0]
    assert len(errors) == 2


def test_projection_widget_data_supports_explicit_hooks_and_vuetify_or_vue_fallbacks():
    """
    widget_data 支持有参/无参 get_widget_data，并在缺省时把 Vue 或 Vuetify 仪表板投影为小组件载荷。
    """
    keyed = _Plugin(
        plugin_name="显式插件",
        get_widget_data=lambda key="": (
            None if key == "empty" else {"kind": "list", "items": [{"id": "1", "title": key or "默认"}]}
        ),
    )
    zero_arg = _Plugin(
        plugin_name="无参插件",
        get_widget_data=lambda: {"metrics": [{"id": "m", "label": "总数", "value": "5"}]},
    )
    bad_format = _Plugin(get_widget_data=lambda _key="": ["not-a-dict"])
    raising = _Plugin(get_widget_data=lambda _key="": (_ for _ in ()).throw(ValueError("bad")))
    vue_dash = _Plugin(
        plugin_name="联邦面板",
        get_render_mode=lambda: ("vue", "dist/assets"),
        get_dashboard=lambda **_kw: ({}, {"title": "联邦标题"}, []),
    )
    vuetify_metrics = _Plugin(
        plugin_name="站点统计",
        get_render_mode=lambda: ("vuetify", ""),
        get_dashboard=lambda **_kw: (
            {"cols": 12},
            {"title": "站点总览", "subtitle": "今日"},
            [
                {"component": "VIcon", "props": {"icon": "mdi-chart"}},
                {"component": "VCardItem", "props": {"title": "总上传", "value": "12.4 TB"}},
                {
                    "component": "div",
                    "content": [
                        {"component": "span", "text": "做种体积"},
                        {"component": "span", "text": "8.5 TB"},
                    ],
                },
                {
                    "component": "div",
                    "props": {"text": "最近刷新成功"},
                    "content": ["mdi-skip", "#123456", "保种正常", "无异常告警", "连接稳定"],
                },
            ],
        ),
    )
    vuetify_list = _Plugin(
        plugin_name="日志摘要",
        get_render_mode=lambda: ("vuetify", ""),
        get_dashboard=lambda **_kw: (
            {},
            {},
            [{"component": "div", "content": ["条目一", "条目二", "条目三"]}],
        ),
    )
    empty_dash = _Plugin(
        get_render_mode=lambda: ("vuetify", ""),
        get_dashboard=lambda **_kw: None,
    )
    plain_plugin = _Plugin()
    disabled_plugin = _Plugin(enabled=False, get_widget_data=lambda: {})

    projection = PluginProjection(
        {
            "Keyed": keyed,
            "ZeroArg": zero_arg,
            "BadFormat": bad_format,
            "Raising": raising,
            "VueDash": vue_dash,
            "VuetifyMetrics": vuetify_metrics,
            "VuetifyList": vuetify_list,
            "EmptyDash": empty_dash,
            "Plain": plain_plugin,
            "Disabled": disabled_plugin,
        }
    )

    with pytest.raises(PluginNotFoundError):
        projection.widget_data("Missing")
    with pytest.raises(PluginNotFoundError):
        projection.widget_data("Disabled")
    with pytest.raises(PluginDashboardError):
        projection.widget_data("BadFormat")
    with pytest.raises(PluginDashboardError):
        projection.widget_data("Raising")

    assert projection.widget_data("Keyed", "empty") is None
    keyed_payload = projection.widget_data("Keyed", "recent")
    assert keyed_payload is not None
    assert keyed_payload["plugin_id"] == "Keyed"
    assert keyed_payload["items"][0]["title"] == "recent"

    zero_payload = projection.widget_data("ZeroArg")
    assert zero_payload is not None and zero_payload["title"] == "无参插件"

    vue_payload = projection.widget_data("VueDash")
    assert vue_payload is not None and vue_payload["kind"] == "poster"

    metrics_payload = projection.widget_data("VuetifyMetrics")
    assert metrics_payload is not None
    assert metrics_payload["kind"] == "metrics"
    assert [(m["label"], m["value"]) for m in metrics_payload["metrics"]] == [
        ("总上传", "12.4 TB"),
        ("做种体积", "8.5 TB"),
    ]
    assert [i["title"] for i in metrics_payload["items"]] == [
        "最近刷新成功",
        "保种正常",
        "无异常告警",
        "连接稳定",
    ]

    list_payload = projection.widget_data("VuetifyList")
    assert list_payload is not None
    assert list_payload["kind"] == "list"
    assert [i["title"] for i in list_payload["items"]] == ["条目一", "条目二", "条目三"]

    assert projection.widget_data("EmptyDash") is None
    assert projection.widget_data("Plain") is None
