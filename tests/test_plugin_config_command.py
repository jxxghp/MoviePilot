from contextlib import nullcontext

from app.application.plugin.config import PluginConfigCommand, PluginPurgeScope
from app.runtime.extensions.plugin.admission import PluginMutationAdmission


def _command(
    calls: list[tuple],
    *,
    save_result: bool = True,
    mutation=None,
    is_clone: bool = False,
    directory_existed: bool = True,
) -> PluginConfigCommand:
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
        mutation=mutation or (lambda _operation: nullcontext()),
        delete_plugin_data_rows=lambda plugin_id: calls.append(("data_rows", plugin_id)),
        destroy_own_database=lambda plugin_id: calls.append(("own_db", plugin_id)),
        delete_data_directory=lambda plugin_id: (
            calls.append(("data_dir", plugin_id)) or directory_existed
        ),
        purge_instance=lambda plugin_id: (
            calls.append(("purge_row", plugin_id)) or True
        ),
        is_clone=lambda _plugin_id: is_clone,
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


def test_sealed_config_command_rejects_before_first_side_effect() -> None:
    """配置事务在 admission 封口后返回失败，且不得先发布 reset 事件。"""
    calls: list[tuple] = []
    admission = PluginMutationAdmission()
    admission.seal()

    update_result = _command(calls, mutation=admission.hold).update(
        "DemoPlugin",
        {"enabled": True},
    )
    reset_result = _command(calls, mutation=admission.hold).reset("DemoPlugin")

    assert update_result.success is False
    assert reset_result.success is False
    assert "停机阶段" in update_result.message
    assert "停机阶段" in reset_result.message
    assert calls == []


def test_purge_refuses_an_empty_scope() -> None:
    """一项都没勾选时直接拒绝，不做「那就全清吧」这种猜测。"""
    calls: list[tuple] = []

    result = _command(calls).purge("DemoPluginWork", PluginPurgeScope())

    assert result.success is False
    assert "至少选择一项" in result.message
    assert calls == []


def test_purge_only_touches_the_selected_scopes() -> None:
    """逐项执行且互不代劳：没勾的范围一个字节都不能动。"""
    calls: list[tuple] = []

    result = _command(calls).purge(
        "DemoPluginWork",
        PluginPurgeScope(config=True),
    )

    assert result.success is True
    assert result.purged == ("config",)
    assert calls == [
        ("stop", "DemoPluginWork"),
        ("delete_config", "DemoPluginWork", True),
    ]


def test_purge_destroys_the_database_before_removing_its_directory() -> None:
    """库文件就落在该目录下，不先销毁句柄直接删目录会留下被占用的文件。"""
    calls: list[tuple] = []

    result = _command(calls).purge(
        "DemoPluginWork",
        PluginPurgeScope(data_directory=True),
    )

    assert result.success is True
    # 用户只勾了删目录，自有库仍必然先销毁，但不计入回报的清理范围
    assert calls == [
        ("stop", "DemoPluginWork"),
        ("own_db", "DemoPluginWork"),
        ("data_dir", "DemoPluginWork"),
    ]
    assert result.purged == ("data_directory",)


def test_purge_removes_the_row_for_a_clone_but_keeps_it_for_a_host() -> None:
    """分身的存在由那一行表达，清理即消失；本体的行要留着，它还说着「应当装载」。"""
    clone_calls: list[tuple] = []
    clone = _command(clone_calls, is_clone=True).purge(
        "DemoPluginWork", PluginPurgeScope(config=True)
    )

    host_calls: list[tuple] = []
    host = _command(host_calls, is_clone=False).purge(
        "DemoPlugin", PluginPurgeScope(config=True)
    )

    assert clone.instance_removed is True
    assert ("purge_row", "DemoPluginWork") in clone_calls
    assert host.instance_removed is False
    assert not any(call[0] == "purge_row" for call in host_calls)


def test_purge_reports_a_missing_directory_as_not_purged() -> None:
    """目录本就不存在时不谎报清理过它。"""
    calls: list[tuple] = []

    result = _command(calls, directory_existed=False).purge(
        "DemoPluginWork",
        PluginPurgeScope(config=True, data_directory=True),
    )

    assert result.purged == ("config",)
