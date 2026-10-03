"""
飞书开放平台 OpenAPI 精简客户端。

只实现飞书渠道用到的 IM 与 CardKit 接口，请求统一经 RequestUtils 发出。接口路径、HTTP 方法
和请求体字段与 lark-oapi 1.6 对应的 Request 模型一致；响应解析为 ``FeishuResponse``，
``data`` 保留开放平台返回的原始 JSON 结构。
"""

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote, unquote

from app.adapters.network.http import RequestUtils
from app.runtime.log import logger

FEISHU_DOMAIN = "https://open.feishu.cn"

# 开放平台请求体、响应 data 与事件报文的 JSON 结构。
JsonDict = Dict[str, Any]
# 下载结果：文件内容、Content-Disposition 中的文件名、Content-Type。
DownloadedFile = Tuple[bytes, Optional[str], Optional[str]]

# 普通接口与文件上传/下载的超时秒数；lark-oapi 默认不设超时，这里显式限定以免发送线程被挂住。
_DEFAULT_TIMEOUT_SECONDS = 30
_TRANSFER_TIMEOUT_SECONDS = 120
# tenant_access_token 提前 10 分钟视为过期，与 lark-oapi TokenManager 一致。
_TOKEN_REFRESH_MARGIN_SECONDS = 10 * 60
# 令牌缺失或失效的错误码；遇到时丢弃缓存令牌并重试一次。
_TOKEN_INVALID_CODES = frozenset({99991661, 99991663})
# 网络失败或响应不是开放平台 JSON 时使用的本地错误码。
NETWORK_ERROR_CODE = -1


@dataclass
class FeishuResponse:
    """开放平台接口响应；``code`` 为 0 表示成功。"""

    code: int  # 开放平台业务错误码，网络失败时为 NETWORK_ERROR_CODE
    msg: str  # 错误描述
    data: JsonDict = field(default_factory=dict)  # 响应中的 data 对象，失败时为空字典
    log_id: Optional[str] = None  # 响应头 X-Tt-Logid，用于向飞书排查请求

    def success(self) -> bool:
        """返回接口是否调用成功。"""
        return self.code == 0


def _parse_file_name(content_disposition: Optional[str]) -> Optional[str]:
    """从 Content-Disposition 解析文件名，兼容 RFC 5987 ``filename*`` 与 UTF-8 原文。"""
    if not content_disposition:
        return None
    params: Dict[str, str] = {}
    for part in content_disposition.split(";"):
        if "=" in part:
            key, value = part.strip().split("=", 1)
            params[key.lower()] = value.strip(' "')
    if "filename*" in params:
        value = params["filename*"]
        # 形如 UTF-8''%E6%96%87%E4%BB%B6.pdf
        charset, _, encoded = value.partition("''")
        return unquote(encoded or value, encoding=charset or "utf-8")
    if "filename" in params:
        name = unquote(params["filename"])
        try:
            # requests 以 latin-1 解码响应头，飞书直接返回 UTF-8 原文时需要还原。
            return name.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return name
    return None


class FeishuOpenApi:
    """
    单个自建应用的 OpenAPI 客户端，负责 tenant_access_token 缓存与接口调用。

    实例在发送线程与长连接回调线程间共享：令牌刷新由锁串行化，requests 会话的连接池
    本身线程安全；POST 请求不会被自动重放，避免网络抖动时重复发送消息。
    """

    def __init__(self, app_id: str, app_secret: str, domain: str = FEISHU_DOMAIN):
        """
        :param app_id: 自建应用 App ID
        :param app_secret: 自建应用 App Secret
        :param domain: 开放平台域名
        """
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain.rstrip("/")
        self._http = RequestUtils(
            headers={"Accept": "application/json"},
            use_session=True,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            # RequestUtils 默认沿用旧宿主的不校验证书行为；请求携带应用凭据，必须校验。
            verify=True,
        )
        self._token_lock = threading.Lock()
        self._token: Optional[str] = None
        self._token_expires_at = 0.0

    def close(self) -> None:
        """关闭 HTTP 会话。"""
        self._http.close()

    # ---------------------------------------------------------------- 请求层

    def _tenant_token(self, force_refresh: bool = False) -> Optional[str]:
        """返回有效的 tenant_access_token，过期或被判定失效时重新获取。"""
        with self._token_lock:
            if not force_refresh and self._token and time.time() < self._token_expires_at:
                return self._token
            self._token = None
            response = self._http.post_res(
                f"{self._domain}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            body = self._json_body(response)
            if body is None or body.get("code") != 0 or not body.get("tenant_access_token"):
                logger.error(
                    "飞书 tenant_access_token 获取失败：code=%s, msg=%s",
                    None if body is None else body.get("code"),
                    None if body is None else body.get("msg"),
                )
                return None
            self._token = str(body["tenant_access_token"])
            expire = int(body.get("expire") or 0)
            self._token_expires_at = time.time() + expire - _TOKEN_REFRESH_MARGIN_SECONDS
            return self._token

    @staticmethod
    def _json_body(response: Any) -> Optional[JsonDict]:
        """解析响应 JSON，响应为空或不是 JSON 对象时返回 None。"""
        if response is None:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def _path(self, template: str, **params: str) -> str:
        """拼接接口地址；路径参数整体 URL 编码，防止 ``/``、``?`` 改写请求目标。"""
        path = template
        for key, value in params.items():
            path = path.replace(f":{key}", quote(str(value or ""), safe=""))
        return f"{self._domain}{path}"

    def _send(self, method: str, url: str, **kwargs: Any) -> Tuple[Any, Optional[str]]:
        """带令牌发起请求，令牌失效时刷新后重试一次；返回 (响应, 获取令牌失败原因)。"""
        response = None
        for attempt in range(2):
            token = self._tenant_token(force_refresh=attempt > 0)
            if not token:
                return None, "获取 tenant_access_token 失败"
            headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
            response = self._http.request(method, url, headers=headers, **kwargs)
            if attempt == 0 and response is not None and response.status_code != 200:
                body = self._json_body(response)
                if body is not None and body.get("code") in _TOKEN_INVALID_CODES:
                    continue
            break
        return response, None

    def call(self, method: str, url: str, **kwargs: Any) -> FeishuResponse:
        """调用返回 JSON 的接口，并把结果统一为 ``FeishuResponse``。"""
        response, error = self._send(method, url, **kwargs)
        if error:
            return FeishuResponse(code=NETWORK_ERROR_CODE, msg=error)
        if response is None:
            return FeishuResponse(code=NETWORK_ERROR_CODE, msg="请求飞书开放平台失败")
        log_id = response.headers.get("X-Tt-Logid")
        body = self._json_body(response)
        if body is None:
            return FeishuResponse(
                code=NETWORK_ERROR_CODE,
                msg=f"HTTP {response.status_code} {response.reason}",
                log_id=log_id,
            )
        data = body.get("data")
        return FeishuResponse(
            code=int(body.get("code", NETWORK_ERROR_CODE)),
            msg=str(body.get("msg") or ""),
            data=data if isinstance(data, dict) else {},
            log_id=log_id,
        )

    def download(self, url: str, params: Optional[Dict[str, str]] = None) -> Optional[DownloadedFile]:
        """下载二进制资源；HTTP 200 即成功，失败时返回 None。"""
        response, error = self._send("GET", url, params=params, timeout=_TRANSFER_TIMEOUT_SECONDS)
        if error or response is None or response.status_code != 200:
            return None
        return (
            response.content,
            _parse_file_name(response.headers.get("Content-Disposition")),
            response.headers.get("Content-Type"),
        )

    # ---------------------------------------------------------------- IM 消息

    def create_message(
            self, receive_id_type: str, receive_id: str, msg_type: str, content: str, uuid: str
    ) -> FeishuResponse:
        """发送消息；``content`` 为按消息类型序列化后的 JSON 字符串。"""
        return self.call(
            "POST",
            self._path("/open-apis/im/v1/messages"),
            params={"receive_id_type": receive_id_type},
            json={"receive_id": receive_id, "msg_type": msg_type, "content": content, "uuid": uuid},
        )

    def reply_message(
            self, message_id: str, msg_type: str, content: str, reply_in_thread: bool, uuid: str
    ) -> FeishuResponse:
        """回复指定消息。"""
        return self.call(
            "POST",
            self._path("/open-apis/im/v1/messages/:message_id/reply", message_id=message_id),
            json={"content": content, "msg_type": msg_type, "reply_in_thread": reply_in_thread, "uuid": uuid},
        )

    def patch_message(self, message_id: str, content: str) -> FeishuResponse:
        """更新已发送的卡片消息内容。"""
        return self.call(
            "PATCH",
            self._path("/open-apis/im/v1/messages/:message_id", message_id=message_id),
            json={"content": content},
        )

    def create_message_reaction(self, message_id: str, emoji_type: str) -> FeishuResponse:
        """为消息添加表情回应，成功时 data 含 reaction_id。"""
        return self.call(
            "POST",
            self._path("/open-apis/im/v1/messages/:message_id/reactions", message_id=message_id),
            json={"reaction_type": {"emoji_type": emoji_type}},
        )

    def delete_message_reaction(self, message_id: str, reaction_id: str) -> FeishuResponse:
        """删除消息上的表情回应。"""
        return self.call(
            "DELETE",
            self._path(
                "/open-apis/im/v1/messages/:message_id/reactions/:reaction_id",
                message_id=message_id,
                reaction_id=reaction_id,
            ),
        )

    # ---------------------------------------------------------------- 图片与文件

    def upload_image(self, file_path: Path) -> FeishuResponse:
        """上传消息图片，成功时 data 含 image_key。"""
        # 先读入内存：令牌失效重试时需要重新发送完整内容，文件句柄读到末尾后无法复用。
        content = file_path.read_bytes()
        return self.call(
            "POST",
            self._path("/open-apis/im/v1/images"),
            data={"image_type": "message"},
            files={"image": (file_path.name, content)},
            timeout=_TRANSFER_TIMEOUT_SECONDS,
        )

    def upload_file(
            self, file_path: Path, file_type: str, file_name: str, duration: Optional[int] = None
    ) -> FeishuResponse:
        """上传消息文件，成功时 data 含 file_key；``duration`` 为音视频毫秒时长。"""
        form: Dict[str, str] = {"file_type": file_type, "file_name": file_name}
        if duration is not None:
            form["duration"] = str(duration)
        content = file_path.read_bytes()
        return self.call(
            "POST",
            self._path("/open-apis/im/v1/files"),
            data=form,
            files={"file": (file_name, content)},
            timeout=_TRANSFER_TIMEOUT_SECONDS,
        )

    def download_image(self, image_key: str) -> Optional[DownloadedFile]:
        """下载机器人上传的图片。"""
        return self.download(self._path("/open-apis/im/v1/images/:image_key", image_key=image_key))

    def download_file(self, file_key: str) -> Optional[DownloadedFile]:
        """下载机器人上传的文件。"""
        return self.download(self._path("/open-apis/im/v1/files/:file_key", file_key=file_key))

    def download_message_resource(
            self, message_id: str, file_key: str, resource_type: str
    ) -> Optional[DownloadedFile]:
        """下载用户消息中的图片、音频或文件资源。"""
        return self.download(
            self._path(
                "/open-apis/im/v1/messages/:message_id/resources/:file_key",
                message_id=message_id,
                file_key=file_key,
            ),
            params={"type": resource_type},
        )

    # ---------------------------------------------------------------- CardKit 卡片

    def create_card(self, card_json: str) -> FeishuResponse:
        """创建卡片实体，成功时 data 含 card_id。"""
        return self.call(
            "POST",
            self._path("/open-apis/cardkit/v1/cards"),
            json={"type": "card_json", "data": card_json},
        )

    def update_card_element_content(
            self, card_id: str, element_id: str, content: str, sequence: int, uuid: str
    ) -> FeishuResponse:
        """流式更新卡片组件文本；``sequence`` 必须严格递增。"""
        return self.call(
            "PUT",
            self._path(
                "/open-apis/cardkit/v1/cards/:card_id/elements/:element_id/content",
                card_id=card_id,
                element_id=element_id,
            ),
            json={"uuid": uuid, "content": content, "sequence": sequence},
        )

    def update_card_settings(self, card_id: str, settings: str, sequence: int, uuid: str) -> FeishuResponse:
        """更新卡片配置，例如关闭流式模式。"""
        return self.call(
            "PATCH",
            self._path("/open-apis/cardkit/v1/cards/:card_id/settings", card_id=card_id),
            json={"settings": settings, "uuid": uuid, "sequence": sequence},
        )
