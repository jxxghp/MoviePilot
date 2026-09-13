"""插件实例日志等级覆盖列的持久化读写测试。"""

from __future__ import annotations

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.models.plugininstance import PluginInstance
from app.db.oper.plugininstance import PluginInstanceOper
from app.db.uow import run_sync_transaction

_INSTANCE_IDS = ("LogLevelHost", "LogLevelHost@clone1", "LogLevelNeverSet")


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


def test_set_log_level_creates_host_row_when_instance_has_no_row_yet():
    """只设过等级、从没存过参数的插件也要有一行，否则覆盖无处落盘。"""
    oper = PluginInstanceOper()

    assert oper.set_log_level(
        instance_id="LogLevelHost",
        log_level="DEBUG",
        log_expires_at="2026-01-01T00:00:00+00:00",
    ) is True

    record = oper.get("LogLevelHost")
    assert record is not None
    assert record.is_host is True
    assert record.log_level == "DEBUG"
    assert record.log_expires_at == "2026-01-01T00:00:00+00:00"


def test_set_log_level_keeps_existing_config_and_identity_on_a_clone_row():
    """在分身行上写等级只动两列，业务参数与归属原样保留。"""
    oper = PluginInstanceOper()
    oper.save(instance_id="LogLevelHost@clone1", source_plugin_id="LogLevelHost")
    oper.save_config_data(
        instance_id="LogLevelHost@clone1",
        source_plugin_id="LogLevelHost",
        config_data={"enable": True},
    )

    assert oper.set_log_level(
        instance_id="LogLevelHost@clone1",
        log_level="ERROR",
        log_expires_at=None,
    ) is True

    record = oper.get("LogLevelHost@clone1")
    assert record is not None
    assert record.log_level == "ERROR"
    assert record.log_expires_at is None
    assert record.config_data == {"enable": True}
    assert record.source_plugin_id == "LogLevelHost"


def test_clearing_log_level_recycles_a_host_row_that_carries_nothing_else():
    """清除覆盖后只剩一对身份列的本体行要回收，不留空行堆积。"""
    oper = PluginInstanceOper()
    oper.set_log_level(
        instance_id="LogLevelHost",
        log_level="WARNING",
        log_expires_at=None,
    )

    assert oper.set_log_level(
        instance_id="LogLevelHost",
        log_level=None,
        log_expires_at=None,
    ) is True

    assert oper.get("LogLevelHost") is None


def test_clearing_log_level_keeps_a_clone_row_and_a_host_row_with_config():
    """分身行与仍承载配置的本体行不因清除等级而消失。"""
    oper = PluginInstanceOper()
    oper.save(instance_id="LogLevelHost@clone1", source_plugin_id="LogLevelHost")
    oper.set_log_level(
        instance_id="LogLevelHost@clone1",
        log_level="DEBUG",
        log_expires_at=None,
    )
    oper.save_config_data(
        instance_id="LogLevelHost",
        source_plugin_id="LogLevelHost",
        config_data={"enable": False},
    )
    oper.set_log_level(
        instance_id="LogLevelHost",
        log_level="DEBUG",
        log_expires_at=None,
    )

    oper.set_log_level(
        instance_id="LogLevelHost@clone1", log_level=None, log_expires_at=None
    )
    oper.set_log_level(instance_id="LogLevelHost", log_level=None, log_expires_at=None)

    clone = oper.get("LogLevelHost@clone1")
    assert clone is not None
    assert clone.log_level is None
    host = oper.get("LogLevelHost")
    assert host is not None
    assert host.config_data == {"enable": False}


def test_clearing_log_level_for_an_unknown_instance_creates_nothing():
    """清除一个从未落盘过的实例不得凭空建出一行。"""
    oper = PluginInstanceOper()

    assert oper.set_log_level(
        instance_id="LogLevelNeverSet",
        log_level=None,
        log_expires_at=None,
    ) is False
    assert oper.get("LogLevelNeverSet") is None
