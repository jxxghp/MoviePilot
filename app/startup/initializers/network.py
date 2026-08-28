"""装配 Chain 使用的同步网络与相关系统窄端口。"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional, Union, cast

from app.adapters.network.http import RequestUtils
from app.adapters.system.host import SystemUtils
from app.chain.download import (
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
            cookies=cookies,
            ua=ua,
            headers=headers,
            proxies=proxies,
            timeout=timeout,
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
            cookies=cookies,
            ua=ua,
            headers=headers,
            proxies=proxies,
            timeout=timeout,
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
            archive_file,
            extract_dir,
            archive_format=archive_format,
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
        options: dict[str, Any] = {
            "proxies": proxies,
            "ua": ua,
            "timeout": timeout,
        }
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
        options: dict[str, Any] = {
            "proxies": proxies,
            "headers": dict(headers),
        }
        response = RequestUtils(**options).get_res(url)
        return cast(Optional[SystemResponsePort], response)


class _SystemEnvironmentAdapter:
    """把 SystemUtils 收窄为系统链容器环境判断端口。"""

    def is_docker(self) -> bool:
        """返回宿主统一环境探针的 Docker 判断。"""
        return bool(SystemUtils.is_docker())


def init_chain_network_ports() -> None:
    """原子式装配四个 Chain 的六条 Adapter 静态依赖边。"""
    reset_chain_network_ports()
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
        reset_chain_network_ports()
        raise


def reset_chain_network_ports() -> None:
    """清除四个 Chain 的技术端口，支持重复 lifespan 与失败回滚。"""
    reset_system_ports()
    reset_scraping_http_port()
    reset_message_http_port()
    reset_download_ports()
