# -*- coding: utf-8 -*-
from types import SimpleNamespace

from app.chain.transfer import JobManager, TransferChain
from app.core.config import settings
from app.schemas import FileItem, TransferTask
from app.schemas.types import MediaType


class _FakeMeta:
    """构造最小可用的剧集元数据。"""

    def __init__(self, episode: int, season: int = 1):
        """初始化剧集编号相关字段。"""
        self.name = "Test Show"
        self.title = f"Test Show S{season:02d}E{episode:02d}"
        self.year = "2026"
        self.type = MediaType.TV
        self.begin_season = season
        self.end_season = None
        self.total_season = 1
        self.begin_episode = episode
        self.end_episode = None
        self.total_episode = 1
        self.episode_list = [episode]
        self.season_episode = f"S{season:02d}E{episode:02d}"
        self.part = None

    @property
    def season(self):
        """返回季字符串。"""
        return f"S{self.begin_season:02d}"

    @property
    def episode(self):
        """返回集字符串。"""
        return f"E{self.begin_episode:02d}"

    def to_dict(self):
        """返回元数据字典。"""
        return {
            "title": self.title,
            "name": self.name,
            "year": self.year,
            "type": self.type.value,
            "begin_season": self.begin_season,
            "end_season": self.end_season,
            "total_season": self.total_season,
            "begin_episode": self.begin_episode,
            "end_episode": self.end_episode,
            "total_episode": self.total_episode,
            "season_episode": self.season_episode,
            "episode_list": self.episode_list,
            "part": self.part,
        }


def _make_chain() -> TransferChain:
    """构造跳过初始化的 TransferChain，仅带作业视图。"""
    chain = object.__new__(TransferChain)
    chain.jobview = JobManager()
    chain._media_exts = settings.RMT_MEDIAEXT
    chain._subtitle_exts = settings.RMT_SUBEXT
    chain._audio_exts = settings.RMT_AUDIOEXT
    chain._allowed_exts = (
        chain._media_exts + chain._audio_exts + chain._subtitle_exts
    )
    chain._success_target_files = {}
    chain._scrape_batches = {}
    return chain


def _make_task(episode: int, download_hash: str, downloader: str) -> TransferTask:
    """构造带下载器信息的整理任务。"""
    name = f"Test.Show.S01E{episode:02d}.mkv"
    task = TransferTask(
        fileitem=FileItem(
            storage="local",
            path=f"/downloads/Test Show/{name}",
            type="file",
            name=name,
            basename=name.removesuffix(".mkv"),
            extension="mkv",
            size=1024,
        ),
        meta=_FakeMeta(episode),
    )
    task.download_hash = download_hash
    task.downloader = downloader
    return task


def _mark(chain: TransferChain, download_hash: str, downloader: str):
    """调用私有的打标签方法。"""
    chain._TransferChain__mark_torrent_completed_if_done(download_hash, downloader)


def _finish_task(chain: TransferChain, task: TransferTask):
    """将任务登记并流转到完成状态。"""
    assert chain.jobview.add_task(task)
    chain.jobview.running_task(task)
    chain.jobview.finish_task(task)


def test_mark_skips_tag_when_torrent_still_downloading():
    """种子未下载完成时（多集种子先完成单集），不得设置已整理标签（#6009）。"""
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)
    chain.list_torrents = lambda **kwargs: [SimpleNamespace(progress=52.3)]
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

    _mark(chain, "hash1", "qbittorrent")

    assert completed == []


def test_mark_tags_when_torrent_completed():
    """种子已下载完成且任务全部结束时，正常设置已整理标签。"""
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)
    chain.list_torrents = lambda **kwargs: [SimpleNamespace(progress=100)]
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

    _mark(chain, "hash1", "qbittorrent")

    assert completed == [{"hashs": "hash1", "downloader": "qbittorrent"}]


def test_mark_short_circuits_downloader_query_when_jobview_not_done():
    """作业视图还有未结束任务时，不应产生任何下载器查询。"""
    chain = _make_chain()
    queries = []
    chain.transfer_completed = lambda **kwargs: None
    chain.list_torrents = lambda **kwargs: queries.append(kwargs) or []
    task = _make_task(1, "hash1", "qbittorrent")
    assert chain.jobview.add_task(task)
    chain.jobview.running_task(task)

    _mark(chain, "hash1", "qbittorrent")

    assert queries == []


def test_mark_skips_tag_when_torrent_not_found():
    """下载器中查不到种子时不打标签，留待定时轮询兜底。"""
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)
    chain.list_torrents = lambda **kwargs: []
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

    _mark(chain, "hash1", "qbittorrent")

    assert completed == []


def test_mark_skips_tag_when_list_torrents_raises():
    """查询下载器异常时不打标签且不向上抛出。"""
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)

    def _raise(**_kwargs):
        raise RuntimeError("downloader unreachable")

    chain.list_torrents = _raise
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

    _mark(chain, "hash1", "qbittorrent")

    assert completed == []


def test_multi_episode_torrent_tags_only_after_last_episode():
    """#6009 回归：E01 先整理完不打标签，种子整体下载完成后才打标签。"""
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)
    progress = {"value": 60}
    chain.list_torrents = lambda **kwargs: [SimpleNamespace(progress=progress["value"])]

    # E01 下载完成并整理，此时种子整体仍在下载
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))
    _mark(chain, "hash1", "qbittorrent")
    assert completed == []

    # E02 下载完成并整理，种子整体到 100%
    progress["value"] = 100
    _finish_task(chain, _make_task(2, "hash1", "qbittorrent"))
    _mark(chain, "hash1", "qbittorrent")
    assert completed == [{"hashs": "hash1", "downloader": "qbittorrent"}]
