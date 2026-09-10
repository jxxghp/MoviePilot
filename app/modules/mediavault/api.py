from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

from app.adapters.network.http import RequestUtils
from app.foundation.url import UrlUtils
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting


@dataclass
class Result:
    """MediaVault 统一响应包装。"""

    success: bool
    data: Optional[Union[Dict[str, Any], List[Any], str, int, bool]] = None
    message: Optional[str] = None
    status_code: Optional[int] = None


class Api:
    """MediaVault 自建媒体库的原生 HTTP 接口。

    统一走管理端口的 `/api/v1`，凭据为管理面板的长效 API Key；
    自建媒体库的接口都挂在 `/api/v1/media-library` 之下。
    """

    LIBRARY_PATH = "/api/v1/media-library"

    def __init__(self, host: Optional[str] = None, apikey: Optional[str] = None):
        self._host = UrlUtils.standardize_base_url(host).rstrip("/") if host else None
        self._apikey = apikey
        self._request_utils = RequestUtils(use_session=True, timeout=15)

    @property
    def host(self) -> Optional[str]:
        return self._host

    @property
    def configured(self) -> bool:
        return bool(self._host and self._apikey)

    def close(self) -> None:
        """释放底层会话。"""
        self._request_utils.close()

    def image_url(self, item_id: str, image_type: str, host: Optional[str] = None) -> str:
        """拼装图片直链；item_id 传媒体库 ID 时得到媒体库封面。

        MediaVault 的图片是免鉴权资源，这里**不能**带上 API Key：该地址会随接口
        响应交给浏览器加载，带 Key 等于把管理凭据下发到客户端。
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
        """请求 MediaVault 接口。

        :param api: 接口路径，默认相对自建媒体库前缀
        :param base_path: 覆盖接口前缀（如站点级 `/api/v1`）
        :param suppress_log: 探测类调用可关闭错误日志
        :return: 网络层失败返回 None，业务失败返回 success=False 的 Result
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
                logger.error(f"请求 MediaVault 接口 {url} 异常：{err}")
            return None
        if res is None:
            if not suppress_log:
                logger.error(f"请求 MediaVault 接口 {url} 无响应")
            return None
        if res.status_code >= 400:
            message = self.__error_message(res)
            if not suppress_log:
                logger.error(f"请求 MediaVault 接口 {url} 失败：{res.status_code} {message}")
            return Result(False, None, message, res.status_code)
        try:
            body = res.json()
        except Exception as err:
            if not suppress_log:
                logger.error(f"解析 MediaVault 接口 {url} 响应失败：{err}")
            return None
        if isinstance(body, dict) and "success" in body:
            if not body.get("success"):
                message = str(body.get("message") or body.get("detail") or "")
                if not suppress_log:
                    logger.error(f"请求 MediaVault 接口 {url} 未成功：{message}")
                return Result(False, None, message, res.status_code)
            return Result(True, body.get("data"), None, res.status_code)
        return Result(True, body, None, res.status_code)

    @staticmethod
    def __error_message(res: Any) -> str:
        """从错误响应中取出可读信息，非 JSON 时退回状态文本。"""
        try:
            body = res.json()
        except Exception:
            return (res.text or "")[:200]
        if isinstance(body, dict):
            return str(body.get("detail") or body.get("message") or "")[:200]
        return str(body)[:200]
