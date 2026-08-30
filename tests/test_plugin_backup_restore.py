"""插件持久化备份与 Docker 重置恢复合同测试。"""

import errno
from dataclasses import replace
from pathlib import Path

from app.adapters.system.host import SystemUtils
from app.adapters.system.plugin import package as package_module
from app.adapters.system.plugin.package import PluginPackageManager
from app.chain import system as system_module
from app.chain.system import SystemChain


def _patch_docker_paths(monkeypatch, tmp_path: Path, *, reset: bool) -> Path:
    """把插件恢复路径和 Docker 重置条件隔离到临时目录。"""
    config_dir = tmp_path / "config"
    runtime_dir = tmp_path / "app" / "plugins"
    config_dir.mkdir(parents=True)
    runtime_dir.mkdir(parents=True)
    runtime_config = replace(
        system_module.get_chain_runtime_config_snapshot(),
        root_path=tmp_path,
        config_path=config_dir,
    )
    monkeypatch.setattr(
        system_module,
        "get_chain_runtime_config_snapshot",
        lambda: runtime_config,
    )
    monkeypatch.setattr(
        SystemUtils,
        "is_docker",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        system_module.SystemHelper,
        "is_system_reset",
        lambda _self: reset,
    )
    return runtime_dir


def _patch_package_paths(
    monkeypatch,
    tmp_path: Path,
) -> tuple[PluginPackageManager, Path, Path]:
    """把插件更新后的持久化备份路径隔离到临时目录。"""
    plugin_root = tmp_path / "app" / "plugins"
    config_dir = tmp_path / "config"
    plugin_root.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    monkeypatch.setattr(
        package_module,
        "get_runtime_setting",
        lambda key, default=None: config_dir if key == "CONFIG_PATH" else default,
    )
    monkeypatch.setattr(
        package_module.SystemUtils,
        "is_docker",
        staticmethod(lambda: True),
    )
    return (
        PluginPackageManager(plugin_root=plugin_root),
        plugin_root,
        config_dir / "plugins_backup",
    )


def _write_plugin(root: Path, plugin_id: str, filename: str, content: str) -> Path:
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True, exist_ok=True)
    target = plugin_dir / filename
    target.write_text(content, encoding="utf-8")
    return target


def test_backup_plugins_refreshes_existing_snapshot(monkeypatch, tmp_path):
    """关停备份应刷新同名插件并移除旧快照中的遗留文件。"""
    runtime_dir = _patch_docker_paths(monkeypatch, tmp_path, reset=False)
    backup_root = tmp_path / "config" / "plugins_backup"
    backup_dir = backup_root / "demo"
    _write_plugin(runtime_dir, "demo", "plugin.py", "new")
    _write_plugin(backup_root, "demo", "plugin.py", "old")
    _write_plugin(backup_root, "demo", "stale.py", "stale")

    SystemChain.backup_plugins()

    assert (backup_dir / "plugin.py").read_text(encoding="utf-8") == "new"
    assert not (backup_dir / "stale.py").exists()


def test_backup_plugins_failure_preserves_previous_snapshot(monkeypatch, tmp_path):
    """复制新快照失败时应保留上一份可恢复内容。"""
    runtime_dir = _patch_docker_paths(monkeypatch, tmp_path, reset=False)
    backup_root = tmp_path / "config" / "plugins_backup"
    backup_dir = backup_root / "demo"
    _write_plugin(runtime_dir, "demo", "plugin.py", "new")
    _write_plugin(backup_root, "demo", "plugin.py", "old")

    def fail_copy(*_args, **_kwargs):
        raise OSError("copy failed")

    monkeypatch.setattr(system_module.shutil, "copytree", fail_copy)

    SystemChain.backup_plugins()

    assert (backup_dir / "plugin.py").read_text(encoding="utf-8") == "old"


def test_backup_plugins_keeps_snapshot_missing_from_runtime(monkeypatch, tmp_path):
    """运行目录缺失时不得删除唯一的持久化备份。"""
    _patch_docker_paths(monkeypatch, tmp_path, reset=False)
    backup_root = tmp_path / "config" / "plugins_backup"
    backup_file = _write_plugin(backup_root, "demo", "plugin.py", "recoverable")

    SystemChain.backup_plugins()

    assert backup_file.read_text(encoding="utf-8") == "recoverable"


def test_restore_plugins_keeps_backup_on_regular_start(monkeypatch, tmp_path):
    """普通重启保留备份，等待真正的容器重置场景消费。"""
    runtime_dir = _patch_docker_paths(monkeypatch, tmp_path, reset=False)
    backup_dir = tmp_path / "config" / "plugins_backup"
    _write_plugin(backup_dir, "demo", "plugin.py", "stable")

    SystemChain.restore_plugins()

    assert not (runtime_dir / "demo").exists()
    assert (backup_dir / "demo" / "plugin.py").exists()


def test_restore_plugins_consumes_backup_after_source_restore(monkeypatch, tmp_path):
    """源码恢复完成即可消费备份，依赖恢复由启动后台任务统一处理。"""
    runtime_dir = _patch_docker_paths(monkeypatch, tmp_path, reset=True)
    backup_dir = tmp_path / "config" / "plugins_backup"
    _write_plugin(backup_dir, "DemoPlugin", "plugin.py", "stable")

    SystemChain.restore_plugins()

    assert (runtime_dir / "DemoPlugin" / "plugin.py").read_text(
        encoding="utf-8"
    ) == "stable"
    assert not backup_dir.exists()


def test_restore_plugins_retries_only_missing_sources(monkeypatch, tmp_path):
    """恢复失败后只补仍缺失的目录，不覆盖用户随后重新安装的插件。"""
    runtime_dir = _patch_docker_paths(monkeypatch, tmp_path, reset=True)
    backup_dir = tmp_path / "config" / "plugins_backup"
    _write_plugin(backup_dir, "DemoPlugin", "plugin.py", "backup")
    reset_state = {"value": True}
    monkeypatch.setattr(
        system_module.SystemHelper,
        "is_system_reset",
        lambda _self: reset_state["value"],
    )
    original_copytree = system_module.shutil.copytree

    def fail_copy(source, target, *args, **kwargs):
        if Path(source).name == "DemoPlugin":
            raise OSError("copy failed")
        return original_copytree(source, target, *args, **kwargs)

    monkeypatch.setattr(system_module.shutil, "copytree", fail_copy)
    SystemChain.restore_plugins()
    pending = backup_dir / SystemChain._plugin_restore_pending_file
    assert pending.exists()

    _write_plugin(runtime_dir, "DemoPlugin", "plugin.py", "reinstalled")
    reset_state["value"] = False
    monkeypatch.setattr(system_module.shutil, "copytree", original_copytree)
    SystemChain.restore_plugins()

    assert (runtime_dir / "DemoPlugin" / "plugin.py").read_text(
        encoding="utf-8"
    ) == "reinstalled"
    assert not backup_dir.exists()


def test_restore_plugins_retries_existing_target_after_copy_failure(
    monkeypatch,
    tmp_path,
):
    """原目标已存在时，失败重试仍应完成备份版本的原子替换。"""
    runtime_dir = _patch_docker_paths(monkeypatch, tmp_path, reset=True)
    backup_dir = tmp_path / "config" / "plugins_backup"
    _write_plugin(runtime_dir, "DemoPlugin", "plugin.py", "runtime-old")
    _write_plugin(backup_dir, "DemoPlugin", "plugin.py", "backup-new")
    reset_state = {"value": True}
    monkeypatch.setattr(
        system_module.SystemHelper,
        "is_system_reset",
        lambda _self: reset_state["value"],
    )
    original_copytree = system_module.shutil.copytree

    def fail_copy(source, target, *args, **kwargs):
        if Path(source).name == "DemoPlugin":
            raise OSError("copy failed")
        return original_copytree(source, target, *args, **kwargs)

    monkeypatch.setattr(system_module.shutil, "copytree", fail_copy)
    SystemChain.restore_plugins()
    assert (runtime_dir / "DemoPlugin" / "plugin.py").read_text(
        encoding="utf-8"
    ) == "runtime-old"

    reset_state["value"] = False
    monkeypatch.setattr(system_module.shutil, "copytree", original_copytree)
    SystemChain.restore_plugins()

    assert (runtime_dir / "DemoPlugin" / "plugin.py").read_text(
        encoding="utf-8"
    ) == "backup-new"
    assert not backup_dir.exists()


def test_restore_plugins_falls_back_when_overlay_rename_returns_exdev(
    monkeypatch,
    tmp_path,
):
    """镜像层目录拒绝 rename 时仍能完成可恢复的快照替换。"""
    runtime_dir = _patch_docker_paths(monkeypatch, tmp_path, reset=True)
    _write_plugin(runtime_dir, "DemoPlugin", "plugin.py", "runtime-old")
    backup_dir = tmp_path / "config" / "plugins_backup"
    _write_plugin(backup_dir, "DemoPlugin", "plugin.py", "backup-new")

    original_replace = Path.replace

    def exdev_for_existing_target(self, target):
        if self == runtime_dir / "DemoPlugin":
            raise OSError(errno.EXDEV, "cross-device link")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", exdev_for_existing_target)

    SystemChain.restore_plugins()

    assert (runtime_dir / "DemoPlugin" / "plugin.py").read_text(
        encoding="utf-8"
    ) == "backup-new"
    assert not backup_dir.exists()


def test_restore_plugins_restores_previous_after_partial_overlay_removal(
    monkeypatch,
    tmp_path,
):
    """overlayfs 删除旧目录部分失败时仍恢复完整旧快照。"""
    runtime_dir = _patch_docker_paths(monkeypatch, tmp_path, reset=True)
    _write_plugin(runtime_dir, "DemoPlugin", "plugin.py", "runtime-old")
    _write_plugin(runtime_dir, "DemoPlugin", "settings.json", "settings-old")
    backup_dir = tmp_path / "config" / "plugins_backup"
    _write_plugin(backup_dir, "DemoPlugin", "plugin.py", "backup-new")

    original_replace = Path.replace
    original_rmtree = system_module.shutil.rmtree
    removal_attempts = 0

    def exdev_for_existing_target(self, target):
        if self == runtime_dir / "DemoPlugin":
            raise OSError(errno.EXDEV, "cross-device link")
        return original_replace(self, target)

    def fail_after_partial_removal(path, *args, **kwargs):
        nonlocal removal_attempts
        if Path(path) == runtime_dir / "DemoPlugin" and removal_attempts == 0:
            removal_attempts += 1
            (runtime_dir / "DemoPlugin" / "plugin.py").unlink()
            raise OSError("directory removal interrupted")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", exdev_for_existing_target)
    monkeypatch.setattr(system_module.shutil, "rmtree", fail_after_partial_removal)

    SystemChain.restore_plugins()

    assert (runtime_dir / "DemoPlugin" / "plugin.py").read_text(
        encoding="utf-8"
    ) == "runtime-old"
    assert (runtime_dir / "DemoPlugin" / "settings.json").read_text(
        encoding="utf-8"
    ) == "settings-old"
    assert backup_dir.exists()
    assert (backup_dir / SystemChain._plugin_restore_pending_file).exists()


def test_backup_keeps_restore_retry_marker(monkeypatch, tmp_path):
    """关停备份不得清除尚未完成的恢复标记。"""
    runtime_dir = _patch_docker_paths(monkeypatch, tmp_path, reset=False)
    backup_dir = tmp_path / "config" / "plugins_backup"
    pending = backup_dir / SystemChain._plugin_restore_pending_file
    pending.parent.mkdir(parents=True)
    pending.touch()
    _write_plugin(runtime_dir, "demo", "plugin.py", "current")

    SystemChain.backup_plugins()

    assert pending.exists()


def test_backup_does_not_overwrite_failed_restore_snapshot(monkeypatch, tmp_path):
    """待重试项目的原快照必须跨关停保留，避免恢复材料被当前目录覆盖。"""
    runtime_dir = _patch_docker_paths(monkeypatch, tmp_path, reset=False)
    backup_dir = tmp_path / "config" / "plugins_backup"
    pending = backup_dir / SystemChain._plugin_restore_pending_file
    pending.parent.mkdir(parents=True)
    pending.write_text(
        '{"failed_items": {"demo": false}}', encoding="utf-8"
    )
    _write_plugin(runtime_dir, "demo", "plugin.py", "reinstalled")
    _write_plugin(backup_dir, "demo", "plugin.py", "recoverable")

    SystemChain.backup_plugins()

    assert (backup_dir / "demo" / "plugin.py").read_text(encoding="utf-8") == "recoverable"


def test_market_refresh_replaces_snapshot_and_removes_stale_files(monkeypatch, tmp_path):
    """插件更新成功后应刷新对应持久化快照。"""
    package, plugin_root, backup_root = _patch_package_paths(monkeypatch, tmp_path)
    backup_dir = backup_root / "demo"
    _write_plugin(plugin_root, "demo", "plugin.py", "new")
    _write_plugin(backup_root, "demo", "plugin.py", "old")
    _write_plugin(backup_root, "demo", "stale.py", "stale")

    assert package.refresh_persistent_backup("demo") is True

    assert (backup_dir / "plugin.py").read_text(encoding="utf-8") == "new"
    assert not (backup_dir / "stale.py").exists()


def test_market_refresh_failure_preserves_previous_snapshot(monkeypatch, tmp_path):
    """插件更新备份失败时应继续保留旧快照。"""
    package, plugin_root, backup_root = _patch_package_paths(monkeypatch, tmp_path)
    backup_dir = backup_root / "demo"
    _write_plugin(plugin_root, "demo", "plugin.py", "new")
    _write_plugin(backup_root, "demo", "plugin.py", "old")

    def fail_copy(*_args, **_kwargs):
        raise OSError("copy failed")

    monkeypatch.setattr(package_module.shutil, "copytree", fail_copy)

    assert package.refresh_persistent_backup("demo") is False
    assert (backup_dir / "plugin.py").read_text(encoding="utf-8") == "old"
