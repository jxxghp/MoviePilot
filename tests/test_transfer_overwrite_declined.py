"""
覆盖「不覆盖裁决不应降级已有成功记录」的行为。

查重闸放行同路径新版本后，若 overwrite_mode 最终裁定不覆盖，媒体库中原有的
成功版本仍然在位——这是一次正常策略裁决而非整理故障。TransferChain 内部的
__is_overwrite_declined 用于识别这一场景，__default_callback 失败分支据此
决定是否写失败历史、发送失败事件与失败通知。本文件覆盖两者。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.chain.transfer import TransferChain
from app.schemas import TransferInfo
from app.schemas.types import EventType
from tests.test_transfer_job_manager import FakeMedia, make_task, make_transfer_chain


def make_history_oper(history=None, success_history=None, raise_on_query: bool = False,
                      add_fail_calls=None):
    """构造 __is_overwrite_declined / __default_callback 查询与写入整理历史使用的替身。"""

    def get_by_src(src, storage=None):
        if raise_on_query:
            raise RuntimeError("boom")
        return history

    def get_success_by_src(src, storage=None):
        return success_history

    def add_fail(**kwargs):
        if add_fail_calls is not None:
            add_fail_calls.append(kwargs)
        return SimpleNamespace(id=1)

    return SimpleNamespace(
        get_by_src=get_by_src,
        get_success_by_src=get_success_by_src,
        add_fail=add_fail,
    )


# ---------------------------------------------------------------------------
# TransferChain.__is_overwrite_declined
# ---------------------------------------------------------------------------


def test_overwrite_declined_false_when_flag_not_set():
    """overwrite_skipped 为假时直接判定为 False，且不应触发历史查询。"""
    task = make_task(1)
    transferinfo = TransferInfo(success=False, overwrite_skipped=False)
    transferhis = make_history_oper(raise_on_query=True)

    result = TransferChain._TransferChain__is_overwrite_declined(
        task, transferinfo, transferhis
    )

    assert result is False


def test_overwrite_declined_true_when_success_history_exists():
    """overwrite_skipped 为真且同源已有成功记录时，应判定为保护场景。"""
    task = make_task(1)
    success_history = SimpleNamespace(id=1, status=True)
    transferinfo = TransferInfo(success=False, overwrite_skipped=True)
    transferhis = make_history_oper(history=success_history)

    result = TransferChain._TransferChain__is_overwrite_declined(
        task, transferinfo, transferhis
    )

    assert result is True


def test_overwrite_declined_false_when_no_history():
    """overwrite_skipped 为真但没有任何整理记录时，不应判定为保护场景。"""
    task = make_task(1)
    transferinfo = TransferInfo(success=False, overwrite_skipped=True)
    transferhis = make_history_oper(history=None)

    result = TransferChain._TransferChain__is_overwrite_declined(
        task, transferinfo, transferhis
    )

    assert result is False


def test_overwrite_declined_false_when_only_failed_history():
    """overwrite_skipped 为真但只有失败记录时，不应判定为保护场景。"""
    task = make_task(1)
    failed_history = SimpleNamespace(id=2, status=False)
    transferinfo = TransferInfo(success=False, overwrite_skipped=True)
    transferhis = make_history_oper(history=failed_history, success_history=None)

    result = TransferChain._TransferChain__is_overwrite_declined(
        task, transferinfo, transferhis
    )

    assert result is False


def test_overwrite_declined_false_when_query_raises():
    """查询整理历史异常时应保守返回 False，不阻断原有失败语义。"""
    task = make_task(1)
    transferinfo = TransferInfo(success=False, overwrite_skipped=True)
    transferhis = make_history_oper(raise_on_query=True)

    result = TransferChain._TransferChain__is_overwrite_declined(
        task, transferinfo, transferhis
    )

    assert result is False


# ---------------------------------------------------------------------------
# __default_callback 失败分支
# ---------------------------------------------------------------------------


def _make_failed_task():
    """构造一个失败回调测试所需的最小整理任务。"""
    task = make_task(1)
    task.mediainfo = FakeMedia()
    # __default_callback 失败通知路径需要读取海报图，FakeMedia 本身不提供该接口
    task.mediainfo.get_message_image = lambda: "poster.jpg"
    task.background = False
    task.manual = True
    return task


def test_default_callback_skips_history_and_notification_when_overwrite_declined():
    """
    同源已有成功记录时，覆盖裁决不覆盖不应写失败历史、不应发送失败事件与通知。
    """
    chain = make_transfer_chain()
    chain.eventmanager = MagicMock()
    chain.post_message = MagicMock()

    task = _make_failed_task()
    success_history = SimpleNamespace(id=99, status=True)
    add_fail_calls = []
    transfer_history_oper = make_history_oper(
        history=success_history, add_fail_calls=add_fail_calls
    )

    transferinfo = TransferInfo(
        success=False,
        fileitem=task.fileitem,
        message="目标已存在，按覆盖策略跳过覆盖",
        transfer_type="copy",
        overwrite_skipped=True,
        need_notify=False,
    )

    with patch(
        "app.chain.transfer.TransferHistoryOper",
        return_value=transfer_history_oper,
    ), patch(
        "app.chain.transfer.settings.AI_AGENT_ENABLE", False
    ), patch(
        "app.chain.transfer.settings.AI_AGENT_RETRY_TRANSFER", False
    ):
        state, errmsg = chain._TransferChain__default_callback(task, transferinfo)

    assert state is False
    assert errmsg == transferinfo.message
    assert add_fail_calls == []
    assert chain.post_message.call_count == 0
    transfer_failed_events = [
        call
        for call in chain.eventmanager.send_event.call_args_list
        if call.args[0] == EventType.TransferFailed
    ]
    assert transfer_failed_events == []


def test_default_callback_keeps_original_failure_semantics_without_success_history():
    """
    没有已有成功记录时（即使 overwrite_skipped 为真），仍应按原有语义写失败历史并通知。
    """
    chain = make_transfer_chain()
    chain.eventmanager = MagicMock()
    chain.post_message = MagicMock()

    task = _make_failed_task()
    add_fail_calls = []
    transfer_history_oper = make_history_oper(
        history=None, add_fail_calls=add_fail_calls
    )

    transferinfo = TransferInfo(
        success=False,
        fileitem=task.fileitem,
        message="目标已存在，按覆盖策略跳过覆盖",
        transfer_type="copy",
        overwrite_skipped=True,
        need_notify=False,
    )

    with patch(
        "app.chain.transfer.TransferHistoryOper",
        return_value=transfer_history_oper,
    ), patch(
        "app.chain.transfer.settings.AI_AGENT_ENABLE", False
    ), patch(
        "app.chain.transfer.settings.AI_AGENT_RETRY_TRANSFER", False
    ):
        state, errmsg = chain._TransferChain__default_callback(task, transferinfo)

    assert state is False
    assert errmsg == transferinfo.message
    assert len(add_fail_calls) == 1
    assert chain.post_message.call_count == 1
    transfer_failed_events = [
        call
        for call in chain.eventmanager.send_event.call_args_list
        if call.args[0] == EventType.TransferFailed
    ]
    assert len(transfer_failed_events) == 1
