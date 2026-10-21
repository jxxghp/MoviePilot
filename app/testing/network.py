"""
主程序与插件仓共享的测试网络守卫。

在 conftest 中导入 block_real_network 即可使 pytest 自动应用该夹具，拦截非本地主机的
DNS 查询与套接字连接，并在用例结束时检查是否发生了被业务代码吞掉的出站尝试。
本模块仅用于测试，不参与应用运行。
"""
from __future__ import annotations

import ipaddress
from collections.abc import Iterator
from typing import Any, NoReturn

import pytest

# 本地回环/通配地址放行，其余主机一律视为真实出站；getaddrinfo 的 host 可能为 str 或 bytes
_ALLOWED_NETWORK_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "::", ""}


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    拦截测试中的非本地主机访问，并在用例结束时复核违规尝试。

    允许回环地址、未指定地址和本地套接字；外部依赖仍应由用例在边界处替换。
    守卫只覆盖这里包装的 DNS 查询和连接接口，不替代外部依赖隔离。

    :param monkeypatch: pytest 提供的临时替换夹具，用例结束后恢复 socket 接口
    :yields: 网络接口已安装守卫的测试执行阶段，不提供额外值
    """
    import socket

    _real_getaddrinfo = socket.getaddrinfo
    _real_connect = socket.socket.connect
    attempts: list[tuple[str, object]] = []

    def _is_allowed_host(host: object) -> bool:
        """
        判断主机参数是否属于无需外部出站的本地地址。

        :param host: DNS 或套接字主机参数，可为文本、字节或 None
        :return: 回环、未指定地址或允许的本地名称为 True，其他主机为 False
        """
        normalized = host.decode() if isinstance(host, (bytes, bytearray)) else host
        if normalized is None or normalized in _ALLOWED_NETWORK_HOSTS:
            return True
        try:
            address = ipaddress.ip_address(str(normalized).split("%", 1)[0])
            return address.is_loopback or address.is_unspecified
        except ValueError:
            return False

    def _blocked(operation: str, host: object) -> NoReturn:
        """
        记录违规访问并立即抛出异常，使收尾也能发现被吞掉的尝试。

        :param operation: 发起访问的接口类别
        :param host: 违规目标主机
        :raises RuntimeError: 发现测试不允许的真实出站访问
        """
        attempts.append((operation, host))
        raise RuntimeError(
            f"测试禁止真实出站网络：尝试通过 {operation} 访问 {host!r}；请 mock 对应外部依赖"
        )

    def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        """
        先检查主机范围，再委托原 DNS 解析函数。

        :param host: 待解析主机
        :param args: 原解析函数的位置参数
        :param kwargs: 原解析函数的关键字参数
        :return: 原 DNS 解析结果；非本地主机访问抛出异常
        """
        if not _is_allowed_host(host):
            _blocked("DNS", host)
        return _real_getaddrinfo(host, *args, **kwargs)

    def _guarded_connect(sock: socket.socket, address: Any) -> None:
        """
        检查网络地址元组中的主机，允许本地套接字继续连接。

        :param sock: 发起连接的套接字实例
        :param address: 连接地址；网络地址元组的首项作为主机检查
        :return: 原套接字连接函数的返回值
        """
        if isinstance(address, tuple) and address and not _is_allowed_host(address[0]):
            _blocked("socket", address[0])
        return _real_connect(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    yield
    if attempts:
        details = ", ".join(f"{operation}:{host}" for operation, host in attempts)
        pytest.fail(f"测试期间发生真实出站网络尝试：{details}")
