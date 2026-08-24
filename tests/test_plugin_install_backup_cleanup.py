"""插件安装成功后的临时回滚备份生命周期测试。"""

from pathlib import Path

import pytest

from app.adapters.external.market import PluginHelper


@pytest.mark.asyncio
async def test_successful_install_flows_remove_transient_backups(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """同步与异步安装成功后都必须删除临时回滚副本。"""
    helper = PluginHelper()
    sync_backup = tmp_path / "sync-backup"
    async_backup = tmp_path / "async-backup"
    sync_backup.mkdir()
    async_backup.mkdir()

    monkeypatch.setattr(
        helper,
        "_PluginHelper__backup_plugin",
        lambda _pid: str(sync_backup),
    )
    monkeypatch.setattr(
        helper,
        "_PluginHelper__remove_old_plugin",
        lambda _pid: None,
    )
    monkeypatch.setattr(
        helper,
        "_PluginHelper__install_dependencies_if_required",
        lambda _pid: (False, False, "不存在依赖"),
    )
    monkeypatch.setattr(
        helper,
        "refresh_persistent_plugin_backup",
        lambda _pid: True,
    )

    sync_result = helper._PluginHelper__install_flow_sync(
        "DemoPlugin",
        False,
        lambda: (True, ""),
    )

    async def backup_plugin(_pid: str) -> str:
        """返回异步路径的临时回滚副本。"""
        return str(async_backup)

    async def remove_plugin(_pid: str) -> None:
        """隔离测试中的真实插件目录删除。"""

    async def install_dependencies(_pid: str) -> tuple[bool, bool, str]:
        """表示测试插件没有额外依赖。"""
        return False, False, "不存在依赖"

    async def prepare_content() -> tuple[bool, str]:
        """表示异步内容准备成功。"""
        return True, ""

    monkeypatch.setattr(
        helper,
        "_PluginHelper__async_backup_plugin",
        backup_plugin,
    )
    monkeypatch.setattr(
        helper,
        "_PluginHelper__async_remove_old_plugin",
        remove_plugin,
    )
    monkeypatch.setattr(
        helper,
        "_PluginHelper__async_install_dependencies_if_required",
        install_dependencies,
    )

    async_result = await helper._PluginHelper__install_flow_async(
        "DemoPlugin",
        False,
        prepare_content,
    )

    assert sync_result == async_result == (True, "")
    assert not sync_backup.exists()
    assert not async_backup.exists()
