"""
Telegram 消息投递、富文本回退、更新转发及输入状态生命周期。
"""

import asyncio
import html as html_utils
import re
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union, cast

from telegramify_markdown import richify, split_rich, standardize, telegramify

try:
    from telegramify_markdown import entities_to_markdownv2
except ImportError:
    entities_to_markdownv2 = None
try:
    from telegramify_markdown.content import ContentTypes, File, Photo, Text
except ImportError:
    from telegramify_markdown.type import ContentTypes, File, Photo, Text

from app.adapters.network.http import RequestUtils
from app.application.image import ImageHelper
from app.application.messaging.ingress import forward_message_to_host
from app.domain.context import Context, MediaInfo
from app.domain.metainfo import MetaInfo
from app.foundation import size as size_tools
from app.modules.telegram.botapi import (
    FORWARDED_CONTENT_TYPES,
    JsonDict,
    TelegramApiError,
    TelegramBotApi,
)
from app.runtime.execution import retry
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.runtime.thread import ThreadHelper
from app.schemas.exception import ImmediateException

TELEGRAM_PARSE_MODE_MARKDOWN = "MarkdownV2"
TELEGRAM_PARSE_MODE_HTML = "HTML"
TELEGRAM_PARSE_MODE_PLAIN = ""
# Telegram 对同一消息的编辑频率有独立限制；流式缓冲会在闸门后合并为最新内容。
TELEGRAM_EDIT_MIN_INTERVAL_SECONDS = 2.0
# 服务端未给出重试时间时使用的默认冷却秒数
TELEGRAM_RATE_LIMIT_DEFAULT_SECONDS = 60.0
# 持续限流期间日志输出间隔，单位为秒
TELEGRAM_RATE_LIMIT_LOG_INTERVAL_SECONDS = 60.0
_TELEGRAM_RETRY_AFTER_PATTERN = re.compile(r"retry\s+after\s+(\d+)", re.IGNORECASE)
# InputRichMessage 在 Bot API 中定义的字段，转换结果中的其他键不发送。
_RICH_MESSAGE_FIELDS = ("html", "markdown", "is_rtl", "skip_entity_detection")
TELEGRAM_PARSE_MODE_ALIASES = {
    "markdownv2": TELEGRAM_PARSE_MODE_MARKDOWN,
    "mdv2": TELEGRAM_PARSE_MODE_MARKDOWN,
    "html": TELEGRAM_PARSE_MODE_HTML,
    "plain": TELEGRAM_PARSE_MODE_PLAIN,
    "text": TELEGRAM_PARSE_MODE_PLAIN,
}


class _HtmlMessageSplitter(HTMLParser):
    """
    按可见文本UTF-16长度拆分Telegram HTML，分块处关闭并重开仍生效的标签。

    :ivar chunks: 各块可独立解析的HTML正文
    :ivar _parts: 当前块的原始标记与文本片段
    :ivar _stack: 仍生效的标签名与原始起始标记，保留链接等属性
    :ivar _size: 当前块解析实体后的UTF-16长度
    :ivar _limit: 每块允许的最大可见长度
    """

    def __init__(self, limit: int) -> None:
        """
        保留实体原文，使分块不会切开实体或丢失换行。

        :param limit: 每块可见文本UTF-16长度上限，必须为正数
        """
        super().__init__(convert_charrefs=False)
        self.chunks: list[str] = []
        self._parts: list[str] = []
        self._stack: list[tuple[str, str]] = []
        self._size = 0
        self._limit = max(1, limit)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        """
        保存完整起始标签及属性，供后续分块重新打开。
        """
        raw = self.get_starttag_text() or f"<{tag}>"
        self._parts.append(raw)
        self._stack.append((tag, raw))

    def handle_endtag(self, tag: str) -> None:
        """
        结束当前标签，输入须遵守Telegram原有合法HTML合同。
        """
        self._parts.append(f"</{tag}>")
        if self._stack and self._stack[-1][0] == tag:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        """
        仅在Unicode字符之间拆分正文，非BMP字符占两个UTF-16单元。
        """
        for character in data:
            self._append_content(character, 2 if ord(character) > 0xFFFF else 1)

    def handle_entityref(self, name: str) -> None:
        """
        作为不可拆分的整体追加命名实体。
        """
        self._append_entity(f"&{name};")

    def handle_charref(self, name: str) -> None:
        """
        作为不可拆分的整体追加数字实体。
        """
        self._append_entity(f"&#{name};")

    def _append_entity(self, raw: str) -> None:
        """
        按解码后的可见长度核对实体边界，发送时保留其原始编码。
        """
        self._append_content(raw, len(html_utils.unescape(raw).encode("utf-16-le")) // 2)

    def _append_content(self, raw: str, size: int) -> None:
        """
        在当前内容超限前结算并重新打开标签，不重复可见正文。
        """
        if self._size and self._size + size > self._limit:
            self._flush()
        self._parts.append(raw)
        self._size += size

    def _flush(self) -> None:
        """
        结算可独立解析的HTML块，并恢复下一块所需的标签上下文。
        """
        closing = "".join(f"</{tag}>" for tag, _raw in reversed(self._stack))
        self.chunks.append("".join(self._parts) + closing)
        self._parts = [raw for _tag, raw in self._stack]
        self._size = 0

    def finish(self) -> List[str]:
        """
        返回包含最后一块的完整结果，不制造只有重复标签的空块。
        """
        self.close()
        if self._size:
            self._flush()
        return self.chunks


class RetryException(Exception):
    """
    Telegram 消息发送重试异常。
    """

    pass


class NonRetryableTelegramError(ImmediateException):
    """
    Telegram 明确拒绝的请求，重试不会改变结果。

    内容非法、格式无法解析、方法不存在等 4xx 错误属于这一类：立即抛出以跳过
    ``retry`` 的退避等待，避免同一条坏消息连续阻塞 3 秒、6 秒后才报错。
    """

    pass


def _is_non_retryable_api_error(err: Exception) -> bool:
    """
    判断 Bot API 错误是否为重试无意义的 4xx。

    5xx 与网络异常仍按原逻辑重试；400 是确定性拒绝，429 需要由调用方建立冷却，
    两者都不适合立即重复请求。
    """
    return isinstance(err, TelegramApiError) and err.error_code in (400, 429)


class Telegram:
    """
    Telegram 消息客户端，负责发送、编辑、接收和转发 Telegram 消息。
    """

    _bot: Optional[TelegramBotApi] = None
    # 按回调命令保存自定义处理器
    _callback_handlers: Dict[str, Callable[..., Any]] = {}
    # 用于群聊提及识别的机器人用户名
    _bot_username: Optional[str] = None
    # 输入状态续发、首发延迟及各场景最长展示时间，单位均为秒
    _typing_interval_seconds = 5
    _typing_initial_delay_seconds = 1
    _typing_max_duration_seconds = 10 * 60
    _typing_command_max_duration_seconds = 30
    _typing_callback_max_duration_seconds = 60
    # 单个输入状态任务与更新分发的关闭等待秒数
    _typing_join_timeout_seconds = 1
    _shutdown_timeout_seconds = 10
    # getUpdates 长轮询等待秒数；停止时不等待进行中的请求，因此不受关闭预算约束。
    _long_polling_timeout_seconds = 30

    def __init__(
            self,
            TELEGRAM_TOKEN: Optional[str] = None,
            TELEGRAM_CHAT_ID: Optional[str] = None,
            TELEGRAM_TOPIC_ID: Optional[str] = None,
            **kwargs: Any,
    ) -> None:
        """
        创建独立机器人客户端并启动长轮询接收线程。

        :param TELEGRAM_TOKEN: 机器人令牌；令牌或默认会话缺失时不创建 API 客户端
        :param TELEGRAM_CHAT_ID: 默认消息接收会话标识
        :param TELEGRAM_TOPIC_ID: 默认会话的话题标识，必须为正十进制整数；无效时忽略
        :param kwargs: 可选 API_URL 自建服务地址和 name 实例名称，其他配置当前不使用
        """
        # 即使配置不完整也保留基础属性，便于测试和未启用实例安全调用发送方法。
        self._telegram_token = TELEGRAM_TOKEN
        self._telegram_chat_id = TELEGRAM_CHAT_ID
        self._telegram_topic_id = None
        if TELEGRAM_TOPIC_ID and str(TELEGRAM_TOPIC_ID).strip():
            topic_id = str(TELEGRAM_TOPIC_ID).strip()
            if topic_id.isdecimal() and int(topic_id) > 0:
                self._telegram_topic_id = int(topic_id)
            else:
                logger.warning("Telegram话题ID无效，通知将发送到默认话题")
        self._polling_thread = None
        # 一个 Telegram 配置对应一个 SDK client，运行状态不能被其他配置共享。
        self._user_chat_mapping: Dict[str, str] = {}
        # 出站编辑使用独立状态，避免高频流式回调把限流放大为重复请求。
        self._edit_state_lock = threading.RLock()
        self._edit_next_allowed: Dict[tuple[str, int], float] = {}
        self._chat_rate_limit_until: Dict[str, float] = {}
        self._chat_rate_limit_skipped: Dict[str, int] = {}
        self._chat_rate_limit_last_log: Dict[str, float] = {}
        self._typing_tasks: Dict[str, threading.Thread] = {}
        self._typing_stop_flags: Dict[str, threading.Event] = {}
        self._typing_lock = threading.RLock()
        self._typing_lifecycle_lock = threading.RLock()
        self._typing_accepting = True
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            logger.error("Telegram配置不完整！")
            return
        # Bot API 服务基址；自建地址不使用全局代理。
        self._bot = TelegramBotApi(
            TELEGRAM_TOKEN,
            api_url=kwargs.get("API_URL"),
            proxy=get_runtime_setting('PROXY'),
            parse_mode=TELEGRAM_PARSE_MODE_MARKDOWN,
        )
        self._config_name = kwargs.get("name")
        # 获取并存储bot用户名用于@检测
        try:
            self._bot_username = self._api.get_me().get("username")
            logger.info(f"Telegram bot用户名: @{self._bot_username}")
        except Exception as e:
            logger.error(f"获取bot信息失败: {e}")
            self._bot_username = None

        self._polling_thread = threading.Thread(
            target=self._run_polling,
            name="MoviePilot-TelegramPolling",
            daemon=True,
        )
        self._polling_thread.start()
        logger.info("Telegram消息接收服务启动")

    @property
    def _api(self) -> TelegramBotApi:
        """
        已初始化的 Bot API 客户端。

        未配置或已停止时抛错，由各发送方法的异常处理记录失败，与历史实现访问空句柄的结果一致。
        """
        if self._bot is None:
            raise RuntimeError("Telegram客户端未初始化")
        return self._bot

    @staticmethod
    def _find_api_error(err: BaseException) -> Optional[TelegramApiError]:
        """
        沿异常链查找 Bot API 原始错误，保留装饰器包装后的 429 语义。
        """
        current: Optional[BaseException] = err
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, TelegramApiError):
                return current
            current = current.__cause__ or current.__context__
        return None

    @classmethod
    def _retry_after_seconds(cls, err: BaseException) -> Optional[int]:
        """
        读取 Telegram 429 的结构化或文本冷却秒数。
        """
        api_error = cls._find_api_error(err)
        if api_error is None or api_error.error_code != 429:
            return None
        retry_after = api_error.parameters.get("retry_after")
        try:
            if retry_after is not None and float(retry_after) > 0:
                return max(1, int(float(retry_after)))
        except (TypeError, ValueError):
            pass
        matched = _TELEGRAM_RETRY_AFTER_PATTERN.search(api_error.description)
        if matched:
            return max(1, int(matched.group(1)))
        return None

    @classmethod
    def _is_rate_limit_error(cls, err: BaseException) -> bool:
        """
        判断异常链是否包含 Telegram 429。
        """
        return cls._retry_after_seconds(err) is not None or (
            (api_error := cls._find_api_error(err)) is not None
            and api_error.error_code == 429
        )

    def _ensure_edit_state(self) -> None:
        """
        为旧测试桩和未完整初始化实例补齐出站编辑状态。
        """
        if hasattr(self, "_edit_state_lock"):
            return
        self._edit_state_lock = threading.RLock()
        self._edit_next_allowed = {}
        self._chat_rate_limit_until = {}
        self._chat_rate_limit_skipped = {}
        self._chat_rate_limit_last_log = {}

    def _mark_rate_limit(
            self,
            chat_id: Union[str, int],
            err: BaseException,
            operation: str = "编辑",
    ) -> None:
        """
        为聊天记录 Telegram 429 冷却，并输出一次可诊断的限流日志。
        """
        self._ensure_edit_state()
        retry_after = self._retry_after_seconds(err) or int(TELEGRAM_RATE_LIMIT_DEFAULT_SECONDS)
        now = time.monotonic()
        chat_key = str(chat_id)
        with self._edit_state_lock:
            previous_until = self._chat_rate_limit_until.get(chat_key, 0.0)
            cooldown_until = max(
                previous_until,
                now + max(retry_after, 1),
            )
            self._chat_rate_limit_until[chat_key] = cooldown_until
            if previous_until <= now:
                self._chat_rate_limit_skipped[chat_key] = 0
                self._chat_rate_limit_last_log[chat_key] = now
        if cooldown_until > previous_until:
            logger.warning(
                "Telegram%s触发429，chat_id=%s，冷却%s秒%s",
                operation,
                chat_id,
                retry_after,
                "（按 retry_after）" if self._retry_after_seconds(err) else "（未提供 retry_after，使用默认值）",
            )

    def _is_chat_rate_limited(self, chat_id: Union[str, int], operation: str = "编辑") -> bool:
        """
        检查聊天是否仍在 Telegram 429 冷却期，并限频记录跳过次数。
        """
        self._ensure_edit_state()
        now = time.monotonic()
        chat_key = str(chat_id)
        with self._edit_state_lock:
            cooldown_until = self._chat_rate_limit_until.get(chat_key, 0.0)
            if cooldown_until <= now:
                if chat_key in self._chat_rate_limit_until:
                    skipped = self._chat_rate_limit_skipped.pop(chat_key, 0)
                    self._chat_rate_limit_until.pop(chat_key, None)
                    self._chat_rate_limit_last_log.pop(chat_key, None)
                    if skipped:
                        logger.info(
                            "Telegram%s限流冷却结束，chat_id=%s，期间跳过%s次请求",
                            operation,
                            chat_id,
                            skipped,
                        )
                return False
            skipped = self._chat_rate_limit_skipped.get(chat_key, 0) + 1
            self._chat_rate_limit_skipped[chat_key] = skipped
            last_log = self._chat_rate_limit_last_log.get(chat_key, 0.0)
            if skipped == 1 or now - last_log >= TELEGRAM_RATE_LIMIT_LOG_INTERVAL_SECONDS:
                self._chat_rate_limit_last_log[chat_key] = now
                remaining = max(0, int(cooldown_until - now))
                logger.warning(
                    "Telegram%s处于429冷却，跳过后续请求：chat_id=%s，剩余约%s秒，累计跳过%s次",
                    operation,
                    chat_id,
                    remaining,
                    skipped,
                )
            return True

    def _reserve_edit(
            self,
            chat_id: Union[str, int],
            message_id: Union[str, int],
            wait_for_interval: bool = False,
    ) -> bool:
        """
        原子预留一次消息编辑，执行聊天冷却和消息级最小间隔检查。

        :param chat_id: 编辑目标会话标识
        :param message_id: 编辑目标消息标识
        :param wait_for_interval: 是否等待一次消息级间隔，最长两秒；聊天 429 冷却不会等待
        :return: 成功预留编辑时返回 True，否则返回 False
        """
        self._ensure_edit_state()
        if self._is_chat_rate_limited(chat_id):
            return False
        key = (str(chat_id), int(message_id))
        if wait_for_interval:
            with self._edit_state_lock:
                remaining = self._edit_next_allowed.get(key, 0.0) - time.monotonic()
            if remaining > 0:
                # 调用方在线程池内收尾；不占用事件循环，也不持锁等待或无限追赶并发编辑。
                time.sleep(min(remaining, TELEGRAM_EDIT_MIN_INTERVAL_SECONDS))
            if self._is_chat_rate_limited(chat_id):
                return False
        now = time.monotonic()
        with self._edit_state_lock:
            if self._edit_next_allowed.get(key, 0.0) > now:
                return False
            self._edit_next_allowed[key] = now + TELEGRAM_EDIT_MIN_INTERVAL_SECONDS
        return True

    def _mark_edit_succeeded(
            self, chat_id: Union[str, int], message_id: Union[str, int]
    ) -> None:
        """
        以实际完成时间重新计算消息编辑间隔，避免并发请求贴着结束时间发送。
        """
        self._ensure_edit_state()
        key = (str(chat_id), int(message_id))
        with self._edit_state_lock:
            self._edit_next_allowed[key] = (
                time.monotonic() + TELEGRAM_EDIT_MIN_INTERVAL_SECONDS
            )

    def _remember_sent_message(self, sent: Any, chat_id: Union[str, int]) -> None:
        """
        记录新消息的发送时间，使首个流式编辑也遵守最小间隔。
        """
        self._ensure_edit_state()
        if not isinstance(sent, dict) or not sent.get("message_id"):
            return
        target_chat_id = sent.get("chat", {}).get("id", chat_id)
        key = (str(target_chat_id), int(sent["message_id"]))
        with self._edit_state_lock:
            self._edit_next_allowed[key] = (
                time.monotonic() + TELEGRAM_EDIT_MIN_INTERVAL_SECONDS
            )

    def _run_polling(self) -> None:
        """
        轮询线程入口，逐条处理 getUpdates 返回的更新。
        """
        try:
            self._api.polling(
                self._handle_update,
                long_polling_timeout=self._long_polling_timeout_seconds,
            )
        except Exception as err:
            logger.error(f"Telegram消息接收服务异常：{str(err)}")

    def _handle_update(self, update: JsonDict) -> None:
        """
        按更新类型分发：普通消息转发主程序，按钮回调先应答再转发。
        """
        if "callback_query" in update:
            self._handle_callback_query(update["callback_query"])
            return
        message = update.get("message")
        if not message or not any(key in message for key in FORWARDED_CONTENT_TYPES):
            return
        if self._reply_welcome_if_needed(message):
            return
        sender = message.get("from") or {}
        self._update_user_chat_mapping(sender.get("id"), message["chat"]["id"])
        if self._should_process_message(message):
            if not self._forward_to_message_chain(message):
                logger.warn("Telegram消息转发失败")

    def _reply_welcome_if_needed(self, message: JsonDict) -> bool:
        """
        /start、/help 命令只回复使用提示，不转发主程序。

        与原 SDK 命令过滤一致：忽略 @bot 后缀和参数，例如 ``/start@mp_bot payload``。
        """
        text = message.get("text") or ""
        if not text.startswith("/"):
            return False
        command = text.split(maxsplit=1)[0][1:].split("@", 1)[0]
        if command not in ("start", "help"):
            return False
        self._api.send_message(
            chat_id=message["chat"]["id"],
            text="温馨提示：直接发送名称或`订阅`+名称，搜索或订阅电影、电视剧",
            reply_to_message_id=message["message_id"],
        )
        return True

    def _handle_callback_query(self, call: JsonDict) -> None:
        """
        处理按钮点击回调。
        """
        try:
            # 按钮回调也更新用户到最近会话的映射
            chat_id = call["message"]["chat"]["id"]
            self._update_user_chat_mapping(call["from"]["id"], chat_id)

            callback_data = call.get("data")
            user_id = str(call["from"]["id"])

            logger.info(f"收到按钮回调：{callback_data}，用户：{user_id}")

            callback_json = {
                "callback_query": {
                    "id": call["id"],
                    "from": call["from"],
                    "message": {
                        "message_id": call["message"]["message_id"],
                        "chat": {
                            "id": chat_id,
                        },
                    },
                    "data": callback_data,
                }
            }

            # 先确认回调，避免用户看到loading状态
            self._api.answer_callback_query(call["id"])

            if not self._forward_to_message_chain(callback_json):
                logger.warn("Telegram按钮回调转发失败")

        except Exception as err:
            logger.error(f"处理按钮回调失败：{str(err)}")
            self._api.answer_callback_query(call["id"], "处理失败，请重试")

    def _forward_to_message_chain(self, payload: dict[str, Any]) -> bool:
        """
        把 Telegram SDK 回调同步转交统一消息入口。
        """
        return forward_message_to_host(payload, self._config_name)

    @property
    def bot(self) -> Optional[TelegramBotApi]:
        """
        获取Telegram Bot实例。

        :return: Bot API 客户端或None
        """
        return self._bot

    @property
    def bot_username(self) -> Optional[str]:
        """
        获取Bot用户名。

        :return: Bot用户名或None
        """
        return self._bot_username

    def download_file(self, file_id: str) -> Optional[bytes]:
        """
        查询文件路径并下载 Telegram 媒体内容。

        :param file_id: Telegram 分配的文件标识
        :return: 下载内容字节；未初始化、参数缺失或下载失败返回 None
        """
        if not self._bot:
            return None
        try:
            file_path = self._api.get_file(file_id).get("file_path")
            if not file_path:
                logger.warn(f"Telegram文件信息缺少下载路径: file_id={file_id}")
                return None
            file_url = self._api.file_url(file_path)
            resp = RequestUtils(
                proxies=self._api.proxy, timeout=30
            ).get_res(file_url)
            if resp and resp.content:
                logger.info(
                    "Telegram文件下载成功: file_id=%s, file_path=%s, content_bytes=%s",
                    file_id,
                    file_path,
                    len(resp.content),
                )
                return cast(bytes, resp.content)
            logger.warn(
                "Telegram文件下载失败: file_id=%s, file_path=%s, proxy_enabled=%s",
                file_id,
                file_path,
                bool(self._api.proxy),
            )
        except Exception as e:
            logger.error(f"下载Telegram文件失败: {e}")
        return None

    @staticmethod
    def _telegramify_item_text(item: Text) -> str:
        """
        将 telegramify 文本片段转换为 Telegram MarkdownV2 字符串。
        """
        if entities_to_markdownv2:
            return cast(str, entities_to_markdownv2(item.text, item.entities))
        return cast(str, standardize(item.text))

    @staticmethod
    def _telegramify_item_caption(item: Text | File | Photo) -> str:
        """
        将 telegramify 文本或媒体片段转换为 Telegram MarkdownV2 caption。
        """
        if isinstance(item, Text):
            return Telegram._telegramify_item_text(item)
        if entities_to_markdownv2:
            return cast(str, entities_to_markdownv2(item.caption_text, item.caption_entities))
        return cast(str, standardize(item.caption_text))

    @staticmethod
    def _normalize_parse_mode(parse_mode: Optional[str] = None) -> str:
        """
        规范化 Telegram 消息格式类型。
        """
        if parse_mode is None:
            return TELEGRAM_PARSE_MODE_MARKDOWN
        normalized = str(parse_mode).strip()
        if not normalized:
            return TELEGRAM_PARSE_MODE_PLAIN
        return TELEGRAM_PARSE_MODE_ALIASES.get(
            normalized.lower(), TELEGRAM_PARSE_MODE_MARKDOWN
        )

    @staticmethod
    def _is_html_parse_mode(parse_mode: Optional[str] = None) -> bool:
        """
        判断本次发送是否使用 Telegram HTML 格式。
        """
        return Telegram._normalize_parse_mode(parse_mode) == TELEGRAM_PARSE_MODE_HTML

    @staticmethod
    def _is_plain_parse_mode(parse_mode: Optional[str] = None) -> bool:
        """
        判断本次发送是否禁用 Telegram 文本格式解析。
        """
        return Telegram._normalize_parse_mode(parse_mode) == TELEGRAM_PARSE_MODE_PLAIN

    @staticmethod
    def _format_title(title: Optional[str], parse_mode: Optional[str] = None) -> Optional[str]:
        """
        按 parse_mode 生成 Telegram 标题文本。
        """
        if not title:
            return None
        if Telegram._is_plain_parse_mode(parse_mode):
            return title.removesuffix("\n")
        if Telegram._is_html_parse_mode(parse_mode):
            return f"<b>{html_utils.escape(title).removesuffix(chr(10))}</b>"
        return f"**{standardize(title).removesuffix(chr(10))}**"

    @staticmethod
    def _format_link(label: str, link: str, parse_mode: Optional[str] = None) -> str:
        """
        按 parse_mode 生成 Telegram 链接文本。
        """
        if Telegram._is_plain_parse_mode(parse_mode):
            return f"{label}: {link}"
        if Telegram._is_html_parse_mode(parse_mode):
            return (
                f'<a href="{html_utils.escape(link, quote=True)}">'
                f"{html_utils.escape(label)}</a>"
            )
        return f"[{label}]({link})"

    @staticmethod
    def _format_italic(text: str, parse_mode: Optional[str] = None) -> str:
        """
        按 parse_mode 生成 Telegram 斜体文本。
        """
        if Telegram._is_plain_parse_mode(parse_mode):
            return text
        if Telegram._is_html_parse_mode(parse_mode):
            return f"<i>{html_utils.escape(text)}</i>"
        return f"_{text}_"

    @staticmethod
    def _format_detail_link(link: str, parse_mode: Optional[str] = None) -> str:
        """
        按 parse_mode 生成查看详情链接。
        """
        return Telegram._format_link("查看详情", link, parse_mode)

    @staticmethod
    def _prepare_text(text: Optional[str], parse_mode: Optional[str] = None) -> Optional[str]:
        """
        按 parse_mode 生成 Telegram 可发送文本。
        """
        if not text:
            return None
        if (
                Telegram._is_plain_parse_mode(parse_mode)
                or Telegram._is_html_parse_mode(parse_mode)
        ):
            return text
        return cast(str, standardize(text))

    @staticmethod
    def _split_plain_text(text: str, limit: int) -> List[str]:
        """
        按 Telegram 长度限制拆分普通文本。
        """
        if not text:
            return []
        if limit <= 0:
            return [text]
        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, limit)
            if split_at <= 0:
                split_at = limit
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip("\n")
        return chunks

    def _update_user_chat_mapping(
            self, userid: Optional[Union[str, int]], chat_id: Optional[Union[str, int]]
    ) -> None:
        """
        更新用户与聊天的映射关系。

        :param userid: 用户ID
        :param chat_id: 聊天ID
        """
        if userid and chat_id:
            self._user_chat_mapping[str(userid)] = str(chat_id)

    def _get_user_chat_id(self, userid: str) -> Optional[str]:
        """
        获取用户对应的聊天ID。

        :param userid: 用户ID
        :return: 聊天ID或None
        """
        return self._user_chat_mapping.get(str(userid)) if userid else None

    def _should_process_message(self, message: JsonDict) -> bool:
        """
        判断是否应该处理这条消息。

        :param message: Bot API Message 结构
        :return: 是否处理
        """
        chat_type = message["chat"].get("type")
        # 私聊消息总是处理
        if chat_type == "private":
            logger.debug(f"处理私聊消息：用户 {(message.get('from') or {}).get('id')}")
            return True

        # 消息内容：文本消息用 text，媒体消息的说明文字用 caption
        message_text = message.get("text") or message.get("caption") or ""

        # 群聊中的命令消息总是处理（以/开头）
        if message_text.startswith("/"):
            logger.debug(f"处理群聊命令消息：{message_text[:20]}...")
            return True

        # 群聊中检查是否@了机器人
        if chat_type in ["group", "supergroup"]:
            if not self._bot_username:
                # 未获取机器人用户名时无法匹配提及，沿用兼容行为接收该群全部消息
                logger.debug("未获取到bot用户名，处理所有群聊消息")
                return True

            # 检查消息文本或说明文字中是否包含@bot_username
            if f"@{self._bot_username}" in message_text:
                logger.debug(f"检测到@{self._bot_username}，处理群聊消息")
                return True

            # 检查消息实体（文本消息用 entities，媒体消息用 caption_entities）
            entities = (
                message.get("entities")
                if message.get("text") is not None
                else message.get("caption_entities")
            ) or []
            for entity in entities:
                if entity.get("type") == "mention":
                    # Telegram offset/length 基于 UTF-16 代码单元，需用字节编码处理含 emoji 的消息
                    offset, length = entity["offset"], entity["length"]
                    mention_text = message_text.encode('utf-16-le')[
                        offset * 2: (offset + length) * 2
                    ].decode('utf-16-le')
                    if mention_text == f"@{self._bot_username}":
                        logger.debug(
                            f"通过实体检测到@{self._bot_username}，处理群聊消息"
                        )
                        return True

            # 群聊中没有@机器人，不处理
            logger.debug(
                f"群聊消息未@机器人，跳过处理：{message_text[:30] if message_text else 'No text'}..."
            )
            return False

        # 其他类型的聊天默认处理
        logger.debug(f"处理其他类型聊天消息：{chat_type}")
        return True

    def get_state(self) -> bool:
        """
        检查当前实例是否持有 Bot API 客户端。

        :return: 客户端存在时返回 True；不额外检查网络或轮询健康状态
        """
        return self._bot is not None

    def _start_typing_task(
            self,
            chat_id: Union[str, int],
            max_duration_seconds: Optional[float] = None,
            initial_delay_seconds: Optional[float] = None,
    ) -> bool:
        """
        启动一个会话独占的输入状态续发任务。

        :param chat_id: 需要展示输入状态的会话标识
        :param max_duration_seconds: 最长续发秒数；未提供或为 0 时使用 600 秒
        :param initial_delay_seconds: 首发延迟秒数；None 使用 1 秒，负数按 0 处理
        :return: 成功启动返回 True；客户端停止接收任务或旧任务未退出时返回 False
        """
        chat_id_str = str(chat_id)
        with self._typing_lifecycle_lock:
            if not self._typing_accepting:
                logger.debug("Telegram client已停止，拒绝启动typing任务")
                return False
            # 如果已有任务在运行，先停止；阻塞的旧 SDK 请求不能被新 owner 覆盖。
            if not self._stop_typing_task(chat_id_str):
                logger.warning(
                    "Telegram typing旧任务尚未结束，拒绝并行启动: chat_id=%s",
                    chat_id_str,
                )
                return False

            # 使用独立 Event 避免同一 chat 新旧 typing 线程互相误改停止标记。
            stop_event = threading.Event()
            max_duration = max_duration_seconds or self._typing_max_duration_seconds
            initial_delay = (
                self._typing_initial_delay_seconds
                if initial_delay_seconds is None
                else max(initial_delay_seconds, 0)
            )

            def typing_worker() -> None:
                """
                延迟首发并定期发送 typing 状态的后台线程。
                """
                started_at = time.monotonic()
                try:
                    # Telegram 没有撤销 typing 的接口，短响应先等待一小段时间，
                    # 避免回复已经发出后客户端仍残留几秒“正在输入”。
                    if initial_delay and stop_event.wait(initial_delay):
                        return
                    while not stop_event.is_set():
                        if time.monotonic() - started_at >= max_duration:
                            logger.warning(
                                "Telegram typing状态超过最大续期，自动停止: chat_id=%s",
                                chat_id_str,
                            )
                            break
                        try:
                            if self._bot:
                                self._api.send_chat_action(chat_id, "typing")
                        except Exception as e:
                            logger.debug(f"发送typing状态失败: {e}")
                        # Telegram 客户端约 5-6 秒后会隐藏 typing，需要周期性续发。
                        stop_event.wait(self._typing_interval_seconds)
                finally:
                    with self._typing_lock:
                        current = self._typing_tasks.get(chat_id_str)
                        if current is threading.current_thread():
                            self._typing_tasks.pop(chat_id_str, None)
                            self._typing_stop_flags.pop(chat_id_str, None)

            thread = threading.Thread(
                target=typing_worker,
                name=f"MoviePilot-TelegramTyping-{chat_id_str}"[:120],
                daemon=True,
            )
            with self._typing_lock:
                self._typing_stop_flags[chat_id_str] = stop_event
                self._typing_tasks[chat_id_str] = thread
                try:
                    thread.start()
                except BaseException:
                    self._typing_stop_flags.pop(chat_id_str, None)
                    self._typing_tasks.pop(chat_id_str, None)
                    raise
            return True

    def _stop_typing_task(self, chat_id: Union[str, int]) -> bool:
        """
        请求结束指定会话的输入状态任务，并最多等待 1 秒。

        :param chat_id: 目标会话标识
        :return: 任务已退出或不存在时返回 True；仍运行时返回 False 并保留任务引用
        """
        chat_id_str = str(chat_id)
        with self._typing_lifecycle_lock:
            with self._typing_lock:
                stop_event = self._typing_stop_flags.get(chat_id_str)
                task = self._typing_tasks.get(chat_id_str)
            if stop_event:
                stop_event.set()
            if task and task.is_alive() and task is not threading.current_thread():
                task.join(timeout=self._typing_join_timeout_seconds)
            task_finished = task is None or not task.is_alive()
            if task_finished:
                with self._typing_lock:
                    if self._typing_tasks.get(chat_id_str) is task:
                        self._typing_tasks.pop(chat_id_str, None)
                        self._typing_stop_flags.pop(chat_id_str, None)
            return task_finished

    def _stop_typing_if_needed(
            self, chat_id: Union[str, int], stop_typing: bool
    ) -> None:
        """
        按调用方要求停止 typing。
        typing 由消息处理状态统一收口，兼容显式要求立即停止的调用。
        """
        if stop_typing:
            self._stop_typing_task(chat_id)

    def start_typing(
            self,
            chat_id: Optional[Union[str, int]] = None,
            userid: Optional[Union[str, int]] = None,
    ) -> bool:
        """
        根据会话或用户启动输入状态续发。

        :param chat_id: 显式会话标识，优先于用户映射
        :param userid: 无显式会话时先查最近会话，再使用用户标识作为私聊目标
        :return: 任务启动成功返回 True；无目标或启动被拒绝返回 False
        """
        if chat_id:
            target_chat_id = chat_id
        elif userid:
            target_chat_id = self._get_user_chat_id(str(userid)) or str(userid)
        else:
            target_chat_id = None
        target_chat_id = target_chat_id or (str(userid) if userid else None)
        if not target_chat_id:
            return False
        return self._start_typing_task(target_chat_id)

    def stop_typing(
            self,
            chat_id: Optional[Union[str, int]] = None,
            userid: Optional[Union[str, int]] = None,
    ) -> bool:
        """
        根据会话或用户请求停止输入状态续发。

        :param chat_id: 显式会话标识，优先于用户映射
        :param userid: 无显式会话时先查最近会话，再使用用户标识作为私聊目标
        :return: 找到目标并请求停止返回 True；无目标返回 False，True 不保证任务已经退出
        """
        if chat_id:
            target_chat_id = chat_id
        elif userid:
            target_chat_id = self._get_user_chat_id(str(userid)) or str(userid)
        else:
            target_chat_id = None
        target_chat_id = target_chat_id or (str(userid) if userid else None)
        if not target_chat_id:
            return False
        self._stop_typing_task(target_chat_id)
        return True

    def send_msg(
            self,
            title: Optional[str],
            text: Optional[str] = None,
            image: Optional[str] = None,
            userid: Optional[str] = None,
            link: Optional[str] = None,
            buttons: Optional[List[List[dict[str, Any]]]] = None,
            force_reply: bool = False,
            original_message_id: Optional[int] = None,
            original_chat_id: Optional[str] = None,
            disable_web_page_preview: Optional[bool] = None,
            stop_typing: bool = False,
            parse_mode: Optional[str] = None,
            rich_message: Optional[str] = None,
            private_delivery: bool = False,
    ) -> Optional[JsonDict]:
        """
        发送文本、图文或富文本，并兼容原消息编辑和强制回复。

        :param title: 可选消息标题，与正文和富文本不能同时为空
        :param text: 普通消息正文
        :param image: 图文封面地址，富文本模式下不使用
        :param userid: 接收用户标识；默认使用该用户最近会话，无记录时用用户标识，未指定时用默认会话
        :param link: 普通正文末尾的详情地址
        :param buttons: 按行排列的链接或回调按钮；优先于强制回复键盘
        :param force_reply: 是否显示强制回复键盘；与原消息上下文结合时发送新回复
        :param original_message_id: 与原会话同时提供时编辑原消息；无按钮的强制回复模式改为回复原消息
        :param original_chat_id: 显式原会话标识，优先于普通用户映射
        :param disable_web_page_preview: 是否禁用普通文本链接预览
        :param stop_typing: 是否在操作收尾时请求停止输入状态，默认 False
        :param parse_mode: 消息格式，默认 MarkdownV2；支持 HTML 和 plain 纯文本
        :param rich_message: 完整富文本正文；非空时替代普通图文内容
        :param private_delivery: 有用户标识时是否绕过最近会话映射直接投递私聊
        :return: 含 success 和消息引用的结果；配置缺失返回 None，限流、空内容或发送失败返回 success 为 False 的字典
        """
        if not self._telegram_token or not self._telegram_chat_id:
            return None

        parse_mode = self._normalize_parse_mode(parse_mode)
        # 兼容原会话回复、最近互动会话及显式私聊投递
        chat_id = self._determine_target_chat_id(
            userid,
            original_chat_id,
            private_delivery=private_delivery,
        )
        if self._is_chat_rate_limited(chat_id, operation="发送"):
            self._stop_typing_if_needed(chat_id, stop_typing)
            return {"success": False}
        if not title and not text and not rich_message:
            logger.warn("标题、内容和富文本内容不能同时为空")
            self._stop_typing_if_needed(chat_id, stop_typing)
            return {"success": False}

        try:
            bold_title = self._format_title(title, parse_mode)
            if bold_title and text:
                caption = f"{bold_title}\n{text}"
            elif bold_title:
                caption = bold_title
            elif text:
                caption = text
            else:
                caption = ""

            if link:
                caption = f"{caption}\n{self._format_detail_link(link, parse_mode)}"

            reply_markup = None
            if buttons:
                reply_markup = self._create_inline_keyboard(buttons)
            elif force_reply:
                reply_markup = self._create_force_reply_markup()

            if rich_message:
                return self._send_rich_message(
                    chat_id=chat_id,
                    text=text,
                    buttons=buttons,
                    force_reply=force_reply,
                    original_message_id=original_message_id,
                    original_chat_id=original_chat_id,
                    stop_typing=stop_typing,
                    rich_message=rich_message,
                    reply_markup=reply_markup,
                )

            if original_message_id and original_chat_id:
                if force_reply and reply_markup and not buttons:
                    sent = self.__send_request(
                        userid=original_chat_id,
                        image=image,
                        caption=caption,
                        reply_markup=reply_markup,
                        disable_web_page_preview=disable_web_page_preview,
                        parse_mode=parse_mode,
                        reply_to_message_id=original_message_id,
                    )
                    self._remember_sent_message(sent, original_chat_id)
                    self._stop_typing_if_needed(chat_id, stop_typing)
                    return self._sent_result(sent, chat_id)
                result = self.__edit_message(
                    original_chat_id,
                    original_message_id,
                    caption,
                    buttons,
                    image,
                    disable_web_page_preview=disable_web_page_preview,
                    parse_mode=parse_mode,
                )
                self._stop_typing_if_needed(chat_id, stop_typing)
                return {
                    "success": bool(result),
                    "message_id": original_message_id,
                    "chat_id": original_chat_id,
                }
            else:
                sent = self.__send_request(
                    userid=chat_id,
                    image=image,
                    caption=caption,
                    reply_markup=reply_markup,
                    disable_web_page_preview=disable_web_page_preview,
                    parse_mode=parse_mode,
                )
                self._remember_sent_message(sent, chat_id)
                self._stop_typing_if_needed(chat_id, stop_typing)
                return self._sent_result(sent, chat_id)

        except Exception as msg_e:
            logger.error(f"发送消息失败：{msg_e}")
            self._stop_typing_if_needed(chat_id, stop_typing)
            return {"success": False}

    def _send_rich_message(
            self,
            *,
            chat_id: Union[str, int],
            text: Optional[str],
            buttons: Optional[List[List[Dict[str, Any]]]],
            force_reply: bool,
            original_message_id: Optional[int],
            original_chat_id: Optional[str],
            stop_typing: bool,
            rich_message: str,
            reply_markup: Optional[JsonDict],
    ) -> JsonDict:
        """
        发送或编辑 Rich Message，并在限流时隔离后续输出请求。
        """
        if original_message_id and original_chat_id and not force_reply:
            result = self.edit_msg(
                chat_id=original_chat_id,
                message_id=original_message_id,
                text=text or "",
                rich_message=rich_message,
                buttons=buttons,
                stop_typing=stop_typing,
            )
            return {
                "success": bool(result),
                "message_id": original_message_id,
                "chat_id": original_chat_id,
            }

        target_chat_id = original_chat_id if force_reply and original_chat_id else chat_id
        try:
            sent = self.__send_rich_message(
                chat_id=target_chat_id,
                rich_message=rich_message,
                reply_markup=reply_markup,
                reply_to_message_id=original_message_id if force_reply else None,
            )
        except Exception as rich_err:
            if self._is_rate_limit_error(rich_err):
                self._mark_rate_limit(target_chat_id, rich_err, operation="发送")
                sent = None
            else:
                # Rich Message 发送失败不再等同于通知丢失，降级为纯文本重发一次。
                logger.warning(
                    f"Telegram Rich Message 发送失败，降级为纯文本：{rich_err}"
                )
                sent = self.__send_plain_fallback(
                    chat_id=target_chat_id,
                    rich_message=rich_message,
                    reply_markup=reply_markup,
                )
        self._remember_sent_message(sent, target_chat_id)
        self._stop_typing_if_needed(chat_id, stop_typing)
        return self._sent_result(sent, chat_id)

    @staticmethod
    def _sent_result(sent: Any, chat_id: Union[str, int]) -> JsonDict:
        """
        把 Bot API 返回的 Message 转为发送结果；非 Message 的真值只表示成功。
        """
        if isinstance(sent, dict) and "message_id" in sent:
            return {
                "success": True,
                "message_id": sent["message_id"],
                "chat_id": sent.get("chat", {}).get("id", chat_id),
            }
        return {"success": bool(sent)}

    @staticmethod
    def _create_force_reply_markup() -> JsonDict:
        """
        生成只对被回复用户生效的 ForceReply 标记。
        """
        return {
            "force_reply": True,
            "selective": True,
            "input_field_placeholder": "请输入内容",
        }

    def send_voice(
            self,
            voice_path: str,
            userid: Optional[str] = None,
            caption: Optional[str] = None,
            original_chat_id: Optional[str] = None,
            stop_typing: bool = False,
            parse_mode: Optional[str] = None,
    ) -> Optional[JsonDict]:
        """
        发送本地语音，进入发送流程后清理源文件。

        :param voice_path: 语音文件路径；发送成功或失败都会尝试删除
        :param userid: 接收用户标识；默认使用该用户最近会话，无记录时用用户标识，未指定时用默认会话
        :param caption: 可选语音说明
        :param original_chat_id: 显式原会话标识，优先于普通用户映射
        :param stop_typing: 是否在操作收尾时请求停止输入状态，默认 False
        :param parse_mode: 消息格式，默认 MarkdownV2；支持 HTML 和 plain 纯文本
        :return: 含 success、message_id 和 chat_id 的结果；失败为 success 为 False 的字典，未配置或无输入路径返回 None
        """
        if not self._bot or not voice_path:
            return None

        chat_id = self._determine_target_chat_id(userid, original_chat_id)
        parse_mode = self._normalize_parse_mode(parse_mode)
        voice_file = Path(voice_path)
        if not voice_file.exists():
            logger.error(f"语音文件不存在: {voice_file}")
            self._stop_typing_if_needed(chat_id, stop_typing)
            return {"success": False}

        try:
            with voice_file.open("rb") as fp:
                sent = self._api.send_voice(
                    chat_id=chat_id,
                    voice=fp,
                    caption=self._prepare_text(caption, parse_mode),
                    parse_mode=parse_mode if caption else None,
                    **self._topic_kwargs(chat_id),
                )
            self._stop_typing_if_needed(chat_id, stop_typing)
            return self._sent_result(sent, chat_id)
        except Exception as err:
            logger.error(f"发送语音消息失败：{err}")
            self._stop_typing_if_needed(chat_id, stop_typing)
            return {"success": False}
        finally:
            try:
                voice_file.unlink(missing_ok=True)
            except Exception as cleanup_err:
                logger.debug(f"清理语音临时文件失败: {cleanup_err}")

    def send_file(
            self,
            file_path: str,
            userid: Optional[str] = None,
            title: Optional[str] = None,
            text: Optional[str] = None,
            file_name: Optional[str] = None,
            original_chat_id: Optional[str] = None,
            stop_typing: bool = False,
            parse_mode: Optional[str] = None,
    ) -> Optional[JsonDict]:
        """
        根据后缀发送本地图片或普通附件，保留源文件。

        :param file_path: 本地普通文件路径
        :param userid: 接收用户标识；默认使用该用户最近会话，无记录时用用户标识，未指定时用默认会话
        :param title: 可选附件说明标题
        :param text: 可选附件说明正文
        :param file_name: 普通附件的显示名称；为空时使用源文件名，图片不使用此值
        :param original_chat_id: 显式原会话标识，优先于普通用户映射
        :param stop_typing: 是否在操作收尾时请求停止输入状态，默认 False
        :param parse_mode: 消息格式，默认 MarkdownV2；支持 HTML 和 plain 纯文本
        :return: 含 success、message_id 和 chat_id 的结果；失败为 success 为 False 的字典，未配置或无输入路径返回 None
        """
        if not self._bot or not file_path:
            return None

        chat_id = self._determine_target_chat_id(userid, original_chat_id)
        parse_mode = self._normalize_parse_mode(parse_mode)
        local_file = Path(file_path)
        if not local_file.exists() or not local_file.is_file():
            logger.error(f"附件文件不存在: {local_file}")
            self._stop_typing_if_needed(chat_id, stop_typing)
            return {"success": False}

        send_name = file_name or local_file.name
        suffix = local_file.suffix.lower()
        is_image = suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

        try:
            bold_title = self._format_title(title, parse_mode)
            if bold_title and text:
                caption = f"{bold_title}\n{text}"
            elif bold_title:
                caption = bold_title
            else:
                caption = text or ""

            with local_file.open("rb") as fp:
                if is_image:
                    sent = self._api.send_photo(
                        chat_id=chat_id,
                        photo=fp,
                        caption=self._prepare_text(caption, parse_mode),
                        parse_mode=parse_mode if caption else None,
                        **self._topic_kwargs(chat_id),
                    )
                else:
                    sent = self._api.send_document(
                        chat_id=chat_id,
                        document=(send_name, fp),
                        caption=self._prepare_text(caption, parse_mode),
                        parse_mode=parse_mode if caption else None,
                        **self._topic_kwargs(chat_id),
                    )
            self._stop_typing_if_needed(chat_id, stop_typing)
            return self._sent_result(sent, chat_id)
        except Exception as err:
            logger.error(f"发送本地附件失败: {err}")
            self._stop_typing_if_needed(chat_id, stop_typing)
            return {"success": False}

    def _determine_target_chat_id(
            self,
            userid: Optional[str] = None,
            original_chat_id: Optional[str] = None,
            private_delivery: bool = False,
    ) -> str:
        """
        按私聊约束、原会话、用户映射和默认配置解析发送目标。

        :param userid: 接收用户标识；默认使用该用户最近会话，无记录时用用户标识，未指定时用默认会话
        :param original_chat_id: 显式原会话标识，优先于普通用户映射
        :param private_delivery: 有用户标识时是否强制直接以该用户作为私聊目标
        :return: 目标会话标识；无用户和原会话时使用配置中的默认值
        """
        # 私聊投递以渠道用户 ID 为目标，最近会话映射可能指向群聊，不能参与解析。
        if private_delivery and userid:
            return str(userid)

        if original_chat_id:
            return original_chat_id

        if userid:
            mapped_chat_id = self._get_user_chat_id(userid)
            if mapped_chat_id:
                return mapped_chat_id
            # 如果映射中没有，回退到使用userid作为聊天ID (私聊场景)
            return userid

        return self._telegram_chat_id or ""

    def _topic_kwargs(self, chat_id: Optional[Union[str, int]]) -> dict[str, int]:
        """
        仅向本渠道配置的群组投递话题参数，避免影响私聊和其它会话。
        """
        topic_id = getattr(self, "_telegram_topic_id", None)
        if topic_id and chat_id is not None and str(chat_id) == str(self._telegram_chat_id):
            return {"message_thread_id": topic_id}
        return {}

    def send_medias_msg(
            self,
            medias: List[MediaInfo],
            userid: Optional[str] = None,
            title: Optional[str] = None,
            link: Optional[str] = None,
            buttons: Optional[List[List[dict[str, Any]]]] = None,
            original_message_id: Optional[int] = None,
            original_chat_id: Optional[str] = None,
            stop_typing: bool = False,
            parse_mode: Optional[str] = None,
    ) -> Optional[Union[bool, JsonDict]]:
        """
        发送或编辑媒体候选列表。

        :param medias: 按原顺序展示的候选项
        :param userid: 接收用户标识；默认使用该用户最近会话，无记录时用用户标识，未指定时用默认会话
        :param title: 列表标题
        :param link: 列表末尾链接
        :param buttons: 按行排列的链接或回调按钮
        :param original_message_id: 与原会话同时提供时编辑该消息
        :param original_chat_id: 显式原会话标识，优先于普通用户映射
        :param stop_typing: 是否在操作收尾时请求停止输入状态，默认 False
        :param parse_mode: 消息格式，默认 MarkdownV2；支持 HTML 和 plain 纯文本
        :return: 编辑时返回成功布尔值，发送时返回最后一条 Bot API 消息字典；异常返回 False，配置缺失或发送无结果返回 None
        """
        if not self._telegram_token or not self._telegram_chat_id:
            return None

        # 列表发送结束后是否停止输入状态，由 stop_typing 明确控制。
        chat_id = self._determine_target_chat_id(userid, original_chat_id)
        parse_mode = self._normalize_parse_mode(parse_mode)
        try:
            index, image = 1, ""
            caption = self._format_title(title, parse_mode) or ""
            for media in medias:
                if not image:
                    image = media.get_message_image()
                media_link = self._format_link(
                    media.title_year, media.detail_link, parse_mode
                )
                type_text = f"类型：{media.type.value if media.type else ""}"
                if media.vote_average:
                    score_text = f"评分：{media.vote_average}"
                    caption = (
                        f"{caption}\n{index}. {media_link}\n"
                        f"{self._format_italic(f'{type_text}，{score_text}', parse_mode)}"
                    )
                else:
                    caption = (
                        f"{caption}\n{index}. {media_link}\n"
                        f"{self._format_italic(type_text, parse_mode)}"
                    )
                index += 1

            if link:
                caption = f"{caption}\n{self._format_detail_link(link, parse_mode)}"

            reply_markup = None
            if buttons:
                reply_markup = self._create_inline_keyboard(buttons)

            if original_message_id and original_chat_id:
                return self.__edit_message(
                    original_chat_id,
                    original_message_id,
                    caption,
                    buttons,
                    image,
                    parse_mode=parse_mode,
                )
            else:
                return self.__send_request(
                    userid=chat_id,
                    image=image,
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )

        except Exception as msg_e:
            logger.error(f"发送消息失败：{msg_e}")
            return False
        finally:
            self._stop_typing_if_needed(chat_id, stop_typing)

    def send_torrents_msg(
            self,
            torrents: List[Context],
            userid: Optional[str] = None,
            title: Optional[str] = None,
            link: Optional[str] = None,
            buttons: Optional[List[List[dict[str, Any]]]] = None,
            original_message_id: Optional[int] = None,
            original_chat_id: Optional[str] = None,
            stop_typing: bool = False,
            parse_mode: Optional[str] = None,
    ) -> Optional[Union[bool, JsonDict]]:
        """
        发送或编辑种子候选列表。

        :param torrents: 按原顺序展示的候选项；种子上下文需含媒体信息，空列表按失败处理
        :param userid: 接收用户标识；默认使用该用户最近会话，无记录时用用户标识，未指定时用默认会话
        :param title: 列表标题
        :param link: 列表末尾链接；当前实现会被最后一项种子详情地址覆盖
        :param buttons: 按行排列的链接或回调按钮
        :param original_message_id: 与原会话同时提供时编辑该消息
        :param original_chat_id: 显式原会话标识，优先于普通用户映射
        :param stop_typing: 是否在操作收尾时请求停止输入状态，默认 False
        :param parse_mode: 消息格式，默认 MarkdownV2；支持 HTML 和 plain 纯文本
        :return: 编辑时返回成功布尔值，发送时返回最后一条 Bot API 消息字典；异常返回 False，配置缺失或发送无结果返回 None
        """
        if not self._telegram_token or not self._telegram_chat_id:
            return None

        # 资源列表发送结束后是否停止输入状态，由 stop_typing 明确控制。
        chat_id = self._determine_target_chat_id(userid, original_chat_id)
        parse_mode = self._normalize_parse_mode(parse_mode)
        try:
            index = 1
            caption = self._format_title(title, parse_mode) or ""
            if not torrents:
                return False
            first_media = torrents[0].media_info
            image = first_media.get_message_image() if first_media else None
            for context in torrents:
                torrent = context.torrent_info
                if not torrent:
                    continue
                site_name = torrent.site_name or ""
                meta = MetaInfo(torrent.title or "", torrent.description)
                link = torrent.page_url or ""
                title = (
                    f"{meta.season_episode} "
                    f"{meta.resource_term} "
                    f"{meta.video_term} "
                    f"{meta.release_group}"
                )
                title = re.sub(r"\s+", " ", title).strip()
                free = torrent.volume_factor
                seeder = f"{torrent.seeders}↑"
                site_name = (
                    html_utils.escape(site_name)
                    if self._is_html_parse_mode(parse_mode)
                    else site_name
                )
                title_link = self._format_link(title, link, parse_mode)
                caption = (
                    f"{caption}\n{index}.【{site_name}】{title_link} "
                    f"{size_tools.format_compact_size(torrent.size)} {free} {seeder}"
                )
                index += 1

            if link:
                caption = f"{caption}\n{self._format_detail_link(link, parse_mode)}"

            reply_markup = None
            if buttons:
                reply_markup = self._create_inline_keyboard(buttons)

            if original_message_id and original_chat_id:
                # 原消息和会话同时存在时替换该消息内容
                return self.__edit_message(
                    original_chat_id,
                    original_message_id,
                    caption,
                    buttons,
                    image,
                    parse_mode=parse_mode,
                )
            else:
                return self.__send_request(
                    userid=chat_id,
                    image=image,
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )

        except Exception as msg_e:
            logger.error(f"发送消息失败：{msg_e}")
            return False
        finally:
            self._stop_typing_if_needed(chat_id, stop_typing)

    @staticmethod
    def _create_inline_keyboard(buttons: List[List[dict[str, Any]]]) -> JsonDict:
        """
        创建内联键盘，忽略没有有效链接或回调数据的按钮及空行。

        :param buttons: 按行排列的按钮配置；非空字符串 URL 优先，否则使用非空字符串回调数据
        :return: Bot API InlineKeyboardMarkup 结构；没有有效按钮时包含空键盘
        """
        keyboard = []
        for row in buttons:
            button_row = []
            for button in row:
                url = button.get("url")
                callback_data = button.get("callback_data")
                if isinstance(url, str) and url.strip():
                    btn = {"text": button["text"], "url": url}
                elif isinstance(callback_data, str) and callback_data:
                    btn = {"text": button["text"], "callback_data": callback_data}
                else:
                    logger.warning("Telegram 按钮缺少有效 URL 或回调数据，已忽略")
                    continue
                button_row.append(btn)
            if button_row:
                keyboard.append(button_row)
        return {"inline_keyboard": keyboard}

    def answer_callback_query(
            self,
            callback_query_id: int,
            text: Optional[str] = None,
            show_alert: bool = False,
    ) -> Optional[bool]:
        """
        确认按钮回调并可显示提示。

        :param callback_query_id: Telegram 按钮回调标识
        :param text: 可选提示正文
        :param show_alert: 是否以弹窗展示提示
        :return: 调用未抛错时返回 True；异常返回 False；客户端未初始化返回 None
        """
        if not self._bot:
            return None

        try:
            self._api.answer_callback_query(
                callback_query_id, text=text, show_alert=show_alert
            )
            return True
        except Exception as e:
            logger.error(f"回应回调查询失败：{str(e)}")
            return False

    def delete_msg(
            self, message_id: int, chat_id: Optional[int] = None
    ) -> Optional[bool]:
        """
        删除指定会话或默认会话中的消息。

        :param message_id: 原消息标识
        :param chat_id: 目标会话；为空时使用默认会话
        :return: 删除成功返回 True，失败返回 False；配置缺失返回 None
        """
        if not self._telegram_token or not self._telegram_chat_id:
            return None

        target_chat_id: Union[str, int]
        try:
            if chat_id:
                target_chat_id = chat_id
            else:
                target_chat_id = self._telegram_chat_id

            result = self._api.delete_message(
                chat_id=target_chat_id, message_id=int(message_id)
            )
            if result:
                logger.info(
                    f"成功删除Telegram消息: chat_id={target_chat_id}, message_id={message_id}"
                )
                return True
            else:
                logger.error(
                    f"删除Telegram消息失败: chat_id={target_chat_id}, message_id={message_id}"
                )
                return False
        except Exception as e:
            logger.error(f"删除Telegram消息异常: {str(e)}")
            return False

    def edit_msg(
            self,
            chat_id: Union[str, int],
            message_id: Union[str, int],
            text: str,
            title: Optional[str] = None,
            buttons: Optional[List[List[dict[str, Any]]]] = None,
            stop_typing: bool = False,
            parse_mode: Optional[str] = None,
            rich_message: Optional[str] = None,
            wait_for_interval: bool = False,
    ) -> Optional[bool]:
        """
        限频更新原消息，富文本失败时尝试纯文本回退。

        :param chat_id: 原会话标识
        :param message_id: 原消息标识
        :param text: 完整的新正文
        :param title: 可选新标题
        :param buttons: 新的内联按钮布局
        :param stop_typing: 是否在操作收尾时请求停止输入状态，默认 False
        :param parse_mode: 消息格式，默认 MarkdownV2；支持 HTML 和 plain 纯文本
        :param rich_message: 可选富文本正文；非空时覆盖普通标题和正文
        :param wait_for_interval: 是否等待消息级编辑间隔后再发送；429 冷却不会等待
        :return: 更新成功或内容未变化返回 True；限流、空内容或失败返回 False；客户端缺失返回 None
        """
        if not self._bot:
            return None

        if not self._reserve_edit(chat_id, message_id, wait_for_interval=wait_for_interval):
            return False

        parse_mode = self._normalize_parse_mode(parse_mode)
        try:
            if rich_message:
                reply_markup = (
                    self._create_inline_keyboard(buttons) if buttons else None
                )
                result = self.__edit_rich_message(
                    chat_id=chat_id,
                    message_id=message_id,
                    rich_message=rich_message,
                    reply_markup=reply_markup,
                )
                if result:
                    self._mark_edit_succeeded(chat_id, message_id)
                    return True
                if self._is_chat_rate_limited(chat_id):
                    return False
                # Rich Message 编辑失败时降级为纯文本覆盖，避免回复停留在旧内容。
                logger.warning(
                    f"Telegram Rich Message 编辑失败，降级为纯文本：chat_id={chat_id}"
                )
                fallback_result = self.__edit_plain_fallback(
                    chat_id=str(chat_id),
                    message_id=int(message_id),
                    rich_message=rich_message,
                    reply_markup=reply_markup,
                )
                if fallback_result:
                    self._mark_edit_succeeded(chat_id, message_id)
                return fallback_result

            if title:
                bold_title = self._format_title(title, parse_mode)
                caption = f"{bold_title}\n{text}" if text else (bold_title or "")
            elif text:
                caption = text
            else:
                return False

            edit_result = self.__edit_message(
                chat_id=str(chat_id),
                message_id=int(message_id),
                text=caption,
                buttons=buttons,
                parse_mode=parse_mode,
            )
            if edit_result:
                self._mark_edit_succeeded(chat_id, message_id)
            return edit_result
        except Exception as e:
            if self._is_rate_limit_error(e):
                self._mark_rate_limit(chat_id, e)
            logger.error(f"编辑Telegram消息异常: {str(e)}")
            return False
        finally:
            self._stop_typing_if_needed(chat_id, stop_typing)

    @staticmethod
    def __is_no_text_edit_error(err: Exception) -> bool:
        """
        判断 Telegram 是否因为原消息没有 text 字段而拒绝文本编辑。
        """
        return "there is no text in the message to edit" in str(err).lower()

    @staticmethod
    def __is_message_not_modified_error(err: Exception) -> bool:
        """
        判断 Telegram 是否因为消息内容未变化而拒绝编辑。
        """
        return "message is not modified" in str(err).lower()

    @staticmethod
    def __is_http_url_content_error(err: Exception) -> bool:
        """
        判断 Telegram 是否因为无法获取远端图片 URL 而拒绝编辑。
        """
        return "failed to get http url content" in str(err).lower()

    def __edit_message_text_or_caption(
            self,
            chat_id: str,
            message_id: int,
            text: str,
            reply_markup: Optional[JsonDict] = None,
            disable_web_page_preview: Optional[bool] = None,
            parse_mode: Optional[str] = None,
    ) -> bool:
        """
        编辑 Telegram 文本消息，原消息无文本时回退为 caption 编辑。
        """
        prepared_text = self._prepare_text(text, parse_mode)
        edit_text_kwargs: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": prepared_text,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup,
        }
        if disable_web_page_preview is not None:
            edit_text_kwargs["disable_web_page_preview"] = (
                disable_web_page_preview
            )
        try:
            self._api.edit_message_text(**edit_text_kwargs)
        except Exception as err:
            if self.__is_message_not_modified_error(err):
                logger.debug(f"Telegram消息内容未变化，跳过编辑：{str(err)}")
                return True
            if not self.__is_no_text_edit_error(err):
                raise
            try:
                self._api.edit_message_caption(
                    chat_id=chat_id,
                    message_id=message_id,
                    caption=prepared_text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
            except Exception as caption_err:
                if self.__is_message_not_modified_error(caption_err):
                    logger.debug(
                        f"Telegram消息内容未变化，跳过编辑：{str(caption_err)}"
                    )
                    return True
                raise
        return True

    def __edit_message(
            self,
            chat_id: str,
            message_id: int,
            text: str,
            buttons: Optional[List[List[dict[str, Any]]]] = None,
            image: Optional[str] = None,
            disable_web_page_preview: Optional[bool] = None,
            parse_mode: Optional[str] = None,
    ) -> Optional[bool]:
        """
        编辑已发送的消息。

        :param chat_id: 聊天ID
        :param message_id: 消息ID
        :param text: 新的消息内容
        :param buttons: 按钮列表
        :param image: 图片URL或路径
        :param disable_web_page_preview: 是否禁用链接预览（仅纯文本编辑时生效）
        :param parse_mode: Telegram 消息格式类型，默认 MarkdownV2，可传 HTML
        :return: 编辑是否成功
        """
        if not self._bot:
            return None

        parse_mode = self._normalize_parse_mode(parse_mode)
        try:
            reply_markup = None
            if buttons:
                reply_markup = self._create_inline_keyboard(buttons)

            if image:
                media = {
                    "type": "photo",
                    "media": image,
                    "caption": self._prepare_text(text, parse_mode),
                }
                if parse_mode:
                    media["parse_mode"] = parse_mode
                self._api.edit_message_media(
                    chat_id=chat_id,
                    message_id=message_id,
                    media=media,
                    reply_markup=reply_markup,
                )
            else:
                return self.__edit_message_text_or_caption(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                    disable_web_page_preview=disable_web_page_preview,
                    parse_mode=parse_mode,
                )
            return True
        except Exception as e:
            if self._is_rate_limit_error(e):
                self._mark_rate_limit(chat_id, e)
                return False
            if self.__is_message_not_modified_error(e):
                logger.debug(f"Telegram消息内容未变化，跳过编辑：{str(e)}")
                return True
            if image and self.__is_http_url_content_error(e):
                logger.warning(
                    f"Telegram图片编辑失败，降级为文本编辑：{str(e)}"
                )
                try:
                    return self.__edit_message_text_or_caption(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        reply_markup=reply_markup,
                        disable_web_page_preview=disable_web_page_preview,
                        parse_mode=parse_mode,
                    )
                except Exception as fallback_err:
                    e = fallback_err
            logger.error(f"编辑消息失败：{str(e)}")
            return False

    @staticmethod
    def _build_rich_message_chunks(
            rich_message: str,
    ) -> List[JsonDict]:
        """
        将 GitHub 风格 Markdown 转换并拆分为 Telegram Rich Message。

        :param rich_message: 完整的 Rich Markdown 正文
        :return: 满足 Telegram 字节数和块数量限制的 InputRichMessage 结构
        """
        converted = richify(rich_message, mode="html")
        return [
            {
                key: value
                for key, value in chunk.to_dict().items()
                if key in _RICH_MESSAGE_FIELDS and value is not None
            }
            for chunk in split_rich(converted)
        ]

    def __edit_rich_message(
            self,
            chat_id: Union[str, int],
            message_id: Union[str, int],
            rich_message: str,
            reply_markup: Optional[JsonDict] = None,
    ) -> bool:
        """
        编辑 Telegram Rich Message。

        :param chat_id: 聊天 ID
        :param message_id: 原消息 ID
        :param rich_message: 完整的 Rich Markdown 正文
        :param reply_markup: 内联键盘
        :return: 编辑是否成功
        """
        chunks = self._build_rich_message_chunks(rich_message)
        if len(chunks) != 1:
            logger.warning("Telegram Rich Message 超出单条限制，无法编辑原消息")
            return False
        try:
            self._api.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=None,
                rich_message=chunks[0],
                reply_markup=reply_markup,
            )
            return True
        except Exception as err:
            if self.__is_message_not_modified_error(err):
                logger.debug(f"Telegram消息内容未变化，跳过编辑：{str(err)}")
                return True
            if self._is_rate_limit_error(err):
                self._mark_rate_limit(chat_id, err)
                return False
            logger.error(f"编辑 Telegram Rich Message 失败：{str(err)}")
            return False

    def __edit_plain_fallback(
            self,
            chat_id: str,
            message_id: int,
            rich_message: str,
            reply_markup: Optional[JsonDict] = None,
    ) -> bool:
        """
        Rich Message 编辑失败后的降级出口，用纯文本覆盖原消息。

        ``parse_mode`` 显式传空字符串，避免 ``edit_message_text`` 回落到客户端默认的
        MarkdownV2——Rich Message 已经在那条解析路径上失败过一次。

        :param chat_id: 聊天 ID
        :param message_id: 原消息 ID
        :param rich_message: 原始 Rich Markdown 正文
        :param reply_markup: 内联键盘
        :return: 降级编辑是否成功
        """
        try:
            self._api.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=rich_message,
                parse_mode=TELEGRAM_PARSE_MODE_PLAIN,
                reply_markup=reply_markup,
            )
            return True
        except Exception as err:
            if self.__is_message_not_modified_error(err):
                logger.debug(f"Telegram消息内容未变化，跳过编辑：{str(err)}")
                return True
            if self._is_rate_limit_error(err):
                self._mark_rate_limit(chat_id, err)
                return False
            logger.error(f"Telegram Rich Message 降级为纯文本编辑仍失败：{str(err)}")
            return False

    @retry(RetryException, logger=logger)
    def __send_rich_message(
            self,
            chat_id: Union[str, int],
            rich_message: str,
            reply_markup: Optional[JsonDict] = None,
            reply_to_message_id: Optional[Union[str, int]] = None,
    ) -> Any:
        """
        发送 Telegram Rich Message，超限内容自动拆分为多条。

        :param chat_id: 目标聊天 ID
        :param rich_message: 完整的 Rich Markdown 正文
        :param reply_markup: 首条消息携带的键盘
        :param reply_to_message_id: 首条消息回复的原消息 ID
        :return: 最后一条已发送的 Telegram 消息
        :raises NonRetryableTelegramError: Telegram 以 400 拒绝该内容，不做退避重试
        :raises RetryException: 网络异常或 5xx，可重试
        """
        chunks = self._build_rich_message_chunks(rich_message)
        reply_parameters = (
            {"message_id": int(reply_to_message_id)}
            if reply_to_message_id is not None
            else None
        )
        sent = None
        try:
            for index, chunk in enumerate(chunks):
                sent = self._api.send_rich_message(
                    chat_id=chat_id,
                    rich_message=chunk,
                    reply_markup=reply_markup if index == 0 else None,
                    reply_parameters=reply_parameters if index == 0 else None,
                    **self._topic_kwargs(chat_id),
                )
            return sent
        except Exception as err:
            # 保留 Telegram 返回的原始描述（错误码、description），否则日志只剩
            # “发送失败”四个字，无法定位是内容非法还是接口不匹配。
            if _is_non_retryable_api_error(err):
                if self._is_rate_limit_error(err):
                    self._mark_rate_limit(chat_id, err, operation="发送")
                raise NonRetryableTelegramError(
                    f"发送 Telegram Rich Message 失败：{err}"
                ) from err
            raise RetryException(
                f"发送 Telegram Rich Message 失败：{err}"
            ) from err

    def __send_plain_fallback(
            self,
            chat_id: Union[str, int],
            rich_message: str,
            reply_markup: Optional[JsonDict] = None,
    ) -> Any:
        """
        Rich Message 发送失败后的降级出口，用纯文本重发同一条内容。

        Rich Message 依赖 Bot API 10.1 的 ``sendRichMessage``，内容里出现无法解析的
        片段或自建 Bot API 未升级该方法时都会整体失败。通知内容本身仍有价值，因此
        降级为纯文本，保证消息不丢失。

        ``parse_mode`` 必须显式传空字符串：``TelegramBotApi`` 在参数为 ``None`` 时会
        回落到客户端默认的 MarkdownV2，那正是 Rich Message 已经失败过的解析路径。

        :param chat_id: 目标聊天 ID
        :param rich_message: 原始 Rich Markdown 正文
        :param reply_markup: 内联键盘
        :return: 降级发送的结果，失败返回 None
        """
        # ``_topic_kwargs`` 只可能产出 message_thread_id，显式传参而不是 ** 展开：
        # ** 展开到 send_message 会被 mypy 匹配到第 5 个形参（disable_web_page_preview，
        # Optional[bool]），把 dict[str, int] 判为类型不兼容。
        topic_kwargs = self._topic_kwargs(chat_id)
        try:
            return self._api.send_message(
                chat_id=chat_id,
                text=rich_message,
                parse_mode=TELEGRAM_PARSE_MODE_PLAIN,
                reply_markup=reply_markup,
                message_thread_id=topic_kwargs.get("message_thread_id"),
            )
        except Exception as err:
            if self._is_rate_limit_error(err):
                self._mark_rate_limit(chat_id, err, operation="发送")
            logger.error(f"Telegram Rich Message 降级为纯文本发送仍失败：{err}")
            return None

    def __send_request(
            self,
            userid: Optional[str] = None,
            image: Optional[str] = "",
            caption: str = "",
            reply_markup: Optional[JsonDict] = None,
            disable_web_page_preview: Optional[bool] = None,
            parse_mode: Optional[str] = None,
            reply_to_message_id: Optional[int] = None,
    ) -> Optional[JsonDict]:
        """
        按内容长度和媒体类型拆分并发送普通消息。

        :param userid: 目标会话标识；为空时使用默认会话
        :param image: 可选封面地址，先下载为字节再发送
        :param caption: 已组合标题、正文和链接的完整内容
        :param reply_markup: 首条消息的内联键盘或强制回复结构
        :param disable_web_page_preview: 是否禁用文本链接预览
        :param parse_mode: 消息格式，默认 MarkdownV2；支持 HTML 和 plain 纯文本
        :param reply_to_message_id: 发送消息回复的原消息标识
        :return: 最后一条发送的 Bot API 消息；失败返回 None，已发送的分块不会撤回
        """
        parse_mode = self._normalize_parse_mode(parse_mode)
        kwargs: Dict[str, Any] = {
            "chat_id": userid or self._telegram_chat_id,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup,
        }
        kwargs.update(self._topic_kwargs(kwargs["chat_id"]))
        if reply_to_message_id:
            kwargs["reply_to_message_id"] = reply_to_message_id
        image_bytes = self.__process_image(image)

        try:
            # 图片消息的标题长度限制为1024，文本消息为4096
            caption_limit = 1024 if image_bytes else 4096
            if len(caption) < caption_limit:
                ret = self.__send_short_message(image_bytes, caption,
                                                disable_web_page_preview=disable_web_page_preview,
                                                **kwargs)
            elif (
                    self._is_plain_parse_mode(parse_mode)
                    or self._is_html_parse_mode(parse_mode)
            ):
                ret = self.__send_long_plain_message(
                    image_bytes,
                    caption,
                    caption_limit,
                    disable_web_page_preview=disable_web_page_preview,
                    **kwargs,
                )
            else:
                sent_idx: set[int] = set()
                ret = self.__send_long_message(image_bytes, caption, sent_idx,
                                               disable_web_page_preview=disable_web_page_preview,
                                               **kwargs)

            return ret
        except Exception as e:
            logger.error(f"发送Telegram消息失败: {e}")
            return None

    @staticmethod
    def __process_image(image_url: Optional[str]) -> Optional[bytes]:
        """
        处理图片URL，获取图片内容。
        """
        if not image_url:
            return None
        image = ImageHelper().fetch_image(image_url)
        if not image:
            logger.warn(f"图片获取失败: {image_url}，仅发送文本消息")
        return image

    @retry(RetryException, logger=logger)
    def __send_short_message(self, image: Optional[bytes], caption: str,
                             disable_web_page_preview: Optional[bool] = None, **kwargs: Any) -> Optional[JsonDict]:
        """
        发送短消息。
        """
        parse_mode = kwargs.get("parse_mode")
        try:
            if image:
                return self.__send_photo_or_text(
                    photo=image,
                    caption=self._prepare_text(caption, parse_mode),
                    disable_web_page_preview=disable_web_page_preview,
                    **kwargs,
                )
            else:
                return self._api.send_message(
                    text=self._prepare_text(caption, parse_mode),
                    disable_web_page_preview=disable_web_page_preview,
                    **kwargs
                )
        except Exception as err:
            if self._is_rate_limit_error(err):
                chat_id = kwargs.get("chat_id") or self._telegram_chat_id or ""
                self._mark_rate_limit(chat_id, err, operation="发送")
                raise NonRetryableTelegramError(
                    f"发送 Telegram {'图片' if image else '文本'}消息被限流：{err}"
                ) from err
            raise RetryException(f"发送{'图片' if image else '文本'}消息失败：{err}") from err

    def __send_photo_or_text(
            self, *, photo: Any, caption: Optional[str],
            disable_web_page_preview: Optional[bool] = None, **kwargs: Any,
    ) -> Optional[JsonDict]:
        """
        海报上传失败立即降级为同一正文，保留会话、话题和按钮；429 不追加请求。
        """
        try:
            return self._api.send_photo(photo=photo, caption=caption, **kwargs)
        except Exception as error:
            if self._is_rate_limit_error(error) or not caption:
                raise
            logger.warning(f"Telegram 海报发送失败，改为发送文本：{error}")
        return self._api.send_message(
            text=caption, disable_web_page_preview=disable_web_page_preview, **kwargs,
        )

    @retry(RetryException, logger=logger)
    def __send_long_plain_message(
            self,
            image: Optional[bytes],
            caption: str,
            caption_limit: int,
            disable_web_page_preview: Optional[bool] = None,
            **kwargs: Any,
    ) -> Optional[JsonDict]:
        """
        按可见长度拆分普通或HTML正文并发送各分块，HTML保持实体和标签完整。

        键盘仅附在第一块。非限流异常由装饰器重试整个方法，已成功的分块可能再次发送；
        限流时记录会话冷却并停止立即重试。

        :param image: 可选图片字节，仅用于第一块
        :param caption: 完整正文，HTML分块关闭并重新打开跨块标签
        :param caption_limit: 每块字符上限；图片说明通常为 1024，纯文本为 4096
        :param disable_web_page_preview: 是否关闭文本分块的链接预览
        :param kwargs: 目标会话、解析格式、话题、回复和键盘参数
        :return: 最后一条发送的 Bot API 消息
        """
        reply_markup = kwargs.pop("reply_markup", None)
        if kwargs.get("parse_mode") == TELEGRAM_PARSE_MODE_HTML:
            splitter = _HtmlMessageSplitter(caption_limit)
            splitter.feed(caption)
            chunks = splitter.finish()
        else:
            chunks = self._split_plain_text(caption, caption_limit)
        ret = None
        try:
            for index, chunk in enumerate(chunks):
                current_reply_markup = reply_markup if index == 0 else None
                if image and index == 0:
                    ret = self.__send_photo_or_text(
                        **kwargs,
                        photo=image,
                        caption=chunk,
                        reply_markup=current_reply_markup,
                        disable_web_page_preview=disable_web_page_preview,
                    )
                    continue
                msg_kwargs = dict(**kwargs)
                if disable_web_page_preview is not None:
                    msg_kwargs["disable_web_page_preview"] = disable_web_page_preview
                ret = self._api.send_message(
                    **msg_kwargs,
                    text=chunk,
                    reply_markup=current_reply_markup,
                )
            return ret
        except Exception as err:
            if self._is_rate_limit_error(err):
                chat_id = kwargs.get("chat_id") or self._telegram_chat_id or ""
                self._mark_rate_limit(chat_id, err, operation="发送")
                raise NonRetryableTelegramError(
                    f"发送 Telegram 长消息被限流：{err}"
                ) from err
            raise RetryException("长消息发送失败") from err

    @retry(RetryException, logger=logger)
    def __send_long_message(
            self, image: Optional[bytes], caption: str, sent_idx: set[int],
            disable_web_page_preview: Optional[bool] = None, **kwargs: Any
    ) -> Optional[JsonDict]:
        """
        通过 Markdown 转换器拆分长消息，并跳过已经发送的分块。

        转换器可产生文字、图片或文件；键盘只附在第一个分块，失败重试不会撤回先前分块。

        :param image: 可选首块封面字节
        :param caption: 待转换的完整 Markdown 正文
        :param sent_idx: 已成功发送的分块下标集合，会原地更新并用于装饰器重试时去重
        :param disable_web_page_preview: 是否关闭文本分块的链接预览
        :param kwargs: 目标会话、解析格式、话题、回复和键盘参数
        :return: 本次发送的最后一条 Bot API 消息；无新分块时为 None
        """

        reply_markup = kwargs.pop("reply_markup", None)

        boxs: list[Text | File | Photo] = (
            ThreadHelper()
            .submit(lambda x: asyncio.run(telegramify(x)), caption)
            .result()
        )

        ret = None
        for i, item in enumerate(boxs):
            if i in sent_idx:
                # 跳过已发送消息
                continue

            try:
                current_reply_markup = reply_markup if i == 0 else None

                if item.content_type == ContentTypes.TEXT and (i != 0 or not image):
                    msg_kwargs = dict(**kwargs)
                    if disable_web_page_preview is not None:
                        msg_kwargs["disable_web_page_preview"] = disable_web_page_preview
                    ret = self._api.send_message(
                        **msg_kwargs,
                        text=self._telegramify_item_text(item),
                        reply_markup=current_reply_markup,
                    )

                elif item.content_type == ContentTypes.PHOTO or (image and i == 0):
                    ret = self.__send_photo_or_text(
                        **kwargs,
                        photo=(
                            getattr(item, "file_name", ""),
                            getattr(item, "file_data", image),
                        ),
                        caption=self._telegramify_item_caption(item),
                        reply_markup=current_reply_markup,
                        disable_web_page_preview=disable_web_page_preview,
                    )

                elif item.content_type == ContentTypes.FILE:
                    ret = self._api.send_document(
                        **kwargs,
                        document=(item.file_name, item.file_data),
                        caption=self._telegramify_item_caption(item),
                        reply_markup=current_reply_markup,
                    )

                sent_idx.add(i)

            except Exception as e:
                if self._is_rate_limit_error(e):
                    chat_id = kwargs.get("chat_id") or self._telegram_chat_id or ""
                    self._mark_rate_limit(chat_id, e, operation="发送")
                    raise NonRetryableTelegramError(
                        f"发送 Telegram 消息被限流：{e}"
                    ) from e
                try:
                    raise RetryException(f"消息 [{i + 1}/{len(boxs)}] 发送失败") from e
                except NameError:
                    raise

        return ret

    def register_commands(self, commands: Dict[str, dict[str, Any]]) -> None:
        """
        先清除旧菜单，再按输入顺序注册命令。

        :param commands: 以斜杠命令为键的映射，读取 description 作为显示说明；空映射不修改菜单
        """
        if not self._bot:
            return
        if commands:
            self._api.delete_my_commands()
            self._api.set_my_commands(
                commands=[
                    {"command": cmd[1:], "description": str(desc.get("description"))}
                    for cmd, desc in commands.items()
                ]
            )

    def delete_commands(self) -> None:
        """
        清除机器人菜单命令；客户端未初始化时不执行。
        """
        if not self._bot:
            return
        self._api.delete_my_commands()

    def stop(self) -> bool:
        """
        停止接收新输入状态任务，结束更新分发并关闭客户端。

        停止时最多等待消息处理 10 秒，不等待已经发出的长轮询请求返回。
        从消息处理线程自身调用时不等待该处理器，返回后不会再分发后续更新。

        :return: 所有输入状态任务收尾且更新分发可安全停止时返回 True，否则返回 False
        """
        converged = True
        with self._typing_lifecycle_lock:
            self._typing_accepting = False
            # 封口与 owner 快照处于同一临界区，停止后不会漏掉并发新增任务。
            for chat_id in list(self._typing_tasks.keys()):
                if not self._stop_typing_task(chat_id):
                    converged = False

        bot = self._bot
        polling_thread = self._polling_thread
        if bot:
            # 在 handler 内触发的 stop 不等待自身：handler 返回后轮询循环即看到停止标记。
            in_polling_thread = polling_thread is threading.current_thread()
            dispatch_idle = bot.stop_polling(
                0 if in_polling_thread else self._shutdown_timeout_seconds
            )
            if not dispatch_idle and not in_polling_thread:
                # 保留 owner，调用方重试 stop 时继续等待同一个 handler。
                logger.error("Telegram 消息处理未在关闭预算内结束")
                return False
            # 进行中的 getUpdates 不再等待；它返回后只会丢弃结果并退出线程。
            bot.close()
        self._polling_thread = None
        self._bot = None
        logger.info("Telegram消息接收服务已停止")
        return converged
