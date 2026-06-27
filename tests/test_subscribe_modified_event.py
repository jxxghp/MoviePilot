from app.schemas.event import SubscribeModifiedEventData


def test_subscribe_modified_event_data_computes_sorted_fields():
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
    data = SubscribeModifiedEventData(
        subscribe_id=9,
        old_subscribe_info={"state": "R"},
        subscribe_info={"state": "S"},
        scene="update",
        fields=["fake"],
    )

    assert data.fields == ["state"]
    assert data.to_dict()["fields"] == ["state"]
