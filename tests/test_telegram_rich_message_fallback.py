"""Telegram Rich Message 失败降级与重试策略测试。"""

from unittest.mock import MagicMock, patch

import pytest

from app.modules.telegram.botapi import TelegramApiError, TelegramNetworkError
from app.modules.telegram.telegram import (
    NonRetryableTelegramError,
    RetryException,
    Telegram,
    _is_non_retryable_api_error,
)


def _make_client() -> Telegram:
    """构造一个不启动轮询线程的 Telegram 客户端。"""
    with patch("app.modules.telegram.telegram.TelegramBotApi"), patch(
            "app.modules.telegram.telegram.threading.Thread"
    ):
        client = Telegram(TELEGRAM_TOKEN="token", TELEGRAM_CHAT_ID="chat")
    client._bot = MagicMock()
    return client


class TestNonRetryableApiError:
    """400 属于确定性拒绝，重试无意义。"""

    def test_400_is_non_retryable(self):
        """明确的 400 拒绝无需退避重试。"""
        err = TelegramApiError("sendRichMessage", "Bad Request: ...", 400)
        assert _is_non_retryable_api_error(err) is True

    def test_500_is_retryable(self):
        """服务端临时失败保留重试能力。"""
        err = TelegramApiError("sendRichMessage", "Internal Server Error", 500)
        assert _is_non_retryable_api_error(err) is False

    def test_network_error_is_retryable(self):
        """网络临时异常保留重试能力。"""
        assert _is_non_retryable_api_error(TelegramNetworkError("m", "timeout")) is False

    def test_unrelated_error_is_retryable(self):
        """非 API 异常不被误判为确定性拒绝。"""
        assert _is_non_retryable_api_error(ValueError("boom")) is False


class TestSendRichMessageRetry:
    """Rich Message 发送失败时的重试与异常类型。"""

    def test_400_skips_backoff_and_raises_immediately(self):
        """400 不应进入 retry 的退避等待。"""
        client = _make_client()
        client._api.send_rich_message.side_effect = TelegramApiError(
            "sendRichMessage", "Bad Request: can't parse entities", 400
        )

        with patch("time.sleep") as sleep_mock:
            with pytest.raises(NonRetryableTelegramError):
                client._Telegram__send_rich_message(
                    chat_id="chat", rich_message="# 结果"
                )

        sleep_mock.assert_not_called()
        assert client._api.send_rich_message.call_count == 1

    def test_500_retries(self):
        """5xx 仍按原逻辑重试。"""
        client = _make_client()
        client._api.send_rich_message.side_effect = TelegramApiError(
            "sendRichMessage", "Internal Server Error", 500
        )

        with patch("time.sleep"), pytest.raises(RetryException):
            client._Telegram__send_rich_message(
                chat_id="chat", rich_message="# 结果"
            )

        assert client._api.send_rich_message.call_count == 3

    def test_error_message_keeps_telegram_description(self):
        """异常文本必须保留 Telegram 原始描述，否则无法定位失败原因。"""
        client = _make_client()
        client._api.send_rich_message.side_effect = TelegramApiError(
            "sendRichMessage", "Bad Request: can't parse entities", 400
        )

        with patch("time.sleep"), pytest.raises(NonRetryableTelegramError) as excinfo:
            client._Telegram__send_rich_message(
                chat_id="chat", rich_message="# 结果"
            )

        message = str(excinfo.value)
        assert "can't parse entities" in message
        assert "400" in message


class TestSendMsgFallsBackToPlainText:
    """Rich Message 发送失败时降级为纯文本，通知不丢失。"""

    def test_falls_back_when_rich_message_fails(self):
        client = _make_client()
        client._api.send_rich_message.side_effect = TelegramApiError(
            "sendRichMessage", "Bad Request", 400
        )
        client._api.send_message.return_value = {"message_id": 99, "chat": {"id": "chat"}}

        with patch("time.sleep"):
            result = client.send_msg(
                title="结果", text="正文", rich_message="# 结果"
            )

        assert result["success"] is True
        assert result["message_id"] == 99
        # parse_mode 必须显式为空字符串：None 会回落到客户端默认的 MarkdownV2，
        # 那正是 Rich Message 已经失败过的解析路径。
        assert client._api.send_message.call_args.kwargs["text"] == "# 结果"
        assert client._api.send_message.call_args.kwargs["parse_mode"] == ""

    def test_no_fallback_when_rich_message_succeeds(self):
        client = _make_client()
        client._api.send_rich_message.return_value = {
            "message_id": 1, "chat": {"id": "chat"}
        }

        result = client.send_msg(title="结果", text="正文", rich_message="# 结果")

        assert result["success"] is True
        client._api.send_message.assert_not_called()

    def test_reports_failure_when_both_paths_fail(self):
        client = _make_client()
        client._api.send_rich_message.side_effect = TelegramApiError(
            "sendRichMessage", "Bad Request", 400
        )
        client._api.send_message.side_effect = TelegramApiError(
            "sendMessage", "Bad Request", 400
        )

        with patch("time.sleep"):
            result = client.send_msg(title="结果", text="正文", rich_message="# 结果")

        assert result["success"] is False


class TestEditMsgFallsBackToPlainText:
    """编辑路径同样需要降级，避免回复停留在旧内容。"""

    def test_falls_back_when_rich_edit_fails(self):
        client = _make_client()
        client._api.edit_message_text.side_effect = [
            TelegramApiError("editMessageText", "Bad Request", 400),
            {"message_id": 5, "chat": {"id": "chat"}},
        ]

        result = client.edit_msg(
            chat_id="chat", message_id=5, text="正文", rich_message="# 结果"
        )

        assert result is True
        assert client._api.edit_message_text.call_count == 2
        # 第二次调用以纯文本覆盖原消息
        assert client._api.edit_message_text.call_args.kwargs["text"] == "# 结果"

    def test_no_fallback_when_rich_edit_succeeds(self):
        client = _make_client()
        client._api.edit_message_text.return_value = {
            "message_id": 5, "chat": {"id": "chat"}
        }

        result = client.edit_msg(
            chat_id="chat", message_id=5, text="正文", rich_message="# 结果"
        )

        assert result is True
        assert client._api.edit_message_text.call_count == 1


class TestFallbackDisablesMarkdownV2:
    """回归防护：降级路径不得回落到客户端默认的 MarkdownV2。

    ``TelegramBotApi._parse_mode`` 在参数为 ``None`` 时使用 ``_default_parse_mode``，
    而该值初始化为 MarkdownV2。若降级调用漏传 parse_mode，纯文本会被再次按
    MarkdownV2 解析，重演 Rich Message 已经失败过的报错，降级等于无效。
    """

    def test_send_fallback_passes_empty_parse_mode(self):
        client = _make_client()
        client._api.send_rich_message.side_effect = TelegramApiError(
            "sendRichMessage", "Bad Request", 400
        )
        client._api.send_message.return_value = {"message_id": 1, "chat": {"id": "chat"}}

        with patch("time.sleep"):
            client.send_msg(title="结果", text="正文", rich_message="# 结果")

        kwargs = client._api.send_message.call_args.kwargs
        assert kwargs["parse_mode"] == "", "降级发送必须显式禁用 MarkdownV2"

    def test_edit_fallback_passes_empty_parse_mode(self):
        client = _make_client()
        client._api.edit_message_text.side_effect = [
            TelegramApiError("editMessageText", "Bad Request", 400),
            {"message_id": 5, "chat": {"id": "chat"}},
        ]

        client.edit_msg(chat_id="chat", message_id=5, text="正文", rich_message="# 结果")

        kwargs = client._api.edit_message_text.call_args.kwargs
        assert kwargs["parse_mode"] == "", "降级编辑必须显式禁用 MarkdownV2"
