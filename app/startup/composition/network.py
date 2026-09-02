"""网络应用端口的宿主组合根。"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Optional, Union, cast

from app.adapters.network.doh import DohHelper
from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.adapters.network.ip import IpUtils
from app.adapters.system.host import SystemUtils
from app.application.configuration import get_runtime_settings
from app.application.image import (
    ImageResponsePort,
    ImageTransport,
    InternalAddressPort,
    configure_image_ports,
    reset_image_ports,
)
from app.application.messaging.ingress import (
    MessageIngressPort,
    configure_message_ingress_port,
    reset_message_ingress_port,
)
from app.application.network import (
    NetworkTestResponse,
    NetworkTestService,
    NetworkTestTransport,
    configure_network_test_service,
    reset_network_test_service,
)
from app.chain.download.ports import (
    DownloadArchivePort,
    DownloadHttpPort,
    DownloadResponsePort,
    configure_download_ports,
    reset_download_ports,
)
from app.chain.message import (
    MessageHttpPort,
    MessageResponsePort,
    configure_message_http_port,
    reset_message_http_port,
)
from app.chain.scraping import (
    ScrapingHttpPort,
    ScrapingResponsePort,
    ScrapingStreamResponsePort,
    configure_scraping_http_port,
    reset_scraping_http_port,
)
from app.chain.system import (
    SystemEnvironmentPort,
    SystemHttpPort,
    SystemResponsePort,
    configure_system_ports,
    reset_system_ports,
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
        headers: Mapping[str, str],
        timeout: float,
    ) -> Optional[int]:
        """同步投递消息，关闭响应后返回状态码。"""
        response = RequestUtils(
            timeout=timeout,  # type: ignore[arg-type]
            headers=dict(headers),
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
                response.close()
            except Exception as error:  # noqa: BLE001 - 释放失败不改变投递结果
                logger.debug(f"释放本地消息入口响应失败：{error}")

    async def async_post(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> Optional[int]:
        """异步投递消息，关闭响应后返回状态码。"""
        response = await AsyncRequestUtils(
            timeout=timeout,  # type: ignore[arg-type]
            headers=dict(headers),
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


class _DownloadHttpAdapter:
    """把 RequestUtils 收窄为下载链同步 HTTP 端口。"""

    @staticmethod
    def _request(
        *,
        cookies: Optional[Union[str, dict[str, str]]],
        ua: Optional[str],
        headers: Optional[dict[str, str]],
        proxies: Optional[dict[str, str]],
        timeout: Optional[int],
    ) -> RequestUtils:
        """按下载链传入的代理、认证与超时参数构造一次请求。"""
        options: dict[str, Any] = {
            "cookies": cookies,
            "ua": ua,
            "headers": headers,
            "proxies": proxies,
            "timeout": timeout,
        }
        return RequestUtils(**options)

    def get(
        self,
        url: str,
        *,
        cookies: Optional[Union[str, dict[str, str]]] = None,
        ua: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        proxies: Optional[dict[str, str]] = None,
        timeout: Optional[int] = None,
        params: Optional[dict[str, Any]] = None,
        raise_exception: bool = False,
    ) -> Optional[DownloadResponsePort]:
        """发送下载链 GET 请求并原样保留响应三态。"""
        request = self._request(
            cookies=cookies, ua=ua, headers=headers, proxies=proxies, timeout=timeout
        )
        kwargs: dict[str, Any] = {"raise_exception": raise_exception}
        if params is not None:
            kwargs["params"] = params
        response = request.get_res(url, **kwargs)
        return cast(Optional[DownloadResponsePort], response)

    def post(
        self,
        url: str,
        *,
        cookies: Optional[Union[str, dict[str, str]]] = None,
        ua: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        proxies: Optional[dict[str, str]] = None,
        timeout: Optional[int] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> Optional[DownloadResponsePort]:
        """发送下载链 POST 请求并原样保留响应三态。"""
        request = self._request(
            cookies=cookies, ua=ua, headers=headers, proxies=proxies, timeout=timeout
        )
        kwargs: dict[str, Any] = {}
        if params is not None:
            kwargs["params"] = params
        response = request.post_res(url, **kwargs)
        return cast(Optional[DownloadResponsePort], response)


class _DownloadArchiveAdapter:
    """把 SystemUtils 收窄为下载链字幕归档端口。"""

    def unpack(
        self,
        archive_file: Path,
        extract_dir: Path,
        *,
        archive_format: Optional[str],
    ) -> None:
        """使用宿主统一归档实现解压字幕文件。"""
        SystemUtils.unpack_archive(
            archive_file, extract_dir, archive_format=archive_format
        )

    def list_files(self, directory: Path, extensions: tuple[str, ...]) -> list[Path]:
        """使用宿主统一文件扫描实现列出字幕文件。"""
        return SystemUtils.list_files(directory, list(extensions))


class _MessageHttpAdapter:
    """把 RequestUtils 收窄为消息附件同步 GET 端口。"""

    def get(self, url: str, *, timeout: int) -> Optional[MessageResponsePort]:
        """按消息链固定超时读取附件响应。"""
        response = RequestUtils(timeout=timeout).get_res(url)
        return cast(Optional[MessageResponsePort], response)


class _ScrapingHttpAdapter:
    """把 RequestUtils 收窄为刮削链普通与流式 GET 端口。"""

    def get(
        self,
        url: str,
        *,
        proxies: Optional[dict[str, str]],
        ua: str,
        timeout: int,
    ) -> Optional[ScrapingResponsePort]:
        """读取需要完整载荷的音乐封面响应。"""
        options: dict[str, Any] = {"proxies": proxies, "ua": ua, "timeout": timeout}
        response = RequestUtils(**options).get_res(url)
        return cast(Optional[ScrapingResponsePort], response)

    def stream(
        self,
        url: str,
        *,
        proxies: Optional[dict[str, str]],
        ua: str,
    ) -> ScrapingStreamResponsePort:
        """打开由刮削链上下文关闭的流式图片响应。"""
        options: dict[str, Any] = {"proxies": proxies, "ua": ua}
        response = RequestUtils(**options).get_stream(url=url)
        return cast(ScrapingStreamResponsePort, response)


class _SystemHttpAdapter:
    """把 RequestUtils 收窄为系统链发布版本 GET 端口。"""

    def get(
        self,
        url: str,
        *,
        proxies: Optional[dict[str, str]],
        headers: Mapping[str, str],
    ) -> Optional[SystemResponsePort]:
        """按系统配置的代理与 GitHub 请求头读取发布列表。"""
        options: dict[str, Any] = {"proxies": proxies, "headers": dict(headers)}
        response = RequestUtils(**options).get_res(url)
        return cast(Optional[SystemResponsePort], response)


class _SystemEnvironmentAdapter:
    """把 SystemUtils 收窄为系统链容器环境判断端口。"""

    def is_docker(self) -> bool:
        """返回宿主统一环境探针的 Docker 判断。"""
        return bool(SystemUtils.is_docker())


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


def reset_application_network_ports() -> None:
    """按发布逆序撤销消息回环、图片与网络探测应用端口。"""
    reset_message_ingress_port()
    reset_image_ports()
    reset_network_test_service()


def configure_chain_network_composition() -> None:
    """原子装配四个 Chain 的同步网络与系统技术端口。"""
    reset_chain_network_composition()
    try:
        configure_download_ports(
            http=cast(DownloadHttpPort, _DownloadHttpAdapter()),
            archive=cast(DownloadArchivePort, _DownloadArchiveAdapter()),
        )
        configure_message_http_port(cast(MessageHttpPort, _MessageHttpAdapter()))
        configure_scraping_http_port(cast(ScrapingHttpPort, _ScrapingHttpAdapter()))
        configure_system_ports(
            http=cast(SystemHttpPort, _SystemHttpAdapter()),
            environment=cast(SystemEnvironmentPort, _SystemEnvironmentAdapter()),
        )
    except Exception:
        reset_chain_network_composition()
        raise


def reset_chain_network_composition() -> None:
    """清除四个 Chain 的技术端口，支持重复 lifespan 与失败回滚。"""
    reset_system_ports()
    reset_scraping_http_port()
    reset_message_http_port()
    reset_download_ports()


def configure_doh_composition() -> None:
    """由组合根物化进程唯一的 DoH 技术 Adapter。"""
    DohHelper()


def stop_doh_composition() -> bool:
    """关闭已存在的 DoH Adapter，停机阶段不得反向物化实例。"""
    helper = DohHelper.get_existing_instance()
    if helper is None:
        return True
    return helper.shutdown() is not False
