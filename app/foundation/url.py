import mimetypes
import re
from hashlib import sha256
from pathlib import Path
from typing import Optional, Union, Tuple
from urllib import parse
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse, urlunparse

class UrlUtils:
    """提供不发起网络请求的 URL 解析与组合能力。"""

    @staticmethod
    def standardize_base_url(host: str) -> str:
        """
        标准化提供的主机地址，确保它以http://或https://开头，并且以斜杠(/)结尾
        :param host: 提供的主机地址字符串
        :return: 标准化后的主机地址字符串
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
        基于传入的host，适配请求的URL，确保每个请求的URL是完整的，用于在发送请求前自动处理和修正请求的URL
        :param host: 主机头
        :param endpoint: 端点
        :return: 完整的请求URL字符串
        """
        if not host and not endpoint:
            return None
        if endpoint.startswith(("http://", "https://")):
            return endpoint
        host = UrlUtils.standardize_base_url(host)
        return urljoin(host, endpoint) if host else endpoint

    @staticmethod
    def combine_url(host: str, path: Optional[str] = None, query: Optional[dict] = None) -> Optional[str]:
        """
        使用给定的主机头、路径和查询参数组合生成完整的URL
        :param host: str, 主机头，例如 https://example.com
        :param path: Optional[str], 包含路径和可能已经包含的查询参数的端点，例如 /path/to/resource?current=1
        :param query: Optional[dict], 可选，额外的查询参数，例如 {"key": "value"}
        :return: str, 完整的请求URL字符串
        """
        try:
            # 如果路径为空，则默认为 '/'
            if path is None:
                path = '/'
            host = UrlUtils.standardize_base_url(host)
            # 使用 urljoin 合并 host 和 path
            url = urljoin(host, path)
            # 解析当前 URL 的组成部分
            url_parts = urlparse(url)
            # 解析已存在的查询参数，并与额外的查询参数合并
            query_params = parse_qs(url_parts.query)
            if query:
                for key, value in query.items():
                    query_params[key] = value

            # 重新构建查询字符串
            query_string = urlencode(query_params, doseq=True)
            # 构建完整的 URL
            new_url_parts = url_parts._replace(query=query_string)
            complete_url = urlunparse(new_url_parts)
            return str(complete_url)
        except Exception:
            return None


    @staticmethod
    def get_mime_type(path_or_url: Union[str, Path], default_type: str = "application/octet-stream") -> str:
        """
        根据文件路径或 URL 获取 MIME 类型，如果无法获取则返回默认类型

        :param path_or_url: 文件路径 (Path) 或 URL (str)
        :param default_type: 无法获取类型时返回的默认 MIME 类型
        :return: 获取到的 MIME 类型或默认类型
        """
        try:
            # 如果是 Path 类型，转换为字符串
            if isinstance(path_or_url, Path):
                path_or_url = str(path_or_url)

            # 尝试根据路径或 URL 获取 MIME 类型
            mime_type, _ = mimetypes.guess_type(path_or_url)
            # 如果无法推测到类型，返回默认类型
            if not mime_type:
                return default_type
            return mime_type
        except Exception:
            return default_type

    @staticmethod
    def quote(s: str) -> str:
        """
        将字符串编码为 URL 安全的格式

        :param s: 要编码的字符串
        :return: 编码后的字符串
        """
        return parse.quote(s)

    @staticmethod
    def parse_url_params(url: str) -> Optional[Tuple[str, str, int, str]]:
        """
        解析给定的 URL，并提取协议、主机名、端口和路径信息

        :param url: str
            需要解析的 URL 字符串
            可以是完整的 URL（例如："http://example.com:8080/path"）或不带协议的地址（例如："example.com:1234"）
        :return: Optional[Tuple[str, str, int, str]]
            - str: 协议（例如："http", "https"）
            - str: 主机名或 IP 地址（例如："example.com", "192.168.1.1"）
            - int: 端口号（例如：80, 443）
            - str: URL 的路径部分（例如："/", "/path"）
            如果输入地址无效或无法解析，则返回 None
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


def split_netloc(url: str) -> Tuple[str, str]:
    """返回 URL 的协议与网络位置，并兼容未带协议的历史输入。"""
    if not url:
        return "", ""
    if not url.startswith("http"):
        return "http", url
    address = urlparse(url)
    return address.scheme, address.netloc


def second_level_label(url: str) -> str:
    """返回不含端口的倒数第二级域名标签，IP 则保持原值。"""
    if not url:
        return ""
    _scheme, netloc = split_netloc(url)
    if not netloc:
        return ""
    labels = netloc.split(":")[0].split(".")
    return labels[-2] if len(labels) >= 2 else labels[0]


def host_label(url: str) -> str:
    """返回兼容历史语义的一级主机标签。"""
    if not url:
        return ""
    _scheme, netloc = split_netloc(url)
    if not netloc:
        return ""
    return netloc.split(".")[-2]


def base_url(url: str) -> str:
    """返回由协议和网络位置组成的根地址。"""
    if not url:
        return ""
    scheme, netloc = split_netloc(url)
    return f"{scheme}://{netloc}"


def parse_address(
    address: str,
    include_scheme: bool = True,
) -> Tuple[Optional[str], Optional[int]]:
    """按历史规则从服务地址中提取域名文本和端口。"""
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
    """判断文本是否为受支持协议链接、IP 或域名形式。"""
    if not value:
        return False
    if re.match(r"^(http|https|ftp|ftps|sftp|ws|wss)://", value):
        return True
    return re.match(r"^[a-zA-Z0-9.-]+(\.[a-zA-Z]{2,})?$", value) is not None


def sanitize_path(url: str, max_length: int = 120) -> str:
    """
    将 URL 的路径部分进行编码，确保合法字符，并对路径长度进行压缩处理（如果超出最大长度）

    :param url: 需要处理的 URL
    :param max_length: 路径允许的最大长度，超出时进行压缩
    :return: 处理后的路径字符串
    """
    # 解析 URL，获取路径部分
    parsed_url = urlparse(url)
    path = parsed_url.path.lstrip("/")

    # 对路径中的特殊字符进行编码
    safe_path = quote(path)

    # 如果路径过长，进行压缩处理
    if len(safe_path) > max_length:
        # 使用 SHA-256 对路径进行哈希，取前 16 位作为压缩后的路径
        hash_value = sha256(safe_path.encode()).hexdigest()[:16]
        # 使用哈希值代替过长的路径，同时保留文件扩展名
        file_extension = Path(safe_path).suffix.lower() if Path(safe_path).suffix else ""
        safe_path = f"compressed_{hash_value}{file_extension}"

    return safe_path
