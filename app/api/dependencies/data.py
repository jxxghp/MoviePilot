"""未迁移领域共用的 API 数据兼容 Facade。"""

from typing import Any

from app.api.data import get_api_data_ports


def repository(name: str, session: Any) -> Any:
    """按旧能力名构造绑定当前请求会话的仓储。"""
    return get_api_data_ports().repository(name, session)


def standalone_repository(name: str) -> Any:
    """按旧能力名构造无需绑定请求会话的仓储。"""
    return get_api_data_ports().standalone_repository(name)


def transaction(name: str, session: Any) -> Any:
    """按旧能力名构造绑定当前请求会话的事务端口。"""
    return get_api_data_ports().transaction(name, session)
