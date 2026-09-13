"""插件实例目录对历史无效持久化行的读取隔离测试。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.startup.initializers import plugins as plugins_initializer


def _record(instance_id: str, source_plugin_id: str) -> SimpleNamespace:
    """构造包含 ORM 所需字段的轻量实例记录替身。"""
    return SimpleNamespace(
        instance_id=instance_id,
        source_plugin_id=source_plugin_id,
        plugin_name=None,
        plugin_desc=None,
        plugin_icon=None,
        is_default_target=False,
        is_enabled=True,
    )


def test_plugin_instance_directory_skips_invalid_records_on_every_read(monkeypatch) -> None:
    """单条历史非法实例记录不得阻断其它实例读取或启动分类。"""
    invalid = _record("__ConfigCenter__", "__ConfigCenter__")
    valid = _record("DemoPlugin", "DemoPlugin")
    oper = MagicMock()
    oper.get.return_value = invalid
    oper.list_all.return_value = [invalid, valid]
    oper.list_by_source.return_value = [invalid, valid]
    oper.list_enabled.return_value = [invalid, valid]
    logger = MagicMock()

    monkeypatch.setattr(plugins_initializer, "PluginInstanceOper", lambda: oper)
    monkeypatch.setattr(plugins_initializer, "logger", logger)

    directory = plugins_initializer._build_plugin_instance_directory()

    assert directory.get(invalid.instance_id) is None
    assert [item.instance_id for item in directory.list_all()] == [valid.instance_id]
    assert [item.instance_id for item in directory.list_by_source("DemoPlugin")] == [
        valid.instance_id
    ]
    assert [item.instance_id for item in directory.list_enabled()] == [valid.instance_id]
    assert logger.warning.call_count == 4
    assert invalid.instance_id in str(logger.warning.call_args)
