import mimetypes
from pathlib import Path
from typing import Optional, Union, Tuple
from urllib import parse
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from app.log import logger


class UrlUtils:

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
        except Exception as e:
            logger.debug(f"Error combining URL: {e}")
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
        except Exception as e:
            logger.debug(f"Error get_mime_type: {e}")
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
        except Exception as e:
            logger.debug(f"Error parse_url_params: {e}")
            return None
