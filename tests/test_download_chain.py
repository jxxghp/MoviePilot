from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import app.chain.download as download_module
from app.chain.download import DownloadChain
from app.core.config import settings
from app.core.context import Context, MediaInfo, SubtitleInfo, TorrentInfo
from app.core.metainfo import MetaInfo
from app.schemas import FileItem, NotExistMediaInfo, TransferDirectoryConf
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


def _download_dirs():
    """
    构造下载成功路径测试使用的允许下载目录配置。
    """
    return [
        TransferDirectoryConf(
            name="本地下载",
            priority=1,
            storage="local",
            download_path="/downloads",
        ),
    ]


class _FakeSubtitleStorageChain:
    """
    模拟字幕 API 保存文件时使用的存储链。
    """

    def __init__(self):
        """
        初始化上传记录。
        """
        self.uploaded_files = []

    def get_folder(self, storage, path):
        """
        模拟目标目录存在或已创建。
        """
        return FileItem(storage=storage, type="dir", path=path.as_posix(), name=path.name)

    def get_file_item(self, _storage, _path):
        """
        模拟目标字幕文件不存在。
        """
        return None

    def upload_file(self, fileitem, path):
        """
        记录上传的临时字幕文件。
        """
        self.uploaded_files.append(path)
        return FileItem(
            storage=fileitem.storage,
            type="file",
            path=(Path(fileitem.path) / path.name).as_posix(),
            name=path.name,
        )


class _FakeSubtitleResponse:
    """
    模拟字幕 API 下载响应。
    """

    content = b"subtitle-content"
    headers = {}


class _FakeSubtitleResponseWithHeader:
    """
    模拟带下载文件名响应头的字幕 API 响应。
    """

    content = b"archive-content"
    headers = {
        "content-disposition": (
            'attachment; filename="Hypnosis_AKA_Saimin_(1999)_480i_JAPANESE_NTSC_DVD_REMUX_MPEG-2_DD_2.0-MeeSta.rar"'
        )
    }


def test_download_single_submits_download_added_to_background(monkeypatch):
    """
    添加下载成功后，站点字幕等后处理应提交到后台，不能阻塞下载接口返回。
    """
    _FakeThreadHelper.submitted = []
    monkeypatch.setattr(
        "app.helper.directory.DirectoryHelper.get_download_dirs",
        lambda _self: _download_dirs(),
    )
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
        meta_info=MetaInfo("Demo Movie 2024"),
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


def test_download_single_persists_custom_words_snapshot(monkeypatch):
    """下载成功登记历史时，应把传入的订阅识别词原样存入快照，供整理时原样复现识别。"""
    captured = {}

    class _CapturingDownloadHistoryOper:
        """捕获写入下载历史的字段，验证识别词快照确实落库。"""

        def add(self, **kwargs):
            captured.update(kwargs)

        def add_files(self, _files):
            pass

    _FakeThreadHelper.submitted = []
    monkeypatch.setattr(
        "app.helper.directory.DirectoryHelper.get_download_dirs",
        lambda _self: _download_dirs(),
    )
    monkeypatch.setattr(download_module, "ThreadHelper", _FakeThreadHelper)
    monkeypatch.setattr(download_module, "DownloadHistoryOper", _CapturingDownloadHistoryOper)
    monkeypatch.setattr(download_module, "TorrentHelper", _FakeTorrentHelper)

    chain = DownloadChain.__new__(DownloadChain)
    chain.download = MagicMock(return_value=("qb", "hash123", "Original", "添加下载成功"))
    chain.download_added = MagicMock()
    chain.eventmanager = MagicMock()
    chain.eventmanager.send_event.return_value = None
    chain.post_message = MagicMock()

    context = Context(
        meta_info=MetaInfo("Demo Show 2024"),
        media_info=MediaInfo(
            type=MediaType.TV,
            title="Demo Show",
            year="2024",
            tmdb_id=1,
            genre_ids=[18],
        ),
        torrent_info=TorrentInfo(
            title="Demo Show 2024",
            enclosure="https://example.com/demo.torrent",
            site_cookie="uid=1",
            site_name="TestSite",
        ),
    )

    custom_words = "S04 => S01\n第 <> 集 >> EP+66"
    result = chain.download_single(
        context=context,
        torrent_content=b"torrent-content",
        save_path="/downloads",
        username="tester",
        custom_words=custom_words,
    )

    assert result == "hash123"
    assert captured["custom_words"] == custom_words


def test_save_subtitle_response_creates_missing_temp_directory(monkeypatch, tmp_path):
    """
    下载字幕 API 保存响应前应自动创建缺失的临时目录。
    """
    storage_chain = _FakeSubtitleStorageChain()
    temp_path = tmp_path / "missing-temp"
    assert not temp_path.exists()

    monkeypatch.setattr(
        download_module,
        "settings",
        SimpleNamespace(TEMP_PATH=temp_path, RMT_SUBEXT=settings.RMT_SUBEXT),
    )
    monkeypatch.setattr(download_module, "StorageChain", lambda: storage_chain)
    chain = DownloadChain.__new__(DownloadChain)
    subtitle = SubtitleInfo(
        title="Demo Movie",
        enclosure="https://example.test/subtitle.srt",
        file_name="Demo.Movie.zh-cn.srt",
    )

    success, message, saved_files = chain._save_subtitle_response(
        subtitle=subtitle,
        response=_FakeSubtitleResponse(),
        storage="local",
        target_dir=Path("/downloads"),
    )

    assert temp_path.exists()
    assert success
    assert message == "字幕文件保存成功"
    assert saved_files == ["/downloads/Demo.Movie.zh-cn.srt"]
    assert storage_chain.uploaded_files


def test_save_subtitle_response_accepts_rar_filename_from_header(monkeypatch, tmp_path):
    """
    PHP 下载链接应按响应头文件名识别 RAR 字幕压缩包，而不是按 URL 后缀误拒绝。
    """
    storage_chain = _FakeSubtitleStorageChain()
    temp_path = tmp_path / "temp"
    extracted_dir = temp_path / "Hypnosis_AKA_Saimin_(1999)_480i_JAPANESE_NTSC_DVD_REMUX_MPEG-2_DD_2.0-MeeSta"
    extracted_subtitle = extracted_dir / "Hypnosis_AKA_Saimin_(1999).srt"

    def fake_unpack_archive(archive_file, extract_dir, archive_format=None):
        assert archive_format == "rar"
        assert archive_file.suffix == ".rar"
        extract_dir.mkdir(parents=True, exist_ok=True)
        extracted_subtitle.write_text("subtitle", encoding="utf-8")

    monkeypatch.setattr(
        download_module,
        "settings",
        SimpleNamespace(TEMP_PATH=temp_path, RMT_SUBEXT=settings.RMT_SUBEXT),
    )
    monkeypatch.setattr(download_module, "StorageChain", lambda: storage_chain)
    monkeypatch.setattr(download_module.SystemUtils, "unpack_archive", fake_unpack_archive)

    chain = DownloadChain.__new__(DownloadChain)
    subtitle = SubtitleInfo(
        title="Hypnosis",
        enclosure="https://audiences.me/downloadsubs.php?torrentid=666519&subid=2195",
    )

    success, message, saved_files = chain._save_subtitle_response(
        subtitle=subtitle,
        response=_FakeSubtitleResponseWithHeader(),
        storage="local",
        target_dir=Path("/downloads"),
    )

    assert success
    assert message == "字幕文件保存成功"
    assert saved_files == ["/downloads/Hypnosis_AKA_Saimin_(1999).srt"]
    assert storage_chain.uploaded_files == [extracted_subtitle]


def test_save_subtitle_response_rejects_unsupported_filename_from_header(monkeypatch, tmp_path):
    """
    响应头文件名不是字幕或支持的压缩包时，应继续拒绝保存。
    """
    storage_chain = _FakeSubtitleStorageChain()
    temp_path = tmp_path / "temp"
    response = SimpleNamespace(
        content=b"<html>error</html>",
        headers={"content-disposition": 'attachment; filename="error.html"'},
    )

    monkeypatch.setattr(
        download_module,
        "settings",
        SimpleNamespace(TEMP_PATH=temp_path, RMT_SUBEXT=settings.RMT_SUBEXT),
    )
    monkeypatch.setattr(download_module, "StorageChain", lambda: storage_chain)

    chain = DownloadChain.__new__(DownloadChain)
    subtitle = SubtitleInfo(
        title="Hypnosis",
        enclosure="https://audiences.me/downloadsubs.php?torrentid=666519&subid=2195",
    )

    success, message, saved_files = chain._save_subtitle_response(
        subtitle=subtitle,
        response=response,
        storage="local",
        target_dir=Path("/downloads"),
    )

    assert not success
    assert message == "下载链接不是支持的字幕文件：error.html"
    assert saved_files == []
    assert storage_chain.uploaded_files == []


class _FakeBatchTorrentHelper:
    """
    为批量下载测试提供稳定排序和种子文件集数解析。
    """

    episodes = []

    def sort_torrents(self, contexts):
        """
        保持测试输入顺序，避免依赖真实站点优先级配置。
        """
        return contexts

    def sort_group_torrents(self, contexts):
        """
        模拟真实提前控重行为，回归时会丢掉同一媒体季集的后续候选。
        """
        results = []
        added = set()
        for context in contexts:
            media = context.media_info
            meta = context.meta_info
            if media.type == MediaType.TV:
                media_name = f"{media.title_year}{meta.season_episode}"
            else:
                media_name = media.title_year
            if media_name in added:
                continue
            added.add(media_name)
            results.append(context)
        return results

    def get_torrent_episodes(self, _files):
        return list(self.episodes)


def _build_tv_context(episode_list=None):
    """
    构造标题未显式标集数的单季电视剧候选。
    """
    episodes = episode_list or []
    return SimpleNamespace(
        media_info=SimpleNamespace(
            type=MediaType.TV,
            title_year="Test Show (2026)",
            tmdb_id=1,
            douban_id=None,
        ),
        meta_info=SimpleNamespace(
            season_list=[1],
            season_episode="S01E01",
            episode_list=episodes,
            title="Test Show",
            org_string="Test Show S01 2160p",
            set_episodes=lambda begin, end: None,
        ),
        torrent_info=SimpleNamespace(title="Test Show S01 2160p", site_name="TestSite"),
        allowed_episodes=None,
        confirmed_full_coverage=False,
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
    assert context.confirmed_full_coverage is False
    chain.download_single.assert_not_called()


def test_batch_download_rejects_complete_coverage_when_only_missing_episodes_match(monkeypatch):
    """
    完整覆盖要求目标范围全集，不能只覆盖当前缺口集。
    """
    _FakeBatchTorrentHelper.episodes = [4, 5]
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
                episodes=[4, 5],
                total_episode=5,
                start_episode=1,
                require_complete_coverage=True,
            )
        }
    }

    downloads, lefts = chain.batch_download(contexts=[context], no_exists=no_exists)

    assert downloads == []
    assert lefts == no_exists
    assert context.confirmed_full_coverage is False
    chain.download_single.assert_not_called()


def test_batch_download_tries_next_episode_candidate_when_first_download_fails(monkeypatch):
    """
    同一季集的首个候选下载失败时，应继续尝试排序后的下一个候选资源。
    """
    _FakeBatchTorrentHelper.episodes = []
    monkeypatch.setattr(download_module, "TorrentHelper", _FakeBatchTorrentHelper)
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)

    chain = DownloadChain.__new__(DownloadChain)
    chain.download_single = MagicMock(side_effect=[None, "hash"])

    first_context = _build_tv_context(episode_list=[1])
    first_context.torrent_info.title = "Test Show S01E01 First"
    second_context = _build_tv_context(episode_list=[1])
    second_context.torrent_info.title = "Test Show S01E01 Second"
    no_exists = {
        1: {
            1: NotExistMediaInfo(
                season=1,
                episodes=[1],
                total_episode=1,
                start_episode=1,
            )
        }
    }

    downloads, lefts = chain.batch_download(
        contexts=[first_context, second_context],
        no_exists=no_exists,
    )

    assert downloads == [second_context]
    assert lefts == {}
    assert chain.download_single.call_count == 2
    assert chain.download_single.call_args_list[0].args[0] is first_context
    assert chain.download_single.call_args_list[1].args[0] is second_context


def test_batch_download_does_not_download_duplicate_movie_after_success(monkeypatch):
    """
    电影保留失败重试能力，但同一影片成功一次后不应继续添加后续候选。
    """
    _FakeBatchTorrentHelper.episodes = []
    monkeypatch.setattr(download_module, "TorrentHelper", _FakeBatchTorrentHelper)
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)

    chain = DownloadChain.__new__(DownloadChain)
    chain.download_single = MagicMock(return_value="hash")

    first_context = SimpleNamespace(
        media_info=SimpleNamespace(type=MediaType.MOVIE, title_year="Demo Movie (2026)"),
        meta_info=SimpleNamespace(season_episode=""),
        torrent_info=SimpleNamespace(title="Demo Movie First"),
    )
    second_context = SimpleNamespace(
        media_info=SimpleNamespace(type=MediaType.MOVIE, title_year="Demo Movie (2026)"),
        meta_info=SimpleNamespace(season_episode=""),
        torrent_info=SimpleNamespace(title="Demo Movie Second"),
    )

    downloads, lefts = chain.batch_download(contexts=[first_context, second_context])

    assert downloads == [first_context]
    assert lefts is None
    chain.download_single.assert_called_once()
    assert chain.download_single.call_args.args[0] is first_context


def test_batch_download_threads_custom_words_to_download_single(monkeypatch):
    """订阅识别词须经 batch_download 透传到 download_single，作为整理快照随下载存档。"""
    _FakeBatchTorrentHelper.episodes = []
    monkeypatch.setattr(download_module, "TorrentHelper", _FakeBatchTorrentHelper)
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)

    chain = DownloadChain.__new__(DownloadChain)
    chain.download_single = MagicMock(return_value="hash")

    context = SimpleNamespace(
        media_info=SimpleNamespace(type=MediaType.MOVIE, title_year="Demo Movie (2026)"),
        meta_info=SimpleNamespace(season_episode=""),
        torrent_info=SimpleNamespace(title="Demo Movie"),
    )

    custom_words = "S04 => S01\n第 <> 集 >> EP+66"
    downloads, _lefts = chain.batch_download(contexts=[context], custom_words=custom_words)

    assert downloads == [context]
    chain.download_single.assert_called_once()
    assert chain.download_single.call_args.kwargs["custom_words"] == custom_words


def test_download_single_records_failure_cooldown_when_downloader_rejects(monkeypatch):
    """
    下载器拒绝种子且没有返回 hash 时，应记录资源级失败冷却。
    """
    captured = {}

    class _CapturingDownloadFailureOper:
        """
        捕获下载失败冷却记录，避免测试写入数据库。
        """

        def record_failure(self, **kwargs: object) -> SimpleNamespace:
            """
            保存写入字段供断言使用。
            """
            captured.update(kwargs)
            return SimpleNamespace(id=1)

    monkeypatch.setattr(
        "app.helper.directory.DirectoryHelper.get_download_dirs",
        lambda _self: _download_dirs(),
    )
    monkeypatch.setattr(download_module, "TorrentHelper", _FakeTorrentHelper)
    monkeypatch.setattr(download_module, "DownloadFailureOper", _CapturingDownloadFailureOper)
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)

    chain = DownloadChain.__new__(DownloadChain)
    error_msg = "添加种子任务失败：无法读取种子文件"
    chain.download = MagicMock(return_value=("qb", None, "Original", error_msg))
    chain.post_message = MagicMock()

    context = Context(
        meta_info=MetaInfo("Demo Movie 2026"),
        media_info=MediaInfo(
            type=MediaType.MOVIE,
            title="Demo Movie",
            year="2026",
            tmdb_id=1,
            genre_ids=[18],
        ),
        torrent_info=TorrentInfo(
            site=12,
            site_name="AGSVPT",
            title="Demo Movie 2026 1080p",
            enclosure="https://example.com/download.php?id=484660",
            size=1024,
        ),
    )

    download_id, returned_error = chain.download_single(
        context=context,
        torrent_content=b"torrent-content",
        save_path="/downloads",
        source="Subscribe|{}",
        return_detail=True,
    )

    assert download_id is None
    assert returned_error == error_msg
    assert captured["fingerprint"] == DownloadChain._build_download_failure_fingerprint(context)
    assert captured["torrent_id"] == "example.com:id=484660"
    assert captured["site"] == 12
    assert captured["error_message"] == error_msg
    assert captured["next_retry_at"] > captured["now_time"]


def test_batch_download_skips_failed_subscription_resource_and_tries_next(monkeypatch):
    """
    订阅自动下载应跳过冷却中的失败资源，但继续尝试同媒体的后续候选。
    """
    _FakeBatchTorrentHelper.episodes = []
    monkeypatch.setattr(download_module, "TorrentHelper", _FakeBatchTorrentHelper)
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)

    first_context = SimpleNamespace(
        media_info=SimpleNamespace(
            type=MediaType.MOVIE,
            title="Demo Movie",
            year="2026",
            title_year="Demo Movie (2026)",
            tmdb_id=1,
            douban_id=None,
        ),
        meta_info=SimpleNamespace(season=None, episode=None, episode_list=[], season_episode=""),
        torrent_info=SimpleNamespace(
            site=12,
            site_name="AGSVPT",
            title="Demo Movie Bad",
            torrent_id="484660",
            size=1024,
        ),
    )
    second_context = SimpleNamespace(
        media_info=SimpleNamespace(
            type=MediaType.MOVIE,
            title="Demo Movie",
            year="2026",
            title_year="Demo Movie (2026)",
            tmdb_id=1,
            douban_id=None,
        ),
        meta_info=SimpleNamespace(season=None, episode=None, episode_list=[], season_episode=""),
        torrent_info=SimpleNamespace(
            site=13,
            site_name="OtherSite",
            title="Demo Movie Good",
            torrent_id="999999",
            size=2048,
        ),
    )
    failed_fingerprint = DownloadChain._build_download_failure_fingerprint(first_context)

    class _ActiveDownloadFailureOper:
        """
        返回第一个候选的活跃失败冷却记录。
        """

        def get_active_by_fingerprints(self, fingerprints: list[str], now_time: str) -> dict:
            """
            模拟数据库批量查询活跃失败记录。
            """
            assert now_time
            assert failed_fingerprint in fingerprints
            return {failed_fingerprint: SimpleNamespace(fingerprint=failed_fingerprint)}

    monkeypatch.setattr(download_module, "DownloadFailureOper", _ActiveDownloadFailureOper)

    chain = DownloadChain.__new__(DownloadChain)
    chain.download_single = MagicMock(return_value="hash")

    downloads, lefts = chain.batch_download(
        contexts=[first_context, second_context],
        source="Subscribe|{}",
    )

    assert downloads == [second_context]
    assert lefts is None
    chain.download_single.assert_called_once()
    assert chain.download_single.call_args.args[0] is second_context


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
    assert context.confirmed_full_coverage is True
    chain.download_single.assert_called_once()


def test_batch_download_rejects_complete_coverage_when_files_have_same_count_but_wrong_range(monkeypatch):
    """
    完整覆盖按目标集号集合判断，不能让同数量的偏移局部包通过。
    """
    _FakeBatchTorrentHelper.episodes = list(range(1, 45))
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

    assert downloads == []
    assert lefts == no_exists
    assert context.confirmed_full_coverage is False
    chain.download_single.assert_not_called()


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
    assert context.confirmed_full_coverage is True
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
    assert context.confirmed_full_coverage is False
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
    assert context.confirmed_full_coverage is False
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
    assert context.confirmed_full_coverage is False
    chain.download_single.assert_called_once()
