from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

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
        基于传入的host，适配请求的URL，确保每个请求的URL是完整的，用于在发送请求前自动处理和修正请求的URL。
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
        使用给定的主机头、路径和查询参数组合生成完整的URL。
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
