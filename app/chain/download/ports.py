"""
下载链网络与归档技术端口。
"""

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional, Protocol, Union

from app.runtime.log import logger


class DownloadResponsePort(Protocol):
    """
    下载链读取的最小同步 HTTP 响应契约。

    :ivar status_code: HTTP 响应状态码，用于区分成功与失败响应
    :ivar reason: HTTP 响应原因短语，供错误诊断使用
    :ivar text: 客户端解码后的响应正文
    :ivar content: 原始响应字节，用于字幕或其他文件内容
    :ivar headers: 用于内容类型和归档格式判断的响应头映射
    """

    status_code: int
    reason: str
    text: str
    content: bytes
    headers: Mapping[str, str]

    def json(self) -> Any:
        """
        返回响应 JSON 载荷。

        :return: 响应正文解析后的 JSON 值；无效 JSON 按客户端解析异常向上传播
        """
        ...

    def close(self) -> None:
        """
        释放响应与连接资源。
        """
        ...


class DownloadHttpPort(Protocol):
    """
    下载链获取字幕与间接下载地址所需的同步 HTTP 端口。
    """

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
        """
        发送 GET 请求并保留无响应、失败响应与成功响应三态。

        :param url: 字幕资源或间接下载服务的完整请求 URL
        :param cookies: 认证 Cookie 文本或键值映射，None 表示未提供
        :param ua: 本次请求使用的 User-Agent，None 时沿用客户端默认值
        :param headers: 额外请求头，None 表示未提供
        :param proxies: 协议到代理地址的映射，None 时沿用客户端默认策略
        :param timeout: 请求超时秒数，None 时沿用客户端默认值
        :param params: 附加到 URL 的查询参数，None 表示不附加
        :param raise_exception: 是否将请求异常向调用方传播，默认转换为无响应结果
        :return: 原始 HTTP 响应，包括非成功状态；未取得响应且不传播异常时为 None，调用方负责关闭
        """
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
        """
        发送 POST 请求并保留无响应、失败响应与成功响应三态。

        :param url: 字幕资源或间接下载服务的完整请求 URL
        :param cookies: 认证 Cookie 文本或键值映射，None 表示未提供
        :param ua: 本次请求使用的 User-Agent，None 时沿用客户端默认值
        :param headers: 额外请求头，None 表示未提供
        :param proxies: 协议到代理地址的映射，None 时沿用客户端默认策略
        :param timeout: 请求超时秒数，None 时沿用客户端默认值
        :param params: 附加到 URL 的查询参数，None 表示不附加
        :return: 原始 HTTP 响应，包括非成功状态；未取得响应时为 None，调用方负责关闭
        """
        ...


class DownloadArchivePort(Protocol):
    """
    下载链保存压缩字幕所需的最小归档文件端口。
    """

    def unpack(
        self,
        archive_file: Path,
        extract_dir: Path,
        *,
        archive_format: Optional[str],
    ) -> None:
        """
        将字幕归档解压到指定临时目录。

        :param archive_file: 已下载到本地的字幕归档文件
        :param extract_dir: 本次操作拥有的临时解压目录
        :param archive_format: 已识别的归档格式，None 表示由后缀或实现判断
        """
        ...

    def list_files(self, directory: Path, extensions: tuple[str, ...]) -> list[Path]:
        """
        返回临时目录内匹配扩展名的字幕文件。

        :param directory: 待枚举的临时解压目录
        :param extensions: 允许的字幕文件扩展名集合
        :return: 目录内扩展名匹配的字幕文件路径列表
        """
        ...


_download_port_lock = threading.RLock()
_download_http_port: Optional[DownloadHttpPort] = None
_download_archive_port: Optional[DownloadArchivePort] = None


def configure_download_ports(
    *,
    http: DownloadHttpPort,
    archive: DownloadArchivePort,
) -> tuple[Optional[DownloadHttpPort], Optional[DownloadArchivePort]]:
    """
    由启动组合根装配下载链技术端口，并返回旧快照供隔离测试恢复。
    """
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
    """
    恢复指定下载链端口；省略参数时回到未装配状态。
    """
    global _download_http_port, _download_archive_port
    with _download_port_lock:
        _download_http_port = http
        _download_archive_port = archive


def _download_ports_snapshot() -> tuple[DownloadHttpPort, DownloadArchivePort]:
    """
    读取一致的下载链端口快照，未装配时稳定失败。
    """
    with _download_port_lock:
        http = _download_http_port
        archive = _download_archive_port
    if http is None or archive is None:
        raise RuntimeError("下载链技术端口尚未由启动组合根装配")
    return http, archive


def _close_download_response(response: DownloadResponsePort) -> None:
    """
    释放下载响应；关闭失败只记录诊断，不覆盖已完成的业务结果。
    """
    try:
        response.close()
    except Exception as err:
        logger.debug(f"释放下载响应失败：{str(err)}")
