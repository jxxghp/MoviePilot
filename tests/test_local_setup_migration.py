"""本地配置迁移的重复执行与提示回归测试。"""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def setup_module_copy(tmp_path, monkeypatch):
    """加载独立安装模块，将配置读写限制在临时目录。"""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "external"))
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "local_setup.py"
    spec = importlib.util.spec_from_file_location("local_setup_migration", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    monkeypatch.setattr(module, "LEGACY_CONFIG_DIR", legacy_dir)
    return module


def test_migration_only_reports_actual_copies(setup_module_copy, tmp_path, capsys):
    """首次迁移提示成功，重复运行保持静默且保留用户修改。"""
    module = setup_module_copy
    legacy_dir = module.LEGACY_CONFIG_DIR
    (legacy_dir / "app.env").write_text("PORT=3001\n", encoding="utf-8")
    (legacy_dir / "logs").mkdir()
    (legacy_dir / "logs" / "old.log").write_text("old log", encoding="utf-8")
    target_dir = tmp_path / "external"

    module.configure_config_dir(target_dir, prefer_external=True)

    assert f"已将现有本地配置迁移到 {target_dir.resolve()}" in capsys.readouterr().out
    assert (target_dir / "app.env").read_text(encoding="utf-8") == "PORT=3001\n"
    assert (target_dir / "logs" / "old.log").read_text(encoding="utf-8") == "old log"
    (target_dir / "app.env").write_text("PORT=3002\n", encoding="utf-8")

    module.configure_config_dir(target_dir, prefer_external=True)

    assert capsys.readouterr().out == ""
    assert (target_dir / "app.env").read_text(encoding="utf-8") == "PORT=3002\n"


def test_migration_reports_missing_directory_copy(setup_module_copy, tmp_path, capsys):
    """已有配置只补充缺失目录时仍应提示，且不覆盖目标文件。"""
    module = setup_module_copy
    (module.LEGACY_CONFIG_DIR / "app.env").write_text("old", encoding="utf-8")
    (module.LEGACY_CONFIG_DIR / "logs").mkdir()
    target_dir = tmp_path / "external"
    target_dir.mkdir()
    (target_dir / "app.env").write_text("current", encoding="utf-8")

    module.configure_config_dir(target_dir, prefer_external=True)

    assert "已将现有本地配置迁移到" in capsys.readouterr().out
    assert (target_dir / "logs").is_dir()
    assert (target_dir / "app.env").read_text(encoding="utf-8") == "current"
