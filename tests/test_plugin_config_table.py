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


def test_deleting_config_keeps_an_enabled_host_row_that_still_says_load_me(
    system_config: SystemConfigOper,
):
    """本体行带着启用位时不是空壳：它承载「这个插件应当装载」，删配置不得连它一起收走。

    本体的装载判据归口到 is_enabled 之后，回收掉这一行等于把插件静默停用。
    """
    system_config.set(PROBE_KEY, {"enable": True})
    assert _row_counts(PROBE_PLUGIN_ID, PROBE_KEY) == (1, 0)

    system_config.delete(PROBE_KEY)

    assert _row_counts(PROBE_PLUGIN_ID, PROBE_KEY) == (1, 0)
    assert system_config.get(PROBE_KEY) is None
    survivor = PluginInstanceOper().get(PROBE_PLUGIN_ID)
    assert survivor.config_data is None
    assert survivor.is_enabled is True


def test_deleting_config_of_a_disabled_bare_host_reclaims_the_empty_row(
    system_config: SystemConfigOper,
):
    """已停用且各列皆空的本体行才是真空壳，删配置时一并回收。"""
    instances = PluginInstanceOper()
    system_config.set(PROBE_KEY, {"enable": True})
    instances.set_enabled(instance_id=PROBE_PLUGIN_ID, is_enabled=False)

    system_config.delete(PROBE_KEY)

    assert _row_counts(PROBE_PLUGIN_ID, PROBE_KEY) == (0, 0)


def test_deleting_config_keeps_the_instance_that_still_carries_settings(
    system_config: SystemConfigOper,
):
    """删一份配置不得把实例本身删掉：同一行还带着锚定版本等设置。

    配置与实例身份合表之后，删配置若照旧删行，「删一份配置」就会静默变成
    「删掉这个实例」，锚定版本与展示信息一并消失。
    """
    instances = PluginInstanceOper()
    instances.save(
        instance_id=PROBE_PLUGIN_ID,
        source_plugin_id=PROBE_PLUGIN_ID,
        pinned_version="1.0.0",
    )
    system_config.set(PROBE_KEY, {"enable": True})

    system_config.delete(PROBE_KEY)

    survivor = instances.get(PROBE_PLUGIN_ID)
    assert survivor is not None
    assert survivor.pinned_version == "1.0.0"
    assert survivor.config_data is None


def test_clone_config_records_its_source_plugin_so_instances_can_be_listed(
    system_config: SystemConfigOper,
):
    """分身配置要记下它属于哪个源插件，否则只能靠字符串前缀去猜归属。

    扁平键时代配置行只按实例 ID 命名，想列出「某插件的全部实例配置」无从下手——
    上一轮想枚举可恢复的分身时就是卡在这里。
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


def test_writing_config_does_not_clobber_the_instance_log_level(
    system_config: SystemConfigOper,
):
    """插件自己存配置不得抹掉宿主控制面写在同一行上的日志等级覆盖。"""
    instances = PluginInstanceOper()
    instances.set_log_level(
        instance_id=PROBE_PLUGIN_ID,
        log_level="debug",
        log_expires_at=None,
    )

    system_config.set(PROBE_KEY, {"enable": True})

    record = instances.get(PROBE_PLUGIN_ID)
    assert record.log_level == "debug"
    assert record.config_data == {"enable": True}


def test_disabling_a_clone_keeps_its_config_and_leaves_the_row_listed(
    system_config: SystemConfigOper,
):
    """分身卸载就是把启用位置假，配置与展示信息留在原行，枚举里仍看得见。"""
    instances = PluginInstanceOper()
    instances.save(
        instance_id=PROBE_CLONE_ID,
        source_plugin_id=PROBE_PLUGIN_ID,
        plugin_name="分身甲",
        is_enabled=True,
    )
    system_config.set(PROBE_CLONE_KEY, {"token": "keep"})

    assert instances.set_enabled(instance_id=PROBE_CLONE_ID, is_enabled=False) is True

    kept = instances.get(PROBE_CLONE_ID)
    assert kept is not None
    assert kept.is_enabled is False
    assert kept.config_data == {"token": "keep"}
    assert kept.plugin_name == "分身甲"
    # 不该被实例化，但仍是一份登记在册的配置
    assert PROBE_CLONE_ID not in {r.instance_id for r in instances.list_enabled()}
    assert PROBE_CLONE_ID in {r.instance_id for r in instances.list_by_source(PROBE_PLUGIN_ID)}


def test_reenabling_a_clone_brings_back_the_settings_untouched(
    system_config: SystemConfigOper,
):
    """再次启用即恢复：配置与展示信息从头到尾没离开过那一行。"""
    instances = PluginInstanceOper()
    instances.save(
        instance_id=PROBE_CLONE_ID,
        source_plugin_id=PROBE_PLUGIN_ID,
        plugin_name="分身甲",
        is_enabled=True,
    )
    system_config.set(PROBE_CLONE_KEY, {"token": "keep"})
    instances.set_enabled(instance_id=PROBE_CLONE_ID, is_enabled=False)

    instances.set_enabled(instance_id=PROBE_CLONE_ID, is_enabled=True)

    restored = instances.get(PROBE_CLONE_ID)
    assert restored.is_enabled is True
    assert restored.config_data == {"token": "keep"}
    assert restored.plugin_name == "分身甲"
    assert PROBE_CLONE_ID in {r.instance_id for r in instances.list_enabled()}


def test_disabling_clears_state_that_only_makes_sense_while_enabled(
    system_config: SystemConfigOper,
):
    """停用要清掉默认调用目标与日志等级覆盖，否则会路由到不会被实例化的实例。"""
    instances = PluginInstanceOper()
    instances.save(
        instance_id=PROBE_CLONE_ID,
        source_plugin_id=PROBE_PLUGIN_ID,
        is_enabled=True,
    )
    instances.set_log_level(
        instance_id=PROBE_CLONE_ID,
        log_level="debug",
        log_expires_at=None,
    )
    instances.set_default_target(PROBE_PLUGIN_ID, PROBE_CLONE_ID)

    instances.set_enabled(instance_id=PROBE_CLONE_ID, is_enabled=False)

    record = instances.get(PROBE_CLONE_ID)
    assert record.is_default_target is False
    assert record.log_level is None


def test_saving_an_instance_under_a_different_source_is_rejected(
    system_config: SystemConfigOper,
):
    """实例归属是身份的一半，改写它等于偷换整行，持久化层直接拒绝。"""
    instances = PluginInstanceOper()
    instances.save(instance_id=PROBE_CLONE_ID, source_plugin_id=PROBE_PLUGIN_ID)

    with pytest.raises(ValueError):
        instances.save(instance_id=PROBE_CLONE_ID, source_plugin_id="PytestOtherSource")


def test_descriptor_save_port_persists_the_enable_bit(system_config: SystemConfigOper):
    """组合根的实例落盘端口必须把启用位一起写进去。

    这条端口是分身创建、版本绑定、默认目标三处写入的唯一出口，而装载判据正是启用
    位。它漏写时新建的实例一律落成停用：实例建得出来、界面看得到，却永远不被加载。
    整个测试面此前都用进程内假 directory 绕过了这条真实路径，因而毫无察觉。
    """
    from app.schemas.plugin import PluginInstance as PluginInstanceSchema
    from app.startup.initializers.plugins import _save_plugin_instance_record

    _save_plugin_instance_record(
        PluginInstanceSchema(
            instance_id=PROBE_CLONE_ID,
            source_plugin_id=PROBE_PLUGIN_ID,
            is_enabled=True,
        )
    )

    record = PluginInstanceOper().get(PROBE_CLONE_ID)
    assert record is not None
    assert record.is_enabled is True


def test_descriptor_save_port_round_trips_a_disabled_instance(
    system_config: SystemConfigOper,
):
    """停用的实例读出来再写回去，不得被默认值悄悄重新启用。"""
    from app.startup.initializers.plugins import (
        _plugin_instance_from_record,
        _save_plugin_instance_record,
    )

    instances = PluginInstanceOper()
    instances.save(
        instance_id=PROBE_CLONE_ID,
        source_plugin_id=PROBE_PLUGIN_ID,
        is_enabled=True,
    )
    instances.set_enabled(instance_id=PROBE_CLONE_ID, is_enabled=False)

    projected = _plugin_instance_from_record(instances.get(PROBE_CLONE_ID))
    _save_plugin_instance_record(projected.model_copy(update={"pinned_version": "1.0.0"}))

    record = instances.get(PROBE_CLONE_ID)
    assert record.is_enabled is False
    assert record.pinned_version == "1.0.0"


def test_disabling_a_host_keeps_its_pinned_version(system_config: SystemConfigOper):
    """纯停用保留锚定版本：插件包还在磁盘上，再次启用应当回到同一个版本。

    卸载才清锚定版本——那时版本目录会随包一起消失，留着会让重装的同名插件按早已
    删除的目录解析源码。两件事必须分开，否则「先停一会儿」会静默丢掉版本选择。
    """
    from app.runtime.extensions.plugin.storage import (
        PluginInstanceDirectory,
        PluginInstanceStore,
        PluginStorage,
    )
    from app.schemas.plugin import PluginInstance as PluginInstanceSchema
    from app.schemas.types import SystemConfigKey

    records: dict[str, PluginInstanceSchema] = {}
    directory = PluginInstanceDirectory(
        get=records.get,
        list_all=lambda: list(records.values()),
        list_enabled=lambda: [r for r in records.values() if r.is_enabled],
        list_by_source=lambda source: [
            r for r in records.values() if r.source_plugin_id == source
        ],
        save=lambda instance: records.__setitem__(instance.instance_id, instance),
        delete=lambda instance_id: records.pop(instance_id, None) is not None,
        set_enabled=lambda instance_id, enabled: bool(
            records.__setitem__(
                instance_id,
                records[instance_id].model_copy(update={"is_enabled": enabled}),
            )
            or True
        ),
    )
    values = {SystemConfigKey.PluginInstancesImported: True}
    storage = PluginStorage(read=values.get, write=lambda k, v: values.__setitem__(k, v))
    store = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)
    store.save_host(
        PluginInstanceSchema(
            instance_id="DemoPlugin",
            source_plugin_id="DemoPlugin",
            pinned_version="1.0.0",
            is_enabled=True,
        )
    )

    assert store.disable_host("DemoPlugin") is True

    assert records["DemoPlugin"].is_enabled is False
    assert records["DemoPlugin"].pinned_version == "1.0.0"

    # 卸载收尾则要把锚定版本一并清掉
    records["DemoPlugin"] = records["DemoPlugin"].model_copy(update={"is_enabled": True})
    assert store.retire_host("DemoPlugin") is True
    assert records["DemoPlugin"].pinned_version is None


def _memory_instance_store():
    """构造进程内实例存储，用于验证三种 for_source 口径的分工。"""
    from app.runtime.extensions.plugin.storage import (
        PluginInstanceDirectory,
        PluginInstanceStore,
        PluginStorage,
    )
    from app.schemas.plugin import PluginInstance as PluginInstanceSchema
    from app.schemas.types import SystemConfigKey

    records: dict[str, PluginInstanceSchema] = {}
    directory = PluginInstanceDirectory(
        get=records.get,
        list_all=lambda: list(records.values()),
        list_enabled=lambda: [r for r in records.values() if r.is_enabled],
        list_by_source=lambda source: [
            r for r in records.values() if r.source_plugin_id == source
        ],
        save=lambda instance: records.__setitem__(instance.instance_id, instance),
        delete=lambda instance_id: records.pop(instance_id, None) is not None,
        set_enabled=lambda instance_id, enabled: bool(
            records.__setitem__(
                instance_id,
                records[instance_id].model_copy(update={"is_enabled": enabled}),
            )
            or True
        ),
    )
    values = {SystemConfigKey.PluginInstancesImported: True}
    storage = PluginStorage(read=values.get, write=lambda k, v: values.__setitem__(k, v))
    return PluginInstanceStore(storage=lambda: storage, directory=lambda: directory), records


def test_uninstalled_clones_drop_out_of_the_instance_listing():
    """卸载即停用，被卸载的分身不再是在册实例，不该出现在实例列表里。

    它仍留在表里等着被恢复，但那是恢复选择器该看见的东西，不是「版本与实例」
    要展示的——列表里摆着一个已卸载的实例，用户无从判断它到底在不在。
    """
    from app.schemas.plugin import PluginInstance as PluginInstanceSchema

    store, _records = _memory_instance_store()
    for suffix, enabled in (("on", True), ("off", True)):
        store.save(
            PluginInstanceSchema(
                instance_id=f"DemoPlugin{suffix}",
                source_plugin_id="DemoPlugin",
                is_enabled=enabled,
            )
        )
    assert store.disable("DemoPluginoff") is True

    assert [item.instance_id for item in store.for_source("DemoPlugin")] == ["DemoPluginon"]
    # 但它没有消失：恢复选择器与版本回收都要看得见
    assert {item.instance_id for item in store.all_for_source("DemoPlugin")} == {
        "DemoPluginon",
        "DemoPluginoff",
    }
    assert [item.instance_id for item in store.disabled_for_source("DemoPlugin")] == [
        "DemoPluginoff"
    ]


def test_version_recycling_still_protects_a_pinned_version_of_an_uninstalled_clone():
    """已卸载分身锚定的版本目录不能被当成无人引用而回收。

    回收掉它，用户恢复出来的分身会落到一个不存在的版本上，只能悄悄回落到当前
    版本——他钉版本的用意就此作废。
    """
    from app.runtime.extensions.plugin.binding import PluginVersionBinding
    from app.schemas.plugin import PluginInstance as PluginInstanceSchema

    store, _records = _memory_instance_store()
    store.save(
        PluginInstanceSchema(
            instance_id="DemoPluginold",
            source_plugin_id="DemoPlugin",
            pinned_version="1.0.0",
            is_enabled=True,
        )
    )
    store.disable("DemoPluginold")

    binding = PluginVersionBinding(
        plugins_root=__import__("pathlib").Path("/nonexistent"),
        plugin_exists=lambda _plugin_id: True,
        get_instance=store.get,
        instances_for_source=store.for_source,
        all_instances_for_source=store.all_for_source,
        save_instance=store.save,
        get_host_instance=store.get_host,
        save_host_instance=store.save_host,
        running=dict,
        start=lambda _instance_id, _version: None,
        stop=lambda _instance_id: None,
        multi_version_blockers=lambda _plugin_id, _dirs: [],
        log=__import__("types").SimpleNamespace(
            debug=lambda *_a: None,
            info=lambda *_a: None,
            warning=lambda *_a: None,
            error=lambda *_a: None,
        ),
    )

    referenced = binding._referenced_versions("DemoPlugin")

    assert "1.0.0" in referenced


def test_instance_writes_resolve_a_differently_cased_id():
    """大小写不一致的实例 ID 仍要命中同一行，否则卸载会回报成功却什么也没做。

    判存走的是大小写不敏感解析（运行态查询一向如此），写入却只做精确比较：
    两边口径不一致时，端点认定这是个分身、调用卸载，而写入静默落空。
    """
    from app.schemas.plugin import PluginInstance as PluginInstanceSchema

    store, records = _memory_instance_store()
    store.save(
        PluginInstanceSchema(
            instance_id="DemoPluginwork",
            source_plugin_id="DemoPlugin",
            is_enabled=True,
        )
    )

    assert store.get("DemoPluginWork") is not None
    assert store.disable("DemoPluginWork") is True
    # 写回的是落盘的那一行，而不是凭调用方给的大小写另建一行
    assert records["DemoPluginwork"].is_enabled is False
    assert len(records) == 1

    assert store.enable("DemoPluginWORK") is True
    assert records["DemoPluginwork"].is_enabled is True

    assert store.purge("DemoPluginWork") is True
    assert records == {}


def test_uninstalled_clones_stay_out_of_the_installed_plugin_list():
    """已卸载的分身不该出现在「我的插件」里。

    目录投影把实例清单直接并进已安装清单；取数口径若不分启用与否，一个已经卸载
    掉的分身会继续以插件卡片的形式摆在那里，用户无从知道它其实已经不在了。
    """
    from app.schemas.plugin import PluginInstance as PluginInstanceSchema

    store, _records = _memory_instance_store()
    for suffix in ('on', 'off'):
        store.save(
            PluginInstanceSchema(
                instance_id=f'DemoPlugin{suffix}',
                source_plugin_id='DemoPlugin',
                is_enabled=True,
            )
        )
    store.disable('DemoPluginoff')

    # 这正是 runtime 装给 PluginCatalog 的那个端口
    assert sorted(store.enabled()) == ['DemoPluginon']
    # 本体的展示覆盖仍要拿得到全部行，否则卡片会丢掉自定义名称与图标
    assert 'DemoPluginoff' not in store.all_hosts()
