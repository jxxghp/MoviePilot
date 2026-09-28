"""GitHub Token 设备授权应用服务的协议边界测试。"""

from dataclasses import dataclass
from threading import RLock
from unittest.mock import patch

import pytest

from app.agent.llm import auth as llm_auth
from app.agent.llm.auth import _ProviderAuth
from app.agent.llm.catalog import ProviderAuthMethod, ProviderSpec
from app.agent.llm.session import _ProviderSession
from app.application.github_auth import GithubAuthService
from app.domain.github import GithubDeviceCode, GithubTokenExchange, GithubUser
from app.schemas.types import SystemConfigKey


class _CopilotAuthHarness(_ProviderAuth, _ProviderSession):
    """提供 Copilot 设备授权测试所需的最小 Provider 运行时。"""

    def __init__(self) -> None:
        """初始化临时会话和保存结果。"""
        self._lock = RLock()
        self._pending_sessions = {}
        self._oauth_state_index = {}
        self.saved_auth: dict[str, dict] = {}

    async def _get_provider_async(self, provider_id: str, **kwargs) -> ProviderSpec:
        """返回支持设备授权的 Copilot 测试 Provider。"""
        assert provider_id == "github-copilot"
        return ProviderSpec(
            id=provider_id,
            name="GitHub Copilot",
            runtime="github_copilot",
            oauth_methods=(
                ProviderAuthMethod(
                    id="device_code",
                    type="device",
                    label="GitHub 设备码授权",
                ),
            ),
        )

    async def save_auth(self, provider_id: str, auth_data: dict) -> None:
        """保存测试中的 Provider 授权结果。"""
        self.saved_auth[provider_id] = auth_data


class _FakeCopilotGithubClient:
    """模拟公共 GitHub 传输模块供 Copilot 授权测试复用。"""

    polled = False

    async def request_device_code(
        self,
        client_id: str,
        scope: str = "read:user",
    ) -> GithubDeviceCode:
        """返回公共模块规范化的设备码。"""
        assert client_id == llm_auth.GITHUB_DEVICE_CLIENT_ID
        assert scope == "read:user"
        return GithubDeviceCode(
            "copilot-device",
            "COPI-LOT",
            "https://github.com/login/device",
            900,
            5,
        )

    async def exchange_device_code(
        self,
        client_id: str,
        device_code: str,
    ) -> GithubTokenExchange:
        """返回先 pending 后成功的公共模块交换结果。"""
        assert client_id == llm_auth.GITHUB_DEVICE_CLIENT_ID
        assert device_code == "copilot-device"
        if not type(self).polled:
            type(self).polled = True
            return GithubTokenExchange(error="authorization_pending")
        return GithubTokenExchange(access_token="copilot-access")


class _FakeSettings:
    """提供 GitHub 授权服务所需的最小运行设置端口。"""

    def __init__(self) -> None:
        """初始化空 Token。"""
        self.values = {"GITHUB_TOKEN": None}

    def get(self, key: str, default=None):
        """读取测试运行设置。"""
        return self.values.get(key, default)

    def update(self, key: str, value):
        """记录并更新测试运行设置。"""
        self.values[key] = value
        return True, ""


class _FakeSystemConfig:
    """提供 OAuth 元数据的异步配置端口。"""

    def __init__(self) -> None:
        """初始化空元数据。"""
        self.values = {}

    def get(self, key):
        """读取测试系统配置。"""
        return self.values.get(key)

    async def async_set(self, key, value):
        """保存测试系统配置。"""
        self.values[key] = value
        return True

    async def async_delete(self, key):
        """删除测试系统配置。"""
        self.values.pop(key, None)


@dataclass
class _FakeTransport:
    """模拟 GitHub Device Flow、用户校验和刷新响应。"""

    exchanges: list[GithubTokenExchange]
    refresh_exchange: GithubTokenExchange | None = None

    async def request_device_code(
        self,
        client_id: str,
        scope: str = "read:user",
    ) -> GithubDeviceCode:
        """返回固定的设备码。"""
        assert client_id == "client-id"
        assert scope == "read:user repo workflow"
        return GithubDeviceCode("device", "ABCD-EFGH", "https://github.com/login/device", 900, 5)

    async def exchange_device_code(self, client_id: str, device_code: str) -> GithubTokenExchange:
        """按测试顺序返回设备授权响应。"""
        assert client_id == "client-id"
        assert device_code == "device"
        return self.exchanges.pop(0)

    async def refresh_access_token(self, client_id: str, refresh_token: str) -> GithubTokenExchange:
        """返回固定的刷新响应。"""
        assert client_id == "client-id"
        assert refresh_token == "refresh-old"
        assert self.refresh_exchange is not None
        return self.refresh_exchange

    async def get_user(self, access_token: str) -> GithubUser:
        """返回 Token 对应的测试用户。"""
        assert access_token
        return GithubUser(login="moviepilot")


def _build_service(clock) -> tuple[GithubAuthService, _FakeSettings, _FakeSystemConfig]:
    """构造不访问真实网络的 GitHub 授权服务。"""
    settings = _FakeSettings()
    system_config = _FakeSystemConfig()
    transport = _FakeTransport(
        exchanges=[
            GithubTokenExchange(error="authorization_pending"),
            GithubTokenExchange(
                access_token="gho_access_token",
                refresh_token="refresh-old",
                expires_in=3600,
                scope="read:user repo workflow",
            ),
        ]
    )
    service = GithubAuthService(
        settings=settings,  # type: ignore[arg-type]
        system_config=system_config,  # type: ignore[arg-type]
        transport=transport,
        client_id="client-id",
        clock=clock,
    )
    return service, settings, system_config


@pytest.mark.asyncio
async def test_device_flow_persists_only_server_side_token_state() -> None:
    """设备授权成功后前端状态只能看到脱敏摘要，刷新令牌不能进入响应。"""
    now = [1000.0]
    service, settings, system_config = _build_service(lambda: now[0])

    started = await service.start_device_auth()
    pending = await service.poll_device_auth(started.session_id)
    assert pending.state == "pending"

    now[0] = 1005.0
    authorized = await service.poll_device_auth(started.session_id)

    assert authorized.state == "authorized"
    assert settings.values["GITHUB_TOKEN"] == "gho_access_token"
    assert authorized.status is not None
    assert authorized.status.masked_token == "gho_…oken"
    assert authorized.status.source == "oauth"
    metadata = system_config.values[SystemConfigKey.GithubAuth]
    assert metadata["refresh_token"] == "refresh-old"
    assert "access_token" not in metadata


@pytest.mark.asyncio
async def test_expiring_oauth_token_refreshes_before_status_validation() -> None:
    """OAuth Token 临近过期时，状态查询应刷新访问 Token 并更新元数据。"""
    now = [2000.0]
    service, settings, system_config = _build_service(lambda: now[0])
    settings.values["GITHUB_TOKEN"] = "old-access"
    system_config.values[SystemConfigKey.GithubAuth] = {
        "login": "moviepilot",
        "refresh_token": "refresh-old",
        "expires_at": 2000,
    }
    transport = service._transport
    assert isinstance(transport, _FakeTransport)
    transport.refresh_exchange = GithubTokenExchange(
        access_token="new-access",
        refresh_token="refresh-new",
        expires_in=3600,
    )

    status = await service.status()

    assert settings.values["GITHUB_TOKEN"] == "new-access"
    assert status.valid is True
    assert status.source == "oauth"
    assert system_config.values[SystemConfigKey.GithubAuth]["refresh_token"] == "refresh-new"


@pytest.mark.asyncio
async def test_device_flow_accepts_alternate_expired_error_code() -> None:
    """设备码过期响应的两种 GitHub 错误拼写都应收敛为 expired 状态。"""
    now = [2500.0]
    service, _, _ = _build_service(lambda: now[0])
    transport = service._transport
    assert isinstance(transport, _FakeTransport)
    transport.exchanges = [GithubTokenExchange(error="token_expired")]

    started = await service.start_device_auth()
    result = await service.poll_device_auth(started.session_id)

    assert result.state == "expired"


@pytest.mark.asyncio
async def test_manual_token_clears_oauth_metadata() -> None:
    """手动 PAT 兼容入口应切换来源并清理旧 OAuth 刷新信息。"""
    now = [3000.0]
    service, settings, system_config = _build_service(lambda: now[0])
    system_config.values[SystemConfigKey.GithubAuth] = {"refresh_token": "refresh-old"}

    status = await service.set_manual_token("github_pat_manual")

    assert settings.values["GITHUB_TOKEN"] == "github_pat_manual"
    assert status.source == "manual"
    assert SystemConfigKey.GithubAuth not in system_config.values


@pytest.mark.asyncio
async def test_copilot_reuses_common_github_device_flow_client() -> None:
    """Copilot 设备授权应复用公共 GitHub 模块，而不是重复 HTTP 协议实现。"""
    harness = _CopilotAuthHarness()

    with patch.object(llm_auth, "GithubAuthClient", _FakeCopilotGithubClient):
        started = await harness.start_auth("github-copilot", "device_code")
        pending = await harness.poll_auth_session(started["session_id"])
        authorized = await harness.poll_auth_session(started["session_id"])

    assert started["user_code"] == "COPI-LOT"
    assert pending["status"] == "pending"
    assert authorized["status"] == "authorized"
    assert harness.saved_auth["github-copilot"]["access_token"] == "copilot-access"
