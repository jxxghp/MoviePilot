"""
Telegram Rich Message 失败降级与重试策略测试。
"""

from unittest.mock import MagicMock, patch

import pytest

from app.modules.telegram.botapi import TelegramApiError, TelegramNetworkError
from app.modules.telegram.telegram import (
    TELEGRAM_EDIT_MIN_INTERVAL_SECONDS,
    NonRetryableTelegramError,
    RetryException,
    Telegram,
    _is_non_retryable_api_error,
)


def _make_client() -> Telegram:
    """
    构造一个不启动轮询线程的 Telegram 客户端。
    """
    with patch("app.modules.telegram.telegram.TelegramBotApi"), patch(
            "app.modules.telegram.telegram.threading.Thread"
    ):
        client = Telegram(TELEGRAM_TOKEN="token", TELEGRAM_CHAT_ID="chat")
    client._bot = MagicMock()
    return client


class TestNonRetryableApiError:
    """
    400 属于确定性拒绝，重试无意义。
    """

    def test_400_is_non_retryable(self):
        """
        明确的 400 拒绝无需退避重试。
        """
        err = TelegramApiError("sendRichMessage", "Bad Request: ...", 400)
        assert _is_non_retryable_api_error(err) is True

    def test_500_is_retryable(self):
        """
        服务端临时失败保留重试能力。
        """
        err = TelegramApiError("sendRichMessage", "Internal Server Error", 500)
        assert _is_non_retryable_api_error(err) is False

    def test_network_error_is_retryable(self):
        """
        网络临时异常保留重试能力。
        """
        assert _is_non_retryable_api_error(TelegramNetworkError("m", "timeout")) is False

    def test_429_is_non_retryable_until_cooldown(self):
        """
        429 不能在通用重试器内立即重放。
        """
        err = TelegramApiError(
            "editMessageText", "Too Many Requests: retry after 17", 429,
            {"retry_after": 17},
        )
        assert _is_non_retryable_api_error(err) is True

    def test_unrelated_error_is_retryable(self):
        """
        非 API 异常不被误判为确定性拒绝。
        """
        assert _is_non_retryable_api_error(ValueError("boom")) is False


class TestSendRichMessageRetry:
    """
    Rich Message 发送失败时的重试与异常类型。
    """

    def test_400_skips_backoff_and_raises_immediately(self):
        """
        400 不应进入 retry 的退避等待。
        """
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
        """
        5xx 仍按原逻辑重试。
        """
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
        """
        异常文本必须保留 Telegram 原始描述，否则无法定位失败原因。
        """
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
    """
    Rich Message 发送失败时降级为纯文本，通知不丢失。
    """

    def test_falls_back_when_rich_message_fails(self):
        """
        富文本发送失败后降级为纯文本，成功结果保留降级消息标识。
        """
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
        """
        富文本发送成功时不重复发送纯文本消息。
        """
        client = _make_client()
        client._api.send_rich_message.return_value = {
            "message_id": 1, "chat": {"id": "chat"}
        }

        result = client.send_msg(title="结果", text="正文", rich_message="# 结果")

        assert result["success"] is True
        client._api.send_message.assert_not_called()

    def test_reports_failure_when_both_paths_fail(self):
        """
        富文本和纯文本发送均失败时，向调用方返回失败。
        """
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

    def test_send_429_skips_fallback_and_retry(self):
        """
        Rich 发送收到 429 时应立即返回失败，不再重试或发送纯文本。
        """
        client = _make_client()
        client._api.send_rich_message.side_effect = TelegramApiError(
            "sendRichMessage", "Too Many Requests: retry after 17", 429,
            {"retry_after": 17},
        )

        with patch("time.sleep") as sleep_mock:
            result = client.send_msg(title="结果", text="正文", rich_message="# 结果")

        assert result["success"] is False
        client._api.send_rich_message.assert_called_once()
        client._api.send_message.assert_not_called()
        sleep_mock.assert_not_called()

    def test_plain_send_429_skips_retry(self):
        """
        普通发送收到 429 时也应隔离输出失败，不能阻塞 Agent。
        """
        client = _make_client()
        client._api.send_message.side_effect = TelegramApiError(
            "sendMessage", "Too Many Requests: retry after 17", 429,
            {"retry_after": 17},
        )

        with patch("time.sleep") as sleep_mock:
            result = client.send_msg(title="结果", text="正文")

        assert result["success"] is False
        client._api.send_message.assert_called_once()
        sleep_mock.assert_not_called()


class TestEditMsgFallsBackToPlainText:
    """
    编辑路径同样需要降级，避免回复停留在旧内容。
    """

    def test_falls_back_when_rich_edit_fails(self):
        """
        富文本编辑失败后以纯文本再次编辑原消息。
        """
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
        """
        富文本编辑成功时不触发额外的纯文本编辑。
        """
        client = _make_client()
        client._api.edit_message_text.return_value = {
            "message_id": 5, "chat": {"id": "chat"}
        }

        result = client.edit_msg(
            chat_id="chat", message_id=5, text="正文", rich_message="# 结果"
        )

        assert result is True
        assert client._api.edit_message_text.call_count == 1

    def test_429_does_not_try_plain_text_fallback(self):
        """
        Rich 编辑收到 429 时不能再用纯文本编辑放大限流。
        """
        client = _make_client()
        client._api.edit_message_text.side_effect = TelegramApiError(
            "editMessageText", "Too Many Requests: retry after 17", 429,
            {"retry_after": 17},
        )

        result = client.edit_msg(
            chat_id="chat", message_id=5, text="正文", rich_message="# 结果"
        )

        assert result is False
        client._api.edit_message_text.assert_called_once()

    @pytest.mark.parametrize("wait_for_interval", [False, True])
    def test_429_cooldown_skips_follow_up_edits(self, monkeypatch, wait_for_interval):
        """
        同一聊天的冷却期内后续编辑不应再触碰 Bot API。
        """
        client = _make_client()
        now = [100.0]
        monkeypatch.setattr("app.modules.telegram.telegram.time.monotonic", lambda: now[0])
        client._api.edit_message_text.side_effect = TelegramApiError(
            "editMessageText", "Too Many Requests: retry after 17", 429,
            {"retry_after": 17},
        )

        assert client.edit_msg(chat_id="chat", message_id=5, text="正文", rich_message="# 结果") is False
        now[0] += 10
        with patch("app.modules.telegram.telegram.time.sleep") as sleep_mock:
            assert client.edit_msg(
                chat_id="chat", message_id=5, text="更新", rich_message="# 更新",
                wait_for_interval=wait_for_interval,
            ) is False
        sleep_mock.assert_not_called()
        assert client._api.edit_message_text.call_count == 1
        now[0] += 8
        client._api.edit_message_text.side_effect = None
        client._api.edit_message_text.return_value = {"message_id": 5}
        assert client.edit_msg(chat_id="chat", message_id=5, text="更新", rich_message="# 更新") is True
        assert client._api.edit_message_text.call_count == 2

    @pytest.mark.parametrize("interruption", ["429", "edit"])
    def test_final_edit_rechecks_cooldown_and_interval_after_one_wait(self, monkeypatch, interruption):
        """
        等待期间出现聊天冷却或同消息并发编辑时，不追加请求或无限延长等待。
        """
        client = _make_client()
        now = [100.0]
        waits = []
        monkeypatch.setattr("app.modules.telegram.telegram.time.monotonic", lambda: now[0])
        client._api.edit_message_text.return_value = {"message_id": 5}
        assert client.edit_msg(chat_id="chat", message_id=5, text="首帧", rich_message="# 首帧") is True

        def interrupt_wait(seconds):
            """
            在虚拟等待结束时模拟其他请求带来的状态变化。

            :param seconds: 本次等待秒数
            """
            waits.append(seconds)
            now[0] += seconds
            if interruption == "429":
                client._mark_rate_limit(
                    "chat", TelegramApiError("editMessageText", "Too Many Requests", 429, {"retry_after": 17}),
                )
            else:
                assert client.edit_msg(chat_id="chat", message_id=5, text="并发编辑") is True

        monkeypatch.setattr("app.modules.telegram.telegram.time.sleep", interrupt_wait)
        assert client.edit_msg(
            chat_id="chat", message_id=5, text="完整正文", rich_message="# 完整正文",
            wait_for_interval=True,
        ) is False
        assert waits == [TELEGRAM_EDIT_MIN_INTERVAL_SECONDS]
        assert client._api.edit_message_text.call_count == (1 if interruption == "429" else 2)

    def test_successful_edits_are_throttled_and_latest_text_wins(self, monkeypatch):
        """
        最小编辑间隔内只保留最新一次刷新，解除后发送当前文本。
        """
        client = _make_client()
        now = [100.0]
        monkeypatch.setattr("app.modules.telegram.telegram.time.monotonic", lambda: now[0])
        client._api.edit_message_text.return_value = {"message_id": 5}

        assert client.edit_msg(chat_id="chat", message_id=5, text="首帧", rich_message="# 首帧") is True
        now[0] += TELEGRAM_EDIT_MIN_INTERVAL_SECONDS / 2
        assert client.edit_msg(chat_id="chat", message_id=5, text="最新", rich_message="# 最新") is False
        assert client._api.edit_message_text.call_count == 1
        now[0] += TELEGRAM_EDIT_MIN_INTERVAL_SECONDS
        assert client.edit_msg(chat_id="chat", message_id=5, text="最新", rich_message="# 最新") is True
        assert client._api.edit_message_text.call_count == 2

    def test_final_edit_waits_for_message_interval(self, monkeypatch):
        """
        收尾编辑应等待消息级间隔，确保完整正文覆盖原消息。
        """
        client = _make_client()
        now = [100.0]
        monkeypatch.setattr("app.modules.telegram.telegram.time.monotonic", lambda: now[0])

        def advance(seconds):
            """
            用虚拟时钟推进消息级编辑间隔。

            :param seconds: 等待秒数
            """
            now[0] += seconds

        monkeypatch.setattr("app.modules.telegram.telegram.time.sleep", advance)
        client._api.edit_message_text.return_value = {"message_id": 5}

        assert client.edit_msg(chat_id="chat", message_id=5, text="首帧", rich_message="# 首帧") is True
        assert client.edit_msg(
            chat_id="chat", message_id=5, text="完整正文", rich_message="# 完整正文",
            wait_for_interval=True,
        ) is True
        assert client._api.edit_message_text.call_count == 2
        assert now[0] == 100.0 + TELEGRAM_EDIT_MIN_INTERVAL_SECONDS
        assert "完整正文" in client._api.edit_message_text.call_args.kwargs["rich_message"]["html"]


class TestFallbackDisablesMarkdownV2:
    """
    回归防护：降级路径不得回落到客户端默认的 MarkdownV2。

    ``TelegramBotApi._parse_mode`` 在参数为 ``None`` 时使用 ``_default_parse_mode``，
    而该值初始化为 MarkdownV2。若降级调用漏传 parse_mode，纯文本会被再次按
    MarkdownV2 解析，重演 Rich Message 已经失败过的报错，降级等于无效。
    """

    def test_send_fallback_passes_empty_parse_mode(self):
        """
        降级发送显式清空解析模式，避免正文再次被 MarkdownV2 拒绝。
        """
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
        """
        降级编辑显式清空解析模式，避免重复触发富文本格式错误。
        """
        client = _make_client()
        client._api.edit_message_text.side_effect = [
            TelegramApiError("editMessageText", "Bad Request", 400),
            {"message_id": 5, "chat": {"id": "chat"}},
        ]

        client.edit_msg(chat_id="chat", message_id=5, text="正文", rich_message="# 结果")

        kwargs = client._api.edit_message_text.call_args.kwargs
        assert kwargs["parse_mode"] == "", "降级编辑必须显式禁用 MarkdownV2"
