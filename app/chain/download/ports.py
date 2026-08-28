"""下载链网络与归档技术端口。"""

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional, Protocol, Union

from app.runtime.log import logger


class DownloadResponsePort(Protocol):
    """下载链读取的最小同步 HTTP 响应契约。"""

    status_code: int
    reason: str
    text: str
    content: bytes
    headers: Mapping[str, str]

    def json(self) -> Any:
        """返回响应 JSON 载荷。"""
        ...

    def close(self) -> None:
        """释放响应与连接资源。"""
        ...


class DownloadHttpPort(Protocol):
    """下载链获取字幕与间接下载地址所需的同步 HTTP 端口。"""

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
        """发送 GET 请求并保留无响应、失败响应与成功响应三态。"""
        ...

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
        """发送 POST 请求并保留无响应、失败响应与成功响应三态。"""
        ...


class DownloadArchivePort(Protocol):
    """下载链保存压缩字幕所需的最小归档文件端口。"""

    def unpack(
        self,
        archive_file: Path,
        extract_dir: Path,
        *,
        archive_format: Optional[str],
    ) -> None:
        """将字幕归档解压到指定临时目录。"""
        ...

    def list_files(self, directory: Path, extensions: tuple[str, ...]) -> list[Path]:
        """返回临时目录内匹配扩展名的字幕文件。"""
        ...


_download_port_lock = threading.RLock()
_download_http_port: Optional[DownloadHttpPort] = None
_download_archive_port: Optional[DownloadArchivePort] = None


def configure_download_ports(
    *,
    http: DownloadHttpPort,
    archive: DownloadArchivePort,
) -> tuple[Optional[DownloadHttpPort], Optional[DownloadArchivePort]]:
    """由启动组合根装配下载链技术端口，并返回旧快照供隔离测试恢复。"""
    global _download_http_port, _download_archive_port
    with _download_port_lock:
        previous = (_download_http_port, _download_archive_port)
        _download_http_port = http
        _download_archive_port = archive
        return previous


def reset_download_ports(
    http: Optional[DownloadHttpPort] = None,
    archive: Optional[DownloadArchivePort] = None,
) -> None:
    """恢复指定下载链端口；省略参数时回到未装配状态。"""
    global _download_http_port, _download_archive_port
    with _download_port_lock:
        _download_http_port = http
        _download_archive_port = archive


def _download_ports_snapshot() -> tuple[DownloadHttpPort, DownloadArchivePort]:
    """读取一致的下载链端口快照，未装配时稳定失败。"""
    with _download_port_lock:
        http = _download_http_port
        archive = _download_archive_port
    if http is None or archive is None:
        raise RuntimeError("下载链技术端口尚未由启动组合根装配")
    return http, archive


def _close_download_response(response: DownloadResponsePort) -> None:
    """释放下载响应；关闭失败只记录诊断，不覆盖已完成的业务结果。"""
    try:
        response.close()
    except Exception as err:
        logger.debug(f"释放下载响应失败：{str(err)}")
