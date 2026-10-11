"""插件实例目录对历史无效持久化行的读取隔离测试。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.db.oper.plugininstance import PluginInstanceOper
from app.runtime.extensions.plugin.storage import PluginInstanceStore, PluginStorage
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


def test_underscore_host_can_be_disabled_without_repeated_projection_warnings(monkeypatch) -> None:
    """含下划线的存量本体能在卸载时停用，保留配置的重复读取也不误报无效行。"""
    plugin_id = "nullbr_search"
    oper = PluginInstanceOper()
    logger = MagicMock()
    monkeypatch.setattr(plugins_initializer, "logger", logger)
    directory = plugins_initializer._build_plugin_instance_directory()
    storage = PluginStorage(read=lambda _key: None, write=lambda _key, _value: None)
    store = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)
    try:
        oper.save(instance_id=plugin_id, source_plugin_id=plugin_id, is_enabled=True)
        oper.save_config_data(
            instance_id=plugin_id, source_plugin_id=plugin_id, config_data={"token": "kept"}
        )
        assert plugin_id in store.enabled_hosts()
        assert store.disable_host(plugin_id)
        assert not store.disable_host(plugin_id)
        for _ in range(3):
            host = store.get_host(plugin_id)
            assert host is not None and host.is_enabled is False
            assert plugin_id in store.all_hosts()
            assert store.for_source(plugin_id) == []
            assert plugin_id not in store.enabled_hosts()
        assert oper.get_config_data(plugin_id) == {"token": "kept"}
        logger.warning.assert_not_called()
    finally:
        oper.delete(plugin_id)
