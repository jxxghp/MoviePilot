"""
验证订阅修改事件的事实内容与触发时机。
"""

from app.schemas.event import SubscribeModifiedEventData


def test_subscribe_modified_event_data_computes_sorted_fields():
    """
    订阅修改事件根据前后快照计算变化字段，并按名称排序。
    """
    data = SubscribeModifiedEventData(
        subscribe_id=7,
        old_subscribe_info={"state": "R", "lack_episode": 3, "name": "A"},
        subscribe_info={"state": "S", "lack_episode": 3, "name": "B"},
        scene="status",
    )

    assert data.fields == ["name", "state"]
    assert data.to_dict() == {
        "subscribe_id": 7,
        "old_subscribe_info": {"state": "R", "lack_episode": 3, "name": "A"},
        "subscribe_info": {"state": "S", "lack_episode": 3, "name": "B"},
        "scene": "status",
        "fields": ["name", "state"],
    }


def test_subscribe_modified_event_data_diffs_missing_keys_as_none():
    """
    计算订阅变化字段时将缺失键按 None 处理。
    """
    data = SubscribeModifiedEventData(
        subscribe_id=8,
        old_subscribe_info={"state": "R", "episode_priority": {"1": 80}},
        subscribe_info={"state": "R"},
        scene="reset",
    )

    assert data.fields == ["episode_priority"]
    assert set(data.to_dict()) == {
        "subscribe_id",
        "old_subscribe_info",
        "subscribe_info",
        "scene",
        "fields",
    }


def test_subscribe_modified_event_data_ignores_caller_supplied_fields():
    """
    订阅修改事件忽略调用方提供的变化字段，始终重新计算。
    """
    data = SubscribeModifiedEventData(
        subscribe_id=9,
        old_subscribe_info={"state": "R"},
        subscribe_info={"state": "S"},
        scene="update",
        fields=["fake"],
    )

    assert data.fields == ["state"]
    assert data.to_dict()["fields"] == ["state"]
