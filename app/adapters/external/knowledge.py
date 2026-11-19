"""
中心知识协议的只读 HTTPS 适配器，不缓存、不重放、不回显中心错误正文。
"""

from collections.abc import Mapping
from typing import Any, Literal, Optional, Protocol, Union

from pydantic import ValidationError

from app.adapters.network.http import AsyncHttpRequestError, AsyncRequestUtils
from app.runtime.settings import get_runtime_setting
from app.schemas.knowledge import (
    QUERY_SECONDS,
    AgentKnowledgeConfig,
    KnowledgeFailure,
    KnowledgeOutcome,
    KnowledgeRead,
    KnowledgeResponse,
    KnowledgeSearch,
)

MAX_RESPONSE_BYTES = 96 * 1024
_HTTP_FAILURES: dict[int, KnowledgeFailure] = {
    401: "unauthorized", 403: "forbidden", 410: "reference_expired", 413: "invalid_request",
    415: "invalid_request", 422: "invalid_request", 429: "rate_limited", 503: "unavailable",
}


class KnowledgeClient:
    """
    复用宿主网络层，只向 MoviePilot Server 发送有界只读请求。
    """

    def __init__(self, transport: Optional["KnowledgeHttpTransport"] = None) -> None:
        """
        允许测试通过宿主 HTTP 抽象注入合成响应，生产固定创建宿主网络层请求器。

        :param transport: 可选的宿主 HTTP 请求器；其生命周期由注入方管理
        """
        self._transport = transport

    async def query(
        self, config: AgentKnowledgeConfig, request: Union[KnowledgeSearch, KnowledgeRead],
    ) -> KnowledgeOutcome:
        """
        固定路径、校验证书并关闭跳转；不携带 Cookie 或用户身份令牌。

        :param config: 当前 Server 源和本地工具开关
        :param request: 宿主验证的检索或补读请求，不接受任意路径
        :return: 不含外部异常详情的结果或受控失败
        """
        if not config.enabled:
            return KnowledgeOutcome("disabled")
        path = "agent/search" if isinstance(request, KnowledgeSearch) else "agent/read"
        origin = config.origin.rstrip("/")
        if not origin:
            return KnowledgeOutcome("unavailable")
        transport = self._transport or AsyncRequestUtils(
            proxies=get_runtime_setting("PROXY"), http2=False, timeout=QUERY_SECONDS, verify=True,
            follow_redirects=False, trust_env=False,
        )
        try:
            response = await transport.post_res(
                f"{origin}/knowledge/v1/{path}", json=request.model_dump(mode="json"),
                allow_redirects=False, raise_exception=True,
                headers={"Accept": "application/json", "Content-Type": "application/json",
                         "Accept-Encoding": "identity"},
            )
        except (AsyncHttpRequestError, OSError, ValueError):
            return KnowledgeOutcome("unavailable")
        if response is None:
            return KnowledgeOutcome("unavailable")
        try:
            if len(response.content) > MAX_RESPONSE_BYTES:
                return KnowledgeOutcome("invalid_response")
            if response.status_code != 200:
                return KnowledgeOutcome(_HTTP_FAILURES.get(response.status_code, "unavailable"))
            if response.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                return KnowledgeOutcome("invalid_response")
            try:
                result = KnowledgeResponse.model_validate_json(response.content)
            except ValidationError:
                return KnowledgeOutcome("invalid_response")
            if isinstance(request, KnowledgeSearch):
                if len(result.evidence) > request.limit or any(len(item.excerpt) > 1600 for item in result.evidence):
                    return KnowledgeOutcome("invalid_response")
                if any(item.product not in ("", request.product)
                       or (request.domain and item.domain != request.domain) for item in result.evidence):
                    return KnowledgeOutcome("invalid_response")
            elif len(result.evidence) > 1 or any(item.evidence_id != request.evidence_id for item in result.evidence):
                return KnowledgeOutcome("invalid_response")
            return KnowledgeOutcome(result.status, result)
        finally:
            await response.aclose()


class KnowledgeHttpResponse(Protocol):
    """
    只暴露协议校验所需的有限 HTTP 响应字段。

    :ivar status_code: HTTP 状态码
    :ivar headers: 响应头的只读映射
    :ivar content: HTTP 层缓冲后的响应字节
    """

    status_code: int
    headers: Mapping[str, str]
    content: bytes

    async def aclose(self) -> None:
        """
        关闭响应资源。
        """
        ...


class KnowledgeHttpTransport(Protocol):
    """
    由宿主网络层实现的单次异步 POST 请求器。
    """

    async def post_res(
        self,
        url: str,
        *,
        json: dict[str, Any],
        allow_redirects: bool,
        raise_exception: Literal[True],
        headers: dict[str, str],
    ) -> Optional[KnowledgeHttpResponse]:
        """
        发送一次指定 HTTPS POST 并返回可关闭响应。

        :param url: Adapter 固定构造的中心协议路径
        :param json: 已验证的协议 DTO
        :param allow_redirects: 必须为 False
        :param raise_exception: 必须为 True，不将传输错误伪装成空结果
        :param headers: 不含 Cookie 或身份令牌的单次请求头
        :return: 已缓冲的 HTTP 响应；无响应时为 None
        """
        ...
