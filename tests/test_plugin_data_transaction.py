"""插件持久化数据删除的事务所有权测试。"""

from unittest.mock import Mock

import pytest

from app.application.plugin.data import DeletePluginDataCommand
from app.db.models.plugindata import PluginData
from app.db.oper.plugindata import PluginDataOper


def test_delete_plugin_data_stages_then_commits() -> None:
    """插件重置必须在仓储暂存完成后由 Application Command 提交。"""
    calls: list[str] = []
    repository = Mock()
    repository.stage_delete.side_effect = lambda _plugin_id: calls.append("stage")
    unit_of_work = Mock()
    unit_of_work.commit.side_effect = lambda: calls.append("commit")
    command = DeletePluginDataCommand(repository, unit_of_work)

    command.execute("Demo")

    assert calls == ["stage", "commit"]
    repository.stage_delete.assert_called_once_with("Demo")
    unit_of_work.rollback.assert_not_called()


def test_delete_plugin_data_rolls_back_commit_failure() -> None:
    """插件数据删除提交失败必须回滚并保留原异常。"""
    error = RuntimeError("commit failed")
    repository = Mock()
    unit_of_work = Mock()
    unit_of_work.commit.side_effect = error
    command = DeletePluginDataCommand(repository, unit_of_work)

    with pytest.raises(RuntimeError) as raised:
        command.execute("Demo")

    assert raised.value is error
    unit_of_work.rollback.assert_called_once_with()


def test_plugin_data_oper_stage_delete_does_not_commit(db, monkeypatch) -> None:
    """Oper 只暂存目标插件删除，其他插件数据与提交权均不受影响。"""
    db.add(
        PluginData(plugin_id="Target", key="one", value=1),
        PluginData(plugin_id="Other", key="two", value=2),
    )
    commit = Mock(wraps=db.session.commit)
    monkeypatch.setattr(db.session, "commit", commit)
    oper = PluginDataOper(db.session)

    oper.stage_delete("Target")

    remaining = PluginData.get_plugin_data(db.session, "Other")
    deleted = PluginData.get_plugin_data(db.session, "Target")
    assert [item.key for item in remaining] == ["two"]
    assert deleted == []
    commit.assert_not_called()
    db.session.rollback()
