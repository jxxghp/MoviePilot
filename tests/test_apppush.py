"""
私有 AppPush 的认证边界、用户身份迁移和现有通知路由回归。
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app.adapters.external.server import MoviePilotServerHelper
from app.api.dependencies.auth import get_current_active_user
from app.api.endpoints import notification
from app.modules.apppush import AppPushModule
from app.schemas.message import Message
from app.schemas.system import NotificationConf
from app.schemas.types import MessageType, NotificationChannel


def test_app_device_endpoint_forces_authenticated_owner(monkeypatch):
    """
    当前用户可管理自己的安装，正文提交其他 owner 必须在进入渠道前拒绝。
    """
    calls = []

    class Chain:
        """
        记录最终进入通知链的参数，不触发真实模块和网络。
        """

        def manage_channel(self, **params):
            """
            返回测试绑定并保留由端点注入的账号身份。
            """
            calls.append(params)
            return {"success": True, "data": {"owner": params["owner"]}}

    monkeypatch.setattr(notification, "NotificationChain", Chain)
    app = FastAPI()
    app.include_router(notification.router, prefix="/notification")
    app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(notification_identity="a" * 32)
    body = {"action": "status", "installation": "installation-001", "origin": "https://mp.test"}
    with TestClient(app) as client:
        assert client.post("/notification/app", json={**body, "owner": "b" * 32}).status_code == 422
        result = client.post("/notification/app", json=body)
    assert result.status_code == 200
    assert result.json()["data"]["owner"] == "a" * 32
    assert len(calls) == 1
    assert calls[0]["channel"] == "AppPush"


def test_apppush_reuses_message_switches_and_never_broadcasts(monkeypatch):
    """
    仅命中现有消息开关、渠道、来源与定向用户时向 Server 发送。
    """
    conf = NotificationConf(name="private", type="apppush", enabled=True, switchs=["资源下载"])
    module = AppPushModule.__new__(AppPushModule)
    module._channel = NotificationChannel.AppPush
    module._configs = {"private": conf}
    monkeypatch.setattr(module, "get_configs", lambda: {"private": conf})
    calls = []
    monkeypatch.setattr(module, "_request", lambda *args: calls.append(args) or {"total": 1, "accepted": 1})
    base = {"mtype": MessageType.Download, "title": "下载完成", "targets": {"apppush": "a" * 32}}
    message = Message(**base, link="https://foreign.test")
    message.set_notification_route_scope("user")
    module.post_message(message)
    assert len(calls) == 1
    assert calls[0][3]["owner"] == "a" * 32
    assert calls[0][3]["route"] == "/?shotcut=message"
    module.post_message(Message(**{**base, "targets": {}}))
    module.post_message(Message(**base, channel=NotificationChannel.Telegram))
    module.post_message(Message(**base, source="other"))
    conf.switchs = []
    module.post_message(Message(**base))
    assert len(calls) == 1


def test_server_request_keeps_key_in_header_and_rejects_redirects(monkeypatch):
    """
    中转只允许固定接口和 HTTPS，不跟随重定向，不把错误正文或密钥暴露给客户端。
    """
    captured = {}

    class Request:
        """
        在实际传输调用处记录 headers、params 和重定向策略。
        """

        def __init__(self, **kwargs):
            """
            保存 HTTP 客户端初始化参数。
            """
            captured.update(kwargs)

        def request(self, method, url, **kwargs):
            """
            模拟 Server 返回成功或重定向响应。
            """
            captured.update(method=method, url=url, **kwargs)
            return SimpleNamespace(status_code=200, json=lambda: {"devices": []})

    monkeypatch.setattr("app.adapters.external.server.RequestUtils", Request)
    key = "private-key-" * 4
    assert MoviePilotServerHelper.relay_push("https://relay.test", key, "GET", "devices", {"owner": "a" * 32}) == {"devices": []}
    assert captured["headers"] == {"Authorization": "Bearer " + key}
    assert captured["allow_redirects"] is False and captured["verify"] is True
    assert key not in captured["url"] and key not in str(captured["params"])
    with pytest.raises(ValueError, match="HTTPS"):
        MoviePilotServerHelper.relay_push("http://relay.test", key, "POST", "messages", {})


def test_notification_identity_migration_is_stable_and_non_nullable(tmp_path):
    """
    旧用户逐个获得不同身份，重复迁移不改已绑定值，改名和主键复用不会串设备。
    """
    path = Path(__file__).parents[1] / "database/versions/da73b6f20418_notification_identity.py"
    spec = importlib.util.spec_from_file_location("notification_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE user (id INTEGER PRIMARY KEY, name TEXT NOT NULL)'))
        connection.execute(text("INSERT INTO user (name) VALUES ('alice'), ('bob')"))
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            first = list(connection.execute(text('SELECT notification_identity FROM user ORDER BY id')).scalars())
            module.upgrade()
        assert len(set(first)) == 2 and all(len(value) == 32 for value in first)
        assert list(connection.execute(text('SELECT notification_identity FROM user ORDER BY id')).scalars()) == first
        assert next(column for column in inspect(connection).get_columns('user') if column['name'] == 'notification_identity')['nullable'] is False
    engine.dispose()
