"""插件配置落在插件实例表上的存放与读写测试。"""

from __future__ import annotations

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.models.plugininstance import PluginInstance
from app.db.models.systemconfig import SystemConfig
from app.db.oper.plugininstance import PluginInstanceOper
from app.db.oper.systemconfig import SystemConfigOper
from app.db.session import SessionFactory
from app.db.uow import run_sync_transaction
from app.runtime.extensions.plugin.database import PluginDatabase
from app.runtime.extensions.plugin.storage import PluginConfigStore, PluginStorage
from app.startup.initializers.plugins import (
    _async_write_plugin_config,
    _delete_plugin_config,
    _read_plugin_config,
    _write_plugin_config,
)

PLUGIN_ID = "PytestPluginConfigProbe"
CLONE_ID = f"{PLUGIN_ID}work"
_INSTANCE_IDS = (PLUGIN_ID, CLONE_ID)


@pytest.fixture(autouse=True)
def purge_written_rows():
    """删除用例写入的行，测试库在整个会话内共享，残留会污染后续用例。"""
    yield

    def purge(session: Session) -> None:
        """按本文件使用的固定标识精确删除，不触碰其他用例的数据。"""
        session.execute(
            delete(PluginInstance).where(PluginInstance.instance_id.in_(_INSTANCE_IDS))
        )

    run_sync_transaction(purge)


@pytest.fixture(name="config_store")
def fixture_config_store() -> PluginConfigStore:
    """装配一个直连插件实例表的配置存储，与启动组合根使用同一组端口实现。"""
    storage = PluginStorage(
        read_config=_read_plugin_config,
        write_config=_write_plugin_config,
        async_write_config=_async_write_plugin_config,
        delete_config=_delete_plugin_config,
    )
    return PluginConfigStore(
        storage=lambda: storage,
        database=PluginDatabase,
        plugin_exists=lambda _plugin_id: True,
    )


def _row_counts(instance_id: str) -> tuple[int, int]:
    """返回该实例分别在实例表与系统设置表中的行数。"""
    session = SessionFactory()
    try:
        in_instance_table = PluginInstance.get_by_instance_id(session, instance_id) is not None
        in_system_table = SystemConfig.get_by_key(session, f"plugin.{instance_id}") is not None
        return int(in_instance_table), int(in_system_table)
    finally:
        session.close()


def test_plugin_config_is_written_to_the_instance_table(config_store: PluginConfigStore):
    """插件配置落进 plugininstance 表，不再占用主程序设置表的行。"""
    assert config_store.write(PLUGIN_ID, {"enable": True, "token": "probe"}) is True

    assert _row_counts(PLUGIN_ID) == (1, 0)


def test_plugin_config_reads_back_through_the_config_store(config_store: PluginConfigStore):
    """写进去的配置从同一个配置存储原样读得回来。"""
    config_store.write(PLUGIN_ID, {"enable": False, "token": "probe"})

    assert config_store.read(PLUGIN_ID) == {"enable": False, "token": "probe"}


def test_reading_config_filters_out_historical_empty_keys(config_store: PluginConfigStore):
    """历史遗留的空键不得进入插件拿到的配置字典。"""
    PluginInstanceOper().save_config_data(
        instance_id=PLUGIN_ID,
        source_plugin_id=PLUGIN_ID,
        config_data={"": "legacy", "enable": True},
    )

    assert config_store.read(PLUGIN_ID) == {"enable": True}


def test_writing_config_for_an_unknown_plugin_is_refused_without_force():
    """未注册插件的配置写入默认拒绝，这是既有的存在性规则。"""
    storage = PluginStorage(
        read_config=_read_plugin_config,
        write_config=_write_plugin_config,
        delete_config=_delete_plugin_config,
    )
    store = PluginConfigStore(
        storage=lambda: storage,
        database=PluginDatabase,
        plugin_exists=lambda _plugin_id: False,
    )

    assert store.write(PLUGIN_ID, {"enable": True}) is False
    assert store.read(PLUGIN_ID) == {}
    assert store.delete(PLUGIN_ID) is False
    assert _row_counts(PLUGIN_ID) == (0, 0)
    assert store.write(PLUGIN_ID, {"enable": True}, force=True) is True
    assert store.delete(PLUGIN_ID, force=True) is True


@pytest.mark.asyncio
async def test_async_config_write_lands_on_the_same_instance_row(
    config_store: PluginConfigStore,
):
    """异步写入与同步写入落在同一行，并遵循同一套建行与去重规则。"""
    assert await config_store.async_write(PLUGIN_ID, {"enable": True}) is True

    assert _row_counts(PLUGIN_ID) == (1, 0)
    assert config_store.read(PLUGIN_ID) == {"enable": True}
    # 同一份值再写一次不产生新行，也不改写归属
    assert await config_store.async_write(PLUGIN_ID, {"enable": True}) is True
    record = PluginInstanceOper().get(PLUGIN_ID)
    assert record is not None
    assert record.source_plugin_id == PLUGIN_ID


def test_deleting_config_of_a_bare_host_reclaims_the_empty_row(
    config_store: PluginConfigStore,
):
    """只承载一份配置的本体行在配置删掉后就是空壳，一并回收。"""
    config_store.write(PLUGIN_ID, {"enable": True})
    assert _row_counts(PLUGIN_ID) == (1, 0)

    assert config_store.delete(PLUGIN_ID) is True

    assert _row_counts(PLUGIN_ID) == (0, 0)
    assert config_store.read(PLUGIN_ID) == {}


def test_deleting_config_keeps_the_instance_that_still_carries_settings(
    config_store: PluginConfigStore,
):
    """删一份配置不得把实例本身删掉：同一行还带着展示信息等设置。

    配置与实例身份合表之后，删配置若照旧删行，「删一份配置」就会静默变成
    「删掉这个实例」，展示信息一并消失。
    """
    instances = PluginInstanceOper()
    instances.save(
        instance_id=PLUGIN_ID,
        source_plugin_id=PLUGIN_ID,
        plugin_name="本体展示名",
    )
    config_store.write(PLUGIN_ID, {"enable": True})

    config_store.delete(PLUGIN_ID)

    survivor = instances.get(PLUGIN_ID)
    assert survivor is not None
    assert survivor.plugin_name == "本体展示名"
    assert survivor.config_data is None


def test_deleting_a_clone_config_never_removes_the_clone_itself(
    config_store: PluginConfigStore,
):
    """分身的存在由它那一行表达，清空配置不等于删掉这个分身。"""
    instances = PluginInstanceOper()
    instances.save(instance_id=CLONE_ID, source_plugin_id=PLUGIN_ID)
    config_store.write(CLONE_ID, {"token": "probe"})

    config_store.delete(CLONE_ID)

    survivor = instances.get(CLONE_ID)
    assert survivor is not None
    assert survivor.config_data is None


def test_clone_config_records_its_source_plugin_so_instances_can_be_listed(
    config_store: PluginConfigStore,
):
    """分身配置要记下它属于哪个源插件，否则只能靠字符串前缀去猜归属。

    扁平键时代配置行只按实例 ID 命名，想列出「某插件的全部实例配置」无从下手。
    """
    instances = PluginInstanceOper()
    instances.save(instance_id=CLONE_ID, source_plugin_id=PLUGIN_ID)

    config_store.write(PLUGIN_ID, {"host": True})
    config_store.write(CLONE_ID, {"clone": True})

    owned = instances.list_by_source(PLUGIN_ID)
    assert {record.instance_id for record in owned} == {PLUGIN_ID, CLONE_ID}
    # 本体自身的 source_plugin_id 等于 instance_id；分身则指向源插件
    clone_record = instances.get(CLONE_ID)
    assert clone_record.source_plugin_id == PLUGIN_ID
    assert clone_record.is_host is False


def test_writing_config_does_not_clobber_the_display_fields_on_the_same_row(
    config_store: PluginConfigStore,
):
    """插件自己存配置不得抹掉宿主写在同一行上的展示信息。"""
    instances = PluginInstanceOper()
    instances.save(
        instance_id=CLONE_ID,
        source_plugin_id=PLUGIN_ID,
        plugin_name="分身甲",
    )

    config_store.write(CLONE_ID, {"enable": True})

    record = instances.get(CLONE_ID)
    assert record.plugin_name == "分身甲"
    assert record.config_data == {"enable": True}


def test_descriptor_save_port_keeps_the_config_already_on_the_row(
    config_store: PluginConfigStore,
):
    """组合根的实例落盘端口只写描述符各列，不得把同一行上的配置抹成空。

    描述符视图里根本没有业务参数，若把它整份写回，用户刚存的配置会在下一次
    改名或重载时静默消失。
    """
    from app.schemas.plugin import PluginInstance as PluginInstanceSchema
    from app.startup.initializers.plugins import _save_plugin_instance_record

    config_store.write(CLONE_ID, {"token": "keep"})

    _save_plugin_instance_record(
        PluginInstanceSchema(
            instance_id=CLONE_ID,
            source_plugin_id=CLONE_ID,
            plugin_name="分身甲",
        )
    )

    record = PluginInstanceOper().get(CLONE_ID)
    assert record is not None
    assert record.plugin_name == "分身甲"
    assert record.config_data == {"token": "keep"}


def test_saving_an_instance_under_a_different_source_is_rejected():
    """实例归属是身份的一半，改写它等于偷换整行，持久化层直接拒绝。"""
    instances = PluginInstanceOper()
    instances.save(instance_id=CLONE_ID, source_plugin_id=PLUGIN_ID)

    with pytest.raises(ValueError):
        instances.save(instance_id=CLONE_ID, source_plugin_id="PytestOtherSource")


def test_system_config_treats_a_plugin_prefixed_key_like_any_other_key():
    """SystemConfigOper 对 `plugin.` 前缀不得有任何特例行为。

    插件配置已经完整拆到插件实例表，系统设置存储不该再知道插件的存在：规范方法
    按键值分支等于在通用存储里塞进一条反向依赖，既让读写路径依插件而异，也让
    「这个键存在哪张表」变得只有读代码才说得清。本用例是那条约束的护栏。
    """
    oper = SystemConfigOper()
    key = f"plugin.{PLUGIN_ID}"
    try:
        oper.set(key, {"plain": "systemconfig row"})

        session = SessionFactory()
        try:
            assert SystemConfig.get_by_key(session, key) is not None
            assert PluginInstance.get_by_instance_id(session, PLUGIN_ID) is None
        finally:
            session.close()
        assert oper.get(key) == {"plain": "systemconfig row"}
    finally:
        oper.delete(key)
