"""
基于宿主 RequestUtils 的自定义弹弹play 兼容上游 HTTP 传输适配器。
"""

from __future__ import annotations

from typing import Any, Optional

from app.adapters.network.http import RequestUtils
from app.schemas.danmaku import DanmakuHttpResponse


class RequestUtilsDanmakuTransport:
    """
    通过宿主统一网络栈 ``RequestUtils`` 访问管理员配置的自定义弹幕服务。
    """

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: Optional[dict[str, Any]] = None,
        timeout: int = 10,
        proxies: Any = None,
        verify_tls: bool = True,
    ) -> DanmakuHttpResponse:
        """
        向上游弹幕 API 发送无 Cookie 隔离 HTTP 请求。

        :param method: HTTP 请求方法（GET 或 POST）
        :param url: 完整目标地址
        :param headers: 弹幕服务专用请求头
        :param json_body: 可选 JSON 请求体
        :param timeout: 请求超时时间（秒）
        :param proxies: 可选代理配置
        :param verify_tls: 是否校验 HTTPS 证书
        :return: 统一响应对象
        :raises RuntimeError: 上游无响应或连接失败时
        """
        client = RequestUtils(
            headers=headers,
            proxies=proxies,
            timeout=timeout,
            accept_type=headers.get("Accept"),
            content_type="application/json" if json_body is not None else None,
        )
        if method.upper() == "POST":
            res = client.post_res(url=url, json=json_body, verify=verify_tls)
        else:
            res = client.get_res(url=url, verify=verify_tls)
        if res is None:
            raise RuntimeError("上游弹幕服务无响应")
        return DanmakuHttpResponse(
            status_code=int(res.status_code),
            content=bytes(res.content or b""),
            content_type=str(res.headers.get("Content-Type") or ""),
        )
