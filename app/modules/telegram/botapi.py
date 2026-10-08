"""
Telegram Bot API 精简客户端。

只实现 MoviePilot 用到的 Bot API 方法和 getUpdates 长轮询，请求统一经 RequestUtils 发出。
方法名与参数沿用 pyTelegramBotAPI 的命名，入参中的键盘、回复参数等结构直接使用 Bot API
的 JSON 字典；返回值是 Bot API 响应里的 ``result`` 原始结构（Message 为 dict）。
"""

import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Union, cast
from urllib.parse import urljoin

from app.adapters.network.http import RequestUtils
from app.runtime.log import logger

DEFAULT_API_URL = "https://api.telegram.org"

# 普通请求的连接/读取超时，与原 SDK 默认值保持一致。
_CONNECT_TIMEOUT_SECONDS = 15
_READ_TIMEOUT_SECONDS = 30
# 持续失败仍须在默认日志级别可见，同时避免网络抖动时每次重试都刷屏。
_POLLING_WARNING_INTERVAL_SECONDS = 60
# 轮询只接收宿主处理的两类更新，其余更新类型不进入 getUpdates 结果。
_ALLOWED_UPDATES = ["message", "callback_query"]
# 群聊/私聊中宿主会转发的消息内容类型，与原 SDK message_handler 注册的类型一致。
FORWARDED_CONTENT_TYPES = (
    "text", "photo", "video", "document", "animation",
    "audio", "voice", "sticker", "video_note",
)

ChatId = Union[str, int]
# Bot API 请求参数与响应对象（Message、User 等）的 JSON 结构。
JsonDict = Dict[str, Any]


class TelegramApiError(Exception):
    """
    Telegram Bot API 调用失败。

    ``str(err)`` 包含 Telegram 返回的 description，调用方据此识别
    "message is not modified" 等可忽略错误。
    """

    def __init__(self, method: str, description: str, error_code: Optional[int] = None):
        """保留 API 错误码和描述，供调用方区分业务拒绝与网络失败。"""
        self.method = method
        self.description = description
        self.error_code = error_code
        super().__init__(
            f"Telegram API {method} 调用失败，错误码：{error_code}，描述：{description}"
        )


class TelegramNetworkError(Exception):
    """
    Telegram Bot API 请求未得到响应（连接失败、超时等）。

    requests 的异常文本包含完整请求地址，而地址路径里有 Bot token；这里只保留脱敏后的
    描述，并切断异常链，避免 token 经日志或 traceback 泄露。
    """

    def __init__(self, method: str, description: str):
        """封装请求层已经脱敏的网络错误描述。"""
        self.method = method
        self.description = description
        super().__init__(f"Telegram API {method} 请求失败：{description}")


class TelegramBotApi:
    """
    单个 Bot token 的 Bot API 客户端，持有一个复用连接的 HTTP 会话。

    会话在发送线程、typing 线程和轮询线程间共享；requests 连接池本身线程安全，
    只有 POST 且不会自动重放，避免网络抖动时重复发送消息。
    """

    def __init__(
            self,
            token: str,
            api_url: Optional[str] = None,
            proxy: Optional[JsonDict] = None,
            parse_mode: Optional[str] = None,
    ):
        """
        :param token: Bot token
        :param api_url: 自建 Bot API 服务地址，为空时使用官方地址
        :param proxy: requests 代理配置，仅在使用官方地址时生效
        :param parse_mode: parse_mode 参数为 None 时使用的默认格式
        """
        self._token = token
        base_url = api_url or DEFAULT_API_URL
        # 与历史实现一致：自定义地址只取协议和主机，路径固定为 /bot<token>/<method>。
        self._api_base = urljoin(base_url, f"/bot{token}/")
        self._file_base = urljoin(base_url, f"/file/bot{token}/")
        # 自定义地址通常是内网自建服务，不走全局代理。
        self._proxy = None if api_url else proxy
        self._default_parse_mode = parse_mode
        self._http = RequestUtils(
            headers={"Accept": "application/json"},
            proxies=self._proxy,
            use_session=True,
            # RequestUtils 默认沿用旧宿主的不校验证书行为；Bot token 随请求发送，必须校验。
            verify=True,
        )
        self._polling_stop = threading.Event()
        # 分发临界区：持锁期间检查停止标记并执行 handler，stop 取得该锁即证明不会再分发。
        self._dispatch_lock = threading.Lock()

    @property
    def proxy(self) -> Optional[JsonDict]:
        """本客户端实际使用的代理配置。"""
        return self._proxy

    def file_url(self, file_path: str) -> str:
        """拼接 getFile 返回路径对应的下载地址（包含 token，不可写入日志）。"""
        return f"{self._file_base}{file_path}"

    def close(self) -> None:
        """关闭 HTTP 会话。"""
        self._http.close()

    # ---------------------------------------------------------------- 请求层

    @staticmethod
    def _encode_params(params: Dict[str, Any]) -> Dict[str, Any]:
        """丢弃 None 值，并按 Bot API 约定把嵌套结构编码为 JSON 字符串。"""
        encoded: Dict[str, Any] = {}
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                encoded[key] = json.dumps(value, ensure_ascii=False)
            elif isinstance(value, bool):
                encoded[key] = "true" if value else "false"
            else:
                encoded[key] = value
        return encoded

    def call(
            self,
            method: str,
            params: Optional[Dict[str, Any]] = None,
            files: Optional[Dict[str, Any]] = None,
            read_timeout: float = _READ_TIMEOUT_SECONDS,
    ) -> Any:
        """
        调用 Bot API 方法并返回 ``result``。

        :raises TelegramApiError: Telegram 返回 ok=false 或非 JSON 响应
        :raises TelegramNetworkError: 网络失败，描述已去除 token
        """
        try:
            response = self._http.post_res(
                f"{self._api_base}{method}",
                data=self._encode_params(params or {}),
                files=files,
                timeout=(_CONNECT_TIMEOUT_SECONDS, read_timeout),
                raise_exception=True,
            )
        except Exception as err:  # RequestUtils 在 raise_exception=True 时抛出 requests 异常
            description = f"{type(err).__name__}: {err}".replace(self._token, "<token>")
            raise TelegramNetworkError(method, description) from None
        if response is None:
            raise TelegramNetworkError(method, "未收到响应")
        try:
            payload = response.json()
        except ValueError:
            raise TelegramApiError(
                method, f"非 JSON 响应（HTTP {response.status_code}）", response.status_code
            ) from None
        if not payload.get("ok"):
            raise TelegramApiError(
                method, str(payload.get("description")), payload.get("error_code")
            )
        return payload.get("result")

    def _call_dict(self, method: str, *args: Any, **kwargs: Any) -> JsonDict:
        """调用返回对象（Message、User、File 等）的方法。"""
        return cast(JsonDict, self.call(method, *args, **kwargs))

    def _call_bool(self, method: str, *args: Any, **kwargs: Any) -> bool:
        """调用返回 True 的方法。"""
        return bool(self.call(method, *args, **kwargs))

    def _call_list(self, method: str, *args: Any, **kwargs: Any) -> List[JsonDict]:
        """调用返回对象数组的方法。"""
        return cast(List[JsonDict], self.call(method, *args, **kwargs) or [])

    def _parse_mode(self, parse_mode: Optional[str]) -> Optional[str]:
        """None 使用默认格式，空字符串表示纯文本，不传 parse_mode。"""
        resolved = self._default_parse_mode if parse_mode is None else parse_mode
        return resolved or None

    @staticmethod
    def _reply_parameters(reply_to_message_id: Optional[int]) -> Optional[JsonDict]:
        """旧式 reply_to_message_id 转为 Bot API 7.0 起的 reply_parameters。"""
        if not reply_to_message_id:
            return None
        return {"message_id": int(reply_to_message_id)}

    @staticmethod
    def _link_preview_options(disable_web_page_preview: Optional[bool]) -> Optional[JsonDict]:
        """旧式 disable_web_page_preview 转为 link_preview_options。"""
        if disable_web_page_preview is None:
            return None
        return {"is_disabled": disable_web_page_preview}

    @staticmethod
    def _input_file(field: str, value: Any, params: Dict[str, Any], files: Dict[str, Any]) -> None:
        """
        把 photo/document/voice 参数放到正确位置。

        字符串是 file_id 或 URL，走普通参数；bytes、文件对象或 ``(文件名, 内容)`` 走 multipart。
        """
        if isinstance(value, str):
            params[field] = value
        elif isinstance(value, tuple):
            name, content = value
            files[field] = (name or field, content)
        else:
            files[field] = value

    # ------------------------------------------------------------- Bot API 方法

    def get_me(self) -> JsonDict:
        """获取 Bot 自身信息。"""
        return self._call_dict("getMe")

    def get_file(self, file_id: str) -> JsonDict:
        """获取文件信息，返回值含 file_path。"""
        return self._call_dict("getFile", {"file_id": file_id})

    def send_message(
            self,
            chat_id: ChatId,
            text: Optional[str],
            parse_mode: Optional[str] = None,
            reply_markup: Optional[JsonDict] = None,
            disable_web_page_preview: Optional[bool] = None,
            reply_to_message_id: Optional[int] = None,
            message_thread_id: Optional[int] = None,
    ) -> JsonDict:
        """发送文本消息。"""
        return self._call_dict("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": self._parse_mode(parse_mode),
            "reply_markup": reply_markup,
            "link_preview_options": self._link_preview_options(disable_web_page_preview),
            "reply_parameters": self._reply_parameters(reply_to_message_id),
            "message_thread_id": message_thread_id,
        })

    def _send_media(
            self,
            method: str,
            field: str,
            chat_id: ChatId,
            media: Any,
            caption: Optional[str] = None,
            parse_mode: Optional[str] = None,
            reply_markup: Optional[JsonDict] = None,
            reply_to_message_id: Optional[int] = None,
            message_thread_id: Optional[int] = None,
    ) -> JsonDict:
        """发送单个图片、文件或语音，按类型选择 URL/file_id 或 multipart 上传。"""
        params: Dict[str, Any] = {
            "chat_id": chat_id,
            "caption": caption,
            "parse_mode": self._parse_mode(parse_mode),
            "reply_markup": reply_markup,
            "reply_parameters": self._reply_parameters(reply_to_message_id),
            "message_thread_id": message_thread_id,
        }
        files: Dict[str, Any] = {}
        self._input_file(field, media, params, files)
        return self._call_dict(method, params, files=files or None)

    def send_photo(self, chat_id: ChatId, photo: Any, **kwargs: Any) -> JsonDict:
        """发送图片，参数见 ``_send_media``。"""
        return self._send_media("sendPhoto", "photo", chat_id, photo, **kwargs)

    def send_document(self, chat_id: ChatId, document: Any, **kwargs: Any) -> JsonDict:
        """发送文件，参数见 ``_send_media``。"""
        return self._send_media("sendDocument", "document", chat_id, document, **kwargs)

    def send_voice(self, chat_id: ChatId, voice: Any, **kwargs: Any) -> JsonDict:
        """发送语音，参数见 ``_send_media``。"""
        return self._send_media("sendVoice", "voice", chat_id, voice, **kwargs)

    def send_rich_message(
            self,
            chat_id: ChatId,
            rich_message: JsonDict,
            reply_markup: Optional[JsonDict] = None,
            reply_parameters: Optional[JsonDict] = None,
            message_thread_id: Optional[int] = None,
    ) -> JsonDict:
        """发送 Rich Message，rich_message 为 InputRichMessage 的 JSON 结构。"""
        return self._call_dict("sendRichMessage", {
            "chat_id": chat_id,
            "rich_message": rich_message,
            "reply_markup": reply_markup,
            "reply_parameters": reply_parameters,
            "message_thread_id": message_thread_id,
        })

    def send_chat_action(self, chat_id: ChatId, action: str) -> bool:
        """发送 typing 等聊天状态。"""
        return self._call_bool("sendChatAction", {"chat_id": chat_id, "action": action})

    def edit_message_text(
            self,
            chat_id: ChatId,
            message_id: int,
            text: Optional[str] = None,
            parse_mode: Optional[str] = None,
            reply_markup: Optional[JsonDict] = None,
            disable_web_page_preview: Optional[bool] = None,
            rich_message: Optional[JsonDict] = None,
    ) -> Any:
        """编辑文本消息；传 rich_message 时替换为 Rich Message 且不带 parse_mode。"""
        return self.call("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "rich_message": rich_message,
            "parse_mode": None if rich_message else self._parse_mode(parse_mode),
            "reply_markup": reply_markup,
            "link_preview_options": self._link_preview_options(disable_web_page_preview),
        })

    def edit_message_caption(
            self,
            chat_id: ChatId,
            message_id: int,
            caption: Optional[str] = None,
            parse_mode: Optional[str] = None,
            reply_markup: Optional[JsonDict] = None,
    ) -> Any:
        """编辑媒体消息的说明文字。"""
        return self.call("editMessageCaption", {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": caption,
            "parse_mode": self._parse_mode(parse_mode),
            "reply_markup": reply_markup,
        })

    def edit_message_media(
            self,
            chat_id: ChatId,
            message_id: int,
            media: JsonDict,
            reply_markup: Optional[JsonDict] = None,
    ) -> Any:
        """替换消息媒体，media 为 InputMedia 的 JSON 结构（媒体须为 URL 或 file_id）。"""
        return self.call("editMessageMedia", {
            "chat_id": chat_id,
            "message_id": message_id,
            "media": media,
            "reply_markup": reply_markup,
        })

    def delete_message(self, chat_id: ChatId, message_id: int) -> bool:
        """删除消息。"""
        return self._call_bool("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def answer_callback_query(
            self,
            callback_query_id: Union[str, int],
            text: Optional[str] = None,
            show_alert: Optional[bool] = None,
    ) -> bool:
        """回应按钮回调，结束客户端的加载状态。"""
        return self._call_bool("answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        })

    def set_my_commands(self, commands: List[JsonDict]) -> bool:
        """设置菜单命令，元素为 ``{"command": ..., "description": ...}``。"""
        return self._call_bool("setMyCommands", {"commands": commands})

    def delete_my_commands(self) -> bool:
        """清除菜单命令。"""
        return self._call_bool("deleteMyCommands")

    # ------------------------------------------------------------------ 轮询

    def get_updates(self, offset: Optional[int], timeout: int) -> List[JsonDict]:
        """长轮询获取更新，读取超时比服务端等待时间多留出网络余量。"""
        return self._call_list(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": _ALLOWED_UPDATES},
            read_timeout=timeout + _CONNECT_TIMEOUT_SECONDS,
        )

    def polling(
            self,
            handler: Callable[[JsonDict], None],
            long_polling_timeout: int,
            retry_max_seconds: int = 60,
    ) -> None:
        """
        在当前线程循环拉取更新并逐条交给 handler，直到 ``stop_polling`` 被调用。

        更新按 update_id 顺序串行处理；handler 抛错只记录日志，仍确认该更新，
        避免同一条坏消息被反复投递。网络或 API 失败按指数退避重试，首次失败立即
        警告，持续失败每分钟再警告一次；恢复时记录失败次数和持续时间。

        停止后仍在进行的 getUpdates 返回时，结果直接丢弃且不确认 offset，Telegram
        会把这些更新重新投递给下一个轮询者；新轮询者的 getUpdates 也会让 Telegram
        立即结束旧请求。
        """
        offset: Optional[int] = None
        failures = 0
        failure_started_at = 0.0
        next_warning_at = 0.0
        while not self._polling_stop.is_set():
            try:
                updates = self.get_updates(offset, long_polling_timeout)
            except Exception as err:
                if self._polling_stop.is_set():
                    return
                failures += 1
                now = time.monotonic()
                retry_seconds = min(3 * 2 ** (failures - 1), retry_max_seconds)
                if failures == 1:
                    failure_started_at = now
                    next_warning_at = now
                if now >= next_warning_at:
                    logger.warning(
                        f"Telegram消息拉取失败，将自动重试（连续{failures}次，"
                        f"已持续{int(now - failure_started_at)}秒，"
                        f"{retry_seconds}秒后重试）：{err}"
                    )
                    next_warning_at = now + _POLLING_WARNING_INTERVAL_SECONDS
                else:
                    logger.debug(f"Telegram消息拉取第{failures}次失败：{err}")
                self._polling_stop.wait(retry_seconds)
                continue
            if failures:
                logger.info(
                    f"Telegram消息拉取已恢复（此前连续{failures}次失败，"
                    f"持续{int(time.monotonic() - failure_started_at)}秒）"
                )
                failures = 0
            for update in updates or []:
                with self._dispatch_lock:
                    # 停止后不再分发，未确认的更新会在下次启动时重新投递。
                    if self._polling_stop.is_set():
                        return
                    offset = update["update_id"] + 1
                    try:
                        handler(update)
                    except Exception as err:
                        logger.error(f"处理Telegram更新失败：{err}")

    def stop_polling(self, timeout: float) -> bool:
        """
        请求轮询循环退出，并等待正在执行的 handler 结束。

        不等待进行中的长轮询请求：它返回后只会丢弃结果并退出，不再触发任何回调。

        :param timeout: 等待 handler 的最长秒数
        :return: 是否已确认之后不会再分发更新
        """
        self._polling_stop.set()
        if not self._dispatch_lock.acquire(timeout=max(timeout, 0)):
            return False
        self._dispatch_lock.release()
        return True
