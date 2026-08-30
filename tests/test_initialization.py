"""首次初始化 API 的业务契约测试。"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.endpoints import login as login_endpoint
from app.schemas.initialization import InitializationRequest


class _FakeRuntimeSettings:
    """记录首次初始化对部署设置的更新与回滚。"""

    def __init__(self) -> None:
        """初始化空设置快照。"""
        self.values = {"SUPERUSER": "", "API_TOKEN": None}
        self.updates: list[tuple[str, object]] = []

    def get(self, key: str, default=None):
        """读取测试设置。"""
        return self.values.get(key, default)

    def update(self, key: str, value: object):
        """记录并应用测试设置。"""
        self.updates.append((key, value))
        self.values[key] = value
        return True, ""


class _FakeUserService:
    """提供初始化接口所需的最小异步用户端口。"""

    def __init__(self, initialized: bool = False) -> None:
        """保存初始用户状态。"""
        self.initialized = initialized
        self.created: list[dict] = []

    async def is_initialized(self) -> bool:
        """返回测试用户状态。"""
        return self.initialized

    async def create(self, payload: dict):
        """记录创建的管理员。"""
        self.created.append(payload)
        self.initialized = True
        return SimpleNamespace(id=1)


def _payload() -> InitializationRequest:
    """构造有效初始化请求。"""
    return InitializationRequest(
        username="admin",
        password="Admin123!",
        confirm_password="Admin123!",
        api_key="a" * 32,
    )


def test_initialization_status_reports_existing_users():
    """状态接口只暴露是否已有用户，不暴露用户名或 API Key。"""
    service = _FakeUserService(initialized=True)

    response = asyncio.run(login_endpoint.get_initialization_status(service))

    assert response.success is True
    assert response.data.initialized is True
    assert not hasattr(response.data, "api_key")


def test_initialize_instance_updates_settings_and_creates_superuser(monkeypatch):
    """首次初始化应先保存部署凭据，再创建唯一超级管理员。"""
    service = _FakeUserService()
    settings = _FakeRuntimeSettings()
    monkeypatch.setattr(login_endpoint, "get_runtime_settings", lambda: settings)
    monkeypatch.setattr(login_endpoint, "get_password_hash", lambda password: "hashed:" + password)

    response = asyncio.run(login_endpoint.initialize_instance(_payload(), service))

    assert response.success is True
    assert settings.updates == [("SUPERUSER", "admin"), ("API_TOKEN", "a" * 32)]
    assert service.created[0]["hashed_password"] == "hashed:Admin123!"
    assert service.created[0]["is_superuser"] is True


def test_initialize_instance_rejects_second_claim(monkeypatch):
    """已经存在用户时，第二次初始化必须返回冲突而不是覆盖账号。"""
    service = _FakeUserService(initialized=True)
    monkeypatch.setattr(login_endpoint, "get_runtime_settings", lambda: _FakeRuntimeSettings())

    with pytest.raises(HTTPException) as error:
        asyncio.run(login_endpoint.initialize_instance(_payload(), service))

    assert error.value.status_code == 409
    assert service.created == []
