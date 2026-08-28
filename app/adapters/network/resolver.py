"""系统 DNS 解析适配器。"""

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
    """把系统 address info 全量归一化；任一异常条目都按解析失败处理。"""
    addresses: list[str] = []
    for address_info in address_infos:
        try:
            address = ipaddress.ip_address(str(address_info[4][0]))
        except (IndexError, TypeError, ValueError):
            return None
        addresses.append(str(address))
    return tuple(addresses) or None


class SocketDnsResolver:
    """通过系统 socket resolver 实现同步与事件循环异步 DNS 查询。"""

    def resolve(self, hostname: str) -> Optional[tuple[str, ...]]:
        """同步解析全部流式连接地址，系统解析失败时返回 None。"""
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
        """通过当前事件循环解析地址，避免阻塞异步安全校验。"""
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
