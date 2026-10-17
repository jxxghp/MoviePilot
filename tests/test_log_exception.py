"""
异常日志入口应保留错误等级、调用位置和备份接口的安全失败响应。
"""

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.api.endpoints import system as system_endpoint
from app.runtime import log as log_module
from app.runtime.log import LoggerManager, logger


@pytest.fixture
def captured_logs(monkeypatch, caplog):
    """
    捕获真实标准库日志记录与文件消息，避免写入运行目录。
    """
    console = logging.Logger("exception-contract", level=logging.DEBUG)
    console.addHandler(caplog.handler)
    writer = Mock()
    monkeypatch.setattr(LoggerManager, "_write_file_log", writer)
    monkeypatch.setattr(LoggerManager, "_get_console_logger", classmethod(lambda _cls, _path: console))
    monkeypatch.setattr(log_module, "_effective_context_log_level", lambda: logging.ERROR)
    return caplog, writer


@pytest.mark.parametrize("include_exception", [None, False, True])
def test_exception_logs_error_with_current_exception(captured_logs, include_exception):
    """
    默认记录当前异常，也支持调用方显式关闭或启用堆栈。
    """
    caplog, writer = captured_logs
    original = ValueError("original failure")
    try:
        raise original
    except ValueError:
        options = {} if include_exception is None else {"exc_info": include_exception}
        logger.exception("operation %s failed", "backup", **options)
    record = caplog.records[-1]
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "test_log_exception.py - operation backup failed"
    if include_exception is False:
        assert record.exc_info is False
    else:
        assert record.exc_info[1] is original
        assert "ValueError: original failure" in caplog.text
    assert writer.call_args.args == ("ERROR", record.getMessage(), LoggerManager._default_log_file)


def test_exception_accepts_explicit_exception(captured_logs):
    """
    离开异常处理分支后仍能显式指定需要记录的原异常。
    """
    caplog, _writer = captured_logs
    failure = RuntimeError("explicit failure")
    logger.exception("saved exception", exc_info=failure)
    assert caplog.records[-1].exc_info[1] is failure


def test_exception_respects_current_log_threshold(captured_logs, monkeypatch):
    """
    高于 ERROR 的日志阈值继续拦截异常消息。
    """
    caplog, writer = captured_logs
    monkeypatch.setattr(log_module, "_effective_context_log_level", lambda: logging.CRITICAL)
    logger.exception("filtered failure")
    assert not caplog.records
    writer.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,operation,args", [
    (system_endpoint.list_database_backups, "list_backups", ()),
    (system_endpoint.create_database_backup, "create_backup", ()),
    (system_endpoint.verify_database_backup, "verify_backup", ("backup.db",)),
    (system_endpoint.delete_database_backup, "delete_backup", ("backup.db",)),
])
async def test_database_failure_preserves_safe_http_error(captured_logs, monkeypatch, handler, operation, args):
    """
    备份服务失败时记录原异常，返回预定 500 且不泄露内部路径。
    """
    caplog, _writer = captured_logs
    failure = RuntimeError("private database path /sensitive/database.db")
    governance = SimpleNamespace(**{operation: Mock(side_effect=failure)})
    monkeypatch.setattr(system_endpoint, "get_database_governance", lambda: governance)
    with pytest.raises(HTTPException) as caught:
        await handler(*args, _=object())
    assert caught.value.status_code == 500
    assert caught.value.detail.endswith("，请查看日志")
    assert "/sensitive/" not in caught.value.detail
    assert caught.value.__cause__ is failure
    assert caplog.records[-1].levelno == logging.ERROR
    assert caplog.records[-1].exc_info[1] is failure


def test_critical_logs_keep_level_and_file_route(captured_logs):
    """
    严重错误入口应将相同内容以 CRITICAL 等级交给控制台和文件。
    """
    caplog, writer = captured_logs
    logger.critical("critical failure")
    record = caplog.records[-1]
    assert record.levelno == logging.CRITICAL
    assert record.getMessage().endswith("critical failure")
    assert writer.call_args.args == ("CRITICAL", record.getMessage(), LoggerManager._default_log_file)


@pytest.mark.parametrize("category,resource_id", [("../invalid", 1), ("plugin", 0)])
def test_invalid_resource_log_scope_preserves_context(category, resource_id):
    """
    资源标识非法时拒绝绑定，避免污染原有日志上下文。
    """
    with log_module.bind_log_scope("plugin", 1, "operation", "test") as scope:
        with pytest.raises(ValueError, match="日志资源标识无效"):
            with log_module.bind_log_scope(category, resource_id, "bad", "test"):
                pytest.fail("invalid resource scope must not be entered")
        with log_module.bind_log_scope("plugin", 1, "nested", "test") as nested:
            assert nested is scope
