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

    def __init__(
            self,
            method: str,
            description: str,
            error_code: Optional[int] = None,
            parameters: Optional[JsonDict] = None,
    ):
        """
        保留业务错误码及附加参数，构造可诊断的异常说明。

        :param method: 失败的 Bot API 方法名
        :param description: Telegram 返回的业务错误描述
        :param error_code: 业务错误码；未知时为 None
        :param parameters: 可选重试秒数或迁移目标等响应参数；缺失时用空字典
        """
        self.method = method
        self.description = description
        self.error_code = error_code
        self.parameters = parameters or {}
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
        """
        封装已经脱敏的网络错误说明。

        :param method: 失败的 Bot API 方法名
        :param description: 调用方已移除机器人令牌的错误描述
        """
        self.method = method
        self.description = description
        super().__init__(f"Telegram API {method} 请求失败：{description}")


class TelegramBotApi:
    """
    管理单个机器人令牌的 Bot API 请求与长轮询会话。

    发送和输入状态使用共用会话，轮询使用独立会话以便连接故障后单独重建。
    调用方负责停止更新分发后关闭会话；普通请求失败不会在本客户端内自动重放。
    """

    def __init__(
            self,
            token: str,
            api_url: Optional[str] = None,
            proxy: Optional[JsonDict] = None,
            parse_mode: Optional[str] = None,
    ):
        """
        创建启用证书校验的发送会话和轮询停止状态。

        :param token: 机器人访问令牌，用于构造 API 和文件地址
        :param api_url: 可选自建服务地址，只取协议与主机；为空时使用官方地址
        :param proxy: requests 代理配置，仅对官方服务地址生效
        :param parse_mode: 调用时 parse_mode 为 None 所使用的默认解析格式
        """
        self._token = token
        base_url = api_url or DEFAULT_API_URL
        # 与历史实现一致：自定义地址只取协议和主机，路径固定为 /bot<token>/<method>。
        self._api_base = urljoin(base_url, f"/bot{token}/")
        self._file_base = urljoin(base_url, f"/file/bot{token}/")
        # 自定义地址通常是内网自建服务，不走全局代理。
        self._proxy = None if api_url else proxy
        self._default_parse_mode = parse_mode
        self._http = self._create_http()
        self._polling_http: Optional[RequestUtils] = None
        self._polling_stop = threading.Event()
        # 分发临界区：持锁期间检查停止标记并执行 handler，stop 取得该锁即证明不会再分发。
        self._dispatch_lock = threading.Lock()

    @property
    def proxy(self) -> Optional[JsonDict]:
        """
        提供当前客户端实际使用的代理配置。

        :return: 官方接口使用的代理字典；自建服务或未配置代理时为 None
        """
        return self._proxy

    def file_url(self, file_path: str) -> str:
        """
        构造文件下载地址，地址包含令牌，不得直接记录日志。

        :param file_path: getFile 返回的相对文件路径
        :return: 包含机器人令牌的完整下载地址
        """
        return f"{self._file_base}{file_path}"

    def _create_http(self) -> RequestUtils:
        """
        创建一个 Bot API 专用 HTTP 会话。
        """
        return RequestUtils(
            headers={"Accept": "application/json"},
            proxies=self._proxy,
            use_session=True,
            # RequestUtils 默认沿用旧宿主的不校验证书行为；Bot token 随请求发送，必须校验。
            verify=True,
        )

    def _get_polling_http(self) -> RequestUtils:
        """
        按需取得轮询专用会话，避免长轮询故障影响出站消息。
        """
        if self._polling_http is None:
            self._polling_http = self._create_http()
        return self._polling_http

    def _reset_polling_http(self) -> None:
        """
        重建轮询连接池；只由轮询线程调用。
        """
        old_http = self._polling_http
        new_http = self._create_http()
        self._polling_http = new_http
        if old_http is not None:
            old_http.close()

    def close(self) -> None:
        """
        关闭发送与轮询 HTTP 会话；调用方应先停止更新分发。
        """
        self._http.close()
        if self._polling_http is not None:
            self._polling_http.close()
            self._polling_http = None


    @staticmethod
    def _encode_params(params: Dict[str, Any]) -> Dict[str, Any]:
        """
        丢弃 None 值，并按 Bot API 约定把嵌套结构编码为 JSON 字符串。
        """
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
            _http: Optional[RequestUtils] = None,
    ) -> Any:
        """
        提交 Bot API 请求并提取原始 result 字段。

        :param method: Bot API 方法名，如 sendMessage
        :param params: 普通请求参数；None 值会移除，嵌套结构会编码为 JSON
        :param files: 可选 multipart 文件字段
        :param read_timeout: 响应读取超时，单位为秒，默认 30
        :param _http: 可选独立请求会话，轮询时使用；为空时使用发送会话
        :return: 原始 result 值，具体结构由 Bot API 方法决定
        :raises TelegramApiError: API 返回失败标记或无法解析为 JSON
        :raises TelegramNetworkError: 请求失败或无响应，异常描述已移除机器人令牌
        """
        request_http = _http or self._http
        try:
            response = request_http.post_res(
                f"{self._api_base}{method}",
                data=self._encode_params(params or {}),
                files=files,
                timeout=(_CONNECT_TIMEOUT_SECONDS, read_timeout),
                raise_exception=True,
            )
        except Exception as err:
            # 请求异常可能带有包含令牌的 URL，先脱敏再向上报告
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
                method,
                str(payload.get("description")),
                payload.get("error_code"),
                payload.get("parameters"),
            )
        return payload.get("result")

    def _call_dict(self, method: str, *args: Any, **kwargs: Any) -> JsonDict:
        """
        调用返回对象（Message、User、File 等）的方法。
        """
        return cast(JsonDict, self.call(method, *args, **kwargs))

    def _call_bool(self, method: str, *args: Any, **kwargs: Any) -> bool:
        """
        调用返回 True 的方法。
        """
        return bool(self.call(method, *args, **kwargs))

    def _call_list(self, method: str, *args: Any, **kwargs: Any) -> List[JsonDict]:
        """
        调用返回对象数组的方法。
        """
        return cast(List[JsonDict], self.call(method, *args, **kwargs) or [])

    def _parse_mode(self, parse_mode: Optional[str]) -> Optional[str]:
        """
        None 使用默认格式，空字符串表示纯文本，不传 parse_mode。
        """
        resolved = self._default_parse_mode if parse_mode is None else parse_mode
        return resolved or None

    @staticmethod
    def _reply_parameters(reply_to_message_id: Optional[int]) -> Optional[JsonDict]:
        """
        旧式 reply_to_message_id 转为 Bot API 7.0 起的 reply_parameters。
        """
        if not reply_to_message_id:
            return None
        return {"message_id": int(reply_to_message_id)}

    @staticmethod
    def _link_preview_options(disable_web_page_preview: Optional[bool]) -> Optional[JsonDict]:
        """
        旧式 disable_web_page_preview 转为 link_preview_options。
        """
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


    def get_me(self) -> JsonDict:
        """
        查询机器人自身身份。

        :return: Bot API User 字典
        """
        return self._call_dict("getMe")

    def get_file(self, file_id: str) -> JsonDict:
        """
        查询资源下载所需的文件信息。

        :param file_id: Telegram 分配的文件标识
        :return: 包含 file_path 等元数据的原始 File 字典
        """
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
        """
        发送文本及可选键盘、回复和话题信息。

        :param chat_id: 目标用户或会话标识，可为数字或 Telegram 支持的公开会话名称
        :param text: 待发送正文
        :param parse_mode: 正文解析格式；None 使用客户端默认值，空字符串表示纯文本
        :param reply_markup: 可选内联键盘或其他 Bot API 键盘结构
        :param disable_web_page_preview: 是否关闭网页预览；None 时不发送该配置
        :param reply_to_message_id: 可选被回复消息标识，会转换为 reply_parameters
        :param message_thread_id: 论坛群组中的可选话题标识
        :return: Telegram 返回的原始消息字典；API 拒绝或网络失败时抛出统一异常
        """
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
        """
        根据资源类型发送媒体引用或上传本地内容。

        :param method: 具体媒体发送 API 方法
        :param field: photo、document 或 voice 等请求字段名
        :param chat_id: 目标用户或会话标识，可为数字或 Telegram 支持的公开会话名称
        :param media: file_id、URL、字节、文件对象或文件名与内容的二元组
        :param caption: 可选媒体说明
        :param parse_mode: 正文解析格式；None 使用客户端默认值，空字符串表示纯文本
        :param reply_markup: 可选内联键盘或其他 Bot API 键盘结构
        :param reply_to_message_id: 可选被回复消息标识，会转换为 reply_parameters
        :param message_thread_id: 论坛群组中的可选话题标识
        :return: Telegram 返回的原始消息字典；API 拒绝或网络失败时抛出统一异常
        """
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
        """
        发送图片资源。

        :param chat_id: 目标用户或会话标识，可为数字或 Telegram 支持的公开会话名称
        :param photo: 资源 file_id、URL、字节、文件对象或文件名与内容的二元组
        :param kwargs: 传给 _send_media 的说明、格式、键盘、回复和话题参数
        :return: Telegram 返回的原始消息字典；API 拒绝或网络失败时抛出统一异常
        """
        return self._send_media("sendPhoto", "photo", chat_id, photo, **kwargs)

    def send_document(self, chat_id: ChatId, document: Any, **kwargs: Any) -> JsonDict:
        """
        发送文件资源。

        :param chat_id: 目标用户或会话标识，可为数字或 Telegram 支持的公开会话名称
        :param document: 资源 file_id、URL、字节、文件对象或文件名与内容的二元组
        :param kwargs: 传给 _send_media 的说明、格式、键盘、回复和话题参数
        :return: Telegram 返回的原始消息字典；API 拒绝或网络失败时抛出统一异常
        """
        return self._send_media("sendDocument", "document", chat_id, document, **kwargs)

    def send_voice(self, chat_id: ChatId, voice: Any, **kwargs: Any) -> JsonDict:
        """
        发送语音资源。

        :param chat_id: 目标用户或会话标识，可为数字或 Telegram 支持的公开会话名称
        :param voice: 资源 file_id、URL、字节、文件对象或文件名与内容的二元组
        :param kwargs: 传给 _send_media 的说明、格式、键盘、回复和话题参数
        :return: Telegram 返回的原始消息字典；API 拒绝或网络失败时抛出统一异常
        """
        return self._send_media("sendVoice", "voice", chat_id, voice, **kwargs)

    def send_rich_message(
            self,
            chat_id: ChatId,
            rich_message: JsonDict,
            reply_markup: Optional[JsonDict] = None,
            reply_parameters: Optional[JsonDict] = None,
            message_thread_id: Optional[int] = None,
    ) -> JsonDict:
        """
        发送结构化富文本消息。

        :param chat_id: 目标用户或会话标识，可为数字或 Telegram 支持的公开会话名称
        :param rich_message: InputRichMessage 字典，包含 HTML 或 Markdown 正文
        :param reply_markup: 可选内联键盘或其他 Bot API 键盘结构
        :param reply_parameters: 可选原消息回复参数，直接透传 Bot API
        :param message_thread_id: 论坛群组中的可选话题标识
        :return: Telegram 返回的原始消息字典；API 拒绝或网络失败时抛出统一异常
        """
        return self._call_dict("sendRichMessage", {
            "chat_id": chat_id,
            "rich_message": rich_message,
            "reply_markup": reply_markup,
            "reply_parameters": reply_parameters,
            "message_thread_id": message_thread_id,
        })

    def send_chat_action(self, chat_id: ChatId, action: str) -> bool:
        """
        发送输入、上传等临时会话状态。

        :param chat_id: 目标用户或会话标识，可为数字或 Telegram 支持的公开会话名称
        :param action: Bot API 状态名称，如 typing
        :return: API 返回结果转换后的布尔值；调用失败时抛出统一异常
        """
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
        """
        修改文本消息，可替换为结构化富文本。

        :param chat_id: 目标用户或会话标识，可为数字或 Telegram 支持的公开会话名称
        :param message_id: 目标会话中的原消息标识
        :param text: 新的普通正文
        :param parse_mode: 正文解析格式；None 使用客户端默认值，空字符串表示纯文本
        :param reply_markup: 可选内联键盘或其他 Bot API 键盘结构
        :param disable_web_page_preview: 是否关闭网页预览；None 时不发送该配置
        :param rich_message: 可选 InputRichMessage；非空时不发送 parse_mode
        :return: Telegram 返回的更新后消息或成功标记；API 拒绝或网络失败时抛出统一异常
        """
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
        """
        修改媒体消息的说明文字。

        :param chat_id: 目标用户或会话标识，可为数字或 Telegram 支持的公开会话名称
        :param message_id: 目标会话中的原消息标识
        :param caption: 新的媒体说明
        :param parse_mode: 正文解析格式；None 使用客户端默认值，空字符串表示纯文本
        :param reply_markup: 可选内联键盘或其他 Bot API 键盘结构
        :return: Telegram 返回的更新后消息或成功标记；API 拒绝或网络失败时抛出统一异常
        """
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
        """
        替换消息中的媒体引用。

        :param chat_id: 目标用户或会话标识，可为数字或 Telegram 支持的公开会话名称
        :param message_id: 目标会话中的原消息标识
        :param media: InputMedia 字典；资源必须是 URL 或 file_id，不在此方法上传文件
        :param reply_markup: 可选内联键盘或其他 Bot API 键盘结构
        :return: Telegram 返回的更新后消息或成功标记；API 拒绝或网络失败时抛出统一异常
        """
        return self.call("editMessageMedia", {
            "chat_id": chat_id,
            "message_id": message_id,
            "media": media,
            "reply_markup": reply_markup,
        })

    def delete_message(self, chat_id: ChatId, message_id: int) -> bool:
        """
        删除目标会话中的消息。

        :param chat_id: 目标用户或会话标识，可为数字或 Telegram 支持的公开会话名称
        :param message_id: 目标会话中的原消息标识
        :return: API 返回结果转换后的布尔值；调用失败时抛出统一异常
        """
        return self._call_bool("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def answer_callback_query(
            self,
            callback_query_id: Union[str, int],
            text: Optional[str] = None,
            show_alert: Optional[bool] = None,
    ) -> bool:
        """
        确认按钮回调并结束客户端加载状态。

        :param callback_query_id: 按钮回调标识
        :param text: 可选提示文字
        :param show_alert: 是否使用弹窗提示；None 时不发送该参数
        :return: API 返回结果转换后的布尔值；调用失败时抛出统一异常
        """
        return self._call_bool("answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        })

    def set_my_commands(self, commands: List[JsonDict]) -> bool:
        """
        替换机器人菜单命令。

        :param commands: 含 command 和 description 的命令字典列表，命令名不带斜杠
        :return: API 返回结果转换后的布尔值；调用失败时抛出统一异常
        """
        return self._call_bool("setMyCommands", {"commands": commands})

    def delete_my_commands(self) -> bool:
        """
        清除机器人菜单命令。

        :return: API 返回结果转换后的布尔值；调用失败时抛出统一异常
        """
        return self._call_bool("deleteMyCommands")


    def get_updates(self, offset: Optional[int], timeout: int) -> List[JsonDict]:
        """
        长轮询读取消息和按钮回调，其他更新类型不请求。

        :param offset: 下一条需要的更新标识；服务端会确认比它更早的更新，None 表示尚无本地进度
        :param timeout: 服务端等待时长，单位为秒；读取超时额外增加 15 秒
        :return: 原始 Update 字典列表；没有更新时返回空列表
        """
        return self._call_list(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": _ALLOWED_UPDATES},
            read_timeout=timeout + _CONNECT_TIMEOUT_SECONDS,
            _http=self._get_polling_http(),
        )

    def polling(
            self,
            handler: Callable[[JsonDict], None],
            long_polling_timeout: int,
            retry_max_seconds: int = 60,
    ) -> None:
        """
        在当前线程拉取更新并按返回顺序串行分发，直到请求停止。

        请求失败从 3 秒开始指数退避，网络异常会重建轮询会话；持续失败每分钟警告一次。
        停止后返回的长轮询结果会丢弃，不推进更新进度，供下一个轮询者重新接收。

        :param handler: 每条更新的同步处理函数；抛错只记录日志，该更新仍推进本地进度
        :param long_polling_timeout: 每次服务端长轮询等待秒数
        :param retry_max_seconds: 连续请求失败的最大退避秒数，默认 60
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
                if isinstance(err, TelegramNetworkError):
                    try:
                        self._reset_polling_http()
                    except Exception as reset_err:
                        logger.warning(f"Telegram轮询连接重建失败，将继续重试：{reset_err}")
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
