"""目录监控分发器的整理历史查重与整理异常重试测试。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.runtime.config import settings
from app.application.history import (
    clear_transfer_failures,
    failed_retry_count,
    record_transfer_failure,
)
from app.monitor.dispatcher import TransferDispatcher


def _build_dispatcher() -> TransferDispatcher:
    """
    构造测试用整理分发器，使用普通字典充当去重缓存。
    :return: 整理分发器
    """
    return TransferDispatcher(all_exts=[".mkv"], cache={})


def _history(status: bool = True, size=None, src_fileitem=..., src=None, src_storage=None,
             history_id: int = 1):
    """
    构造整理历史记录替身。
    :param status: 整理是否成功
    :param size: 记录中的源文件大小
    :param src_fileitem: 直接指定源文件项，默认按 size 生成
    :param src: 记录源路径，未指定时查重闸的失败重试计数按 None 处理（恒为 0）
    :param src_storage: 记录源存储
    :param history_id: 记录 ID，describe_history_gate 的日志文案需要
    :return: 整理历史记录替身
    """
    if src_fileitem is ...:
        src_fileitem = {"size": size}
    return SimpleNamespace(id=history_id, status=status, src_fileitem=src_fileitem,
                            src=src, src_storage=src_storage)


def _reset_failed_retries(src_path, storage=None):
    """清空失败重试计数，隔离用例之间共享的模块级计数缓存。"""
    clear_transfer_failures(src_path, storage)


def _patch_history(monkeypatch, record=None, success_record=None) -> MagicMock:
    """
    替换整理历史查询，返回指定记录。
    :param monkeypatch: pytest monkeypatch
    :param record: get_by_src 返回的记录
    :param success_record: 成功记录二次确认的返回值
    :return: 整理历史操作替身
    """
    oper = MagicMock()
    oper.get_by_src.return_value = record
    oper.get_success_by_src.return_value = success_record
    monkeypatch.setattr("app.monitor.dispatcher.get_transfer_history_port", MagicMock(return_value=oper))
    return oper


def _patch_chain(monkeypatch, side_effect=None) -> MagicMock:
    """
    替换整理链，记录整理调用。
    :param monkeypatch: pytest monkeypatch
    :param side_effect: do_transfer 的副作用
    :return: 整理链替身
    """
    chain = MagicMock()
    if side_effect is not None:
        chain.do_transfer.side_effect = side_effect
    monkeypatch.setattr("app.monitor.dispatcher.TransferChain", MagicMock(return_value=chain))
    return chain


def test_no_history_goes_to_transfer(monkeypatch):
    """没有任何整理记录的文件应直接进入整理链。"""
    dispatcher = _build_dispatcher()
    _patch_history(monkeypatch, record=None)
    chain = _patch_chain(monkeypatch)

    assert dispatcher.handle_file(storage="local", event_path=Path("/downloads/a.mkv"), file_size=100) is True
    chain.do_transfer.assert_called_once()


def test_failed_history_is_retried_within_retry_budget(monkeypatch):
    """失败重试次数未达上限（默认计数为 0）时，失败的整理记录不得永久锁死文件，应放行重试。"""
    dispatcher = _build_dispatcher()
    _patch_history(monkeypatch, record=_history(status=False, size=100))
    chain = _patch_chain(monkeypatch)

    assert dispatcher.handle_file(storage="local", event_path=Path("/downloads/a.mkv"), file_size=100) is True
    chain.do_transfer.assert_called_once()


def test_failed_history_is_skipped_when_retry_budget_exhausted(monkeypatch):
    """失败重试次数已达上限时，失败的整理记录仍应被拦截、跳过整理。"""
    monkeypatch.setattr(settings, "TRANSFER_MAX_FAILED_RETRIES", 1)
    src_path = "/downloads/a.mkv"
    _reset_failed_retries(src_path, "local")
    try:
        record_transfer_failure(src_path, "local")
        dispatcher = _build_dispatcher()
        _patch_history(monkeypatch,
                       record=_history(status=False, size=100, src=src_path, src_storage="local"))
        chain = _patch_chain(monkeypatch)

        assert dispatcher.handle_file(storage="local", event_path=Path(src_path), file_size=100) is False
        chain.do_transfer.assert_not_called()
    finally:
        _reset_failed_retries(src_path, "local")


def test_failed_history_new_version_bypasses_exhausted_retry_budget(monkeypatch):
    """失败预算耗尽后新版本仍应进入整理链，而不是被错误标记为已处理。"""
    monkeypatch.setattr(settings, "TRANSFER_MAX_FAILED_RETRIES", 1)
    src_path = "/downloads/failed-new-version.mkv"
    _reset_failed_retries(src_path, "local")
    try:
        record_transfer_failure(src_path, "local", file_size=100)
        dispatcher = _build_dispatcher()
        _patch_history(
            monkeypatch,
            record=_history(
                status=False,
                size=100,
                src=src_path,
                src_storage="local",
            ),
        )
        chain = _patch_chain(monkeypatch)

        assert (
            dispatcher.handle_file(
                storage="local",
                event_path=Path(src_path),
                file_size=200,
            )
            is True
        )
        chain.do_transfer.assert_called_once()
    finally:
        _reset_failed_retries(src_path, "local")


def test_should_skip_by_history_returns_true_when_retry_budget_exhausted(monkeypatch):
    """直接调用 _should_skip_by_history：失败计数达到上限时应判定为跳过。"""
    monkeypatch.setattr(settings, "TRANSFER_MAX_FAILED_RETRIES", 1)
    src_path = "/downloads/b.mkv"
    _reset_failed_retries(src_path, "local")
    try:
        record_transfer_failure(src_path, "local")
        _patch_history(monkeypatch,
                       record=_history(status=False, size=100, src=src_path, src_storage="local"))

        result = TransferDispatcher._should_skip_by_history(
            storage="local", src_path=src_path, file_size=100
        )

        assert result is True
    finally:
        _reset_failed_retries(src_path, "local")


def test_success_history_with_changed_size_is_retried(monkeypatch):
    """同路径重新上传的不同版本应放行，由整理链的覆盖模式决断。"""
    dispatcher = _build_dispatcher()
    _patch_history(monkeypatch, record=_history(status=True, size=100))
    chain = _patch_chain(monkeypatch)

    assert dispatcher.handle_file(storage="local", event_path=Path("/downloads/a.mkv"), file_size=200) is True
    chain.do_transfer.assert_called_once()


def test_success_history_without_change_is_skipped(monkeypatch):
    """已成功整理且文件未变化时跳过，并留下 debug 痕迹便于排查。"""
    dispatcher = _build_dispatcher()
    _patch_history(monkeypatch, record=_history(status=True, size=100))
    chain = _patch_chain(monkeypatch)
    recorder = MagicMock()
    monkeypatch.setattr("app.monitor.dispatcher.logger", recorder)

    assert dispatcher.handle_file(storage="local", event_path=Path("/downloads/a.mkv"), file_size=100) is False
    chain.do_transfer.assert_not_called()
    assert any("跳过" in str(call.args[0]) for call in recorder.debug.call_args_list)


def test_success_history_without_size_info_is_skipped(monkeypatch):
    """记录中缺少源文件大小时无法比对，保守跳过而不是重复整理。"""
    chain_calls = []
    for src_fileitem in (None, {}, {"size": None}, {"size": "未知"}, "bad-json"):
        dispatcher = _build_dispatcher()
        _patch_history(monkeypatch, record=_history(status=True, src_fileitem=src_fileitem))
        chain = _patch_chain(monkeypatch)

        assert dispatcher.handle_file(storage="local", event_path=Path("/downloads/a.mkv"),
                                      file_size=100) is False
        chain_calls.append(chain.do_transfer.call_count)
    assert chain_calls == [0, 0, 0, 0, 0]


def test_bluray_folder_without_file_size_is_skipped(monkeypatch):
    """蓝光原盘目录没有文件大小可比对，已成功整理过时应跳过。"""
    dispatcher = _build_dispatcher()
    oper = _patch_history(monkeypatch, record=_history(status=True, size=100))
    chain = _patch_chain(monkeypatch)

    assert dispatcher.handle_file(storage="local",
                                  event_path=Path("/downloads/Movie/BDMV/STREAM/00000.m2ts"),
                                  file_size=None) is False
    chain.do_transfer.assert_not_called()
    # 蓝光目录的整理记录源路径带尾斜杠，查询必须原样传入
    assert oper.get_by_src.call_args.args[0] == "/downloads/Movie/"


def test_success_history_wins_over_failed_history(monkeypatch):
    """同一源路径同时存在成功与失败记录时，以成功记录为准。"""
    dispatcher = _build_dispatcher()
    _patch_history(monkeypatch,
                   record=_history(status=False, size=100),
                   success_record=_history(status=True, size=100))
    chain = _patch_chain(monkeypatch)

    assert dispatcher.handle_file(storage="local", event_path=Path("/downloads/a.mkv"), file_size=100) is False
    chain.do_transfer.assert_not_called()


def test_history_query_error_registers_pending(monkeypatch):
    """整理历史查询异常仍应登记待重试，而不是被当作已整理跳过。"""
    dispatcher = _build_dispatcher()
    oper = MagicMock()
    oper.get_by_src.side_effect = RuntimeError("数据库不可用")
    monkeypatch.setattr("app.monitor.dispatcher.get_transfer_history_port", MagicMock(return_value=oper))
    chain = _patch_chain(monkeypatch)

    assert dispatcher.handle_file(storage="local", event_path=Path("/downloads/a.mkv"), file_size=100) is False
    chain.do_transfer.assert_not_called()
    assert list(dispatcher._pending_retries) == ["local:/downloads/a.mkv"]


def test_transfer_exception_registers_pending_and_retries(monkeypatch):
    """整理执行抛异常的文件必须登记待重试，否则已落地的文件永久丢失。"""
    dispatcher = _build_dispatcher()
    _patch_history(monkeypatch, record=None)
    chain = _patch_chain(monkeypatch, side_effect=RuntimeError("数据库瞬断"))

    assert dispatcher.handle_file(storage="local", event_path=Path("/downloads/a.mkv"), file_size=100) is False
    assert list(dispatcher._pending_retries) == ["local:/downloads/a.mkv"]

    # 故障恢复后由健康检查周期驱动重试
    chain.do_transfer.side_effect = None
    dispatcher.retry_pending()

    assert chain.do_transfer.call_count == 2
    assert dispatcher._pending_retries == {}


def test_transfer_exception_retry_keeps_attempt_count(monkeypatch):
    """整理持续异常时重试次数要累计，避免无限重试。"""
    dispatcher = _build_dispatcher()
    _patch_history(monkeypatch, record=None)
    _patch_chain(monkeypatch, side_effect=RuntimeError("持续失败"))

    dispatcher.handle_file(storage="local", event_path=Path("/downloads/a.mkv"), file_size=100)
    dispatcher.retry_pending()

    assert dispatcher._pending_retries["local:/downloads/a.mkv"]["attempts"] == 2


def test_bluray_retry_uses_origin_event_path(monkeypatch):
    """蓝光原盘整理异常后应按原始事件路径重试，重试时重新解析目录。"""
    dispatcher = _build_dispatcher()
    _patch_history(monkeypatch, record=None)
    _patch_chain(monkeypatch, side_effect=RuntimeError("整理失败"))
    event_path = Path("/downloads/Movie/BDMV/STREAM/00000.m2ts")

    dispatcher.handle_file(storage="local", event_path=event_path, file_size=None)

    assert list(dispatcher._pending_retries) == [f"local:{event_path.as_posix()}"]
