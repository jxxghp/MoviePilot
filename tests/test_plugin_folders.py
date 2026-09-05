import asyncio
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.plugin import folders
from app.schemas.exception import (
    PersistenceUnavailableError,
    PluginMutationRejectedError,
)
from app.schemas.types import SystemConfigKey


def test_remove_plugin_from_folders_updates_current_and_legacy_shapes(monkeypatch):
    """卸载插件时应同时清理字典格式和旧列表格式的文件夹引用。"""
    stored = {
        "常用": {"plugins": ["DemoPlugin", "OtherPlugin"], "order": 1},
        "旧目录": ["DemoPlugin", "ThirdPlugin"],
    }
    config = MagicMock()
    config.get.return_value = stored
    monkeypatch.setattr(folders, "get_configured_system_config", lambda: config)

    folders.remove_plugin_from_folders("DemoPlugin")

    config.get.assert_called_once_with(SystemConfigKey.PluginFolders)
    written = config.set.call_args.args[1]
    assert written["常用"]["plugins"] == ["OtherPlugin"]
    assert written["旧目录"] == ["ThirdPlugin"]
    assert stored["常用"]["plugins"] == ["DemoPlugin", "OtherPlugin"]
    config.set.assert_called_once_with(SystemConfigKey.PluginFolders, written)


def test_remove_plugin_from_folders_skips_write_when_plugin_is_absent(monkeypatch):
    """插件不在任何文件夹时不得产生无意义配置写入。"""
    config = MagicMock()
    config.get.return_value = {"常用": {"plugins": ["OtherPlugin"]}}
    monkeypatch.setattr(folders, "get_configured_system_config", lambda: config)

    folders.remove_plugin_from_folders("DemoPlugin")

    config.set.assert_not_called()


def test_remove_plugin_from_folders_does_not_block_uninstall_on_config_error(
    monkeypatch,
):
    """文件夹配置读取失败时只记录错误，不应阻断插件卸载主流程。"""
    config = MagicMock()
    config.get.side_effect = RuntimeError("broken folders")
    error = MagicMock()
    monkeypatch.setattr(folders, "get_configured_system_config", lambda: config)
    monkeypatch.setattr(folders.logger, "error", error)

    folders.remove_plugin_from_folders("DemoPlugin")

    error.assert_called_once()


def test_save_folder_maps_ordinary_error_to_failure_result():
    """普通持久化失败应保持旧 HTTP 语义，由结果表达而不是抛出 500。"""
    service = folders.PluginFolderService(
        read=lambda: {},
        write=AsyncMock(side_effect=RuntimeError("write failed")),
        write_sync=MagicMock(),
        mutation=lambda _operation: nullcontext(),
    )

    result = asyncio.run(service.save({"常用": []}))

    assert result.success is False
    assert result.message == "write failed"


def test_get_folder_maps_read_error_to_empty_snapshot():
    """文件夹读取失败应保持旧接口的空对象兜底语义。"""
    service = folders.PluginFolderService(
        read=MagicMock(side_effect=RuntimeError("read failed")),
        write=AsyncMock(),
        write_sync=MagicMock(),
        mutation=lambda _operation: nullcontext(),
    )

    assert service.get_or_empty() == {}


def test_save_folder_propagates_persistence_unavailable():
    """持久化基础设施不可用必须继续交给统一异常处理器映射。"""
    service = folders.PluginFolderService(
        read=lambda: {},
        write=AsyncMock(side_effect=PersistenceUnavailableError()),
        write_sync=MagicMock(),
        mutation=lambda _operation: nullcontext(),
    )

    with pytest.raises(PersistenceUnavailableError):
        asyncio.run(service.save({"常用": []}))


def test_folder_mutations_write_isolated_snapshots():
    """创建、更新和删除必须按顺序写入副本，不能修改配置读取值。"""
    stored = {"常用": ["DemoPlugin"]}
    writes: list[dict] = []

    async def write(value):
        """记录每次 Application 文件夹写入。"""
        writes.append(value)

    service = folders.PluginFolderService(
        read=lambda: stored if not writes else writes[-1],
        write=write,
        write_sync=MagicMock(),
        mutation=lambda _operation: nullcontext(),
    )

    created = asyncio.run(service.create("稍后"))
    updated = asyncio.run(service.update_plugins("稍后", ["OtherPlugin"]))
    deleted = asyncio.run(service.delete("常用"))

    assert created == folders.PluginFolderResult(True, "文件夹 '稍后' 创建成功")
    assert updated == folders.PluginFolderResult(
        True,
        "文件夹 '稍后' 中的插件已更新",
    )
    assert deleted == folders.PluginFolderResult(True, "文件夹 '常用' 删除成功")
    assert writes == [
        {"常用": ["DemoPlugin"], "稍后": []},
        {"常用": ["DemoPlugin"], "稍后": ["OtherPlugin"]},
        {"稍后": ["OtherPlugin"]},
    ]
    assert stored == {"常用": ["DemoPlugin"]}


def test_folder_mutations_report_duplicate_and_missing_names():
    """重复创建和删除不存在目录都应返回稳定业务结果且不写配置。"""
    write = AsyncMock()
    service = folders.PluginFolderService(
        read=lambda: {"常用": []},
        write=write,
        write_sync=MagicMock(),
        mutation=lambda _operation: nullcontext(),
    )

    duplicate = asyncio.run(service.create("常用"))
    missing = asyncio.run(service.delete("不存在"))

    assert duplicate == folders.PluginFolderResult(False, "文件夹 '常用' 已存在")
    assert missing == folders.PluginFolderResult(False, "文件夹 '不存在' 不存在")
    write.assert_not_awaited()


def test_incremental_folder_updates_preserve_metadata_and_use_latest_snapshot():
    """增量更新应在原子端口提供的最新快照上保留展示字段和其他文件夹。"""
    state = {
        "folders": {
            "常用": {"plugins": ["DemoPlugin"], "color": "#00ff00", "order": 2},
            "稍后": ["OtherPlugin"],
        }
    }

    async def update(change):
        """模拟配置原子端口发布 mutation 返回的新快照。"""
        result, value = change(state["folders"])
        state["folders"] = value
        return result

    service = folders.PluginFolderService(
        read=lambda: state["folders"],
        write=AsyncMock(),
        write_sync=MagicMock(),
        mutation=lambda _operation: nullcontext(),
        update=update,
    )

    appearance = asyncio.run(
        service.update_folder("常用", changes={"icon": "mdi-folder-star"})
    )
    members = asyncio.run(
        service.update_plugins(
            "常用",
            ["DemoPlugin", "ThirdPlugin"],
            ["DemoPlugin"],
        )
    )
    moved = asyncio.run(service.assign_plugin("稍后", "DemoPlugin"))
    removed = asyncio.run(service.remove_plugin_from_folder("稍后", "OtherPlugin"))
    renamed = asyncio.run(service.update_folder("常用", new_name="工具"))

    assert all(result.success for result in (appearance, members, moved, removed, renamed))
    assert list(state["folders"]) == ["工具", "稍后"]
    assert state["folders"]["工具"] == {
        "plugins": ["ThirdPlugin"],
        "color": "#00ff00",
        "icon": "mdi-folder-star",
        "order": 2,
    }
    assert state["folders"]["稍后"] == ["DemoPlugin"]


def test_folder_member_replacement_rejects_stale_snapshot_without_losing_config():
    """成员顺序条件不匹配时应保留当前成员和文件夹展示配置。"""
    state = {
        "folders": {
            "常用": {"plugins": ["CurrentPlugin"], "color": "#00ff00"},
        }
    }

    async def update(change):
        """模拟即使业务拒绝也发布同值的底层原子配置端口。"""
        result, value = change(state["folders"])
        state["folders"] = value
        return result

    service = folders.PluginFolderService(
        read=lambda: state["folders"],
        write=AsyncMock(),
        write_sync=MagicMock(),
        mutation=lambda _operation: nullcontext(),
        update=update,
    )

    result = asyncio.run(
        service.update_plugins(
            "常用",
            ["ReplacementPlugin"],
            ["StalePlugin"],
        )
    )

    assert result == folders.PluginFolderResult(
        False,
        "插件文件夹已被其他请求修改，请重新读取后再试",
    )
    assert state["folders"]["常用"] == {
        "plugins": ["CurrentPlugin"],
        "color": "#00ff00",
    }


def test_folder_mutation_rejection_is_returned_without_accessing_storage():
    """运行时封口时应直接返回拒绝结果，不得继续访问配置存储。"""
    read = MagicMock()

    def reject(operation):
        """模拟插件运行时拒绝新的可变事务。"""
        manager = MagicMock()
        manager.__enter__.side_effect = PluginMutationRejectedError(operation)
        return manager

    service = folders.PluginFolderService(
        read=read,
        write=AsyncMock(),
        write_sync=MagicMock(),
        mutation=reject,
    )

    result = asyncio.run(service.update_plugins("常用", ["DemoPlugin"]))

    assert result.success is False
    assert result.message == "插件运行时已进入停机阶段，拒绝更新插件文件夹 常用"
    read.assert_not_called()


def test_all_folder_write_commands_map_runtime_sealing_to_results():
    """保存、创建和删除入口都应把运行时封口映射为稳定失败结果。"""
    def reject(operation):
        """模拟每个写命令在进入配置存储前被运行时拒绝。"""
        manager = MagicMock()
        manager.__enter__.side_effect = PluginMutationRejectedError(operation)
        return manager

    service = folders.PluginFolderService(
        read=MagicMock(),
        write=AsyncMock(),
        write_sync=MagicMock(),
        mutation=reject,
    )

    saved = asyncio.run(service.save({"常用": []}))
    created = asyncio.run(service.create("常用"))
    deleted = asyncio.run(service.delete("常用"))

    assert saved.message.endswith("拒绝保存插件文件夹配置")
    assert created.message.endswith("拒绝创建插件文件夹 常用")
    assert deleted.message.endswith("拒绝删除插件文件夹 常用")
    assert saved.success is created.success is deleted.success is False


def test_add_clone_inherits_first_current_or_legacy_folder():
    """分身只继承原插件命中的首个目录，并兼容字典和旧列表形态。"""
    for stored in (
        {"常用": {"plugins": ["DemoPlugin"], "order": 1}},
        {"旧目录": ["DemoPlugin"]},
    ):
        state = {"folders": stored}

        def persist(value):
            """模拟同步配置存储把写入值发布为下一次读取快照。"""
            state["folders"] = value

        write_sync = MagicMock(side_effect=persist)
        service = folders.PluginFolderService(
            read=lambda state=state: state["folders"],
            write=AsyncMock(),
            write_sync=write_sync,
            mutation=lambda _operation: nullcontext(),
        )

        service.add_clone("DemoPlugin", "DemoPlugin__copy")
        service.add_clone("DemoPlugin", "DemoPlugin__copy")

        written = write_sync.call_args.args[0]
        plugins = folders._folder_plugins(next(iter(written.values())))
        assert plugins == ["DemoPlugin", "DemoPlugin__copy"]
        write_sync.assert_called_once()


def test_add_clone_and_remove_plugin_ignore_invalid_folder_shapes():
    """损坏的目录条目不得被当作插件列表，也不能触发无意义写入。"""
    write_sync = MagicMock()
    service = folders.PluginFolderService(
        read=lambda: {"损坏": {"plugins": "DemoPlugin"}},
        write=AsyncMock(),
        write_sync=write_sync,
        mutation=lambda _operation: nullcontext(),
    )

    service.add_clone("DemoPlugin", "DemoPlugin__copy")
    service.remove_plugin("DemoPlugin")

    write_sync.assert_not_called()


def test_add_clone_failure_and_public_entry_do_not_escape(monkeypatch):
    """分身目录处理失败只记录诊断，公开入口仍委托唯一服务实例。"""
    error = MagicMock()
    failing = folders.PluginFolderService(
        read=MagicMock(side_effect=RuntimeError("broken folders")),
        write=AsyncMock(),
        write_sync=MagicMock(),
        mutation=lambda _operation: nullcontext(),
    )
    monkeypatch.setattr(folders.logger, "error", error)

    failing.add_clone("DemoPlugin", "DemoPlugin__copy")

    error.assert_called_once()
    service = MagicMock()
    monkeypatch.setattr(folders, "get_plugin_folder_service", lambda: service)

    folders.add_clone_to_plugin_folder("DemoPlugin", "DemoPlugin__copy")

    service.add_clone.assert_called_once_with("DemoPlugin", "DemoPlugin__copy")
