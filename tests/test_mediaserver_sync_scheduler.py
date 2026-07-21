from app.schemas.system import MediaServerConf
from app.scheduler import Scheduler


def test_build_mediaserver_sync_schedules_uses_server_interval_and_legacy_fallback():
    """媒体服务器自动任务应支持独立周期，并在缺省时回退旧全局值。"""
    schedules = Scheduler._build_mediaserver_sync_schedules(
        mediaservers=[
            MediaServerConf(name="default", enabled=True),
            MediaServerConf(name="custom", enabled=True, sync_interval=12),
            MediaServerConf(name="disabled-sync", enabled=True, sync_interval=0),
            MediaServerConf(name="disabled-server", enabled=False, sync_interval=3),
        ],
        default_interval=6,
    )

    assert [(item["server"], item["interval"]) for item in schedules] == [
        ("default", 6),
        ("custom", 12),
    ]
    assert len({item["id"] for item in schedules}) == 2
    assert all(item["id"].startswith("mediaserver_sync_") for item in schedules)


def test_build_mediaserver_sync_schedules_keeps_ids_stable():
    """同名媒体服务器重载配置后应生成稳定的自动任务标识。"""
    mediaservers = [MediaServerConf(name="My Plex", enabled=True, sync_interval=8)]

    first = Scheduler._build_mediaserver_sync_schedules(mediaservers, 6)
    second = Scheduler._build_mediaserver_sync_schedules(mediaservers, 24)

    assert first[0]["id"] == second[0]["id"]
    assert first[0]["interval"] == second[0]["interval"] == 8
