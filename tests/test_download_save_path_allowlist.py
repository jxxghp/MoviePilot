import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.agent.tools.impl.add_download_tasks as add_tasks_module
import app.agent.tools.impl.update_download_tasks as update_tasks_module
import app.chain.download as download_module
from app.agent.tools.impl.add_download_tasks import AddDownloadTasksTool
from app.agent.tools.impl.update_download_tasks import UpdateDownloadTasksTool
from app.chain.download import DownloadChain
from app.core.context import Context, MediaInfo, SubtitleInfo, TorrentInfo
from app.core.metainfo import MetaInfo
from app.helper.directory import validate_download_save_path
from app.schemas import DownloaderTorrent, TransferDirectoryConf
from app.schemas.types import MediaType


@pytest.fixture(autouse=True)
def _mock_tmdb_supplement(monkeypatch):
    """隔离下载路径用例中的 TMDB 辅助识别外部边界。"""

    class _NoopMediaChain:
        """保持原媒体对象不变的 TMDB 辅助识别替身。"""

        @staticmethod
        def supplement_tmdb_info(media, _meta):
            """返回原媒体对象。"""
            return media

    monkeypatch.setattr(download_module, "MediaChain", _NoopMediaChain)


def _download_dirs():
    return [
        TransferDirectoryConf(
            name="本地下载",
            priority=1,
            storage="local",
            download_path="/downloads",
        ),
        TransferDirectoryConf(
            name="动漫远程下载",
            priority=2,
            storage="rclone",
            download_path="/media/anime",
        ),
    ]


def _windows_download_dirs():
    return [
        TransferDirectoryConf(
            name="Windows 下载",
            priority=1,
            storage="local",
            download_path="C:/downloads",
        ),
    ]


def _classified_download_dirs():
    return [
        TransferDirectoryConf(
            name="分类下载",
            priority=1,
            storage="local",
            download_path="/downloads",
            download_type_folder=True,
            download_category_folder=True,
        ),
        TransferDirectoryConf(
            name="远程分类下载",
            priority=2,
            storage="rclone",
            download_path="/media",
            download_type_folder=True,
            download_category_folder=True,
        ),
    ]


def _media_specific_download_dirs():
    return [
        TransferDirectoryConf(
            name="电影下载",
            priority=1,
            storage="local",
            download_path="/downloads",
            media_type=MediaType.MOVIE.value,
            download_category_folder=True,
        ),
        TransferDirectoryConf(
            name="电视剧下载",
            priority=2,
            storage="local",
            download_path="/downloads",
            media_type=MediaType.TV.value,
            download_category_folder=True,
        ),
    ]


def _nested_download_dirs():
    return [
        TransferDirectoryConf(
            name="A",
            priority=1,
            storage="local",
            download_path="/downloads",
            download_type_folder=True,
        ),
        TransferDirectoryConf(
            name="B",
            priority=2,
            storage="local",
            download_path="/downloads/tv",
            download_category_folder=True,
        ),
        TransferDirectoryConf(
            name="C",
            priority=3,
            storage="local",
            download_path="/downloads/tv/collection",
            download_type_folder=True,
            download_category_folder=True,
        ),
    ]


@pytest.fixture(autouse=True)
def patch_download_dirs(monkeypatch):
    monkeypatch.setattr(
        "app.helper.directory.DirectoryHelper.get_download_dirs",
        lambda _self: _download_dirs(),
    )


@pytest.mark.parametrize(
    ("save_path", "expected"),
    [
        ("/downloads", "/downloads"),
        ("/downloads/movie/demo", "/downloads/movie/demo"),
        ("rclone:/media/anime/sub", "rclone:/media/anime/sub"),
    ],
)
def test_validate_download_save_path_accepts_configured_roots_and_children(save_path, expected):
    assert validate_download_save_path(save_path) == expected


def test_validate_download_save_path_accepts_legacy_remote_path_without_storage_prefix():
    """旧版订阅保存的远程原始路径应恢复为带存储前缀的 FileURI。"""
    assert validate_download_save_path("/media/anime/sub") == "rclone:/media/anime/sub"


def test_validate_download_save_path_prefers_configured_local_root(monkeypatch):
    """无前缀路径同时命中本地和远程根目录时应保持本地语义。"""
    monkeypatch.setattr(
        "app.helper.directory.DirectoryHelper.get_download_dirs",
        lambda _self: [
            TransferDirectoryConf(
                name="远程下载",
                priority=1,
                storage="rclone",
                download_path="/shared",
            ),
            TransferDirectoryConf(
                name="本地下载",
                priority=2,
                storage="local",
                download_path="/shared",
            ),
        ],
    )

    assert validate_download_save_path("/shared/movie") == "/shared/movie"


@pytest.mark.parametrize(
    ("save_path", "expected"),
    [
        ("C:/downloads", "C:/downloads"),
        ("C:/downloads/movie", "C:/downloads/movie"),
    ],
)
def test_validate_download_save_path_accepts_windows_configured_root_and_children(
    monkeypatch,
    save_path,
    expected,
):
    monkeypatch.setattr(
        "app.helper.directory.DirectoryHelper.get_download_dirs",
        lambda _self: _windows_download_dirs(),
    )

    assert validate_download_save_path(save_path) == expected


@pytest.mark.parametrize(
    "save_path",
    [
        "C:/other",
        "D:/downloads",
        "C:/downloads/../Windows",
        "C:\\downloads\\movie",
        "\\\\server\\share\\downloads",
    ],
)
def test_validate_download_save_path_rejects_windows_paths_outside_configured_root(
    monkeypatch,
    save_path,
):
    monkeypatch.setattr(
        "app.helper.directory.DirectoryHelper.get_download_dirs",
        lambda _self: _windows_download_dirs(),
    )

    with pytest.raises(ValueError):
        validate_download_save_path(save_path)


@pytest.mark.parametrize(
    "save_path",
    [
        "/etc",
        "/downloads/../etc",
        "/downloads\\..\\etc",
        "C:/downloads",
        "\\\\server\\share\\downloads",
        "//server/share/downloads",
        "relative/downloads",
        "",
        "   ",
        "rclone:/media/movies",
        "smb:/media/anime/sub",
    ],
)
def test_validate_download_save_path_rejects_paths_outside_configured_roots(save_path):
    with pytest.raises(ValueError):
        validate_download_save_path(save_path)


def _build_tv_media() -> MediaInfo:
    return MediaInfo(
        type=MediaType.TV,
        title="Demo Show",
        year="2026",
        tmdb_id=2,
        genre_ids=[16],
        category="动漫",
    )


def test_resolve_media_download_dir_applies_configured_root_classification(monkeypatch):
    monkeypatch.setattr(
        "app.helper.directory.DirectoryHelper.get_download_dirs",
        lambda _self: _classified_download_dirs(),
    )

    storage, target_dir, error_msg = DownloadChain._resolve_media_download_dir(
        media_info=_build_tv_media(),
        save_path="/downloads",
    )

    assert storage == "local"
    assert target_dir == Path("/downloads/电视剧/动漫")
    assert error_msg == ""


def test_resolve_media_download_dir_keeps_configured_child_path_exact(monkeypatch):
    monkeypatch.setattr(
        "app.helper.directory.DirectoryHelper.get_download_dirs",
        lambda _self: _classified_download_dirs(),
    )

    storage, target_dir, error_msg = DownloadChain._resolve_media_download_dir(
        media_info=_build_tv_media(),
        save_path="/downloads/收藏区",
    )

    assert storage == "local"
    assert target_dir == Path("/downloads/收藏区")
    assert error_msg == ""


def test_resolve_media_download_dir_applies_remote_root_classification(monkeypatch):
    monkeypatch.setattr(
        "app.helper.directory.DirectoryHelper.get_download_dirs",
        lambda _self: _classified_download_dirs(),
    )

    storage, target_dir, error_msg = DownloadChain._resolve_media_download_dir(
        media_info=_build_tv_media(),
        save_path="rclone:/media",
    )

    assert storage == "rclone"
    assert target_dir == Path("/media/电视剧/动漫")
    assert error_msg == ""


def test_resolve_media_download_dir_accepts_legacy_remote_root_without_storage_prefix(monkeypatch):
    """订阅中的旧版远程根路径应按对应存储和分类配置解析。"""
    monkeypatch.setattr(
        "app.helper.directory.DirectoryHelper.get_download_dirs",
        lambda _self: _classified_download_dirs(),
    )

    storage, target_dir, error_msg = DownloadChain._resolve_media_download_dir(
        media_info=_build_tv_media(),
        save_path="/media",
    )

    assert storage == "rclone"
    assert target_dir == Path("/media/电视剧/动漫")
    assert error_msg == ""


def test_resolve_media_download_dir_uses_matching_media_specific_root(monkeypatch):
    monkeypatch.setattr(
        "app.helper.directory.DirectoryHelper.get_download_dirs",
        lambda _self: _media_specific_download_dirs(),
    )

    storage, target_dir, error_msg = DownloadChain._resolve_media_download_dir(
        media_info=_build_tv_media(),
        save_path="/downloads",
    )

    assert storage == "local"
    assert target_dir == Path("/downloads/动漫")
    assert error_msg == ""


@pytest.mark.parametrize(
    ("save_path", "expected"),
    [
        ("/downloads", "/downloads/电视剧"),
        ("/downloads/tv", "/downloads/tv/动漫"),
        ("/downloads/tv/collection", "/downloads/tv/collection/电视剧/动漫"),
    ],
)
def test_resolve_media_download_dir_uses_exact_nested_root_configuration(
    monkeypatch,
    save_path,
    expected,
):
    monkeypatch.setattr(
        "app.helper.directory.DirectoryHelper.get_download_dirs",
        lambda _self: _nested_download_dirs(),
    )

    storage, target_dir, error_msg = DownloadChain._resolve_media_download_dir(
        media_info=_build_tv_media(),
        save_path=save_path,
    )

    assert storage == "local"
    assert target_dir == Path(expected)
    assert error_msg == ""


def _build_context() -> Context:
    return Context(
        meta_info=MetaInfo("Demo Movie 2026"),
        media_info=MediaInfo(
            type=MediaType.MOVIE,
            title="Demo Movie",
            year="2026",
            tmdb_id=1,
            genre_ids=[18],
        ),
        torrent_info=TorrentInfo(
            title="Demo Movie 2026",
            enclosure="https://example.test/demo.torrent",
            site_cookie="uid=1",
            site_name="TestSite",
        ),
    )


def _build_download_chain() -> DownloadChain:
    chain = DownloadChain.__new__(DownloadChain)
    chain.download = MagicMock()
    chain.post_message = MagicMock()
    chain.messagehelper = MagicMock()
    return chain


def test_download_single_rejects_bad_save_path_before_downloader(monkeypatch):
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)
    chain = _build_download_chain()

    download_id, error_msg = chain.download_single(
        context=_build_context(),
        torrent_content=b"torrent-content",
        save_path="/etc",
        return_detail=True,
    )

    assert download_id is None
    assert "保存路径" in error_msg
    chain.download.assert_not_called()


def test_download_single_rejects_event_overridden_bad_save_path_before_downloader(monkeypatch):
    event_data = SimpleNamespace(cancel=False, source="plugin", reason="", options={"save_path": "/etc"})
    monkeypatch.setattr(
        download_module.eventmanager,
        "send_event",
        lambda *args, **kwargs: SimpleNamespace(event_data=event_data),
    )
    chain = _build_download_chain()

    download_id, error_msg = chain.download_single(
        context=_build_context(),
        torrent_content=b"torrent-content",
        save_path="/downloads",
        return_detail=True,
    )

    assert download_id is None
    assert "保存路径" in error_msg
    chain.download.assert_not_called()


def test_download_single_applies_configured_root_classification(monkeypatch):
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.helper.directory.DirectoryHelper.get_download_dirs",
        lambda _self: _classified_download_dirs(),
    )
    chain = _build_download_chain()
    chain.download.return_value = ("qb", None, "Original", "test stop")
    context = _build_context()
    context.media_info = _build_tv_media()

    chain.download_single(
        context=context,
        torrent_content=b"torrent-content",
        save_path="/downloads",
    )

    assert chain.download.call_args.kwargs["download_dir"] == Path("/downloads/电视剧/动漫")


def test_download_single_accepts_legacy_remote_root_without_storage_prefix(monkeypatch):
    """旧订阅的无前缀远程根应以正确 FileURI 提交给下载模块。"""
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.helper.directory.DirectoryHelper.get_download_dirs",
        lambda _self: _classified_download_dirs(),
    )
    chain = _build_download_chain()
    chain.download.return_value = ("qb", None, "Original", "test stop")
    context = _build_context()
    context.media_info = _build_tv_media()

    chain.download_single(
        context=context,
        torrent_content=b"torrent-content",
        save_path="/media",
    )

    assert chain.download.call_args.kwargs["download_dir"] == Path("rclone:/media/电视剧/动漫")


@pytest.mark.parametrize("save_path", ["", "   "])
def test_download_single_rejects_explicit_empty_save_path_before_default_fallback(monkeypatch, save_path):
    monkeypatch.setattr(download_module.eventmanager, "send_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        download_module.DirectoryHelper,
        "get_dir",
        lambda *_args, **_kwargs: TransferDirectoryConf(storage="local", download_path="/downloads"),
    )
    chain = _build_download_chain()

    download_id, error_msg = chain.download_single(
        context=_build_context(),
        torrent_content=b"torrent-content",
        save_path=save_path,
        return_detail=True,
    )

    assert download_id is None
    assert "保存路径" in error_msg
    chain.download.assert_not_called()


def test_download_single_rejects_event_empty_save_path_override_before_downloader(monkeypatch):
    event_data = SimpleNamespace(cancel=False, source="plugin", reason="", options={"save_path": ""})
    monkeypatch.setattr(
        download_module.eventmanager,
        "send_event",
        lambda *args, **kwargs: SimpleNamespace(event_data=event_data),
    )
    chain = _build_download_chain()

    download_id, error_msg = chain.download_single(
        context=_build_context(),
        torrent_content=b"torrent-content",
        save_path="/downloads",
        return_detail=True,
    )

    assert download_id is None
    assert "保存路径" in error_msg
    chain.download.assert_not_called()


def test_resolve_media_download_dir_rejects_bad_subtitle_save_path():
    media_info = MediaInfo(
        type=MediaType.MOVIE,
        title="Demo Movie",
        year="2026",
        tmdb_id=1,
    )

    storage, target_dir, error_msg = DownloadChain._resolve_media_download_dir(
        media_info=media_info,
        save_path="/etc",
    )

    assert storage is None
    assert target_dir is None
    assert error_msg == "保存路径不在允许的下载目录范围内"


def test_download_subtitle_returns_specific_error_for_bad_save_path():
    chain = DownloadChain.__new__(DownloadChain)
    chain.recognize_media = MagicMock(
        return_value=MediaInfo(
            type=MediaType.MOVIE,
            title="Demo Movie",
            year="2026",
            tmdb_id=1,
        )
    )
    subtitle = SubtitleInfo(
        title="Demo Movie",
        enclosure="https://example.test/subtitle.srt",
    )

    success, message, saved_files = chain.download_subtitle(
        subtitle=subtitle,
        save_path="/etc",
    )

    assert not success
    assert message == "保存路径不在允许的下载目录范围内"
    assert saved_files == []


@pytest.mark.parametrize("save_path", ["", "   "])
def test_resolve_media_download_dir_rejects_explicit_empty_save_path_before_default_fallback(
    monkeypatch,
    save_path,
):
    monkeypatch.setattr(
        download_module.DirectoryHelper,
        "get_dir",
        lambda *_args, **_kwargs: TransferDirectoryConf(storage="local", download_path="/downloads"),
    )
    media_info = MediaInfo(
        type=MediaType.MOVIE,
        title="Demo Movie",
        year="2026",
        tmdb_id=1,
    )

    storage, target_dir, error_msg = DownloadChain._resolve_media_download_dir(
        media_info=media_info,
        save_path=save_path,
    )

    assert storage is None
    assert target_dir is None
    assert "保存路径" in error_msg


def test_add_download_tasks_direct_magnet_rejects_bad_save_path_before_downloader():
    with pytest.raises(ValueError):
        AddDownloadTasksTool._resolve_direct_download_dir("/etc")


@pytest.mark.parametrize("save_path", ["", "   "])
def test_add_download_tasks_direct_magnet_rejects_explicit_empty_save_path_before_default_fallback(save_path):
    with pytest.raises(ValueError):
        AddDownloadTasksTool._resolve_direct_download_dir(save_path)


def test_add_download_tasks_cached_context_rejects_bad_save_path_before_download_single(monkeypatch):
    download_chain = MagicMock()
    monkeypatch.setattr(add_tasks_module, "DownloadChain", lambda: download_chain)

    with pytest.raises(ValueError):
        AddDownloadTasksTool._download_single_sync(
            context=_build_context(),
            downloader="qb",
            save_path="/etc",
            merged_labels=None,
        )

    download_chain.download_single.assert_not_called()


@pytest.mark.parametrize("save_path", ["", "   "])
def test_add_download_tasks_cached_context_rejects_explicit_empty_save_path_before_download_single(
    monkeypatch,
    save_path,
):
    download_chain = MagicMock()
    monkeypatch.setattr(add_tasks_module, "DownloadChain", lambda: download_chain)

    with pytest.raises(ValueError):
        AddDownloadTasksTool._download_single_sync(
            context=_build_context(),
            downloader="qb",
            save_path=save_path,
            merged_labels=None,
        )

    download_chain.download_single.assert_not_called()


def test_update_download_tasks_rejects_bad_save_path_before_update_torrent(monkeypatch):
    hash_value = "a" * 40
    download_chain = MagicMock()
    download_chain.list_torrents.return_value = [
        DownloaderTorrent(downloader="qb", hash=hash_value, title="Demo")
    ]
    monkeypatch.setattr(update_tasks_module, "DownloadChain", lambda: download_chain)

    result = UpdateDownloadTasksTool._update_download_sync(
        hash_value=hash_value,
        save_path="/etc",
    )

    assert result["downloader"] == "qb"
    assert result["results"] == [
        {
            "operation": "save_path",
            "success": False,
            "message": "保存目录不在允许的下载目录范围内",
        }
    ]
    download_chain.update_torrent.assert_not_called()


def test_update_download_tasks_passes_normalized_save_path_to_update_torrent(monkeypatch):
    hash_value = "b" * 40
    download_chain = MagicMock()
    download_chain.list_torrents.return_value = [
        DownloaderTorrent(downloader="qb", hash=hash_value, title="Demo")
    ]
    download_chain.update_torrent.return_value = {"save_path": True}
    monkeypatch.setattr(update_tasks_module, "DownloadChain", lambda: download_chain)

    result = UpdateDownloadTasksTool._update_download_sync(
        hash_value=hash_value,
        save_path="rclone:/media/anime/sub",
    )

    assert result["results"][0]["success"] is True
    download_chain.update_torrent.assert_called_once_with(
        hash_string=hash_value,
        downloader="qb",
        download_limit=None,
        upload_limit=None,
        tracker_list=None,
        save_path="rclone:/media/anime/sub",
        category=None,
        ratio_limit=None,
        seeding_time_limit=None,
    )


def test_add_download_tasks_run_rejects_bad_save_path_before_partial_download(monkeypatch):
    tool = AddDownloadTasksTool(session_id="session-1", user_id="10001")
    download_chain = MagicMock()
    monkeypatch.setattr(add_tasks_module, "DownloadChain", lambda: download_chain)

    result = asyncio.run(
        tool.run(
            torrent_url=["magnet:?xt=urn:btih:123"],
            save_path="/etc",
        )
    )

    assert "save_path" in result
    download_chain.download.assert_not_called()


@pytest.mark.parametrize("save_path", ["", "   "])
def test_add_download_tasks_run_rejects_explicit_empty_save_path_before_partial_download(
    monkeypatch,
    save_path,
):
    tool = AddDownloadTasksTool(session_id="session-1", user_id="10001")
    download_chain = MagicMock()
    monkeypatch.setattr(add_tasks_module, "DownloadChain", lambda: download_chain)

    result = asyncio.run(
        tool.run(
            torrent_url=["magnet:?xt=urn:btih:123"],
            save_path=save_path,
        )
    )

    assert "save_path" in result
    download_chain.download.assert_not_called()
