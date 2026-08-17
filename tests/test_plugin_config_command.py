from app.application.plugin.config import PluginConfigCommand


def _command(calls: list[tuple], *, save_result: bool = True) -> PluginConfigCommand:
    """构造记录端口调用顺序的插件配置用例。"""
    return PluginConfigCommand(
        save_config=lambda plugin_id, config, force: (
            calls.append(("save", plugin_id, config, force)) or save_result
        ),
        initialize=lambda plugin_id, config: calls.append(
            ("initialize", plugin_id, config)
        ),
        stop=lambda plugin_id: calls.append(("stop", plugin_id)),
        delete_config=lambda plugin_id, force: (
            calls.append(("delete_config", plugin_id, force)) or True
        ),
        delete_data=lambda plugin_id, force: (
            calls.append(("delete_data", plugin_id, force)) or True
        ),
        reload_runtime=lambda plugin_id: calls.append(("reload", plugin_id)),
        publish_reset=lambda plugin_id: calls.append(("publish", plugin_id)),
        refresh_registrations=lambda plugin_id: calls.append(
            ("registrations", plugin_id)
        ),
    )


def test_update_stops_before_runtime_side_effects_when_save_fails() -> None:
    """配置持久化失败时不得初始化插件或刷新宿主注册。"""
    calls: list[tuple] = []

    result = _command(calls, save_result=False).update("DemoPlugin", {"enabled": True})

    assert result.success is False
    assert result.message == "插件配置保存失败"
    assert calls == [("save", "DemoPlugin", {"enabled": True}, False)]


def test_update_refreshes_runtime_only_after_config_is_saved() -> None:
    """配置保存成功后按初始化、注册刷新顺序生效。"""
    calls: list[tuple] = []

    result = _command(calls).update("DemoPlugin", {"enabled": True})

    assert result.success is True
    assert calls == [
        ("save", "DemoPlugin", {"enabled": True}, False),
        ("initialize", "DemoPlugin", {"enabled": True}),
        ("registrations", "DemoPlugin"),
    ]


def test_reset_preserves_compensation_cleanup_and_reload_order() -> None:
    """重置必须先让插件补偿，再停止、清理并重建运行态和注册。"""
    calls: list[tuple] = []

    result = _command(calls).reset("DemoPlugin")

    assert result.success is True
    assert calls == [
        ("publish", "DemoPlugin"),
        ("stop", "DemoPlugin"),
        ("delete_config", "DemoPlugin", True),
        ("delete_data", "DemoPlugin", True),
        ("reload", "DemoPlugin"),
        ("registrations", "DemoPlugin"),
    ]
