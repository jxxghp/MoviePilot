"""
验证绿联客户端类型收窄前后的播放进度及遍历边界。
"""

from unittest.mock import Mock

import pytest

from app.modules.ugreen.ugreen import Ugreen


@pytest.mark.parametrize("progress,expected", [(None, 0), ("0.5", 0), (0.5, 50), (2, 100)])
def test_play_wrapper_preserves_progress_fallback(progress: object, expected: int) -> None:
    """
    缺失或非数值进度仍按零显示，合法数值继续限制在百分比范围内。
    """
    client = Ugreen.__new__(Ugreen)
    item = client._Ugreen__build_play_item_from_wrapper({
        "video_info": {"ug_video_info_id": "movie", "type": 1, "name": "电影"},
        "play_status": {"progress": progress},
    })
    assert item is not None
    assert item.percent == expected


@pytest.mark.parametrize("limit,expected", [(None, 3), (-1, 3), (0, 1), (1, 1), (2, 2)])
def test_internal_item_limit_preserves_existing_zero_behavior(
    monkeypatch: pytest.MonkeyPatch, limit: object, expected: int,
) -> None:
    """
    内部模式的不限量和零上限继续遵循历史行为，不借类型治理改变分页结果。
    """
    client = Ugreen.__new__(Ugreen)
    client._api = Mock()
    client._library_paths = {"library": "/media"}
    monkeypatch.setattr(client, "is_authenticated", lambda: True)
    monkeypatch.setattr(client, "_iter_library_videos", lambda **_kwargs: iter([
        {"ug_video_info_id": str(index), "type": 1, "name": "电影"}
        for index in range(3)
    ]))
    assert len(list(client.get_items("library", limit=limit))) == expected
