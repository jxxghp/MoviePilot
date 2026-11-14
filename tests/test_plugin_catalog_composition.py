"""
验证启动组合根注入插件目录时的版本比较契约。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.startup.initializers import plugins as plugins_initializer


def test_catalog_version_comparison_accepts_only_explicit_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    不可解析版本返回 False，避免把比较失败当作可安装候选。

    :param monkeypatch: 用无网络客户端隔离市场读取
    """
    client = SimpleNamespace(
        get_plugins=lambda *_args, **_kwargs: [],
        async_get_plugins=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        plugins_initializer, "get_composed_plugin_market_client", lambda: client
    )

    catalog = plugins_initializer._build_plugin_catalog(lambda plugin: plugin)

    assert catalog._version_compare("2.0.0", "gt", "1.0.0") is True
    assert catalog._version_compare("invalid", "gt", "1.0.0") is False
