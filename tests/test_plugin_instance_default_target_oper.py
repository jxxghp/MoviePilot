"""插件实例默认调用目标置位与清除的数据访问层测试。"""

from __future__ import annotations

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.models.plugininstance import PluginInstance
from app.db.oper.plugininstance import PluginInstanceOper
from app.db.uow import run_sync_transaction

_INSTANCE_IDS = (
    "TargetOperHost",
    "TargetOperHostX2",
    "TargetOperHostX3",
    "TargetOperOther",
)


@pytest.fixture(autouse=True)
def purge_written_rows():
    """删除用例写入的行，测试库在整个会话内共享，残留会干扰后续用例。"""
    yield

    def purge(session: Session) -> None:
        """按本文件使用的固定标识精确删除，不触碰其他用例的数据。"""
        session.execute(
            delete(PluginInstance).where(
                PluginInstance.instance_id.in_(_INSTANCE_IDS)
            )
        )

    run_sync_transaction(purge)


def _seed(oper: PluginInstanceOper) -> None:
    """建出一个本体和两个分身，供置位与清除用例共用。"""
    oper.save(instance_id="TargetOperHost", source_plugin_id="TargetOperHost")
    oper.save(instance_id="TargetOperHostX2", source_plugin_id="TargetOperHost")
    oper.save(instance_id="TargetOperHostX3", source_plugin_id="TargetOperHost")


def test_set_default_target_marks_only_the_requested_row():
    """置位只落在目标行上，同插件其余行保持未置位。"""
    oper = PluginInstanceOper()
    _seed(oper)

    assert oper.set_default_target("TargetOperHost", "TargetOperHostX2") is True

    flags = {
        record.instance_id: record.is_default_target
        for record in oper.list_by_source("TargetOperHost")
    }
    assert flags == {
        "TargetOperHost": False,
        "TargetOperHostX2": True,
        "TargetOperHostX3": False,
    }


def test_set_default_target_clears_the_previous_default_in_one_transaction():
    """改置默认目标时旧置位必须一并清掉，不能出现两行同时为真。

    「同一源插件至多一个默认目标」由条件唯一索引在库层强制，清旧与置新若分处两个
    事务就会撞上索引；这条用例正是在真实连接上证明清旧置新落在同一次写入里。
    """
    oper = PluginInstanceOper()
    _seed(oper)
    oper.set_default_target("TargetOperHost", "TargetOperHostX2")

    assert oper.set_default_target("TargetOperHost", "TargetOperHostX3") is True

    marked = [
        record.instance_id
        for record in oper.list_by_source("TargetOperHost")
        if record.is_default_target
    ]
    assert marked == ["TargetOperHostX3"]


def test_set_default_target_rejects_a_row_belonging_to_another_plugin():
    """目标行不归属该源插件时原样返回失败，且不动原有置位。"""
    oper = PluginInstanceOper()
    _seed(oper)
    oper.save(instance_id="TargetOperOther", source_plugin_id="TargetOperOther")
    oper.set_default_target("TargetOperHost", "TargetOperHostX2")

    assert oper.set_default_target("TargetOperHost", "TargetOperOther") is False

    marked = [
        record.instance_id
        for record in oper.list_by_source("TargetOperHost")
        if record.is_default_target
    ]
    assert marked == ["TargetOperHostX2"]


def test_set_default_target_reports_false_for_a_row_that_does_not_exist():
    """目标行尚未落盘时如实返回失败，不隐式建行。"""
    oper = PluginInstanceOper()
    _seed(oper)

    assert oper.set_default_target("TargetOperHost", "TargetOperHostMissing") is False
    assert oper.get("TargetOperHostMissing") is None


def test_clear_default_target_is_idempotent():
    """清除置位后重复调用保持幂等，不报错也不产生新的置位。"""
    oper = PluginInstanceOper()
    _seed(oper)
    oper.set_default_target("TargetOperHost", "TargetOperHostX2")

    oper.clear_default_target("TargetOperHost")
    oper.clear_default_target("TargetOperHost")

    assert not any(
        record.is_default_target for record in oper.list_by_source("TargetOperHost")
    )


def test_host_row_carrying_only_a_default_target_is_not_recycled_as_empty():
    """本体行只剩默认目标置位时不算「只剩身份列」，不能被空行回收清掉。

    回收判据漏掉这一列会让用户把本体设为默认调用目标后，下一次清空配置就把置位
    连同整行一起删掉，默认目标无声失效。
    """
    oper = PluginInstanceOper()
    _seed(oper)
    oper.set_default_target("TargetOperHost", "TargetOperHost")

    record = oper.get("TargetOperHost")
    assert record is not None
    assert record.is_default_target is True
    assert record.carries_only_identity is False
