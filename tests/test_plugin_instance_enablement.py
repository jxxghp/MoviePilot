"""插件实例启停语义测试：数据访问层、实例存储与运行期装载取数。"""

from __future__ import annotations

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.models.plugininstance import PluginInstance as PluginInstanceRecord
from app.db.oper.plugininstance import PluginInstanceOper
from app.db.uow import run_sync_transaction
from app.runtime.extensions.plugin.storage import PluginInstanceStore, PluginStorage
from app.schemas.plugin import PluginInstance

_INSTANCE_IDS = ("EnablementHost", "EnablementHostX2", "EnablementOther")


@pytest.fixture(autouse=True)
def purge_written_rows():
    """删除用例写入的行，测试库在整个会话内共享，残留会干扰后续用例。"""
    yield

    def purge(session: Session) -> None:
        """按本文件使用的固定标识精确删除，不触碰其他用例的数据。"""
        session.execute(
            delete(PluginInstanceRecord).where(
                PluginInstanceRecord.instance_id.in_(_INSTANCE_IDS)
            )
        )

    run_sync_transaction(purge)


# --------------------------------------------------------------------------- #
# PluginInstanceOper.set_enabled
# --------------------------------------------------------------------------- #


def test_set_enabled_keeps_configuration_when_disabling():
    """停用只翻启用位，业务参数与展示信息原样留在那一行等待再次启用。

    这正是这一列存在的理由：把「在册」与「有配置」拆开，让用户能停掉一个实例而
    不必在「删掉配置」和「留着它继续跑」之间二选一。
    """
    oper = PluginInstanceOper()
    oper.save(
        instance_id="EnablementHostX2",
        source_plugin_id="EnablementHost",
        plugin_name="分身显示名",
        is_enabled=True,
    )
    oper.save_config_data(
        instance_id="EnablementHostX2",
        source_plugin_id="EnablementHost",
        config_data={"token": "kept"},
    )

    assert oper.set_enabled(instance_id="EnablementHostX2", is_enabled=False) is True

    record = oper.get("EnablementHostX2")
    assert record is not None
    assert record.is_enabled is False
    assert record.config_data == {"token": "kept"}
    assert record.plugin_name == "分身显示名"

    # 再次启用即恢复，配置不需要重填
    assert oper.set_enabled(instance_id="EnablementHostX2", is_enabled=True) is True
    restored = oper.get("EnablementHostX2")
    assert restored is not None
    assert restored.is_enabled is True
    assert restored.config_data == {"token": "kept"}


def test_disabling_clears_default_target_and_log_level_override():
    """停用同时清掉默认调用目标置位与日志等级覆盖，两者只对在册实例才有意义。

    不清默认目标，未指定实例的外部调用会被路由到一个不会被实例化的实例；不清日志
    等级，运行期停用时清掉的进程内覆盖与库里留下的那份会长期不一致。
    """
    oper = PluginInstanceOper()
    oper.save(instance_id="EnablementHost", source_plugin_id="EnablementHost", is_enabled=True)
    oper.save(
        instance_id="EnablementHostX2",
        source_plugin_id="EnablementHost",
        is_enabled=True,
    )
    oper.set_default_target("EnablementHost", "EnablementHostX2")
    oper.set_log_level(
        instance_id="EnablementHostX2",
        log_level="DEBUG",
        log_expires_at=None,
    )

    oper.set_enabled(instance_id="EnablementHostX2", is_enabled=False)

    record = oper.get("EnablementHostX2")
    assert record is not None
    assert record.is_default_target is False
    assert record.log_level is None
    assert record.log_expires_at is None


def test_set_enabled_reports_false_for_a_row_that_does_not_exist():
    """行不存在时如实返回失败，不隐式建行。"""
    oper = PluginInstanceOper()

    assert oper.set_enabled(instance_id="EnablementMissing", is_enabled=True) is False
    assert oper.get("EnablementMissing") is None


def test_list_enabled_excludes_disabled_rows():
    """运行期装载取数只看启用中的行。"""
    oper = PluginInstanceOper()
    oper.save(instance_id="EnablementHost", source_plugin_id="EnablementHost", is_enabled=True)
    oper.save(
        instance_id="EnablementHostX2",
        source_plugin_id="EnablementHost",
        is_enabled=False,
    )

    enabled = {record.instance_id for record in oper.list_enabled()}
    listed = {record.instance_id for record in oper.list_by_source("EnablementHost")}

    assert "EnablementHost" in enabled
    assert "EnablementHostX2" not in enabled
    # 停用的行仍然在册：卡片要看得见、卸载守卫要拦得住
    assert listed == {"EnablementHost", "EnablementHostX2"}


def test_an_enabled_host_row_is_never_recycled_as_empty():
    """启用中的本体行不算「只剩身份列」，清空配置不得把它连同装载判据一起回收。"""
    oper = PluginInstanceOper()
    oper.save(instance_id="EnablementHost", source_plugin_id="EnablementHost", is_enabled=True)
    oper.save_config_data(
        instance_id="EnablementHost",
        source_plugin_id="EnablementHost",
        config_data={"a": 1},
    )

    oper.clear_config_data("EnablementHost")

    record = oper.get("EnablementHost")
    assert record is not None, "启用中的本体行被当成空行回收，该插件将永不加载"
    assert record.is_enabled is True


# --------------------------------------------------------------------------- #
# PluginInstanceStore：分身与本体共用一个开关
# --------------------------------------------------------------------------- #


class _FakeDirectory:
    """在内存里模拟实例表读写的目录替身。"""

    def __init__(self, records: dict[str, PluginInstance]) -> None:
        self.records = records

    def get(self, instance_id: str):
        """按实例 ID 读取单行。"""
        return self.records.get(instance_id)

    def list_all(self):
        """列出全部行，含停用的。"""
        return list(self.records.values())

    def list_by_source(self, source_plugin_id: str):
        """按源插件列出全部行。"""
        return [
            record
            for record in self.records.values()
            if record.source_plugin_id == source_plugin_id
        ]

    def list_enabled(self):
        """列出启用中的行。"""
        return [record for record in self.records.values() if record.is_enabled]

    def save(self, instance: PluginInstance) -> None:
        """新增或更新一行。"""
        self.records[instance.instance_id] = instance

    def delete(self, instance_id: str) -> bool:
        """删除一行。"""
        return self.records.pop(instance_id, None) is not None

    def set_enabled(self, instance_id: str, is_enabled: bool) -> bool:
        """写入启用位。"""
        record = self.records.get(instance_id)
        if record is None:
            return False
        self.records[instance_id] = record.model_copy(update={"is_enabled": is_enabled})
        return True


def _store(records: dict[str, PluginInstance]) -> tuple[PluginInstanceStore, _FakeDirectory]:
    """构造挂接内存目录、且旧键导入已标记完成的实例存储。"""
    directory = _FakeDirectory(records)
    storage = PluginStorage(read=lambda _key: True, write=lambda _key, _value: None)
    return (
        PluginInstanceStore(storage=lambda: storage, directory=lambda: directory),
        directory,
    )


def _clone(instance_id: str, source: str, *, enabled: bool = True) -> PluginInstance:
    """构造一个分身实例描述。"""
    return PluginInstance(
        instance_id=instance_id, source_plugin_id=source, is_enabled=enabled
    )


def _host(plugin_id: str, *, enabled: bool = True) -> PluginInstance:
    """构造一个本体实例描述。"""
    return PluginInstance(
        instance_id=plugin_id, source_plugin_id=plugin_id, is_enabled=enabled
    )


def test_enabled_hosts_is_the_host_loading_criterion():
    """本体的装载取数只给出启用中的本体，停用的本体不在其中。"""
    store, _ = _store(
        {
            "PluginA": _host("PluginA"),
            "PluginB": _host("PluginB", enabled=False),
            "PluginAx2": _clone("PluginAx2", "PluginA"),
        }
    )

    assert set(store.enabled_hosts()) == {"PluginA"}
    # 分身不混进本体取数
    assert set(store.enabled()) == {"PluginAx2"}


def test_disabled_clone_stays_registered_and_visible():
    """停用的分身仍在册：卡片列表、卸载守卫与默认目标候选都要看得见它。

    读取口若替调用方把停用的行藏起来，卸载守卫会放过一个还留着行的分身，用户随后
    只能看到一个既列不出来、也删不掉的孤儿实例。
    """
    store, _ = _store(
        {"PluginA": _host("PluginA"), "PluginAx2": _clone("PluginAx2", "PluginA")}
    )
    assert store.disable("PluginAx2") is True

    assert set(store.all()) == {"PluginAx2"}
    assert [record.instance_id for record in store.for_source("PluginA")] == ["PluginAx2"]
    assert store.get("PluginAx2") is not None
    assert set(store.enabled()) == set()


def test_enable_and_disable_report_whether_state_changed():
    """重复启用或重复停用报告未发生变化，调用方据此决定要不要重载运行态。"""
    store, _ = _store(
        {"PluginA": _host("PluginA"), "PluginAx2": _clone("PluginAx2", "PluginA")}
    )

    assert store.disable("PluginAx2") is True
    assert store.disable("PluginAx2") is False
    assert store.enable("PluginAx2") is True
    assert store.enable("PluginAx2") is False


def test_enable_host_creates_the_row_when_the_plugin_has_none():
    """安装收尾时本体还没有行，必须按默认视图建出并置为启用。

    只往安装清单里加一条而不建这一行，插件会装完当次靠定向重载跑起来、重启后再也
    不加载。
    """
    store, directory = _store({})

    assert store.enable_host("PluginA") is True

    record = directory.records["PluginA"]
    assert record.is_host is True
    assert record.is_enabled is True
    assert store.enable_host("PluginA") is False


def test_disable_host_keeps_the_row_and_its_configuration():
    """停用本体保留整行，只翻启用位。"""
    store, directory = _store({"PluginA": _host("PluginA")})

    assert store.disable_host("PluginA") is True
    assert store.disable_host("PluginA") is False

    assert directory.records["PluginA"].is_enabled is False
    assert store.get_host("PluginA") is not None


def test_host_and_clone_views_never_leak_into_each_other():
    """本体与分身两组读写口互不可见，避免按分身写入顶掉本体那一行。"""
    store, _ = _store(
        {"PluginA": _host("PluginA"), "PluginAx2": _clone("PluginAx2", "PluginA")}
    )

    assert store.get("PluginA") is None
    assert store.get_host("PluginAx2") is None
    assert set(store.all_hosts()) == {"PluginA"}
    assert set(store.all()) == {"PluginAx2"}
