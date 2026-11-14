"""
验证旧文件项快照恢复后的结算兼容及证据冲突保护。
"""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from app.application.transfer.execution import (
    TransferExecutionCheckpoint,
    TransferExecutionConflictError,
)
from app.application.transfer.workflow import TransferAdmission
from app.chain.transfer.settlement import _build_transfer_result_settlement
from app.schemas.file import FileItem
from tests.test_transfer_settling_recovery import (
    _build_chain,
    _plan_checkpoint,
    _planning_input,
    _recovered_task,
    _transfer_result,
)


def _remove_timestamp_defaults(item: dict) -> None:
    """
    还原纳秒时间字段引入前的文件项快照。

    :param item: 本测试新建且允许修改的文件项字典
    """
    item.pop("modify_time_ns", None)
    item.pop("change_time_ns", None)
    for child in item.get("children") or []:
        _remove_timestamp_defaults(child)


@pytest.mark.parametrize("changed", [None, "need_notify", "fileid", "size", "modify_time_ns", "change_time_ns"])
@pytest.mark.parametrize("recorded_time", [None, "9007199254740993"])
def test_recovered_legacy_result_accepts_only_added_null_timestamp_defaults(changed, recorded_time) -> None:
    """
    旧结果仅允许补齐空时间字段，通知、目标身份、大小和实际时间变化仍拒绝。
    """
    path = "/downloads/Movie.mkv"
    planning = _planning_input(path)
    result = _transfer_result(path, success=True)
    result.fileitem.children = [FileItem(storage="local", path="/downloads/child.mkv", type="file")]
    result.target_diritem = FileItem(storage="local", path="/library", type="dir")
    raw = result.model_dump(mode="json")
    for key in ("fileitem", "target_item", "target_diritem"):
        _remove_timestamp_defaults(raw[key])
    if recorded_time is not None:
        raw["target_item"]["modify_time_ns"] = recorded_time
    checkpoint = TransferExecutionCheckpoint.create(
        payload={"outcome": "succeeded", "transferinfo": raw}, operation_ids=("operation-1",),
    )
    before = deepcopy(checkpoint.to_payload())
    admission = TransferAdmission(
        task_id="settling-task", storage="local", src_path=path, state="planned",
        created_at="2026-08-27 09:00:00", updated_at="2026-08-27 09:00:00",
        planning_input=planning, checkpoint=_plan_checkpoint(planning),
        lease_owner="recovery-owner", lease_token="recovery-token",
    )
    chain = _build_chain(MagicMock())
    task = _recovered_task(chain, admission, checkpoint)

    def settle(callback_task, info):
        """
        使用真实结算校验检查从持久快照恢复的结果。
        """
        if changed == "need_notify":
            info.need_notify = not info.need_notify
        elif changed == "fileid":
            info.target_item.fileid = "changed"
        elif changed == "size":
            info.target_item.size = 2048
        elif changed:
            setattr(info.target_item, changed, 123)
        settlement = _build_transfer_result_settlement(callback_task, info)
        assert settlement.execution_fingerprint == checkpoint.fingerprint
        return True, ""

    if changed:
        with pytest.raises(TransferExecutionConflictError):
            chain._TransferChain__handle_planned_transfer(task, settle)
    else:
        assert chain._TransferChain__handle_planned_transfer(task, settle) == (True, "")
    assert checkpoint.to_payload() == before
    chain._TransferChain__select_storage_oper.assert_not_called()
    chain._plan_checkpoint_and_execute.assert_not_called()
