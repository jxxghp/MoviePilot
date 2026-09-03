import errno
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.adapters.system.plugin import package as plugin_package_module
from app.adapters.system.plugin.package import PluginPackageManager
from app.runtime.dependencies.native import LoadedNativeDependencySnapshot

_swap_staged_plugin_content = (
    PluginPackageManager._PluginPackageManager__swap_staged_plugin_content
)


def _manager(monkeypatch, tmp_path: Path) -> PluginPackageManager:
    """构造使用隔离运行目录和事务目录的插件包管理器。"""
    settings = SimpleNamespace(
        ROOT_PATH=tmp_path,
        TEMP_PATH=tmp_path / "temp",
        CONFIG_PATH=tmp_path / "config",
    )
    monkeypatch.setattr(
        "app.adapters.system.plugin.package.get_runtime_setting",
        lambda key: getattr(settings, key),
    )
    monkeypatch.setattr(
        "app.adapters.system.plugin.package.capture_loaded_native_dependencies",
        LoadedNativeDependencySnapshot,
    )
    return PluginPackageManager(source=Mock())


def test_checkpoint_rollback_restores_existing_package(monkeypatch, tmp_path):
    """已存在插件在后续阶段失败时应完整恢复原文件。"""
    manager = _manager(monkeypatch, tmp_path)
    plugin_dir = tmp_path / "app" / "plugins" / "demoplugin"
    plugin_dir.mkdir(parents=True)
    source_file = plugin_dir / "__init__.py"
    source_file.write_text("old", encoding="utf-8")

    checkpoint = manager.checkpoint("DemoPlugin")
    source_file.write_text("new", encoding="utf-8")
    (plugin_dir / "partial.py").write_text("partial", encoding="utf-8")
    manager.rollback(checkpoint)

    assert source_file.read_text(encoding="utf-8") == "old"
    assert not (plugin_dir / "partial.py").exists()
    assert not checkpoint.transaction_dir.exists()


def test_checkpoint_uses_injected_plugin_root(monkeypatch, tmp_path):
    """显式装配的插件根目录必须覆盖全局运行目录设置。"""
    _manager(monkeypatch, tmp_path)
    plugin_root = tmp_path / "custom-plugins"
    plugin_dir = plugin_root / "demoplugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("custom", encoding="utf-8")
    manager = PluginPackageManager(source=Mock(), plugin_root=plugin_root)

    checkpoint = manager.checkpoint("DemoPlugin")

    assert checkpoint.plugin_dir == plugin_dir.resolve()
    assert (checkpoint.transaction_dir / "package" / "__init__.py").read_text(
        encoding="utf-8"
    ) == "custom"
    manager.commit(checkpoint)


def test_remove_plugin_uses_package_owner_path_boundary(tmp_path):
    """物理卸载只删除注入根目录内的目标插件。"""
    plugin_root = tmp_path / "plugins"
    plugin_dir = plugin_root / "demoplugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("plugin", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    manager = PluginPackageManager(source=Mock(), plugin_root=plugin_root)

    assert manager.remove_plugin("DemoPlugin") is True
    assert not plugin_dir.exists()
    assert outside.exists()
    assert manager.remove_plugin("DemoPlugin") is False
    with pytest.raises(ValueError, match="非法插件ID"):
        manager.remove_plugin("../outside")


@pytest.mark.parametrize(
    ("remote_path", "package_version"),
    [
        ("../escaped.py", None),
        ("/tmp/escaped.py", None),
        ("C:\\escaped.py", None),
        ("plugins/other/file.py", None),
        ("plugins.v2/demoplugin/../escaped.py", "v2"),
    ],
)
def test_file_list_download_rejects_paths_outside_plugin_root(
    monkeypatch,
    tmp_path,
    remote_path,
    package_version,
):
    """同步文件列表安装不得把远端路径写到当前插件目录之外。"""
    plugin_root = tmp_path / "plugins"
    manager = PluginPackageManager(source=Mock(), plugin_root=plugin_root)
    request = Mock()
    monkeypatch.setattr(
        manager,
        "_PluginPackageManager__request_with_fallback",
        request,
    )

    result = manager._PluginPackageManager__download_files(
        "DemoPlugin",
        [{"path": remote_path, "download_url": "https://example.invalid/file"}],
        "owner/repo",
        package_version,
        plugin_root / "demoplugin",
    )

    assert result == (False, "插件文件路径无效")
    request.assert_not_called()
    assert not (tmp_path / "escaped.py").exists()


@pytest.mark.asyncio
async def test_async_file_list_download_rejects_path_outside_plugin_root(
    monkeypatch,
    tmp_path,
):
    """异步文件列表安装复用同一受控路径边界。"""
    manager = PluginPackageManager(source=Mock(), plugin_root=tmp_path / "plugins")
    request = AsyncMock()
    monkeypatch.setattr(
        manager,
        "_PluginPackageManager__async_request_with_fallback",
        request,
    )

    result = await manager._PluginPackageManager__async_download_files(
        "DemoPlugin",
        [
            {
                "path": "plugins.v2/demoplugin/../../escaped.py",
                "download_url": "https://example.invalid/file",
            }
        ],
        "owner/repo",
        "v2",
        tmp_path / "plugins" / "demoplugin",
    )

    assert result == (False, "插件文件路径无效")
    request.assert_not_awaited()
    assert not (tmp_path / "escaped.py").exists()


@pytest.mark.asyncio
async def test_file_list_download_rejects_traversal_directory_names(
    monkeypatch,
    tmp_path,
):
    """目录项名称不得扩大后续市场查询到当前插件树之外。"""
    manager = PluginPackageManager(source=Mock(), plugin_root=tmp_path / "plugins")
    sync_query = Mock()
    async_query = AsyncMock()
    monkeypatch.setattr(
        manager,
        "_PluginPackageManager__get_file_list",
        sync_query,
    )
    monkeypatch.setattr(
        manager,
        "_PluginPackageManager__async_get_file_list",
        async_query,
    )
    item = {"name": "..", "download_url": None}
    dest_root = tmp_path / "plugins" / "demoplugin"

    assert manager._PluginPackageManager__download_files(
        "DemoPlugin", [item], "owner/repo", None, dest_root
    ) == (False, "插件目录路径无效")
    assert await manager._PluginPackageManager__async_download_files(
        "DemoPlugin", [item], "owner/repo", None, dest_root
    ) == (False, "插件目录路径无效")
    sync_query.assert_not_called()
    async_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_list_download_maps_valid_paths_into_injected_plugin_root(
    monkeypatch,
    tmp_path,
):
    """同步与异步文件列表都只写入显式装配的插件根目录。"""
    plugin_root = tmp_path / "plugins"
    response = SimpleNamespace(status_code=200, text="payload")
    manager = PluginPackageManager(source=Mock(), plugin_root=plugin_root)
    monkeypatch.setattr(
        manager,
        "_PluginPackageManager__request_with_fallback",
        Mock(return_value=response),
    )
    monkeypatch.setattr(
        manager,
        "_PluginPackageManager__async_request_with_fallback",
        AsyncMock(return_value=response),
    )
    item = {
        "path": "plugins.v2/demoplugin/nested/file.py",
        "download_url": "https://example.invalid/file",
    }
    dest_root = plugin_root / "demoplugin"

    assert manager._PluginPackageManager__download_files(
        "DemoPlugin", [item], "owner/repo", "v2", dest_root
    ) == (True, "")
    assert await manager._PluginPackageManager__async_download_files(
        "DemoPlugin", [item], "owner/repo", "v2", dest_root
    ) == (True, "")
    assert (plugin_root / "demoplugin" / "nested" / "file.py").read_text(
        encoding="utf-8"
    ) == "payload"


def test_checkpoint_does_not_scan_native_dependencies(monkeypatch, tmp_path):
    """普通插件文件快照不应枚举宿主全部原生发行包。"""
    manager = _manager(monkeypatch, tmp_path)
    capture = Mock()
    monkeypatch.setattr(
        "app.adapters.system.plugin.package.capture_loaded_native_dependencies",
        capture,
    )

    checkpoint = manager.checkpoint("DemoPlugin")

    assert checkpoint.native_dependencies is None
    capture.assert_not_called()
    assert manager.native_dependency_changes(checkpoint) == ()


def test_checkpoint_rollback_removes_new_package(monkeypatch, tmp_path):
    """首次安装失败时应删除安装过程创建的不完整目录。"""
    manager = _manager(monkeypatch, tmp_path)
    checkpoint = manager.checkpoint("DemoPlugin")
    plugin_dir = tmp_path / "app" / "plugins" / "demoplugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("partial", encoding="utf-8")

    manager.rollback(checkpoint)

    assert not plugin_dir.exists()
    assert not checkpoint.transaction_dir.exists()


def test_rollback_does_not_delete_package_when_snapshot_is_missing(monkeypatch, tmp_path):
    """补偿快照损坏时先失败，不能先删除当前可用插件。"""
    manager = _manager(monkeypatch, tmp_path)
    plugin_dir = tmp_path / "app" / "plugins" / "demoplugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("old", encoding="utf-8")

    checkpoint = manager.checkpoint("DemoPlugin")
    shutil.rmtree(checkpoint.transaction_dir / "package")
    (plugin_dir / "__init__.py").write_text("new", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        manager.rollback(checkpoint)

    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "new"


def test_durable_checkpoint_stages_backup_without_overwriting_current_backup(
    monkeypatch,
    tmp_path,
):
    """数据库提交前只准备新备份，现有容器恢复材料保持可用。"""
    manager = _manager(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "app.adapters.system.plugin.package.SystemUtils.is_docker",
        lambda: True,
    )
    plugin_dir = tmp_path / "app" / "plugins" / "demoplugin"
    backup_dir = tmp_path / "config" / "plugins_backup" / "demoplugin"
    plugin_dir.mkdir(parents=True)
    backup_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("new", encoding="utf-8")
    (backup_dir / "__init__.py").write_text("old", encoding="utf-8")

    checkpoint = manager.checkpoint("DemoPlugin", "txn-1")
    manager.stage_persistent_backup(checkpoint)

    assert checkpoint.transaction_dir.parent == tmp_path / "config" / "plugin_transactions"
    assert (backup_dir / "__init__.py").read_text(encoding="utf-8") == "old"
    assert checkpoint.backup_staging_dir is not None
    assert (checkpoint.backup_staging_dir / "__init__.py").read_text(
        encoding="utf-8"
    ) == "new"


def test_activate_and_finalize_persistent_backup_are_retryable(monkeypatch, tmp_path):
    """备份激活保留旧载荷，数据库提交后的清理可以重复执行。"""
    manager = _manager(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "app.adapters.system.plugin.package.SystemUtils.is_docker",
        lambda: True,
    )
    plugin_dir = tmp_path / "app" / "plugins" / "demoplugin"
    backup_dir = tmp_path / "config" / "plugins_backup" / "demoplugin"
    plugin_dir.mkdir(parents=True)
    backup_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("new", encoding="utf-8")
    (backup_dir / "__init__.py").write_text("old", encoding="utf-8")
    checkpoint = manager.checkpoint("DemoPlugin", "txn-2")
    manager.stage_persistent_backup(checkpoint)

    manager.activate_persistent_backup(checkpoint)
    manager.activate_persistent_backup(checkpoint)

    assert (backup_dir / "__init__.py").read_text(encoding="utf-8") == "new"
    assert checkpoint.backup_staging_dir is not None
    assert not checkpoint.backup_staging_dir.exists()
    assert checkpoint.backup_previous_dir is not None
    assert (checkpoint.backup_previous_dir / "__init__.py").read_text(
        encoding="utf-8"
    ) == "old"

    manager.finalize_persistent_backup(checkpoint)
    manager.finalize_persistent_backup(checkpoint)

    assert not checkpoint.backup_previous_dir.exists()


def test_rollback_removes_staging_but_preserves_current_backup(monkeypatch, tmp_path):
    """提交前失败只恢复运行目录，不修改上一份容器恢复备份。"""
    manager = _manager(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "app.adapters.system.plugin.package.SystemUtils.is_docker",
        lambda: True,
    )
    plugin_dir = tmp_path / "app" / "plugins" / "demoplugin"
    backup_dir = tmp_path / "config" / "plugins_backup" / "demoplugin"
    plugin_dir.mkdir(parents=True)
    backup_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("old-runtime", encoding="utf-8")
    (backup_dir / "__init__.py").write_text("old-backup", encoding="utf-8")
    checkpoint = manager.checkpoint("DemoPlugin", "txn-3")
    (plugin_dir / "__init__.py").write_text("new-runtime", encoding="utf-8")
    manager.stage_persistent_backup(checkpoint)

    manager.rollback(checkpoint)

    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "old-runtime"
    assert (backup_dir / "__init__.py").read_text(encoding="utf-8") == "old-backup"
    assert checkpoint.backup_staging_dir is not None
    assert not checkpoint.backup_staging_dir.exists()


def test_rollback_after_backup_activation_restores_previous_backup(
    monkeypatch,
    tmp_path,
):
    """数据库提交前失败时，已激活的新备份必须回退到上一份载荷。"""
    manager = _manager(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "app.adapters.system.plugin.package.SystemUtils.is_docker",
        lambda: True,
    )
    plugin_dir = tmp_path / "app" / "plugins" / "demoplugin"
    backup_dir = tmp_path / "config" / "plugins_backup" / "demoplugin"
    plugin_dir.mkdir(parents=True)
    backup_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("old-runtime", encoding="utf-8")
    (backup_dir / "__init__.py").write_text("old-backup", encoding="utf-8")
    checkpoint = manager.checkpoint("DemoPlugin", "txn-4")
    (plugin_dir / "__init__.py").write_text("new-runtime", encoding="utf-8")
    manager.stage_persistent_backup(checkpoint)
    manager.activate_persistent_backup(checkpoint)

    manager.rollback(checkpoint)

    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "old-runtime"
    assert (backup_dir / "__init__.py").read_text(encoding="utf-8") == "old-backup"


def test_restore_checkpoint_derives_only_controlled_paths(monkeypatch, tmp_path):
    """崩溃回放只按事务 ID 在受控根目录内重建文件引用。"""
    manager = _manager(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "app.adapters.system.plugin.package.SystemUtils.is_docker",
        lambda: True,
    )

    checkpoint = manager.restore_checkpoint(
        plugin_id="DemoPlugin",
        transaction_id="txn-5",
        plugin_existed=True,
        persistent_backup_existed=False,
    )

    assert checkpoint.transaction_dir == (
        tmp_path / "config" / "plugin_transactions" / "txn-5"
    )
    assert checkpoint.backup_staging_dir == (
        tmp_path / "config" / "plugins_backup" / ".demoplugin.staging-txn-5"
    )
    assert checkpoint.backup_previous_dir == (
        tmp_path / "config" / "plugins_backup" / ".demoplugin.previous-txn-5"
    )


def test_local_sync_failure_restores_previous_runtime_copy(monkeypatch, tmp_path):
    """本地来源不可复制时不得丢失已经运行的插件副本。"""
    manager = _manager(monkeypatch, tmp_path)
    plugin_dir = tmp_path / "app" / "plugins" / "demoplugin"
    plugin_dir.mkdir(parents=True)
    source_file = plugin_dir / "__init__.py"
    source_file.write_text("stable", encoding="utf-8")
    missing_source = tmp_path / "missing" / "demoplugin"

    assert manager.sync_local("DemoPlugin", missing_source) is False

    assert source_file.read_text(encoding="utf-8") == "stable"


def test_clone_rewrites_python_and_federation_assets(monkeypatch, tmp_path):
    """插件分身文件处理应由包适配器完成并隔离配置命名空间。"""
    manager = _manager(monkeypatch, tmp_path)
    plugin_dir = tmp_path / "app" / "plugins" / "demoplugin"
    dist_dir = plugin_dir / "dist"
    dist_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text(
        "class DemoPlugin:\n"
        "    plugin_name = 'Demo'\n"
        "    plugin_desc = 'Description'\n"
        "    plugin_config_prefix = 'demo_'\n"
        "    plugin_version = '1.0.0'\n"
        "    plugin_icon = 'old.png'\n"
        "    def init_plugin(self, config=None):\n"
        "        pass\n",
        encoding="utf-8",
    )
    (dist_dir / "demoplugin.js").write_text(
        "const name = 'DemoPlugin'; const css = 'css__DemoPlugin__root';",
        encoding="utf-8",
    )

    success, message = manager.clone(
        plugin_id="DemoPlugin",
        clone_id="DemoPluginBlue",
        original_class_name="DemoPlugin",
        suffix="blue",
        name="Demo Blue",
        description="Blue clone",
        version="2.0.0",
        icon="blue.png",
    )

    clone_dir = tmp_path / "app" / "plugins" / "demopluginblue"
    clone_source = (clone_dir / "__init__.py").read_text(encoding="utf-8")
    assert success is True
    assert message == "文件修改成功"
    assert "class DemoPluginblue" in clone_source
    assert 'plugin_name = "Demo Blue"' in clone_source
    assert 'plugin_config_prefix = "demopluginblue_"' in clone_source
    assert "is_clone = True" in clone_source
    assert (clone_dir / "dist" / "demopluginblue.js").is_file()


def _fake_rename_failing_staging_source(staging_dir: Path):
    """构造只让暂存目录改名失败（模拟 EXDEV）、其余改名走真实实现的 os.rename 替身。"""
    real_rename = os.rename

    def fake_rename(src, dst):
        if str(src) == str(staging_dir):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst)

    return fake_rename


def _fake_copytree_leaves_partial_content_then_fails(marker_name: str, message: str):
    """构造先写入半份新内容再抛错的 copytree 替身，模拟复制中途磁盘写满。"""

    def fake_copytree(src, dst, **_kwargs):
        Path(dst).mkdir(parents=True, exist_ok=True)
        (Path(dst) / marker_name).write_text("partial", encoding="utf-8")
        raise OSError(errno.ENOSPC, message)

    return fake_copytree


def test_swap_staged_plugin_content_keeps_old_content_intact_when_cross_device_copy_fails(
    monkeypatch, tmp_path,
):
    """跨设备退化为复制时复制中途失败，旧内容必须逐字节完好，不留半份新内容，原始异常向上抛出。"""
    staging_dir = tmp_path / "staging"
    final_dir = tmp_path / "plugins" / "demoplugin"
    staging_dir.mkdir(parents=True)
    (staging_dir / "new.txt").write_text("new-payload", encoding="utf-8")
    final_dir.mkdir(parents=True)
    (final_dir / "old.txt").write_text("old-payload", encoding="utf-8")

    monkeypatch.setattr(
        plugin_package_module.os,
        "rename",
        _fake_rename_failing_staging_source(staging_dir),
    )
    monkeypatch.setattr(
        plugin_package_module.shutil,
        "copytree",
        _fake_copytree_leaves_partial_content_then_fails("partial.txt", "No space left on device"),
    )

    with pytest.raises(OSError) as exc_info:
        _swap_staged_plugin_content(staging_dir, final_dir)

    assert exc_info.value.errno == errno.ENOSPC
    assert final_dir.is_dir()
    assert (final_dir / "old.txt").read_text(encoding="utf-8") == "old-payload"
    assert not (final_dir / "new.txt").exists()
    assert not (final_dir / "partial.txt").exists()
    assert list(final_dir.parent.glob(f".{final_dir.name}.previous-*")) == []


def test_swap_staged_plugin_content_leaves_nothing_behind_when_target_never_existed(
    monkeypatch, tmp_path,
):
    """全新版本目录首次落盘时复制中途失败，目标目录必须完全回到不存在状态，不留半成品。"""
    staging_dir = tmp_path / "staging"
    final_dir = tmp_path / "plugins" / "demoplugin" / "v3_0_0"
    staging_dir.mkdir(parents=True)
    (staging_dir / "new.txt").write_text("new-payload", encoding="utf-8")

    monkeypatch.setattr(
        plugin_package_module.os,
        "rename",
        _fake_rename_failing_staging_source(staging_dir),
    )
    monkeypatch.setattr(
        plugin_package_module.shutil,
        "copytree",
        _fake_copytree_leaves_partial_content_then_fails("partial.txt", "No space left on device"),
    )

    with pytest.raises(OSError):
        _swap_staged_plugin_content(staging_dir, final_dir)

    assert not final_dir.exists()
    assert list(final_dir.parent.glob(f".{final_dir.name}.previous-*")) == []


def test_swap_staged_plugin_content_atomic_rename_success_path_is_unaffected(tmp_path):
    """同一文件系统内可原子改名时保持一次改名换入，不会改走复制加删除的退化路径。"""
    staging_dir = tmp_path / "staging"
    final_dir = tmp_path / "plugins" / "demoplugin"
    staging_dir.mkdir(parents=True)
    (staging_dir / "new.txt").write_text("new-payload", encoding="utf-8")
    final_dir.mkdir(parents=True)
    (final_dir / "old.txt").write_text("old-payload", encoding="utf-8")

    _swap_staged_plugin_content(staging_dir, final_dir)

    assert not staging_dir.exists()
    assert (final_dir / "new.txt").read_text(encoding="utf-8") == "new-payload"
    assert not (final_dir / "old.txt").exists()
    assert list(final_dir.parent.glob(f".{final_dir.name}.previous-*")) == []


def test_swap_staged_plugin_content_preserves_recovery_material_when_rollback_itself_fails(
    monkeypatch, tmp_path,
):
    """回滚换回旧内容也失败时不吞掉原始异常，且旧内容以恢复材料形式保留而不是被清空。"""
    staging_dir = tmp_path / "staging"
    final_dir = tmp_path / "plugins" / "demoplugin"
    staging_dir.mkdir(parents=True)
    (staging_dir / "new.txt").write_text("new-payload", encoding="utf-8")
    final_dir.mkdir(parents=True)
    (final_dir / "old.txt").write_text("old-payload", encoding="utf-8")

    real_rename = os.rename
    previous_prefix = str(final_dir.parent / f".{final_dir.name}.previous-")

    def fake_rename(src, dst):
        src_str = str(src)
        if src_str == str(staging_dir):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        if src_str.startswith(previous_prefix):
            raise OSError(errno.EACCES, "Permission denied")
        return real_rename(src, dst)

    monkeypatch.setattr(plugin_package_module.os, "rename", fake_rename)
    monkeypatch.setattr(
        plugin_package_module.shutil,
        "copytree",
        _fake_copytree_leaves_partial_content_then_fails("partial.txt", "No space left on device"),
    )

    with pytest.raises(OSError) as exc_info:
        _swap_staged_plugin_content(staging_dir, final_dir)

    assert exc_info.value.errno == errno.ENOSPC
    assert isinstance(exc_info.value.__cause__, OSError)
    assert exc_info.value.__cause__.errno == errno.EACCES
    preserved = list(final_dir.parent.glob(f".{final_dir.name}.previous-*"))
    assert len(preserved) == 1
    assert (preserved[0] / "old.txt").read_text(encoding="utf-8") == "old-payload"
