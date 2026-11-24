"""
渠道身份核验与连接缓存对账的启动装配回归，平台 API 与后台执行边界均隔离。
"""

from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.telegram.channel import TelegramPlatform
from app.schemas.channel import ChannelConnection
from app.startup.initializers import modules


def _connection(provider="telegram", **config):
    """
    构造仅含合成凭据的连接，不访问任何真实平台。
    """
    return ChannelConnection(
        id="fixture", name="fixture", provider=provider, mode="standard",
        identity_namespace="fixture-identity", enabled=True, revision=1, provider_config=config,
    )


@pytest.mark.parametrize(("response", "expected"), [
    ({"id": 123, "is_bot": True}, "123"),
    ({"id": True, "is_bot": True}, None),
    ({"id": 0, "is_bot": True}, None),
    ({"id": "123", "is_bot": True}, None),
    ({"id": 123, "is_bot": False}, None),
    (ValueError("invalid platform response"), None),
])
def test_telegram_identity_requires_verified_bot_and_always_closes_client(monkeypatch, response, expected):
    """
    仅接受平台确认的正整数机器人身份，错误响应及异常均关闭诊断客户端。
    """
    client = Mock()
    if isinstance(response, Exception):
        client.get_me.side_effect = response
    else:
        client.get_me.return_value = response
    factory = Mock(return_value=client)
    monkeypatch.setattr("app.modules.telegram.botapi.TelegramBotApi", factory)
    monkeypatch.setattr("app.runtime.settings.get_runtime_setting", lambda _key: None)

    assert TelegramPlatform().probe_identity(_connection(TELEGRAM_TOKEN="123:fixture_token")) == expected
    client.get_me.assert_called_once_with()
    client.close.assert_called_once_with()
    factory.assert_called_once_with("123:fixture_token", proxy=None)


@pytest.mark.parametrize("token", [None, 7, "invalid-token"])
def test_invalid_telegram_credentials_do_not_create_platform_client(monkeypatch, token):
    """
    格式无效或缺失的凭据在创建客户端前拒绝，避免无意义的外部请求。
    """
    factory = Mock()
    monkeypatch.setattr("app.modules.telegram.botapi.TelegramBotApi", factory)

    assert TelegramPlatform().probe_identity(_connection(TELEGRAM_TOKEN=token)) is None
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_clawbot_cache_reconciliation_surfaces_failure_after_rename(monkeypatch, success):
    """
    连接重命名必须完成登录缓存对账；失败保留诊断，不伪报配置已同步。
    """
    before = _connection("wechatclawbot")
    after = before.model_copy(update={"name": "renamed"})
    cache = Mock(return_value={"success": success})
    monkeypatch.setattr("app.modules.wechatclawbot.wechatclawbot.WechatClawBot.reconcile_cached_states", cache)

    async def execute(operation, *args):
        """
        在测试线程执行模块 hook，保留失败传播并隔离真实缓存。
        """
        return operation(*args)

    worker = AsyncMock(side_effect=execute)
    monkeypatch.setattr(modules, "run_in_threadpool_to_completion", worker)

    if success:
        await modules._reconcile_channel_connections((before,), (after,))
    else:
        with pytest.raises(RuntimeError, match="登录缓存未同步"):
            await modules._reconcile_channel_connections((before,), (after,))
    worker.assert_awaited_once()
    assert cache.call_args.args == (
        [{"id": "fixture", "name": "fixture", "type": "wechatclawbot"}],
        [{"id": "fixture", "name": "renamed", "type": "wechatclawbot"}],
    )


@pytest.mark.asyncio
async def test_unchanged_clawbot_cache_does_not_start_reconciliation(monkeypatch):
    """
    缓存地址未变化时不启动后台对账，也不把其他平台连接传入 ClawBot 缓存。
    """
    connection = _connection("wechatclawbot")
    worker = AsyncMock()
    monkeypatch.setattr(modules, "run_in_threadpool_to_completion", worker)

    await modules._reconcile_channel_connections((connection,), (connection, _connection()))
    worker.assert_not_awaited()
