"""
Vyo 原生管理 API 请求与公开图片地址构造。
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

from app.adapters.network.http import RequestUtils
from app.foundation.url import UrlUtils
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting


@dataclass
class Result:
    """
    Vyo HTTP 与业务响应的统一包装。

    :param success: HTTP 未报错且业务未显式失败时为 True
    :param data: 解析后的业务数据；失败响应或无数据时为 None
    :param message: 请求失败时提取的可读说明，成功时为 None
    :param status_code: 原始 HTTP 状态码，手动构造且未提供时为 None
    """

    success: bool
    data: Optional[Union[Dict[str, Any], List[Any], str, int, bool]] = None
    message: Optional[str] = None
    status_code: Optional[int] = None


class Api:
    """
    Vyo 媒体服务的原生 HTTP 接口。

    统一走管理端口的 `/api/v1`，凭据为管理面板的长效 API Key；
    Vyo 的接口都挂在 `/api/v1/media-library` 之下。
    """

    LIBRARY_PATH = "/api/v1/media-library"

    def __init__(self, host: Optional[str] = None, apikey: Optional[str] = None):
        """
        保存管理端地址和长期 API 密钥，并创建复用请求会话。

        :param host: Vyo 管理端地址，缺少时保持未配置状态
        :param apikey: 管理面板生成的长期 API 密钥
        """
        self._host = UrlUtils.standardize_base_url(host).rstrip("/") if host else None
        self._apikey = apikey
        self._request_utils = RequestUtils(use_session=True, timeout=15)

    @property
    def host(self) -> Optional[str]:
        """
        读取规范化后的 Vyo 管理端地址。

        :return: 不带尾部斜杠的根地址；未配置时为 None
        """
        return self._host

    @property
    def configured(self) -> bool:
        """
        检查请求所需的地址与密钥是否齐全。

        :return: 两项均非空时返回 True，不发起远程验证
        """
        return bool(self._host and self._apikey)

    def close(self) -> None:
        """
        释放底层会话。
        """
        self._request_utils.close()

    def image_url(self, item_id: str, image_type: str, host: Optional[str] = None) -> str:
        """
        构造不含管理凭据的公开图片直链。

        图片是免鉴权资源，链接会交给浏览器，不能附带管理 API 密钥。

        :param item_id: 媒体条目或媒体库标识
        :param image_type: 图片类型，如 primary 或 backdrop
        :param host: 可选图片根地址，空值使用管理端地址
        :return: 图片 URL；地址或密钥未配置、标识为空时返回空字符串
        """
        if not self.configured or not item_id:
            return ""
        base = (UrlUtils.standardize_base_url(host).rstrip("/") if host else self._host)
        return f"{base}{self.LIBRARY_PATH}/items/{item_id}/image/{image_type}"

    def request(
        self,
        api: str,
        method: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        base_path: Optional[str] = None,
        suppress_log: bool = False,
    ) -> Optional[Result]:
        """
        发送带管理 API 密钥的请求，并统一解析 HTTP 与业务失败。

        :param api: 接口路径，默认相对媒体库接口前缀
        :param method: HTTP 方法；省略时有 data 使用 post，否则 get
        :param params: 查询参数
        :param data: 以 JSON 发送的请求体
        :param base_path: 覆盖接口前缀，例如账号认证使用 /api/v1/user-auth
        :param suppress_log: 是否关闭本方法的错误日志，适用于探测调用
        :return: 网络或 JSON 解析失败时返回 None；HTTP 或业务失败返回 success=False 的结果，
            未包含 success 字段的有效 JSON 响应按成功处理
        """
        if not self.configured or not api:
            return None
        host = self._host or ""
        prefix = base_path if base_path is not None else self.LIBRARY_PATH
        url = host + prefix + (api if api.startswith("/") else f"/{api}")
        if method is None:
            method = "get" if data is None else "post"
        headers = {
            "User-Agent": get_runtime_setting("USER_AGENT"),
            "Accept": "application/json",
            "X-API-Key": self._apikey,
        }
        try:
            res = self._request_utils.request(
                method=method, url=url, headers=headers, params=params, json=data
            )
        except Exception as err:
            if not suppress_log:
                logger.error(f"请求 Vyo 接口 {url} 异常：{err}")
            return None
        if res is None:
            if not suppress_log:
                logger.error(f"请求 Vyo 接口 {url} 无响应")
            return None
        if res.status_code >= 400:
            message = self.__error_message(res)
            if not suppress_log:
                logger.error(f"请求 Vyo 接口 {url} 失败：{res.status_code} {message}")
            return Result(False, None, message, res.status_code)
        try:
            body = res.json()
        except Exception as err:
            if not suppress_log:
                logger.error(f"解析 Vyo 接口 {url} 响应失败：{err}")
            return None
        if isinstance(body, dict) and "success" in body:
            if not body.get("success"):
                message = str(body.get("message") or body.get("detail") or "")
                if not suppress_log:
                    logger.error(f"请求 Vyo 接口 {url} 未成功：{message}")
                return Result(False, None, message, res.status_code)
            return Result(True, body.get("data"), None, res.status_code)
        return Result(True, body, None, res.status_code)

    @staticmethod
    def __error_message(res: Any) -> str:
        """
        从错误响应中取出可读信息，非 JSON 时退回状态文本。
        """
        try:
            body = res.json()
        except Exception:
            return (res.text or "")[:200]
        if isinstance(body, dict):
            return str(body.get("detail") or body.get("message") or "")[:200]
        return str(body)[:200]
