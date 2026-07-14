import socket

import pytest

from app.testing.network_guard import block_real_network


def test_network_guard_fails_when_blocked_attempt_is_swallowed(monkeypatch):
    """业务代码即使捕获网络异常，网络守卫仍应在用例收尾报告失败"""
    fixture = block_real_network.__wrapped__(monkeypatch)
    next(fixture)

    try:
        try:
            socket.getaddrinfo("external.example", 443)
        except RuntimeError:
            pass

        with pytest.raises(pytest.fail.Exception, match="external.example"):
            next(fixture)
    finally:
        monkeypatch.undo()
