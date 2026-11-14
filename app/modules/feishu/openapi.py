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
    """
    保留飞书开放平台业务结果及请求诊断信息。

    :param code: 业务错误码；0 表示成功，NETWORK_ERROR_CODE 表示本地请求或解析失败
    :param msg: 开放平台或本地错误说明
    :param data: 原始响应中的 data 对象；缺失或非字典时使用空字典
    :param log_id: 响应头 X-Tt-Logid 的请求追踪标识；未提供时为 None
    """

    code: int
    msg: str
    data: JsonDict = field(default_factory=dict)
    log_id: Optional[str] = None

    def success(self) -> bool:
        """
        判断开放平台是否报告业务成功。

        :return: code 为 0 时返回 True，否则返回 False
        """
        return self.code == 0


def _parse_file_name(content_disposition: Optional[str]) -> Optional[str]:
    """
    从 Content-Disposition 解析文件名，兼容 RFC 5987 ``filename*`` 与 UTF-8 原文。
    """
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
    管理单个飞书自建应用的访问令牌和 OpenAPI 请求。

    令牌刷新由锁串行化。仅在首次响应为非 200 且错误码明确表示令牌失效时刷新并重发一次，
    不会把普通请求失败解释为可安全重发的消息。调用方负责使用结束后关闭会话。
    """

    def __init__(self, app_id: str, app_secret: str, domain: str = FEISHU_DOMAIN):
        """
        创建启用证书校验的 HTTP 会话和独立令牌缓存。

        :param app_id: 自建应用标识
        :param app_secret: 自建应用密钥
        :param domain: 开放平台基址，默认使用飞书官方域名
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
        """
        关闭 HTTP 会话。
        """
        self._http.close()


    def _tenant_token(self, force_refresh: bool = False) -> Optional[str]:
        """
        在锁内复用有效令牌，失效或强制刷新时请求新令牌。

        服务端有效期提前 600 秒视为到期；刷新失败不会保留原令牌。

        :param force_refresh: 是否清空旧令牌并强制重新获取
        :return: 有效令牌；响应不合法或服务端拒绝时返回 None
        """
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
        """
        解析响应 JSON，响应为空或不是 JSON 对象时返回 None。
        """
        if response is None:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def _path(self, template: str, **params: str) -> str:
        """
        拼接接口地址；路径参数整体 URL 编码，防止 ``/``、``?`` 改写请求目标。
        """
        path = template
        for key, value in params.items():
            path = path.replace(f":{key}", quote(str(value or ""), safe=""))
        return f"{self._domain}{path}"

    def _send(self, method: str, url: str, **kwargs: Any) -> Tuple[Any, Optional[str]]:
        """
        带令牌发起请求，令牌失效时刷新后重试一次；返回 (响应, 获取令牌失败原因)。
        """
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
        """
        调用 JSON 接口并保留业务错误和请求追踪信息。

        :param method: HTTP 请求方法
        :param url: 完整接口地址
        :param kwargs: 传给 HTTP 请求层的查询参数、请求体、上传文件或超时配置
        :return: 统一接口响应；通过 success() 判断业务是否成功，错误码和说明保留在响应中
        """
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
        """
        下载二进制资源，按响应头提取文件元数据。

        :param url: 完整资源地址
        :param params: 可选查询参数
        :return: 文件字节、可选文件名和可选 MIME 类型的三元组；无令牌、无响应或 HTTP 状态非 200 返回 None
        """
        response, error = self._send("GET", url, params=params, timeout=_TRANSFER_TIMEOUT_SECONDS)
        if error or response is None or response.status_code != 200:
            return None
        return (
            response.content,
            _parse_file_name(response.headers.get("Content-Disposition")),
            response.headers.get("Content-Type"),
        )


    def create_message(
            self, receive_id_type: str, receive_id: str, msg_type: str, content: str, uuid: str
    ) -> FeishuResponse:
        """
        向指定用户或会话发送消息。

        :param receive_id_type: 接收标识类型，如 open_id、user_id 或 chat_id
        :param receive_id: 与标识类型匹配的用户或会话标识
        :param msg_type: 飞书消息类型，如 text、interactive 或 file
        :param content: 按消息类型序列化后的 JSON 文本
        :param uuid: 本次消息的去重标识，由调用方提供
        :return: 统一接口响应；通过 success() 判断业务是否成功，错误码和说明保留在响应中
        """
        return self.call(
            "POST",
            self._path("/open-apis/im/v1/messages"),
            params={"receive_id_type": receive_id_type},
            json={"receive_id": receive_id, "msg_type": msg_type, "content": content, "uuid": uuid},
        )

    def reply_message(
            self, message_id: str, msg_type: str, content: str, reply_in_thread: bool, uuid: str
    ) -> FeishuResponse:
        """
        回复指定消息，可选择进入话题。

        :param message_id: 被回复消息的标识
        :param msg_type: 回复消息类型
        :param content: 按消息类型序列化后的 JSON 文本
        :param reply_in_thread: 是否在原消息的话题内回复
        :param uuid: 本次回复的去重标识
        :return: 统一接口响应；通过 success() 判断业务是否成功，错误码和说明保留在响应中
        """
        return self.call(
            "POST",
            self._path("/open-apis/im/v1/messages/:message_id/reply", message_id=message_id),
            json={"content": content, "msg_type": msg_type, "reply_in_thread": reply_in_thread, "uuid": uuid},
        )

    def patch_message(self, message_id: str, content: str) -> FeishuResponse:
        """
        替换已发送消息的卡片内容。

        :param message_id: 需要更新的已发送消息标识
        :param content: 完整卡片内容的 JSON 文本
        :return: 统一接口响应；通过 success() 判断业务是否成功，错误码和说明保留在响应中
        """
        return self.call(
            "PATCH",
            self._path("/open-apis/im/v1/messages/:message_id", message_id=message_id),
            json={"content": content},
        )

    def create_message_reaction(self, message_id: str, emoji_type: str) -> FeishuResponse:
        """
        添加表情回应，成功响应的 data 含 reaction_id。

        :param message_id: 被回应消息的标识
        :param emoji_type: 飞书表情类型名称
        :return: 统一接口响应；通过 success() 判断业务是否成功，错误码和说明保留在响应中
        """
        return self.call(
            "POST",
            self._path("/open-apis/im/v1/messages/:message_id/reactions", message_id=message_id),
            json={"reaction_type": {"emoji_type": emoji_type}},
        )

    def delete_message_reaction(self, message_id: str, reaction_id: str) -> FeishuResponse:
        """
        删除指定消息上的表情回应。

        :param message_id: 被回应消息的标识
        :param reaction_id: 需要删除的回应标识
        :return: 统一接口响应；通过 success() 判断业务是否成功，错误码和说明保留在响应中
        """
        return self.call(
            "DELETE",
            self._path(
                "/open-apis/im/v1/messages/:message_id/reactions/:reaction_id",
                message_id=message_id,
                reaction_id=reaction_id,
            ),
        )


    def upload_image(self, file_path: Path) -> FeishuResponse:
        """
        上传消息图片，成功响应的 data 含 image_key。

        :param file_path: 本地图片路径；完整读入内存后上传
        :return: 统一接口响应；通过 success() 判断业务是否成功，错误码和说明保留在响应中
        """
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
        """
        上传消息文件，成功响应的 data 含 file_key。

        :param file_path: 本地文件路径；完整读入内存后上传
        :param file_type: 飞书上传文件类型，如 opus、mp4 或 stream
        :param file_name: 在飞书中展示的文件名
        :param duration: 音视频时长，单位为毫秒；为 None 时不发送该字段
        :return: 统一接口响应；通过 success() 判断业务是否成功，错误码和说明保留在响应中
        """
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
        """
        下载机器人上传的图片。

        :param image_key: 机器人上传后取得的图片标识
        :return: 文件字节、可选文件名和可选 MIME 类型的三元组；无令牌、无响应或 HTTP 状态非 200 返回 None
        """
        return self.download(self._path("/open-apis/im/v1/images/:image_key", image_key=image_key))

    def download_file(self, file_key: str) -> Optional[DownloadedFile]:
        """
        下载机器人上传的文件。

        :param file_key: 机器人上传后取得的文件标识
        :return: 文件字节、可选文件名和可选 MIME 类型的三元组；无令牌、无响应或 HTTP 状态非 200 返回 None
        """
        return self.download(self._path("/open-apis/im/v1/files/:file_key", file_key=file_key))

    def download_message_resource(
            self, message_id: str, file_key: str, resource_type: str
    ) -> Optional[DownloadedFile]:
        """
        下载用户消息附带的图片、音频或文件。

        :param message_id: 携带资源的用户消息标识
        :param file_key: 消息中的资源标识
        :param resource_type: 飞书资源类型，如 image、audio 或 file
        :return: 文件字节、可选文件名和可选 MIME 类型的三元组；无令牌、无响应或 HTTP 状态非 200 返回 None
        """
        return self.download(
            self._path(
                "/open-apis/im/v1/messages/:message_id/resources/:file_key",
                message_id=message_id,
                file_key=file_key,
            ),
            params={"type": resource_type},
        )


    def create_card(self, card_json: str) -> FeishuResponse:
        """
        创建卡片实体，成功响应的 data 含 card_id。

        :param card_json: 卡片实体定义的 JSON 文本
        :return: 统一接口响应；通过 success() 判断业务是否成功，错误码和说明保留在响应中
        """
        return self.call(
            "POST",
            self._path("/open-apis/cardkit/v1/cards"),
            json={"type": "card_json", "data": card_json},
        )

    def update_card_element_content(
            self, card_id: str, element_id: str, content: str, sequence: int, uuid: str
    ) -> FeishuResponse:
        """
        更新卡片组件的流式正文。

        :param card_id: 卡片实体标识
        :param element_id: 要更新的组件标识
        :param content: 该组件的完整新正文
        :param sequence: 本卡片严格递增的更新序号，由调用方维护
        :param uuid: 本次更新的去重标识
        :return: 统一接口响应；通过 success() 判断业务是否成功，错误码和说明保留在响应中
        """
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
        """
        更新卡片配置，可用于启停流式展示。

        :param card_id: 卡片实体标识
        :param settings: 配置对象的 JSON 文本，如流式模式开关
        :param sequence: 与内容更新共用的递增序号
        :param uuid: 本次配置更新的去重标识
        :return: 统一接口响应；通过 success() 判断业务是否成功，错误码和说明保留在响应中
        """
        return self.call(
            "PATCH",
            self._path("/open-apis/cardkit/v1/cards/:card_id/settings", card_id=card_id),
            json={"settings": settings, "uuid": uuid, "sequence": sequence},
        )
