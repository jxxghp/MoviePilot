"""网络地址判断工具的回归测试。"""

import pytest

from app.adapters.network.ip import IpUtils


@pytest.mark.parametrize("url", ["/poster.jpg", "poster.jpg"])
def test_is_internal_rejects_urls_without_hostname(url: str) -> None:
    """相对图片路径没有主机名时应按外网处理，不得触发 IP 解析异常。"""
    assert IpUtils.is_internal(url) is False
