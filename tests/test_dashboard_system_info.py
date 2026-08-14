from app.db import SessionFactory
from app.db.models.transferhistory import TransferHistory
from app.schemas.types import MediaSource, MediaType
from app.adapters.system import host as system_module
from app.adapters.system.host import SystemUtils


def test_dashboard_system_info_returns_runtime_environment(monkeypatch):
    """系统摘要应返回主机、系统、进程运行时间和后端版本。"""

    class FakeProcess:
        """提供固定启动时间的进程桩。"""

        @staticmethod
        def create_time() -> float:
            """返回固定的进程启动时间。"""
            return 400.0

    monkeypatch.setattr(system_module.socket, "gethostname", lambda: "moviepilot-host")
    monkeypatch.setattr(system_module.time, "time", lambda: 1000.0)
    monkeypatch.setattr(system_module.psutil, "Process", FakeProcess)
    monkeypatch.setattr(SystemUtils, "_operating_system_name", staticmethod(lambda: "Ubuntu 24.04.4 LTS"))
    monkeypatch.setattr(system_module, "APP_VERSION", "v2.13.16")

    result = SystemUtils.dashboard_system_info()

    assert result.hostname == "moviepilot-host"
    assert result.operating_system == "Ubuntu 24.04.4 LTS"
    assert result.runtime == 600
    assert result.version == "v2.13.16"


def test_memory_usage_returns_used_cached_and_available(monkeypatch):
    """内存统计应返回应用进程占用以及系统缓存、可用和总容量。"""

    class FakeMemoryInfo:
        """提供固定进程 RSS 的桩。"""

        rss = 2 * 1024**3

    class FakeProcess:
        """提供固定进程内存信息的桩。"""

        @staticmethod
        def memory_info() -> FakeMemoryInfo:
            """返回固定进程内存信息。"""
            return FakeMemoryInfo()

    class FakeMemory:
        """提供固定系统内存值的桩。"""

        total = 16 * 1024**3
        cached = 3 * 1024**3
        buffers = 512 * 1024**2
        available = 7 * 1024**3

    monkeypatch.setattr(system_module.psutil, "Process", FakeProcess)
    monkeypatch.setattr(system_module.psutil, "virtual_memory", FakeMemory)

    result = SystemUtils.memory_usage()

    assert result.total == 16 * 1024**3
    assert result.used == 2 * 1024**3
    assert result.cached == int(3.5 * 1024**3)
    assert result.available == 7 * 1024**3
    assert result.usage == 12.5


def test_monthly_media_statistics_counts_successful_unique_media():
    """本月新增统计应只计算成功记录，并按媒体去重。"""
    month = system_module.time.strftime("%Y-%m-", system_module.time.localtime())
    histories = [
        TransferHistory(status=True, date=f"{month}01 10:00:00", type=MediaType.MOVIE.value, media_source=MediaSource.TMDB.value, media_id="1", title="电影"),
        TransferHistory(status=True, date=f"{month}02 10:00:00", type=MediaType.MOVIE.value, media_source=MediaSource.TMDB.value, media_id="1", title="电影"),
        TransferHistory(status=True, date=f"{month}03 10:00:00", type=MediaType.TV.value, media_source=MediaSource.TMDB.value, media_id="2", title="剧集", episodes="E01-E03"),
        TransferHistory(status=False, date=f"{month}04 10:00:00", type=MediaType.TV.value, media_source=MediaSource.TMDB.value, media_id="3", title="失败剧集"),
        # 同一首歌多次整理只计一首，无媒体 ID 的曲目按记录计数
        TransferHistory(status=True, date=f"{month}05 10:00:00", type=MediaType.MUSIC.value, title="晴天", media_source=MediaSource.MusicBrainz.value, media_id="r1"),
        TransferHistory(status=True, date=f"{month}06 10:00:00", type=MediaType.MUSIC.value, title="晴天", media_source=MediaSource.MusicBrainz.value, media_id="r1"),
        TransferHistory(status=True, date=f"{month}07 10:00:00", type=MediaType.MUSIC.value, title="未知曲目"),
        TransferHistory(status=False, date=f"{month}08 10:00:00", type=MediaType.MUSIC.value, title="失败曲目"),
        # 整专内不同目标曲目共享专辑 ID，但应分别计入音乐数量
        TransferHistory(
            status=True, date=f"{month}09 10:00:00", type=MediaType.MUSIC.value,
            title="以父之名", media_source="musicbrainz", media_id="release-group-1",
            music_type="album", dest="/music/叶惠美/01 - 以父之名.flac",
        ),
        TransferHistory(
            status=True, date=f"{month}10 10:00:00", type=MediaType.MUSIC.value,
            title="晴天", media_source="musicbrainz", media_id="release-group-1",
            music_type="album", dest="/music/叶惠美/03 - 晴天.flac",
        ),
    ]
    db = SessionFactory()
    try:
        db.add_all(histories)
        db.commit()

        assert TransferHistory.monthly_media_statistics(db) == (1, 1, 3, 4)
    finally:
        history_ids = [history.id for history in histories if history.id is not None]
        if history_ids:
            db.query(TransferHistory).filter(TransferHistory.id.in_(history_ids)).delete(synchronize_session=False)
            db.commit()
        db.close()
