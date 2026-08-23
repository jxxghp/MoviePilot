"""多消息渠道复用统一宿主回环入口的契约测试。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest

from app.application.messaging import ingress
from app.modules.discord import discord as discord_module
from app.modules.feishu import feishu as feishu_module
from app.modules.qqbot import qqbot as qqbot_module
from app.modules.slack import slack as slack_module
from app.modules.telegram import telegram as telegram_module
from app.modules.wechat import wechatbot as wechat_module
from app.modules.wechatclawbot import wechatclawbot as clawbot_module


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_forward_message_to_host_encodes_source_and_closes_response(monkeypatch):
    """统一入口必须安全编码查询参数并释放本地 HTTP 响应。"""
    response = SimpleNamespace(status_code=200, close=MagicMock())
    post_res = MagicMock(return_value=response)
    request = MagicMock()
    request.post_res = post_res
    request_factory = MagicMock(return_value=request)
    monkeypatch.setattr(
        ingress,
        "settings",
        SimpleNamespace(PORT=3000, API_TOKEN="token value"),
    )
    monkeypatch.setattr(ingress, "RequestUtils", request_factory)

    assert ingress.forward_message_to_host(
        {"text": "hello"},
        "channel & one",
        timeout=9,
    ) is True

    request_factory.assert_called_once_with(timeout=9)
    url = post_res.call_args.args[0]
    assert urlparse(url).path == "/api/v1/message"
    assert parse_qs(urlparse(url).query) == {
        "token": ["token value"],
        "source": ["channel & one"],
    }
    assert post_res.call_args.kwargs["json"] == {"text": "hello"}
    response.close.assert_called_once_with()


@pytest.mark.parametrize("status_code", [400, 500])
def test_forward_message_to_host_rejects_unconfirmed_response(
    monkeypatch,
    status_code,
):
    """本地入口无响应或返回错误状态时不得宣称渠道消息已接收。"""
    response = SimpleNamespace(status_code=status_code, close=MagicMock())
    request = MagicMock()
    request.post_res.return_value = response
    monkeypatch.setattr(
        ingress,
        "settings",
        SimpleNamespace(PORT=3000, API_TOKEN="token"),
    )
    monkeypatch.setattr(ingress, "RequestUtils", MagicMock(return_value=request))

    assert ingress.forward_message_to_host({}, "channel") is False
    response.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_async_forward_message_to_host_uses_same_contract(monkeypatch):
    """自有事件循环的渠道必须复用同一 URL、确认规则和异步资源释放。"""
    response = SimpleNamespace(status_code=200, aclose=AsyncMock())
    request = MagicMock()
    request.post_res = AsyncMock(return_value=response)
    request_factory = MagicMock(return_value=request)
    monkeypatch.setattr(
        ingress,
        "settings",
        SimpleNamespace(PORT=3000, API_TOKEN="token value"),
    )
    monkeypatch.setattr(ingress, "AsyncRequestUtils", request_factory)

    assert await ingress.async_forward_message_to_host(
        {"text": "hello"},
        "discord & one",
        timeout=10,
    ) is True

    request_factory.assert_called_once_with(timeout=10)
    url = request.post_res.await_args.args[0]
    assert parse_qs(urlparse(url).query) == {
        "token": ["token value"],
        "source": ["discord & one"],
    }
    response.aclose.assert_awaited_once_with()


def test_submit_message_to_host_copies_payload_and_reports_admission_failure():
    """异步渠道提交时冻结顶层 payload，执行器拒绝任务则返回 False。"""
    submitted = []

    def submit(function, *args, **kwargs):
        """记录受管执行器收到的函数和参数。"""
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
        """模拟生命周期关闭后的执行器拒绝新任务。"""
        raise RuntimeError("executor closed")

    assert ingress.submit_message_to_host({}, "channel", submit=reject) is False


@pytest.mark.parametrize(
    ("module", "client_type", "source_attr"),
    [
        (feishu_module, feishu_module.Feishu, "_name"),
        (qqbot_module, qqbot_module.QQBot, "_config_name"),
        (wechat_module, wechat_module.WeChatBot, "_config_name"),
    ],
)
def test_threaded_channels_submit_through_managed_executor(
    monkeypatch,
    module,
    client_type,
    source_attr,
):
    """原裸线程渠道必须把回环任务交给共享 ThreadHelper。"""
    calls = []
    executor = SimpleNamespace(submit=lambda *_args, **_kwargs: None)

    def submit_message(payload, source, *, submit, timeout=15):
        """记录渠道传给统一提交边界的参数。"""
        calls.append((payload, source, submit, timeout))
        return True

    monkeypatch.setattr(module, "ThreadHelper", lambda: executor)
    monkeypatch.setattr(module, "submit_message_to_host", submit_message)
    client = object.__new__(client_type)
    setattr(client, source_attr, "channel-main")

    assert client._forward_to_message_chain({"text": "hello"}) is True
    assert calls == [({"text": "hello"}, "channel-main", executor.submit, 15)]


@pytest.mark.parametrize(
    ("module", "client_type", "source_attr"),
    [
        (telegram_module, telegram_module.Telegram, "_config_name"),
        (clawbot_module, clawbot_module.WechatClawBot, "_config_name"),
    ],
)
def test_sync_channels_forward_through_shared_ingress(
    monkeypatch,
    module,
    client_type,
    source_attr,
):
    """同步轮询渠道必须复用统一回环请求和确认语义。"""
    forward = MagicMock(return_value=True)
    monkeypatch.setattr(module, "forward_message_to_host", forward)
    client = object.__new__(client_type)
    setattr(client, source_attr, "channel-main")

    assert client._forward_to_message_chain({"text": "hello"}) is True
    forward.assert_called_once_with({"text": "hello"}, "channel-main")


def test_slack_preserves_callback_timeout_through_shared_ingress(monkeypatch):
    """Slack action 的历史长超时必须继续传给统一入口。"""
    forward = MagicMock(return_value=True)
    monkeypatch.setattr(slack_module, "forward_message_to_host", forward)
    client = object.__new__(slack_module.Slack)
    client._config_name = "slack-main"

    assert client._forward_to_message_chain({"action": "run"}, timeout=60) is True
    forward.assert_called_once_with(
        {"action": "run"},
        "slack-main",
        timeout=60,
    )


@pytest.mark.asyncio
async def test_discord_uses_shared_async_ingress(monkeypatch):
    """Discord 自有事件循环不得继续维护独立 httpx 回环实现。"""
    forward = AsyncMock(return_value=True)
    monkeypatch.setattr(discord_module, "async_forward_message_to_host", forward)
    client = object.__new__(discord_module.Discord)
    client._config_name = "discord-main"

    await client._post_to_ds({"text": "hello"})

    forward.assert_awaited_once_with(
        {"text": "hello"},
        "discord-main",
        timeout=10,
    )


def test_message_modules_cannot_reimplement_loopback_endpoint():
    """消息模块不得重新拼接宿主 URL，新增渠道必须复用统一 ingress。"""
    violations = []
    for path in (PROJECT_ROOT / "app" / "modules").rglob("*.py"):
        if "/api/v1/message" in path.read_text(encoding="utf-8-sig"):
            violations.append(path.relative_to(PROJECT_ROOT).as_posix())

    assert violations == []
