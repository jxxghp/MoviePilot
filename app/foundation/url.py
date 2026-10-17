"""
提供 URL 解析、参数处理与地址拼接。
"""

import mimetypes
import re
from pathlib import Path
from typing import Iterable, Mapping, Optional, Tuple, Union
from urllib import parse
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse


class UrlUtils:
    """
    提供不发起网络请求的 URL 解析与组合能力。
    """

    @staticmethod
    def normalize_http_url(url: object) -> Optional[str]:
        """
        校验绝对 HTTP(S) 地址，无效输入返回 None。

        仅去除首尾空白，不补协议或重写路径、查询串和 fragment，避免改变地址语义。
        此处只检查地址格式，不保证远端可访问或响应内容有效。
        """
        if not isinstance(url, str):
            return None
        normalized_url = url.strip()
        if not normalized_url:
            return None
        if any(character.isspace() for character in normalized_url):
            return None
        try:
            parsed_url = urlparse(normalized_url)
            hostname = parsed_url.hostname
            port = parsed_url.port
        except ValueError:
            return None
        if (
            parsed_url.scheme.lower() not in ("http", "https")
            or not hostname
            or (port is not None and not 0 <= port <= 65535)
        ):
            return None
        return normalized_url

    @staticmethod
    def standardize_base_url(host: str) -> str:
        """
        为主机地址补充 HTTP 协议和末尾斜杠。

        :param host: 主机地址；空值保持原样
        :return: 标准化后的地址，未显式使用 HTTP(S) 时补充 http://
        """
        if not host:
            return host
        if not host.endswith("/"):
            host += "/"
        if not host.startswith("http://") and not host.startswith("https://"):
            host = "http://" + host
        return host

    @staticmethod
    def adapt_request_url(host: str, endpoint: str) -> Optional[str]:
        """
        组合主机地址与接口路径，兼容已完整提供的 HTTP(S) 地址。

        :param host: 基础主机地址；为空时直接使用接口地址
        :param endpoint: 相对路径或完整 HTTP(S) 地址
        :return: 组合后的地址；两个输入都为空时返回 None
        """
        if not host and not endpoint:
            return None
        if endpoint.startswith(("http://", "https://")):
            return endpoint
        host = UrlUtils.standardize_base_url(host)
        return urljoin(host, endpoint) if host else endpoint

    @staticmethod
    def combine_url(
        host: str,
        path: Optional[str] = None,
        query: Optional[Mapping[str, object]] = None,
    ) -> Optional[str]:
        """
        组合基础地址、路径和查询参数。

        :param host: 基础主机地址，例如 https://example.com
        :param path: 可携带查询串的路径；None 表示根路径
        :param query: 额外查询参数，同名项覆盖路径中的参数，序列值按多值编码
        :return: 组合后的 URL；组合失败时返回 None
        """
        try:
            if path is None:
                path = '/'
            host = UrlUtils.standardize_base_url(host)
            url = urljoin(host, path)
            url_parts = urlparse(url)
            query_params: dict[str, object] = dict(parse_qs(url_parts.query))
            if query:
                for key, value in query.items():
                    query_params[key] = value

            query_string = urlencode(query_params, doseq=True)
            new_url_parts = url_parts._replace(query=query_string)
            complete_url = urlunparse(new_url_parts)
            return str(complete_url)
        except Exception:
            return None

    @staticmethod
    def get_mime_type(path_or_url: Union[str, Path], default_type: str = "application/octet-stream") -> str:
        """
        根据文件路径或 URL 获取 MIME 类型，如果无法获取则返回默认类型。

        :param path_or_url: 文件路径 (Path) 或 URL (str)
        :param default_type: 无法获取类型时返回的默认 MIME 类型
        :return: 获取到的 MIME 类型或默认类型
        """
        try:
            if isinstance(path_or_url, Path):
                path_or_url = str(path_or_url)

            mime_type, _ = mimetypes.guess_type(path_or_url)
            if not mime_type:
                return default_type
            return mime_type
        except Exception:
            return default_type

    @staticmethod
    def quote(s: str) -> str:
        """
        将字符串编码为 URL 安全的格式。

        :param s: 要编码的字符串
        :return: 编码后的字符串
        """
        return parse.quote(s)

    @staticmethod
    def parse_url_params(url: str) -> Optional[Tuple[str, str, int, str]]:
        """
        提取地址的协议、主机名、端口和路径。

        :param url: 完整 URL 或未带协议的地址，未带协议时按 HTTP 处理
        :return: 协议、主机名、端口、路径组成的元组；无效地址返回 None
            未提供端口时 HTTPS 使用 443，其余协议使用 80；空路径使用 /
        """
        try:
            if not url:
                return None

            url = UrlUtils.standardize_base_url(host=url)
            parsed = urlparse(url)

            if not parsed.hostname:
                return None
            protocol = parsed.scheme
            hostname = parsed.hostname
            port = parsed.port or (443 if protocol == "https" else 80)
            path = parsed.path or "/"

            return protocol, hostname, port, path
        except Exception:
            return None


def url_matches_trusted_hosts(url: str, trusted_hosts: Iterable[str]) -> bool:
    """
    判断 HTTP(S) URL 的网络位置或主机名是否精确匹配受信主机。
    """
    try:
        parsed_url = urlparse(url)
    except ValueError:
        return False
    if parsed_url.scheme.lower() not in {"http", "https"} or not parsed_url.hostname:
        return False
    trusted = {host.lower() for host in trusted_hosts}
    return parsed_url.netloc.lower() in trusted or parsed_url.hostname.lower() in trusted


def split_netloc(url: str) -> Tuple[str, str]:
    """
    返回 URL 的协议与网络位置，并兼容未带协议的历史输入。
    """
    if not url:
        return "", ""
    if not url.startswith("http"):
        return "http", url
    address = urlparse(url)
    return address.scheme, address.netloc


def second_level_label(url: str) -> str:
    """
    返回去除端口后的倒数第二级域名标签。

    不区分域名与 IP 文本；没有点号时返回整个主机标签，空输入返回空字符串。
    """
    if not url:
        return ""
    _scheme, netloc = split_netloc(url)
    if not netloc:
        return ""
    labels = netloc.split(":")[0].split(".")
    return labels[-2] if len(labels) >= 2 else labels[0]


def host_label(url: str) -> str:
    """
    返回兼容历史语义的一级主机标签。
    """
    if not url:
        return ""
    _scheme, netloc = split_netloc(url)
    if not netloc:
        return ""
    return netloc.split(".")[-2]


def base_url(url: str) -> str:
    """
    返回由协议和网络位置组成的根地址。
    """
    if not url:
        return ""
    scheme, netloc = split_netloc(url)
    return f"{scheme}://{netloc}"


def parse_address(
    address: str,
    include_scheme: bool = True,
) -> Tuple[Optional[str], Optional[int]]:
    """
    按历史规则从服务地址中提取域名文本和端口。

    :param address: 服务地址，不支持含多个冒号的 IPv6 形式
    :param include_scheme: 是否在结果中保留协议，并为缺失协议的地址补充 HTTP
    :return: 域名文本与端口；结构不受支持时返回两个 None
    :raises ValueError: 显式端口不能转换为整数
    """
    if not address:
        return None, None
    address = address.rstrip("/")
    if include_scheme and not address.startswith("http"):
        address = f"http://{address}"
    elif not include_scheme and address.startswith("http"):
        address = address.split("://")[-1]
    parts = address.split(":")
    if len(parts) > 3:
        return None, None
    if len(parts) == 3:
        port = int(parts[-1])
        domain = ":".join(parts[:-1]).rstrip("/")
    elif len(parts) == 2:
        port = 443 if address.startswith("https") else 80
        domain = address
    else:
        return None, None
    return domain, port


def is_link(value: str) -> bool:
    """
    判断文本是否为受支持协议链接、IP 或域名形式。
    """
    if not value:
        return False
    if re.match(r"^(http|https|ftp|ftps|sftp|ws|wss)://", value):
        return True
    return re.match(r"^[a-zA-Z0-9.-]+(\.[a-zA-Z]{2,})?$", value) is not None
