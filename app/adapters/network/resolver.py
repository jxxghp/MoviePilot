"""
系统 DNS 解析适配器。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from typing import Optional


def _normalize_addresses(
    address_infos: Iterable[
        tuple[object, object, object, object, tuple[object, ...]]
    ],
) -> Optional[tuple[str, ...]]:
    """
    把系统 address info 全量归一化；任一异常条目都按解析失败处理。
    """
    addresses: list[str] = []
    for address_info in address_infos:
        try:
            address = ipaddress.ip_address(str(address_info[4][0]))
        except (IndexError, TypeError, ValueError):
            return None
        addresses.append(str(address))
    return tuple(addresses) or None


class SocketDnsResolver:
    """
    通过系统套接字解析器实现同步与异步 DNS 查询。
    """

    def resolve(self, hostname: str) -> Optional[tuple[str, ...]]:
        """
        同步解析主机的全部流式连接地址。

        :param hostname: 要交给系统 DNS 解析的主机名
        :return: 地址文本元组；解析失败、结果为空或任一条目无效时返回 None
        """
        try:
            address_infos = socket.getaddrinfo(
                hostname,
                None,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror:
            return None
        return _normalize_addresses(address_infos)

    async def async_resolve(self, hostname: str) -> Optional[tuple[str, ...]]:
        """
        通过当前事件循环解析地址，避免阻塞异步安全校验。

        :param hostname: 要交给系统 DNS 解析的主机名
        :return: 地址文本元组；解析失败、结果为空或任一条目无效时返回 None
        """
        loop = asyncio.get_running_loop()
        try:
            address_infos = await loop.getaddrinfo(
                hostname,
                None,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror:
            return None
        return _normalize_addresses(address_infos)
