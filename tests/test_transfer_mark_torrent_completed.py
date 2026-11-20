"""
验证整理完成后下载器任务完成标记的更新。
"""

# -*- coding: utf-8 -*-
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.application.transfer.workflow import JobManager, TransferTask
from app.chain.transfer.facade import TransferChain
from app.domain.meta.metabase import MetaBase
from app.runtime.config import settings
from app.schemas.file import FileItem
from app.schemas.transfer import DownloaderTorrent
from app.schemas.types import MediaType


class _FakeMeta(MetaBase):
    """
    构造最小可用的剧集元数据。

    继承 MetaBase 而非鸭子类型：TransferTask.meta 已标注为 MetaBase，pydantic 会做
    isinstance 校验。三个类级属性用来遮蔽 MetaBase 上的同名 property。
    """

    name = None
    episode_list = None
    season_episode = None

    def __init__(self, episode: int, season: int = 1):
        """
        初始化剧集编号相关字段。
        """
        super().__init__(title=f"Test Show S{season:02d}E{episode:02d}")
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
        """
        返回季字符串。
        """
        return f"S{self.begin_season:02d}"

    @property
    def episode(self):
        """
        返回集字符串。
        """
        return f"E{self.begin_episode:02d}"

    def to_dict(self):
        """
        返回元数据字典。
        """
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
    """
    构造跳过初始化的 TransferChain，仅带作业视图。
    """
    chain = object.__new__(TransferChain)
    chain.jobview = JobManager()
    chain._media_exts = settings.RMT_MEDIAEXT
    chain._subtitle_exts = settings.RMT_SUBEXT
    chain._lyrics_exts = settings.RMT_LYRICSEXT
    chain._audio_exts = settings.RMT_AUDIOEXT
    chain._allowed_exts = (
        chain._media_exts + chain._audio_exts + chain._subtitle_exts + chain._lyrics_exts
    )
    chain._success_target_files = {}
    chain._scrape_batches = {}
    chain._transfer_scan_sizes = {}
    return chain


def _make_task(episode: int, download_hash: str, downloader: str) -> TransferTask:
    """
    构造带下载器信息的整理任务。
    """
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
    """
    调用私有的打标签方法。
    """
    chain._TransferChain__mark_torrent_completed_if_done(download_hash, downloader)


def _finish_task(chain: TransferChain, task: TransferTask):
    """
    将任务登记并流转到完成状态。
    """
    assert chain.jobview.add_task(task)
    chain.jobview.running_task(task)
    chain.jobview.finish_task(task)


def test_mark_skips_tag_when_torrent_still_downloading():
    """
    种子未下载完成时（多集种子先完成单集），不得设置已整理标签（#6009）。
    """
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)
    chain.list_torrents = lambda **kwargs: [SimpleNamespace(progress=52.3)]
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

    _mark(chain, "hash1", "qbittorrent")

    assert completed == []


def test_mark_tags_when_torrent_completed():
    """
    种子已下载完成且任务全部结束时，正常设置已整理标签。
    """
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)
    chain.list_torrents = lambda **kwargs: [SimpleNamespace(progress=100)]
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

    _mark(chain, "hash1", "qbittorrent")

    assert completed == [{"hashs": "hash1", "downloader": "qbittorrent"}]


def test_mark_queries_torrent_by_hash_regardless_of_builtin_tag():
    """
    按 Hash 回写时不得受 TORRENT_TAG 过滤，否则工作流自定义标签的任务永远无法打上已整理标签。
    """
    chain = _make_chain()
    completed = []
    queries = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)
    chain.list_torrents = lambda **kwargs: queries.append(kwargs) or (
        [SimpleNamespace(progress=100)] if kwargs.get("include_all_tags") else []
    )
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

    _mark(chain, "hash1", "qbittorrent")

    assert queries == [{"hashs": "hash1", "downloader": "qbittorrent", "include_all_tags": True}]
    assert completed == [{"hashs": "hash1", "downloader": "qbittorrent"}]


def test_mark_short_circuits_downloader_query_when_jobview_not_done():
    """
    作业视图还有未结束任务时，不应产生任何下载器查询。
    """
    chain = _make_chain()
    queries = []
    chain.transfer_completed = lambda **kwargs: None
    chain.list_torrents = lambda **kwargs: queries.append(kwargs) or []
    task = _make_task(1, "hash1", "qbittorrent")
    assert chain.jobview.add_task(task)
    chain.jobview.running_task(task)

    _mark(chain, "hash1", "qbittorrent")

    assert queries == []


def test_mark_skips_tag_when_torrent_not_found(monkeypatch):
    """
    查不到种子时不打标签，并告警提供下载器与 hash 供排查关联。
    """
    warning = MagicMock()
    monkeypatch.setattr("app.chain.transfer.settlement.logger.warning", warning)
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)
    chain.list_torrents = lambda **kwargs: []
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

    _mark(chain, "hash1", "qbittorrent")

    assert completed == []
    warning.assert_called_once()
    message = warning.call_args.args[0]
    assert "qbittorrent" in message
    assert "hash1" in message
    assert "未查询到种子" in message


def test_mark_skips_tag_when_list_torrents_raises():
    """
    查询下载器异常时不打标签且不向上抛出。
    """
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)

    def _raise(**_kwargs):
        """
        模拟下载器查询失败。
        """
        raise RuntimeError("downloader unreachable")

    chain.list_torrents = _raise
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

    _mark(chain, "hash1", "qbittorrent")

    assert completed == []


def test_multi_episode_torrent_tags_only_after_last_episode():
    """
    #6009 回归：E01 先整理完不打标签，种子整体下载完成后才打标签。
    """
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


def test_mark_skips_tag_when_files_were_appended_since_scan():
    """
    E05 交给整理后同一任务又追加了 E06，且 E06 在整理期间就下载完成：任务仍是"已完成"，但选中大小
    变了，不能打"已整理"，否则 E06 永远不会再进入下载器整理；下一轮整理会重新扫描。

    扫描记录要保留：同一轮之后的整理回调（同一种子其他文件稍后完成、"补打已整理"）也必须看到
    "已变化"，删掉记录会让它们退回原有判断，把没整理过的 E06 标成已整理。
    """
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)
    chain.list_torrents = lambda **kwargs: [SimpleNamespace(progress=100, selected_size=150)]
    chain._transfer_scan_sizes[("qbittorrent", "hash1")] = 100
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

    _mark(chain, "hash1", "qbittorrent")
    _mark(chain, "hash1", "qbittorrent")

    assert completed == []
    assert chain._transfer_scan_sizes == {("qbittorrent", "hash1"): 100}


def test_mark_tags_when_selection_unchanged_and_forgets_snapshot():
    """
    选中大小与扫描时一致时照常打标签，并清理扫描记录。
    """
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)
    chain.list_torrents = lambda **kwargs: [SimpleNamespace(progress=100, selected_size=100)]
    chain._transfer_scan_sizes[("qbittorrent", "hash1")] = 100
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

    _mark(chain, "hash1", "qbittorrent")

    assert completed == [{"hashs": "hash1", "downloader": "qbittorrent"}]
    assert chain._transfer_scan_sizes == {}


def test_mark_without_comparable_sizes_keeps_original_behavior():
    """
    没有扫描记录（例如重启后）或下载器给不出选中大小时无法比较，沿用原有判断照常打标签。
    """
    chain = _make_chain()
    completed = []
    chain.transfer_completed = lambda **kwargs: completed.append(kwargs)
    chain.list_torrents = lambda **kwargs: [SimpleNamespace(progress=100, selected_size=None)]
    chain._transfer_scan_sizes[("qbittorrent", "hash1")] = 100
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

    _mark(chain, "hash1", "qbittorrent")

    assert completed == [{"hashs": "hash1", "downloader": "qbittorrent"}]


def test_mark_checks_completion_and_tags_under_task_lock():
    """
    "确认已完成"与"写入已整理标签"必须在同一把任务锁内：否则刚确认完成，下载链就在同一任务上追加了
    数据已齐的文件，随后写入的标签会让追加的文件再也不进入下载器整理。
    """
    from app.chain.transfer import settlement as settlement_module

    chain = _make_chain()
    locks = settlement_module.downloader_task_locks
    stripe = locks._locks[locks._stripe("hash1")]
    held = []
    chain.list_torrents = lambda **kwargs: held.append(stripe.locked()) or [SimpleNamespace(progress=100)]
    chain.transfer_completed = lambda **kwargs: held.append(stripe.locked())
    _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

    _mark(chain, "hash1", "qbittorrent")

    assert held == [True, True]
    assert not stripe.locked()


def test_mark_without_scan_record_defers_polled_torrents_to_next_scan():
    """
    重启后回放的整理任务没有扫描记录，无法核实整理的是哪批文件：任务在下载器整理轮询范围内时不打标签，
    下一轮轮询会重新扫描、记录选中大小后再打；不在轮询范围内时标签不影响轮询，照常打标签。
    """
    for covered, expected in ((True, []), (False, [{"hashs": "hash1", "downloader": "qbittorrent"}])):
        chain = _make_chain()
        completed = []
        chain.transfer_completed = lambda completed=completed, **kwargs: completed.append(kwargs)
        chain.list_torrents = lambda **kwargs: [SimpleNamespace(progress=100, selected_size=150)]
        chain._covered_by_downloader_polling = lambda _torrent, covered=covered: covered
        _finish_task(chain, _make_task(1, "hash1", "qbittorrent"))

        _mark(chain, "hash1", "qbittorrent")

        assert completed == expected


def test_downloader_polling_coverage_follows_monitor_directories(monkeypatch):
    """
    只看任务路径是否位于"下载器监控"类型的下载目录内（存储一致），不访问存储；没有路径不在范围内。
    """
    from pathlib import Path

    from app.chain.transfer import workflow as workflow_module

    dirs = [
        SimpleNamespace(monitor_type="downloader", storage="local", download_path="/downloads/tv"),
        SimpleNamespace(monitor_type="monitor", storage="local", download_path="/downloads/movie"),
    ]
    monkeypatch.setattr(workflow_module, "DirectoryHelper", lambda: SimpleNamespace(get_download_dirs=lambda: dirs))

    assert TransferChain._covered_by_downloader_polling(SimpleNamespace(path=Path("/downloads/tv/Show"))) is True
    assert TransferChain._covered_by_downloader_polling(SimpleNamespace(path=Path("/downloads/movie/Film"))) is False
    assert TransferChain._covered_by_downloader_polling(SimpleNamespace(path=None)) is False


def test_mark_uses_selected_completion_when_downloader_reports_it():
    """
    rTorrent 的完成百分比按全部数据计算：只选了部分文件的任务，选中部分下完后进度不到 100，由 ``selected_completed``
    表示选中部分已下完，照常打标签；选中部分没下完（例如追加的新集还在下载）时不打。没有该字段时按完成百分比判断。
    """
    for torrent, expected in (
            (DownloaderTorrent(progress=25.0, selected_completed=True), True),
            (DownloaderTorrent(progress=100, selected_completed=False), False),
            (DownloaderTorrent(progress=100), True),
            (DownloaderTorrent(progress=25.0), False),
    ):
        chain = _make_chain()
        completed = []
        chain.transfer_completed = lambda **kwargs: completed.append(kwargs)
        chain.list_torrents = lambda _torrent=torrent, **kwargs: [_torrent]
        _finish_task(chain, _make_task(1, "hash1", "rtorrent"))

        _mark(chain, "hash1", "rtorrent")

        assert bool(completed) is expected, torrent
