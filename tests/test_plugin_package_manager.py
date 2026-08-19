from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.adapters.system.plugin.package import PluginPackageManager


def _manager(monkeypatch, tmp_path: Path) -> PluginPackageManager:
    """构造使用隔离运行目录和事务目录的插件包管理器。"""
    monkeypatch.setattr(
        "app.adapters.system.plugin.package.settings",
        SimpleNamespace(ROOT_PATH=tmp_path, TEMP_PATH=tmp_path / "temp"),
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


def test_remove_deletes_existing_plugin_directory(monkeypatch, tmp_path):
    """删除存在的插件目录应成功并清空磁盘内容。"""
    manager = _manager(monkeypatch, tmp_path)
    plugin_dir = tmp_path / "app" / "plugins" / "demoplugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("class DemoPlugin:\n    pass\n", encoding="utf-8")

    success, message = manager.remove("DemoPlugin")

    assert success is True
    assert message == "插件目录删除成功"
    assert not plugin_dir.exists()


def test_remove_missing_plugin_directory_is_a_noop(monkeypatch, tmp_path):
    """删除不存在的插件目录应视为已达成目标，不报错。"""
    manager = _manager(monkeypatch, tmp_path)

    success, message = manager.remove("DemoPlugin")

    assert success is True
    assert "不存在" in message


def test_remove_rejects_path_traversal_plugin_id(monkeypatch, tmp_path):
    """越出插件根目录的标识必须被拒绝，不得删到插件目录之外。"""
    manager = _manager(monkeypatch, tmp_path)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "marker.txt").write_text("keep", encoding="utf-8")

    success, message = manager.remove("../../outside")

    assert success is False
    assert "非法插件ID" in message
    assert (outside_dir / "marker.txt").exists()
