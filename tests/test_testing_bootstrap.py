"""共享测试引导工具的回归用例。"""
from __future__ import annotations

import builtins
import types
from pathlib import Path

from app.testing import bootstrap


def test_isolate_config_cleanup_uses_loaded_db_module_without_late_import(monkeypatch):
    """清理回调只读取已加载模块，避免解释器关停期触发二次导入。"""
    captured = {}
    import_calls = []

    def fake_import(name, *args, **kwargs):
        """记录清理回调是否试图重新导入数据库模块。"""
        if name == "app.db":
            import_calls.append(name)
        return original_import(name, *args, **kwargs)

    def fake_register(func):
        """截获 atexit 回调，便于直接验证清理行为。"""
        captured["cleanup"] = func

    monkeypatch.setattr(bootstrap, "_isolated_config_dir", None)
    monkeypatch.delenv("CONFIG_DIR", raising=False)
    monkeypatch.setattr(bootstrap.tempfile, "mkdtemp", lambda prefix: "/tmp/mp-test-config-demo")
    monkeypatch.setattr(bootstrap.shutil, "rmtree", lambda *args, **kwargs: None)
    monkeypatch.setattr(bootstrap.atexit, "register", fake_register)

    original_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", fake_import)

    bootstrap.isolate_config_dir()
    captured["cleanup"]()

    assert import_calls == []


def test_mark_plugin_generation_prefers_pathlib_item_path():
    """pytest 新版 item.path 可独立驱动 v1/v2 marker 标记。"""

    class FakeItem:
        """只暴露 pytest 7+ 的 path 属性，模拟新版收集对象。"""

        def __init__(self, value: str):
            self.path = Path(value)
            self.markers = []

        def add_marker(self, marker):
            """记录被添加的 marker。"""
            self.markers.append(marker)

    pytest_module = types.SimpleNamespace(mark=types.SimpleNamespace(v1="v1", v2="v2"))
    v2_item = FakeItem("/repo/tests/v2/test_demo.py")
    v1_item = FakeItem("/repo/tests/v1/test_demo.py")

    bootstrap.mark_plugin_generation([v2_item, v1_item], pytest_module)

    assert v2_item.markers == ["v2"]
    assert v1_item.markers == ["v1"]
