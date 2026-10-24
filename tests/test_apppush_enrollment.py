"""
中心自动接入的真实配置持久化、故障接续及管理员权限边界回归。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.external.server import MoviePilotServerHelper
from app.api.dependencies.auth import get_current_active_user
from app.api.endpoints import notification
from app.application.configuration import SystemConfigService
from app.application.settings.service import SystemSettingsService
from app.db.models.systemconfig import SystemConfig
from app.db.oper.systemconfig import SystemConfigOper
from app.db.session import SessionFactory
from app.modules.apppush import AppPushModule
from app.schemas.system import NotificationConf
from app.schemas.types import SystemConfigKey

SERVER = "https://relay.test"
UID = "existing-deployment-user-unique-id"


def fresh_service() -> SystemConfigService:
    """
    创建独立配置快照并从测试数据库重载，模拟宿主重启后的实际持久化读取。
    """
    oper = object.__new__(SystemConfigOper)
    SystemConfigOper.__init__(oper)
    oper.load_snapshot()
    return SystemConfigService(repository=oper)


@pytest.fixture
def configuration(monkeypatch):
    """
    只替换本模块的配置入口，复用测试引导的临时 SQL，结束时还原本键。
    """
    service = fresh_service()
    original = service.get(SystemConfigKey.AppPushCredentials)
    service.set(SystemConfigKey.AppPushCredentials, {})
    monkeypatch.setattr("app.modules.apppush.get_configured_system_config", lambda: service)
    monkeypatch.setattr("app.modules.apppush.get_runtime_setting", lambda _key: SERVER)
    monkeypatch.setattr(MoviePilotServerHelper, "get_user_uid", lambda: UID)
    try:
        yield service
    finally:
        service.set(SystemConfigKey.AppPushCredentials, original)


def test_registration_persists_before_network_and_reuses_key_after_restart(monkeypatch, configuration):
    """
    先落库再登记，渠道和普通状态响应均不包含密钥，重启不产生第二个实例。
    """
    calls = []

    class Transport:
        """
        只替换最后 HTTP 边界，在发送时用独立 SQL 会话核对凭据已提交。
        """

        def __init__(self, **options):
            """
            保存出站请求头里的私有凭据，不改变请求构造行为。
            """
            self.key = options["headers"]["Authorization"].removeprefix("Bearer ")

        def request(self, method, url, **options):
            """
            验证已有 UID、密钥存放位置和先落库约束，再响应登记或设备查询。
            """
            with SessionFactory() as db:
                stored = SystemConfig.get_by_key(db, SystemConfigKey.AppPushCredentials.value).value
                assert stored[SERVER]["key"] == self.key
            calls.append((method, url, self.key))
            assert options["allow_redirects"] is False
            if url.endswith('/instances'):
                assert options["json"] == {"user_uid": UID}
                payload = {"instance": "a" * 32, "registered": True}
            else:
                payload = {"devices": []}
            return SimpleNamespace(status_code=200, json=lambda: payload)

    monkeypatch.setattr("app.adapters.external.server.RequestUtils", Transport)
    conf = NotificationConf(name="automatic", type="apppush", enabled=True, config={})
    module = AppPushModule.__new__(AppPushModule)
    monkeypatch.setattr(module, "get_configs", lambda: {"automatic": conf})
    result = module.channel_manage("AppPush", "status", owner="a" * 32)
    key = configuration.get(SystemConfigKey.AppPushCredentials)[SERVER]["key"]
    assert result["success"] is True and result["data"]["relay_url"] == SERVER + "/push/app/"
    assert key.startswith('mpa_') and len(key) == 47
    assert key not in str(result) and key not in str(conf.model_dump())
    restarted = fresh_service()
    monkeypatch.setattr("app.modules.apppush.get_configured_system_config", lambda: restarted)
    second_module = AppPushModule.__new__(AppPushModule)
    monkeypatch.setattr(second_module, "get_configs", lambda: {"automatic": conf})
    assert second_module.channel_manage("AppPush", "status", owner="a" * 32)["success"] is True
    assert sum(url.endswith('/instances') for _, url, _ in calls) == 1
    assert {request_key for _, _, request_key in calls} == {key}


def test_rotation_response_loss_keeps_candidate_for_restart_recovery(monkeypatch, configuration):
    """
    已生效轮换的响应丢失时保留两份本地状态，重启只重试相同候选，不新建空间。
    """
    remote = {"key": "", "previous": "", "lost": False}
    calls = []

    class Transport:
        """
        模拟中心接受轮换后丢失响应，出站请求仍经过真实适配器。
        """

        def __init__(self, **options):
            """
            提取真实构造的 Bearer 头用于服务端鉴权替身。
            """
            self.key = options["headers"]["Authorization"].removeprefix("Bearer ")

        def request(self, method, url, **options):
            """
            按中心幂等规则模拟登记和轮换，丢响应不会回滚远端已接受的状态。
            """
            calls.append((method, url, self.key))
            if url.endswith('/instances/credentials'):
                candidate = options['json']['key']
                stored = fresh_service().get(SystemConfigKey.AppPushCredentials)[SERVER]
                assert stored['pending'] == candidate
                assert (self.key == remote['key'] or
                        (self.key == remote['previous'] and candidate == remote['key']))
                remote['previous'], remote['key'] = self.key, candidate
                if not remote['lost']:
                    remote['lost'] = True
                    return None
                payload = {"instance": "a" * 32, "rotated": True}
            elif url.endswith('/instances'):
                remote['key'] = self.key
                payload = {"instance": "a" * 32, "registered": True}
            else:
                assert self.key == remote['key']
                payload = {"devices": []}
            return SimpleNamespace(status_code=200, json=lambda: payload)

    monkeypatch.setattr("app.adapters.external.server.RequestUtils", Transport)
    module = AppPushModule.__new__(AppPushModule)
    original = module._automatic_key(SERVER)
    with pytest.raises(ValueError, match="请求失败"):
        module._automatic_key(SERVER, rotate=True)
    stored = fresh_service().get(SystemConfigKey.AppPushCredentials)[SERVER]
    assert stored['key'] == original and stored['pending'] == remote['key'] != original
    restarted = fresh_service()
    monkeypatch.setattr("app.modules.apppush.get_configured_system_config", lambda: restarted)
    recovered = AppPushModule.__new__(AppPushModule)._automatic_key(SERVER)
    assert recovered == remote['key'] != original
    assert restarted.get(SystemConfigKey.AppPushCredentials)[SERVER]['pending'] == ''
    assert sum(url.endswith('/instances') for _, url, _ in calls) == 1
    assert sum(url.endswith('/instances/credentials') for _, url, _ in calls) == 2
    spec = SystemSettingsService(MagicMock(), restarted, MagicMock()).describe(setting_key=SystemConfigKey.AppPushCredentials.value)
    assert recovered not in str(spec) and spec['definition']['generic_write_allowed'] is False


def test_instance_rotation_requires_container_superuser_and_does_not_accept_keys(monkeypatch):
    """
    普通用户不能轮换整个容器的凭据，管理员请求不接受自选用户、密钥或 URL。
    """
    calls = []

    class Chain:
        """
        记录已通过真实角色检查的专用管理调用。
        """

        def manage_channel(self, **params):
            """
            只返回轮换结果，不向客户端返回凭据。
            """
            calls.append(params)
            return {"success": True, "data": {"rotated": True}}

    monkeypatch.setattr(notification, "NotificationChain", Chain)
    user = SimpleNamespace(notification_identity="a" * 32, is_superuser=False)
    app = FastAPI()
    app.include_router(notification.router, prefix="/notification")
    app.dependency_overrides[get_current_active_user] = lambda: user
    body = {"action": "rotate", "source": "automatic"}
    with TestClient(app) as client:
        assert client.post('/notification/app/instance', json=body).status_code == 403
        assert calls == []
        user.is_superuser = True
        assert client.post('/notification/app/instance', json={**body, "key": "private"}).status_code == 422
        response = client.post('/notification/app/instance', json=body)
        assert response.json()['data'] == {"rotated": True}
    assert calls == [{"channel": "AppPush", "action": "rotate_instance", "source": "automatic",
                      "owner": "a" * 32, "administrative": True}]
