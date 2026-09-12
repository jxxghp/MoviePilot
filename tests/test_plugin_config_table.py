"""插件配置落在插件实例表上的存放与读写路由测试。"""

from __future__ import annotations

import pytest

from app.db.models.plugininstance import PluginInstance
from app.db.models.systemconfig import SystemConfig
from app.db.oper.plugininstance import PluginInstanceOper
from app.db.oper.systemconfig import SystemConfigOper
from app.db.session import SessionFactory

PROBE_PLUGIN_ID = "PytestPluginConfigProbe"
PROBE_KEY = f"plugin.{PROBE_PLUGIN_ID}"
PROBE_CLONE_ID = f"{PROBE_PLUGIN_ID}work"
PROBE_CLONE_KEY = f"plugin.{PROBE_CLONE_ID}"


@pytest.fixture(name="system_config")
def fixture_system_config():
    """提供系统配置入口，并在用例结束后精确删除本次写入的行。

    测试库全局共享且没有按用例重置的 fixture，残留会按文件名字母序污染后续用例。
    """
    oper = SystemConfigOper()
    instances = PluginInstanceOper()
    try:
        yield oper
    finally:
        oper.delete(PROBE_KEY)
        oper.delete(PROBE_CLONE_KEY)
        instances.delete(PROBE_PLUGIN_ID)
        instances.delete(PROBE_CLONE_ID)


def _row_counts(instance_id: str, key: str) -> tuple[int, int]:
    """返回该插件配置分别在实例表与系统设置表中的行数。"""
    session = SessionFactory()
    try:
        in_instance_table = PluginInstance.get_by_instance_id(session, instance_id) is not None
        in_system_table = SystemConfig.get_by_key(session, key) is not None
        return int(in_instance_table), int(in_system_table)
    finally:
        session.close()


def test_plugin_config_is_written_to_the_instance_table(system_config: SystemConfigOper):
    """插件配置落进 plugininstance 表，不再占用主程序设置表的行。"""
    system_config.set(PROBE_KEY, {"enable": True, "token": "probe"})

    assert _row_counts(PROBE_PLUGIN_ID, PROBE_KEY) == (1, 0)


def test_plugin_config_reads_back_through_the_same_key(system_config: SystemConfigOper):
    """读取仍按 plugin.<ID> 键进行：这是插件侧的既有契约，不能因换表而改变。"""
    system_config.set(PROBE_KEY, {"enable": False, "token": "probe"})

    assert system_config.get(PROBE_KEY) == {"enable": False, "token": "probe"}


def test_plugin_config_enters_the_snapshot_under_the_same_key(
    system_config: SystemConfigOper,
):
    """重新加载快照后仍按 plugin.<ID> 键读得到，换表对读取端不可见。"""
    system_config.set(PROBE_KEY, {"enable": True})

    system_config.load_snapshot()

    assert system_config.get(PROBE_KEY) == {"enable": True}


def test_non_plugin_settings_still_live_in_system_config(system_config: SystemConfigOper):
    """真·系统设置不受影响，路由只认 plugin. 前缀。"""
    probe_key = "PytestPluginConfigProbeSystemKey"
    try:
        system_config.set(probe_key, {"kept": True})
        session = SessionFactory()
        try:
            assert SystemConfig.get_by_key(session, probe_key) is not None
            assert PluginInstance.get_by_instance_id(session, probe_key) is None
        finally:
            session.close()
    finally:
        system_config.delete(probe_key)


def test_deleting_config_of_a_bare_host_reclaims_the_empty_row(
    system_config: SystemConfigOper,
):
    """只承载一份配置的本体行在配置删掉后就是空壳，一并回收。"""
    system_config.set(PROBE_KEY, {"enable": True})
    assert _row_counts(PROBE_PLUGIN_ID, PROBE_KEY) == (1, 0)

    system_config.delete(PROBE_KEY)

    assert _row_counts(PROBE_PLUGIN_ID, PROBE_KEY) == (0, 0)
    assert system_config.get(PROBE_KEY) is None


def test_deleting_config_keeps_the_instance_that_still_carries_settings(
    system_config: SystemConfigOper,
):
    """删一份配置不得把实例本身删掉：同一行还带着展示信息等设置。

    配置与实例身份合表之后，删配置若照旧删行，「删一份配置」就会静默变成
    「删掉这个实例」，展示信息一并消失。
    """
    instances = PluginInstanceOper()
    instances.save(
        instance_id=PROBE_PLUGIN_ID,
        source_plugin_id=PROBE_PLUGIN_ID,
        plugin_name="本体展示名",
    )
    system_config.set(PROBE_KEY, {"enable": True})

    system_config.delete(PROBE_KEY)

    survivor = instances.get(PROBE_PLUGIN_ID)
    assert survivor is not None
    assert survivor.plugin_name == "本体展示名"
    assert survivor.config_data is None


def test_deleting_a_clone_config_never_removes_the_clone_itself(
    system_config: SystemConfigOper,
):
    """分身的存在由它那一行表达，清空配置不等于删掉这个分身。"""
    instances = PluginInstanceOper()
    instances.save(instance_id=PROBE_CLONE_ID, source_plugin_id=PROBE_PLUGIN_ID)
    system_config.set(PROBE_CLONE_KEY, {"token": "probe"})

    system_config.delete(PROBE_CLONE_KEY)

    survivor = instances.get(PROBE_CLONE_ID)
    assert survivor is not None
    assert survivor.config_data is None


def test_clone_config_records_its_source_plugin_so_instances_can_be_listed(
    system_config: SystemConfigOper,
):
    """分身配置要记下它属于哪个源插件，否则只能靠字符串前缀去猜归属。

    扁平键时代配置行只按实例 ID 命名，想列出「某插件的全部实例配置」无从下手。
    """
    instances = PluginInstanceOper()
    instances.save(instance_id=PROBE_CLONE_ID, source_plugin_id=PROBE_PLUGIN_ID)

    system_config.set(PROBE_KEY, {"host": True})
    system_config.set(PROBE_CLONE_KEY, {"clone": True})

    owned = instances.list_by_source(PROBE_PLUGIN_ID)
    assert {record.instance_id for record in owned} == {PROBE_PLUGIN_ID, PROBE_CLONE_ID}
    # 本体自身的 source_plugin_id 等于 instance_id；分身则指向源插件
    clone_record = instances.get(PROBE_CLONE_ID)
    assert clone_record.source_plugin_id == PROBE_PLUGIN_ID
    assert clone_record.is_host is False


def test_writing_config_does_not_clobber_the_display_fields_on_the_same_row(
    system_config: SystemConfigOper,
):
    """插件自己存配置不得抹掉宿主写在同一行上的展示信息。"""
    instances = PluginInstanceOper()
    instances.save(
        instance_id=PROBE_CLONE_ID,
        source_plugin_id=PROBE_PLUGIN_ID,
        plugin_name="分身甲",
    )

    system_config.set(PROBE_CLONE_KEY, {"enable": True})

    record = instances.get(PROBE_CLONE_ID)
    assert record.plugin_name == "分身甲"
    assert record.config_data == {"enable": True}


def test_descriptor_save_port_keeps_the_config_already_on_the_row(
    system_config: SystemConfigOper,
):
    """组合根的实例落盘端口只写描述符各列，不得把同一行上的配置抹成空。

    描述符视图里根本没有业务参数，若把它整份写回，用户刚存的配置会在下一次
    改名或重载时静默消失。
    """
    from app.schemas.plugin import PluginInstance as PluginInstanceSchema
    from app.startup.initializers.plugins import _save_plugin_instance_record

    system_config.set(PROBE_CLONE_KEY, {"token": "keep"})

    _save_plugin_instance_record(
        PluginInstanceSchema(
            instance_id=PROBE_CLONE_ID,
            source_plugin_id=PROBE_CLONE_ID,
            plugin_name="分身甲",
        )
    )

    record = PluginInstanceOper().get(PROBE_CLONE_ID)
    assert record is not None
    assert record.plugin_name == "分身甲"
    assert record.config_data == {"token": "keep"}


def test_saving_an_instance_under_a_different_source_is_rejected(
    system_config: SystemConfigOper,
):
    """实例归属是身份的一半，改写它等于偷换整行，持久化层直接拒绝。"""
    instances = PluginInstanceOper()
    instances.save(instance_id=PROBE_CLONE_ID, source_plugin_id=PROBE_PLUGIN_ID)

    with pytest.raises(ValueError):
        instances.save(instance_id=PROBE_CLONE_ID, source_plugin_id="PytestOtherSource")
