import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.adapters.system.plugin.package import PluginPackageManager


def _manager(monkeypatch, tmp_path: Path) -> PluginPackageManager:
    """构造使用隔离运行目录和事务目录的插件包管理器。"""
    monkeypatch.setattr(
        "app.adapters.system.plugin.package.settings",
        SimpleNamespace(
            ROOT_PATH=tmp_path,
            TEMP_PATH=tmp_path / "temp",
            CONFIG_PATH=tmp_path / "config",
        ),
    )
    return PluginPackageManager(helper=Mock())


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
