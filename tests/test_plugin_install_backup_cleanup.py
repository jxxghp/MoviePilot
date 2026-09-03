"""插件安装成功后的临时回滚备份生命周期测试。"""

from pathlib import Path

import pytest

from app.adapters.external.market import PluginHelper
from app.adapters.external.plugin.client import PluginPackageSourceClient
from app.adapters.system.plugin.package import (
    PluginPackageManager,
    _PluginContentPlacement,
)


@pytest.mark.asyncio
async def test_successful_install_flows_remove_transient_backups(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """同步与异步安装成功后都必须删除临时回滚副本。"""
    package = PluginPackageManager(
        source=PluginPackageSourceClient(PluginHelper())
    )
    sync_backup = tmp_path / "sync-backup"
    async_backup = tmp_path / "async-backup"
    sync_backup.mkdir()
    async_backup.mkdir()

    monkeypatch.setattr(
        package,
        "_PluginPackageManager__backup_plugin",
        lambda _pid: str(sync_backup),
    )
    monkeypatch.setattr(
        package,
        "_PluginPackageManager__remove_old_plugin",
        lambda _pid: None,
    )
    monkeypatch.setattr(
        package,
        "_PluginPackageManager__place_staged_plugin_content",
        lambda _pid, _plugin_dir, _staging_dir, _source_label: _PluginContentPlacement(
            tmp_path / "content", "", None, True
        ),
    )
    monkeypatch.setattr(
        package,
        "_PluginPackageManager__install_dependencies_if_required",
        lambda _pid, _content_dir, _before=None: (False, False, "不存在依赖"),
    )
    monkeypatch.setattr(
        package,
        "refresh_persistent_backup",
        lambda _pid: True,
    )

    sync_result = package._PluginPackageManager__install_flow_sync(
        "DemoPlugin",
        False,
        lambda _staging_dir: (True, ""),
    )

    async def backup_plugin(_pid: str) -> str:
        """返回异步路径的临时回滚副本。"""
        return str(async_backup)

    async def remove_plugin(_pid: str) -> None:
        """隔离测试中的真实插件目录删除。"""

    async def install_dependencies(
        _pid: str, _content_dir: Path, _before=None
    ) -> tuple[bool, bool, str]:
        """表示测试插件没有额外依赖。"""
        return False, False, "不存在依赖"

    async def prepare_content(_staging_dir: Path) -> tuple[bool, str]:
        """表示异步内容准备成功。"""
        return True, ""

    monkeypatch.setattr(
        package,
        "_PluginPackageManager__async_backup_plugin",
        backup_plugin,
    )
    monkeypatch.setattr(
        package,
        "_PluginPackageManager__async_remove_old_plugin",
        remove_plugin,
    )
    monkeypatch.setattr(
        package,
        "_PluginPackageManager__async_install_dependencies_if_required",
        install_dependencies,
    )

    async_result = await package._PluginPackageManager__install_flow_async(
        "DemoPlugin",
        False,
        prepare_content,
    )

    assert sync_result == async_result == (True, "")
    assert not sync_backup.exists()
    assert not async_backup.exists()
