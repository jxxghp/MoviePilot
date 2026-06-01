from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import app.chain.download as download_module
from app.chain.download import DownloadChain
from app.core.context import Context, MediaInfo, TorrentInfo
from app.schemas import NotExistMediaInfo
from app.schemas.types import MediaType


class _FakeDownloadHistoryOper:
    """
    避免单元测试写入真实下载历史，只验证下载链路的控制流。
    """

    def add(self, **_kwargs):
        pass

    def add_files(self, _files):
        pass


class _FakeTorrentHelper:
    """
    避免解析真实种子内容，让测试聚焦下载成功后的后台处理。
    """

    def get_fileinfo_from_torrent_content(self, _torrent_content):
        return "", []


class _FakeThreadHelper:
    """
    捕获提交到线程池的任务，测试中手动触发以避免真正启动后台线程。
    """

    submitted = []

    def submit(self, func, *args, **kwargs):
        self.submitted.append((func, args, kwargs))


def test_download_single_submits_download_added_to_background(monkeypatch):
    """
    添加下载成功后，站点字幕等后处理应提交到后台，不能阻塞下载接口返回。
    """
    _FakeThreadHelper.submitted = []
    monkeypatch.setattr(download_module, "ThreadHelper", _FakeThreadHelper)
    monkeypatch.setattr(download_module, "DownloadHistoryOper", _FakeDownloadHistoryOper)
    monkeypatch.setattr(download_module, "TorrentHelper", _FakeTorrentHelper)

    chain = DownloadChain.__new__(DownloadChain)
    chain.download = MagicMock(return_value=("qb", "hash123", "Original", "添加下载成功"))
    chain.download_added = MagicMock()
    chain.eventmanager = MagicMock()
    chain.eventmanager.send_event.return_value = None
    chain.post_message = MagicMock()

    context = Context(
        meta_info=SimpleNamespace(episode_list=[], season=None, episode=None),
        media_info=MediaInfo(
            type=MediaType.MOVIE,
            title="Demo Movie",
            year="2024",
            tmdb_id=1,
            genre_ids=[18],
        ),
        torrent_info=TorrentInfo(
            title="Demo Movie 2024",
            enclosure="https://example.com/demo.torrent",
            site_cookie="uid=1",
            site_name="TestSite",
        ),
    )

    result = chain.download_single(
        context=context,
        torrent_content=b"torrent-content",
        save_path="/downloads",
        username="tester",
    )

    assert result == "hash123"
    chain.download_added.assert_not_called()
    assert len(_FakeThreadHelper.submitted) == 1

    task, args, kwargs = _FakeThreadHelper.submitted[0]
    assert args == ()
    assert kwargs == {}

    task()

    chain.download_added.assert_called_once_with(
        context=context,
        download_dir=Path("/downloads"),
        torrent_content=b"torrent-content",
    )


class _FakeBatchTorrentHelper:
    """
    为批量下载测试提供稳定排序和种子文件集数解析。
    """

    episodes = []

    def sort_group_torrents(self, contexts):
        return contexts

    def get_torrent_episodes(self, _files):
        return list(self.episodes)


def _build_tv_context(episode_list=None):
    """
    构造标题未显式标集数的单季电视剧候选。
    """
    episodes = episode_list or []
    return SimpleNamespace(
        media_info=SimpleNamespace(type=MediaType.TV, tmdb_id=1, douban_id=None),
        meta_info=SimpleNamespace(
            season_list=[1],
            episode_list=episodes,
            title="Test Show",
            org_string="Test Show S01 2160p",
            set_episodes=lambda begin, end: None,
        ),
        torrent_info=SimpleNamespace(title="Test Show S01 2160p", site_name="TestSite"),
        allowed_episodes=None,
        located_episodes=None,
    )


def test_batch_download_rejects_complete_coverage_when_files_do_not_cover_target(monkeypatch):
    """
    完整覆盖要求不能让 1-13 这种局部包冒充 1-143 的目标范围。
    """
    _FakeBatchTorrentHelper.episodes = list(range(1, 14))
    monkeypatch.setattr(download_module, "TorrentHelper", _FakeBatchTorrentHelper)
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)

    chain = DownloadChain.__new__(DownloadChain)
    chain.download_torrent = MagicMock(return_value=(b"torrent-content", "", ["demo.mkv"]))
    chain.download_single = MagicMock(return_value="hash")

    context = _build_tv_context()
    no_exists = {
        1: {
            1: NotExistMediaInfo(
                season=1,
                episodes=[],
                total_episode=143,
                start_episode=1,
                require_complete_coverage=True,
            )
        }
    }

    downloads, lefts = chain.batch_download(contexts=[context], no_exists=no_exists)

    assert downloads == []
    assert lefts == no_exists
    chain.download_single.assert_not_called()


def test_batch_download_accepts_complete_coverage_when_files_cover_target_range(monkeypatch):
    """
    自定义起始集场景按目标范围覆盖判断，100-143 可满足 start=100、total=143。
    """
    _FakeBatchTorrentHelper.episodes = list(range(100, 144))
    monkeypatch.setattr(download_module, "TorrentHelper", _FakeBatchTorrentHelper)
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)

    chain = DownloadChain.__new__(DownloadChain)
    chain.download_torrent = MagicMock(return_value=(b"torrent-content", "", ["demo.mkv"]))
    chain.download_single = MagicMock(return_value="hash")

    context = _build_tv_context()
    no_exists = {
        1: {
            1: NotExistMediaInfo(
                season=1,
                episodes=[],
                total_episode=143,
                start_episode=100,
                require_complete_coverage=True,
            )
        }
    }

    downloads, lefts = chain.batch_download(contexts=[context], no_exists=no_exists)

    assert downloads == [context]
    assert lefts == {}
    chain.download_single.assert_called_once()


def test_batch_download_accepts_complete_coverage_when_title_episodes_cover_target(monkeypatch):
    """
    显式标出完整范围的候选也可满足完整覆盖任务。
    """
    _FakeBatchTorrentHelper.episodes = []
    monkeypatch.setattr(download_module, "TorrentHelper", _FakeBatchTorrentHelper)
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)

    chain = DownloadChain.__new__(DownloadChain)
    chain.download_torrent = MagicMock()
    chain.download_single = MagicMock(return_value="hash")

    context = _build_tv_context(episode_list=list(range(1, 144)))
    no_exists = {
        1: {
            1: NotExistMediaInfo(
                season=1,
                episodes=[],
                total_episode=143,
                start_episode=1,
                require_complete_coverage=True,
            )
        }
    }

    downloads, lefts = chain.batch_download(contexts=[context], no_exists=no_exists)

    assert downloads == [context]
    assert lefts == {}
    chain.download_torrent.assert_not_called()
    chain.download_single.assert_called_once()


def test_batch_download_rejects_complete_coverage_when_title_episodes_are_partial(monkeypatch):
    """
    显式标出局部范围的候选不能满足完整覆盖任务。
    """
    _FakeBatchTorrentHelper.episodes = []
    monkeypatch.setattr(download_module, "TorrentHelper", _FakeBatchTorrentHelper)
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)

    chain = DownloadChain.__new__(DownloadChain)
    chain.download_torrent = MagicMock()
    chain.download_single = MagicMock(return_value="hash")

    context = _build_tv_context(episode_list=list(range(1, 14)))
    no_exists = {
        1: {
            1: NotExistMediaInfo(
                season=1,
                episodes=[],
                total_episode=143,
                start_episode=1,
                require_complete_coverage=True,
            )
        }
    }

    downloads, lefts = chain.batch_download(contexts=[context], no_exists=no_exists)

    assert downloads == []
    assert lefts == no_exists
    chain.download_torrent.assert_not_called()
    chain.download_single.assert_not_called()


def test_batch_download_complete_coverage_ignores_allowed_episode_narrowing(monkeypatch):
    """
    完整覆盖任务不能因候选允许集裁剪而把局部包误判为覆盖目标范围。
    """
    _FakeBatchTorrentHelper.episodes = []
    monkeypatch.setattr(download_module, "TorrentHelper", _FakeBatchTorrentHelper)
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)

    chain = DownloadChain.__new__(DownloadChain)
    chain.download_torrent = MagicMock()
    chain.download_single = MagicMock(return_value="hash")

    context = _build_tv_context(episode_list=[1, 2])
    context.allowed_episodes = {1, 2}
    no_exists = {
        1: {
            1: NotExistMediaInfo(
                season=1,
                episodes=[],
                total_episode=12,
                start_episode=1,
                require_complete_coverage=True,
            )
        }
    }

    downloads, lefts = chain.batch_download(contexts=[context], no_exists=no_exists)

    assert downloads == []
    assert lefts == no_exists
    chain.download_torrent.assert_not_called()
    chain.download_single.assert_not_called()


def test_batch_download_keeps_count_check_without_complete_coverage(monkeypatch):
    """
    普通整季缺失仍沿用数量判断，避免完整覆盖语义影响非严格场景。
    """
    _FakeBatchTorrentHelper.episodes = list(range(2, 145))
    monkeypatch.setattr(download_module, "TorrentHelper", _FakeBatchTorrentHelper)
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)

    chain = DownloadChain.__new__(DownloadChain)
    chain.download_torrent = MagicMock(return_value=(b"torrent-content", "", ["demo.mkv"]))
    chain.download_single = MagicMock(return_value="hash")

    context = _build_tv_context()
    no_exists = {
        1: {
            1: NotExistMediaInfo(
                season=1,
                episodes=[],
                total_episode=143,
                start_episode=1,
            )
        }
    }

    downloads, lefts = chain.batch_download(contexts=[context], no_exists=no_exists)

    assert downloads == [context]
    assert lefts == {}
    chain.download_single.assert_called_once()


def test_batch_download_checks_files_for_located_absolute_numbered_full_pack(monkeypatch):
    """
    普通订阅可借助集数定位把累计总集编号候选放入整包检查，再由种子文件确认季内集数覆盖。
    """
    _FakeBatchTorrentHelper.episodes = list(range(1, 27))
    monkeypatch.setattr(download_module, "TorrentHelper", _FakeBatchTorrentHelper)
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)

    chain = DownloadChain.__new__(DownloadChain)
    chain.download_torrent = MagicMock(return_value=(b"torrent-content", "", ["demo.mkv"]))
    chain.download_single = MagicMock(return_value="hash")

    context = _build_tv_context(episode_list=list(range(57, 83)))
    context.meta_info.season_list = [5]
    context.located_episodes = set(range(1, 27))
    no_exists = {
        1: {
            5: NotExistMediaInfo(
                season=5,
                episodes=[],
                total_episode=26,
                start_episode=1,
            )
        }
    }

    downloads, lefts = chain.batch_download(contexts=[context], no_exists=no_exists)

    assert downloads == [context]
    assert lefts == {}
    chain.download_torrent.assert_called_once()
    chain.download_single.assert_called_once()


def test_batch_download_selects_needed_files_for_located_absolute_numbered_pack(monkeypatch):
    """
    分集洗版可借助集数定位进入拆包检查，并只选择订阅仍需要的季内集数。
    """
    _FakeBatchTorrentHelper.episodes = list(range(1, 27))
    monkeypatch.setattr(download_module, "TorrentHelper", _FakeBatchTorrentHelper)
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)

    chain = DownloadChain.__new__(DownloadChain)
    chain.download_torrent = MagicMock(return_value=(b"torrent-content", "", ["demo.mkv"]))
    chain.download_single = MagicMock(return_value="hash")

    context = _build_tv_context(episode_list=list(range(57, 83)))
    context.meta_info.season_list = [5]
    context.allowed_episodes = {1, 2}
    context.located_episodes = set(range(1, 27))
    no_exists = {
        1: {
            5: NotExistMediaInfo(
                season=5,
                episodes=[1, 2],
                total_episode=26,
                start_episode=1,
            )
        }
    }

    downloads, lefts = chain.batch_download(contexts=[context], no_exists=no_exists)

    assert downloads == [context]
    assert lefts == {}
    chain.download_torrent.assert_called_once()
    chain.download_single.assert_called_once()
    assert chain.download_single.call_args.kwargs["episodes"] == {1, 2}
