"""
验证可扩展媒体来源注册表遇到异常值时不会返回非枚举对象。
"""

from unittest.mock import Mock

import pytest

from app.schemas.types import MediaSource


def test_missing_media_source_rejects_conflicted_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    动态成员登记发生冲突时只接受真正的媒体来源枚举成员。

    :param monkeypatch: 用局部注册表替身隔离枚举全局状态
    """
    registry = Mock()
    registry.get.return_value = None
    registry.setdefault.return_value = object()
    monkeypatch.setattr(MediaSource, "_value2member_map_", registry)

    assert MediaSource._missing_("custom.provider") is None
    registry.setdefault.assert_called_once()
