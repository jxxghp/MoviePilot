from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.routing import APIRoute

from app.api.endpoints import history as history_endpoint


def _route(path: str) -> APIRoute:
    """按路径返回整理历史 API 路由。"""
    return next(route for route in history_endpoint.router.routes if isinstance(route, APIRoute) and route.path == path)


def test_transfer_history_clear_uses_delete_and_hides_legacy_get() -> None:
    """新清空入口必须使用 DELETE，旧 GET 只保留为隐藏兼容。"""
    current = _route("/transfer/all")
    legacy = _route("/empty/transfer")

    assert current.methods == {"DELETE"}
    assert current.include_in_schema is True
    assert legacy.methods == {"GET"}
    assert legacy.include_in_schema is False


def test_transfer_history_clear_delegates_to_transaction_command() -> None:
    """清空入口只委托事务命令，不直接操作文件或数据库。"""
    command = Mock()
    command.truncate.return_value = SimpleNamespace(
        success=True,
        message="已清空旧整理记录，失败任务记录已保留",
    )

    response = history_endpoint.clear_transfer_history(
        command=command,
        _=object(),
    )

    command.truncate.assert_called_once_with()
    assert response.success is True
    assert response.message == "已清空旧整理记录，失败任务记录已保留"
