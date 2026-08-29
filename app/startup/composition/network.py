"""网络应用端口的宿主组合根。"""

from collections.abc import Mapping
from typing import Any, Callable, Optional, cast

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.adapters.network.ip import IpUtils
from app.application.configuration import get_runtime_settings
from app.application.image import (
    ImageResponsePort,
    ImageTransport,
    InternalAddressPort,
    configure_image_ports,
)
from app.application.messaging.ingress import (
    MessageIngressPort,
    configure_message_ingress_port,
)
from app.application.network import (
    NetworkTestResponse,
    NetworkTestService,
    NetworkTestTransport,
    configure_network_test_service,
)
from app.runtime.log import logger


class _NetworkTestTransportAdapter:
    """把通用异步 HTTP Adapter 收窄为网络探测 GET 端口。"""

    async def get(
        self,
        url: str,
        *,
        proxy: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        user_agent: Optional[str] = None,
    ) -> Optional[NetworkTestResponse]:
        """使用固定超时、证书校验和手动重定向策略请求目标。"""
        response = await AsyncRequestUtils(
            proxies=proxy,
            headers=dict(headers) if headers else None,
            timeout=10,
            ua=user_agent or "",
            verify=True,
            follow_redirects=False,
        ).get_res(url, allow_redirects=False)
        return cast(Optional[NetworkTestResponse], response)


def _read_network_test_setting(key: str, default: Any = None) -> Any:
    """延迟读取已由组合根发布的部署设置，避免装配阶段提前取值。"""
    return get_runtime_settings().get(key, default)


class _ImageTransportAdapter:
    """把通用 HTTP Adapter 收窄为图片应用服务的 GET 端口。"""

    def get(
        self,
        url: str,
        *,
        options: Mapping[str, Any],
    ) -> Optional[ImageResponsePort]:
        """同步创建短生命周期请求对象并返回响应。"""
        response = RequestUtils(**dict(options)).get_res(url=url)
        return cast(Optional[ImageResponsePort], response)

    async def async_get(
        self,
        url: str,
        *,
        options: Mapping[str, Any],
    ) -> Optional[ImageResponsePort]:
        """异步创建短生命周期请求对象并返回响应。"""
        response = await AsyncRequestUtils(**dict(options)).get_res(url=url)
        return cast(Optional[ImageResponsePort], response)


class _InternalAddressAdapter:
    """把通用地址判断收窄为图片代理决策端口。"""

    @staticmethod
    def is_internal(url: str) -> bool:
        """委托通用 IP 工具判断 URL 是否指向内部地址。"""
        probe = cast(Callable[[str], bool], IpUtils.is_internal)
        return bool(probe(url))


class _MessageIngressAdapter:
    """通过通用 HTTP Adapter 投递本地消息并负责释放响应。"""

    def post(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
    ) -> Optional[int]:
        """同步投递消息，关闭响应后返回状态码。"""
        response = RequestUtils(timeout=timeout).post_res(  # type: ignore[arg-type]
            url,
            json=dict(payload),
        )
        if response is None:
            return None
        try:
            return int(response.status_code)
        finally:
            try:
                response.close()
            except Exception as error:  # noqa: BLE001 - 释放失败不改变投递结果
                logger.debug(f"释放本地消息入口响应失败：{error}")

    async def async_post(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
    ) -> Optional[int]:
        """异步投递消息，关闭响应后返回状态码。"""
        response = await AsyncRequestUtils(
            timeout=timeout  # type: ignore[arg-type]
        ).post_res(
            url,
            json=dict(payload),
        )
        if response is None:
            return None
        try:
            return int(response.status_code)
        finally:
            try:
                await response.aclose()
            except Exception as error:  # noqa: BLE001 - 释放失败不改变投递结果
                logger.debug(f"释放本地消息入口响应失败：{error}")


def configure_application_network_ports() -> None:
    """装配网络探测、图片读取、内部地址判断和消息回环传输端口。"""
    network_test_transport: NetworkTestTransport = _NetworkTestTransportAdapter()
    image_transport: ImageTransport = _ImageTransportAdapter()
    internal_address: InternalAddressPort = _InternalAddressAdapter()
    message_ingress: MessageIngressPort = _MessageIngressAdapter()
    configure_network_test_service(
        NetworkTestService(
            transport=network_test_transport,
            settings=_read_network_test_setting,
            logger=logger,
        )
    )
    configure_image_ports(
        transport=image_transport,
        internal_address=internal_address,
    )
    configure_message_ingress_port(message_ingress)
