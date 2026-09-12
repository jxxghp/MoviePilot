"""插件实例与身份列举在独占事务下的结果物化测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.models.pluginidentity import PluginIdentity
from app.db.models.plugininstance import PluginInstance
from app.db.oper.pluginidentity import PluginIdentityOper
from app.db.oper.plugininstance import PluginInstanceOper
from app.db.uow import run_sync_transaction

_INSTANCE_IDS = ("DemoPlugin", "DemoPlugin@clone1", "OtherPlugin@clone1")
_NORMALIZED_PLUGIN_IDS = ("demoplugin",)


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
        session.execute(
            delete(PluginIdentity).where(
                PluginIdentity.normalized_plugin_id.in_(_NORMALIZED_PLUGIN_IDS)
            )
        )

    run_sync_transaction(purge)


def test_list_all_materializes_rows_before_session_close():
    """表中有记录时 list_all 必须返回已物化的实例，而不是关闭会话后的游标。"""
    oper = PluginInstanceOper()
    oper.save(instance_id="DemoPlugin", source_plugin_id="DemoPlugin")
    oper.save(instance_id="DemoPlugin@clone1", source_plugin_id="DemoPlugin")

    records = oper.list_all()

    assert {record.instance_id for record in records} >= {
        "DemoPlugin",
        "DemoPlugin@clone1",
    }


def test_list_by_source_materializes_rows_before_session_close():
    """按源插件列举同样要在会话关闭前取完行。"""
    oper = PluginInstanceOper()
    oper.save(instance_id="OtherPlugin@clone1", source_plugin_id="OtherPlugin")

    records = oper.list_by_source("OtherPlugin")

    assert [record.instance_id for record in records] == ["OtherPlugin@clone1"]


def test_save_rejects_flipping_an_existing_row_between_virtual_and_host():
    """分身与本体互不转换，改写已存在行的归属必须在持久化层被拒绝。

    角色由 instance_id 是否等于 source_plugin_id 派生，把归属改成实例 ID 自身正是
    把一个分身就地变成本体，只可能来自调用方错把分身实例 ID 当作源插件 ID 使用。
    """
    oper = PluginInstanceOper()
    oper.save(instance_id="DemoPlugin@clone1", source_plugin_id="DemoPlugin")

    with pytest.raises(ValueError):
        oper.save(
            instance_id="DemoPlugin@clone1",
            source_plugin_id="DemoPlugin@clone1",
        )

    preserved = oper.get("DemoPlugin@clone1")
    assert preserved is not None
    assert preserved.is_host is False
    assert preserved.source_plugin_id == "DemoPlugin"


def test_list_by_plugin_ids_materializes_rows_before_session_close():
    """插件身份的批量读取存在同样的游标物化要求。"""
    now = datetime.now(timezone.utc).isoformat()
    oper = PluginIdentityOper()
    oper.stage_create(
        PluginIdentity(
            plugin_id="DemoPlugin",
            normalized_plugin_id="demoplugin",
            trusted_source_type="local",
            binding_basis="package",
            payload_source_type="local",
            created_at=now,
            updated_at=now,
        )
    )

    records = oper.list_by_plugin_ids(["demoplugin"])

    assert [record.normalized_plugin_id for record in records] == ["demoplugin"]
