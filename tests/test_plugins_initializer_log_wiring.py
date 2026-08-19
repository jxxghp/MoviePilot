"""插件启动组合根装配日志目录解析器与等级预热的行为契约测试。

`app/runtime/log.py` 是依赖叶节点，不允许依赖 `app/db`；插件实例日志目录的推导
（经 `app.plugins.plugin_instance_path`）和等级覆盖的数据库预热都只能由组合根
（`app/startup/plugins_initializer.py`）完成并注入。
"""

from unittest.mock import MagicMock

from app.runtime import log as log_module
from app.startup.plugins_initializer import (
    _resolve_plugin_instance_log_dir,
    _seed_plugin_instance_log_levels,
)


def test_resolve_plugin_instance_log_dir_matches_data_dir_sibling(tmp_path, monkeypatch):
    """日志目录推导须与实例数据目录同级，都经 plugin_instance_path 校验。"""
    from app.runtime.config import settings

    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))

    log_dir = _resolve_plugin_instance_log_dir("DemoPlugin", "second")

    assert log_dir.name == "logs"
    assert log_dir.parent.name == "second"
    assert log_dir.parent.parent.name == "DemoPlugin"


def test_seed_plugin_instance_log_levels_loads_configured_overrides(monkeypatch):
    """启动预热应把数据库中已配置的 log_level 逐一推入日志模块缓存。"""
    plugin_manager = MagicMock()
    plugin_manager.plugins = {"DemoPlugin": object()}
    monkeypatch.setattr(
        "app.startup.plugins_initializer.PluginManager", lambda: plugin_manager
    )

    config_oper = MagicMock()
    row = MagicMock(instance_id="second", log_level="DEBUG", log_expires_at=None)
    other_row = MagicMock(instance_id="default", log_level=None, log_expires_at=None)
    config_oper.list_by_plugin.return_value = [row, other_row]
    monkeypatch.setattr(
        "app.startup.plugins_initializer.PluginConfigOper", lambda: config_oper
    )
    monkeypatch.setattr(log_module, "_plugin_level_overrides", {})
    monkeypatch.setattr(log_module, "_plugin_level_floor", log_module._current_global_log_level())

    _seed_plugin_instance_log_levels()

    assert log_module.get_effective_plugin_instance_log_level("DemoPlugin", "second") == "DEBUG"
    # 未配置等级的行不应产生覆盖，仍跟随全局
    assert log_module.get_plugin_instance_log_level_override("DemoPlugin", "default") is None


def test_seed_plugin_instance_log_levels_skips_invalid_level_without_raising(monkeypatch):
    """数据库中出现非法等级值时跳过该行，不让预热流程整体失败。"""
    plugin_manager = MagicMock()
    plugin_manager.plugins = {"DemoPlugin": object()}
    monkeypatch.setattr(
        "app.startup.plugins_initializer.PluginManager", lambda: plugin_manager
    )

    config_oper = MagicMock()
    row = MagicMock(instance_id="second", log_level="TRACE", log_expires_at=None)
    config_oper.list_by_plugin.return_value = [row]
    monkeypatch.setattr(
        "app.startup.plugins_initializer.PluginConfigOper", lambda: config_oper
    )
    monkeypatch.setattr(log_module, "_plugin_level_overrides", {})
    monkeypatch.setattr(log_module, "_plugin_level_floor", log_module._current_global_log_level())

    _seed_plugin_instance_log_levels()

    assert log_module.get_plugin_instance_log_level_override("DemoPlugin", "second") is None
