"""已有下载任务的资源目录分类测试。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.api.endpoints.download as download_endpoint
import app.application.download.classification as classification_module
from app.application.directory import DirectoryHelper
from app.application.download.classification import (
    DownloadSourceClassificationPlan,
    DownloadSourceClassificationService,
    resolve_download_source_classification,
)
from app.application.history import DownloadHistorySnapshot
from app.domain.context import MusicInfo
from app.schemas.download import DownloadSourceClassificationRequest
from app.schemas.system import TransferDirectoryConf
from app.schemas.transfer import DownloaderTorrent
from app.schemas.types import MediaType

HASH = "a" * 40


def _history(**overrides: object) -> DownloadHistorySnapshot:
    """构造含稳定音乐分类快照的下载历史。"""
    values = {
        "id": 1,
        "path": "/volume1/UT/Musics/Example",
        "type": MediaType.MUSIC.value,
        "title": "Example",
        "media_category": "Album",
    }
    values.update(overrides)
    return DownloadHistorySnapshot(**values)


def test_resolve_source_classification_uses_task_root_and_history_category(monkeypatch):
    """不论任务内容名称如何，目标都应是配置根目录下的历史分类。"""
    directory = TransferDirectoryConf(
        storage="local",
        download_path="/volume1/UT/Musics",
        download_category_folder=True,
    )
    helper = MagicMock()
    helper.get_download_dir_by_task_path.return_value = directory
    helper.has_fixed_category.return_value = False
    helper.resolve_media_category.return_value = SimpleNamespace(
        usable=True,
        path=("Album",),
    )
    monkeypatch.setattr(
        classification_module,
        "validate_download_save_path",
        lambda value: value,
    )

    plan = resolve_download_source_classification(
        DownloaderTorrent(hash=HASH, save_path="/volume1/UT/Musics"),
        _history(),
        directory_helper=helper,
    )

    assert plan == DownloadSourceClassificationPlan(
        current_save_path="/volume1/UT/Musics",
        target_save_path="/volume1/UT/Musics/Album",
        category="Album",
        changed=True,
    )


def test_task_path_matching_prefers_the_deepest_configured_download_root(monkeypatch):
    """嵌套资源目录同时命中时必须使用最具体的配置根目录。"""
    generic = TransferDirectoryConf(
        storage="local",
        download_path="/downloads",
        download_category_folder=True,
    )
    music = TransferDirectoryConf(
        storage="local",
        download_path="/downloads/music",
        download_category_folder=True,
    )
    helper = DirectoryHelper(classification_resolver=MagicMock())
    monkeypatch.setattr(helper, "get_download_dirs", lambda: [generic, music])

    matched = helper.get_download_dir_by_task_path(
        MusicInfo(title="Example", category="Album"),
        "/downloads/music/Album",
    )

    assert matched is music


def test_source_classification_preview_has_no_downloader_side_effect():
    """预览只返回当前与目标目录，不调用下载器。"""
    torrent = DownloaderTorrent(hash=HASH, downloader="qb-main", save_path="/downloads")
    update_torrent = MagicMock()
    service = DownloadSourceClassificationService(
        list_torrents=lambda **_kwargs: [torrent],
        get_history_by_hash=lambda _hash: _history(),
        update_torrent=update_torrent,
        resolve_plan=lambda _torrent, _history, _category: DownloadSourceClassificationPlan(
            current_save_path="/downloads",
            target_save_path="/downloads/Album",
            category="Album",
            changed=True,
        ),
    )

    result = service.plan(hash_value=HASH)

    assert result["target_save_path"] == "/downloads/Album"
    assert result["executed"] is False
    update_torrent.assert_not_called()


def test_source_classification_execute_delegates_location_move_to_downloader():
    """确认执行时只向下载器提交校验后的目标保存目录。"""
    torrent = DownloaderTorrent(hash=HASH, downloader="qb-main", save_path="/downloads")
    update_torrent = MagicMock(return_value={"save_path": True})
    service = DownloadSourceClassificationService(
        list_torrents=lambda **_kwargs: [torrent],
        get_history_by_hash=lambda _hash: _history(),
        update_torrent=update_torrent,
        resolve_plan=lambda _torrent, _history, _category: DownloadSourceClassificationPlan(
            current_save_path="/downloads",
            target_save_path="/downloads/Album",
            category="Album",
            changed=True,
        ),
    )

    result = service.plan(hash_value=HASH, execute=True)

    assert result["executed"] is True
    update_torrent.assert_called_once_with(
        hash_string=HASH,
        downloader="qb-main",
        save_path="/downloads/Album",
    )


def test_source_classification_rejects_missing_history_category(monkeypatch):
    """无分类历史时不应猜测目录或移动做种数据。"""
    directory = TransferDirectoryConf(
        storage="local",
        download_path="/downloads",
        download_category_folder=True,
    )
    helper = MagicMock()
    helper.get_download_dir_by_task_path.return_value = directory
    helper.has_fixed_category.return_value = False
    helper.resolve_media_category.return_value = SimpleNamespace(
        usable=False,
        path=(),
    )
    monkeypatch.setattr(
        classification_module,
        "validate_download_save_path",
        lambda value: value,
    )

    with pytest.raises(ValueError, match="没有可用的媒体分类"):
        resolve_download_source_classification(
            DownloaderTorrent(hash=HASH, save_path="/downloads"),
            _history(media_category=None),
            directory_helper=helper,
        )


def test_source_classification_accepts_an_enabled_manual_category(monkeypatch):
    """旧下载历史无分类时，允许用户精确选择当前策略中的已启用分类。"""
    directory = TransferDirectoryConf(
        storage="local",
        download_path="/downloads",
        download_category_folder=True,
    )
    helper = MagicMock()
    helper.get_download_dir_by_task_path.return_value = directory
    helper.has_fixed_category.return_value = False
    helper.classification_category_paths.return_value = (("Album",),)
    helper.resolve_media_category.return_value = SimpleNamespace(
        usable=True,
        path=("Album",),
    )
    monkeypatch.setattr(
        classification_module,
        "validate_download_save_path",
        lambda value: value,
    )

    plan = resolve_download_source_classification(
        DownloaderTorrent(hash=HASH, save_path="/downloads"),
        _history(media_category=None),
        media_category="Album",
        directory_helper=helper,
    )

    assert plan.target_save_path == "/downloads/Album"
    assert plan.category == "Album"


def test_source_classification_rejects_an_unknown_manual_category():
    """手动分类不能成为绕过活动策略校验的任意子目录。"""
    helper = MagicMock()
    helper.classification_category_paths.return_value = (("Album",),)

    with pytest.raises(ValueError, match="不存在、已停用或与媒体类型不匹配"):
        resolve_download_source_classification(
            DownloaderTorrent(hash=HASH, save_path="/downloads"),
            _history(media_category=None),
            media_category="Unknown",
            directory_helper=helper,
        )

    helper.get_download_dir_by_task_path.assert_not_called()


@pytest.mark.asyncio
async def test_classify_source_endpoint_preserves_preview_mode(monkeypatch):
    """REST 预览应保留 execute=false，不把查询变成移动。"""
    chain = SimpleNamespace(
        list_torrents=MagicMock(),
        download_history_repository=SimpleNamespace(get_by_hash=MagicMock()),
        update_torrent=MagicMock(),
    )
    media_chain = object()
    organize = MagicMock(
        return_value={
            "hash": HASH,
            "downloader": "qb-main",
            "current_save_path": "/downloads",
            "target_save_path": "/downloads/Album",
            "category": "Album",
            "changed": True,
            "executed": False,
        }
    )
    payload = DownloadSourceClassificationRequest(
        downloader="qb-main",
        execute=False,
    )
    monkeypatch.setattr(download_endpoint, "DownloadChain", lambda: chain)
    monkeypatch.setattr(download_endpoint, "MediaChain", lambda: media_chain)
    monkeypatch.setattr(download_endpoint, "organize_existing_source", organize)

    response = await download_endpoint.classify_source(
        HASH,
        payload,
        SimpleNamespace(),
    )

    assert response.success is True
    assert response.data["target_save_path"] == "/downloads/Album"
    assert response.data["executed"] is False
    organize.assert_called_once_with(HASH, payload, chain, media_chain)
