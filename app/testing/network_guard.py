"""测试网络守卫（主程序与插件仓共享）。

提供一个 autouse 的 pytest fixture，拦截测试期对非本地主机的真实出站网络。主程序
``tests/conftest.py`` 与各插件仓 conftest 只需 ``from app.testing.network_guard import
block_real_network`` 即复用同一道守卫——pytest 会把 conftest 命名空间内（含 import 进来的）
fixture 一并识别，autouse 自动作用于每个用例，无需逐用例改动。

仅供测试使用，不参与运行时逻辑。
"""
from __future__ import annotations

import pytest

# 本地回环/通配地址放行，其余主机一律视为真实出站；getaddrinfo 的 host 可能为 str 或 bytes
_ALLOWED_NETWORK_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "::", ""}


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    """防御纵深：拦截对非本地主机的真实出站，强制测试零真实网络。

    补在各用例自身 mock 之上：某用例万一漏 mock 外部依赖（TMDB / LLM 目录 / 下载器 /
    媒体服务器 / 任意外链），其真实 DNS 解析会在此被拦并报错，而非静默发请求。本地回环放行
    （sqlite 等）。asyncio 默认解析器经线程池调用 ``socket.getaddrinfo``，故拦此一处即覆盖
    同步与异步出站。``monkeypatch`` 在用例结束后自动还原，不影响其他用例与进程退出。
    """
    import socket

    _real_getaddrinfo = socket.getaddrinfo

    def _guarded_getaddrinfo(host, *args, **kwargs):
        normalized = host.decode() if isinstance(host, (bytes, bytearray)) else host
        if normalized is not None and normalized not in _ALLOWED_NETWORK_HOSTS:
            raise RuntimeError(
                f"测试禁止真实出站网络：尝试解析 {normalized!r}；请 mock 对应外部依赖"
            )
        return _real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    yield
