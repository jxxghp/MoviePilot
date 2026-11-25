"""
宿主 SDK 单路径入站、生命周期封口与显式插件 HTTP 兼容契约。
"""

from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest

from app.application.messaging import ingress
from app.modules.discord import discord as discord_module
from app.modules.feishu import feishu as feishu_module
from app.modules.qqbot import qqbot as qqbot_module
from app.modules.slack import slack as slack_module
from app.modules.telegram import telegram as telegram_module
from app.modules.wechatclawbot import wechatclawbot as clawbot_module

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeMessageIngressPort:
    """
    返回固定状态码并记录同步、异步回环投递。
    """

    def __init__(self, status_code=200) -> None:
        """
        保存固定状态码。
        """
        self.status_code = status_code
        self.sync_calls = []
        self.async_calls = []

    def post(self, url, payload, *, headers, timeout):
        """
        记录同步投递并返回固定状态码。
        """
        self.sync_calls.append((url, payload, headers, timeout))
        return self.status_code

    async def async_post(self, url, payload, *, headers, timeout):
        """
        记录异步投递并返回固定状态码。
        """
        self.async_calls.append((url, payload, headers, timeout))
        return self.status_code


@pytest.fixture(autouse=True)
def restore_message_ingress_port():
    """
    隔离消息回环端口并在用例结束后恢复。
    """
    previous = ingress.configure_message_ingress_port(_FakeMessageIngressPort())
    yield
    ingress.reset_message_ingress_port(previous)


def _patch_ingress_settings(monkeypatch, **values):
    """
    通过只读运行配置端口注入回环入口测试设置。
    """
    settings = SimpleNamespace(**values)
    monkeypatch.setattr(
        ingress,
        "get_runtime_setting",
        lambda key: getattr(settings, key),
    )


def test_forward_message_to_host_keeps_token_out_of_url(monkeypatch):
    """
    统一入口仅编码来源参数，并通过请求头传递凭据和 payload 副本。
    """
    port = _FakeMessageIngressPort()
    ingress.configure_message_ingress_port(port)
    _patch_ingress_settings(monkeypatch, PORT=3000, API_TOKEN="token value")
    payload = {"text": "hello"}

    assert ingress.forward_message_to_host(
        payload,
        "channel & one",
        timeout=9,
    ) is True

    url, forwarded, headers, timeout = port.sync_calls[0]
    assert urlparse(url).path == "/api/v1/message"
    assert parse_qs(urlparse(url).query) == {
        "source": ["channel & one"],
    }
    assert headers == {"X-API-KEY": "token value"}
    assert forwarded == {"text": "hello"}
    assert forwarded is not payload
    assert timeout == 9


@pytest.mark.parametrize("status_code", [None, 400, 500])
def test_forward_message_to_host_rejects_unconfirmed_response(
    monkeypatch,
    status_code,
):
    """
    本地入口无响应或返回错误状态时不得宣称渠道消息已接收。
    """
    ingress.configure_message_ingress_port(_FakeMessageIngressPort(status_code))
    _patch_ingress_settings(monkeypatch, PORT=3000, API_TOKEN="token")

    assert ingress.forward_message_to_host({}, "channel") is False


def test_forward_message_to_host_fails_closed_when_port_is_unconfigured(monkeypatch):
    """
    组合根未装配时公开入口稳定拒绝，不得懒加载具体 Adapter。
    """
    ingress.reset_message_ingress_port()
    _patch_ingress_settings(monkeypatch, PORT=3000, API_TOKEN="token")

    assert ingress.forward_message_to_host({}, "channel") is False


@pytest.mark.asyncio
async def test_async_forward_message_to_host_uses_same_contract(monkeypatch):
    """
    自有事件循环的渠道必须复用同一 URL、payload 和确认规则。
    """
    port = _FakeMessageIngressPort()
    ingress.configure_message_ingress_port(port)
    _patch_ingress_settings(monkeypatch, PORT=3000, API_TOKEN="token value")

    assert await ingress.async_forward_message_to_host(
        {"text": "hello"},
        "discord & one",
        timeout=10,
    ) is True

    url, payload, headers, timeout = port.async_calls[0]
    assert parse_qs(urlparse(url).query) == {
        "source": ["discord & one"],
    }
    assert headers == {"X-API-KEY": "token value"}
    assert payload == {"text": "hello"}
    assert timeout == 10


def test_startup_message_ingress_adapter_closes_sync_response(monkeypatch):
    """
    生产同步注入适配层必须负责关闭 HTTP 响应。
    """
    from app.startup.composition import network as network_composition

    response = SimpleNamespace(status_code=202, close=MagicMock())
    request = MagicMock()
    request.post_res.return_value = response
    monkeypatch.setattr(
        network_composition,
        "RequestUtils",
        MagicMock(return_value=request),
    )

    status_code = network_composition._MessageIngressAdapter().post(
        "http://127.0.0.1/message",
        {"text": "hello"},
        headers={"X-API-KEY": "secret"},
        timeout=9,
    )

    assert status_code == 202
    network_composition.RequestUtils.assert_called_once_with(
        timeout=9,
        headers={"X-API-KEY": "secret"},
    )
    response.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_startup_message_ingress_adapter_closes_async_response(monkeypatch):
    """
    生产异步注入适配层必须负责关闭 HTTP 响应。
    """
    from app.startup.composition import network as network_composition

    response = SimpleNamespace(status_code=202, aclose=AsyncMock())
    request = MagicMock()
    request.post_res = AsyncMock(return_value=response)
    monkeypatch.setattr(
        network_composition,
        "AsyncRequestUtils",
        MagicMock(return_value=request),
    )

    status_code = await network_composition._MessageIngressAdapter().async_post(
        "http://127.0.0.1/message",
        {"text": "hello"},
        headers={"X-API-KEY": "secret"},
        timeout=10,
    )

    assert status_code == 202
    network_composition.AsyncRequestUtils.assert_called_once_with(
        timeout=10,
        headers={"X-API-KEY": "secret"},
    )
    response.aclose.assert_awaited_once_with()


def test_submit_message_to_host_copies_payload_and_reports_admission_failure():
    """
    异步渠道提交时冻结顶层 payload，执行器拒绝任务则返回 False。
    """
    submitted = []

    def submit(function, *args, **kwargs):
        """
        记录受管执行器收到的函数和参数。
        """
        submitted.append((function, args, kwargs))

    payload = {"text": "before"}
    assert ingress.submit_message_to_host(
        payload,
        "channel",
        submit=submit,
    ) is True
    payload["text"] = "after"

    assert submitted[0][0] is ingress.forward_message_to_host
    assert submitted[0][1] == ({"text": "before"}, "channel")
    assert submitted[0][2] == {"timeout": 15}

    def reject(*_args, **_kwargs):
        """
        模拟生命周期关闭后的执行器拒绝新任务。
        """
        raise RuntimeError("executor closed")

    assert ingress.submit_message_to_host({}, "channel", submit=reject) is False


@pytest.mark.parametrize(
    "module,client_type,source_attr",
    [
        (feishu_module, feishu_module.Feishu, "_name"),
        (qqbot_module, qqbot_module.QQBot, "_config_name"),
        (telegram_module, telegram_module.Telegram, "_config_name"),
        (slack_module, slack_module.Slack, "_config_name"),
        (clawbot_module, clawbot_module.WechatClawBot, "_config_name"),
    ],
)
@pytest.mark.parametrize("accepted", [True, False, None])
def test_sdk_clients_use_only_the_bound_ingress(monkeypatch, module, client_type, source_attr, accepted):
    """
    已装配、拒绝及身份缺失均只走绑定提交，不调用已保留的插件 HTTP 兼容路径。
    """
    executor = SimpleNamespace(submit=Mock())
    monkeypatch.setattr(module, "ThreadHelper", lambda: executor)
    client = object.__new__(client_type)
    setattr(client, source_attr, "channel-main")
    bound = Mock(return_value=accepted) if accepted is not None else None
    client._message_ingress = bound
    if module is clawbot_module:
        client._stop_event = Event()
    payload = {"text": "hello"}
    extra = {"timeout": 60} if module is slack_module else {}
    port = ingress._message_ingress_snapshot()
    assert client._forward_to_message_chain(payload, **extra) is (accepted is True)
    if bound is not None:
        bound.assert_called_once_with(payload, submit=executor.submit)
    assert port.sync_calls == [] and port.async_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False, None])
async def test_discord_uses_only_the_bound_ingress(monkeypatch, accepted):
    """
    Discord 自有事件循环也只提交绑定任务，失败不能切换为 HTTP。
    """
    executor = SimpleNamespace(submit=Mock())
    monkeypatch.setattr(discord_module, "ThreadHelper", lambda: executor)
    client = object.__new__(discord_module.Discord)
    client._config_name = "discord-main"
    bound = Mock(return_value=accepted) if accepted is not None else None
    client._message_ingress = bound
    assert await client._post_to_ds({"text": "hello"}) is (accepted is True)
    if bound is not None:
        bound.assert_called_once_with({"text": "hello"}, submit=executor.submit)
    port = ingress._message_ingress_snapshot()
    assert port.sync_calls == [] and port.async_calls == []


def test_message_modules_cannot_reimplement_loopback_endpoint():
    """
    消息模块不得重新拼接宿主 URL，新增渠道必须复用统一 ingress。
    """
    violations = []
    for path in (PROJECT_ROOT / "app" / "modules").rglob("*.py"):
        if "/api/v1/message" in path.read_text(encoding="utf-8-sig"):
            violations.append(path.relative_to(PROJECT_ROOT).as_posix())

    assert violations == []


@pytest.mark.parametrize("outcome", ["accepted", "rejected", "stopped", "invalid_payload"])
def test_platform_payload_submission_preserves_queue_acknowledgement(monkeypatch, outcome):
    """
    已认证平台载荷仅在执行器接受后确认；停机或无法序列化时拒绝，保留平台重试机会。
    """
    import json

    from app.schemas.types import NotificationChannel

    receiver = Mock()
    submit = Mock(return_value=outcome == "accepted")
    if outcome == "stopped":
        submit.side_effect = RuntimeError("executor stopped")
    monkeypatch.setattr(ingress, "_platform_payload_receiver", receiver)
    monkeypatch.setattr("app.application.security.authorization.get_channel_authorization", lambda: object())
    payload = {"text": object() if outcome == "invalid_payload" else "hello"}

    assert ingress.submit_platform_payload(
        payload, "fixture", NotificationChannel.Telegram, "connection", submit=submit,
        provider="telegram", identity_namespace="namespace", parser_owner="TelegramModule", policy_revision=1,
    ) is (outcome == "accepted")
    receiver.assert_not_called()
    if outcome == "invalid_payload":
        submit.assert_not_called()
    else:
        call = submit.call_args.args
        assert call[0] is ingress._deliver_platform_payload and call[1] is receiver
        assert json.loads(call[2]) == payload
        assert call[3:] == ("fixture", NotificationChannel.Telegram, "connection", "telegram", "namespace", "TelegramModule", 1)


def test_verified_event_claim_fails_closed_when_shared_store_is_unavailable(monkeypatch):
    """
    共享认领失败不得回退进程缓存，防止多 worker 重复执行已认证的业务事件。
    """
    from app.domain.admission import AdmissionProof

    monkeypatch.setattr(ingress, "time", SimpleNamespace(time=lambda: 100))
    claim = Mock(side_effect=RuntimeError("shared store unavailable"))
    monkeypatch.setattr(ingress, "_verified_event_claim", claim)
    proof = AdmissionProof("connection", 1, "member", "group", 200)

    assert not ingress.claim_verified_event(proof, "event")
    assert not ingress.claim_verified_event(proof, "event")
    assert claim.call_count == 2
    assert claim.call_args.args[1:] == (100, 220)
