"""
订阅下载治理上下文与提交依赖装配；不注册测试用例。
"""

from __future__ import annotations
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
import app.chain.download.submission as download_submission
from app.chain.download import DownloadChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaSource, MediaType


class _FakeTorrentHelper:
    """
    返回固定种子目录和文件清单，隔离真实 bencode 解析。
    """

    @staticmethod
    def get_fileinfo_from_torrent_content(_content):
        """
        返回单文件种子结构。
        """
        return "Demo.Show.S01", ["Demo.Show.S01E01.mkv"]


def _download_chain() -> DownloadChain:
    """
    构造只执行下载提交边界的 Chain 测试实例。
    """
    chain = DownloadChain.__new__(DownloadChain)
    chain.download_history_repository = MagicMock()
    chain.download_history_repository.get_by_media_identity.return_value = []
    chain.download_failure_repository = MagicMock()
    chain.eventmanager = MagicMock()
    chain.eventmanager.send_event.return_value = None
    chain.post_message = MagicMock()
    chain._settle_download_success = MagicMock()
    chain._resolve_media_download_dir = MagicMock(
        return_value=("local", Path("/downloads"), None)
    )
    chain.runtime_config = SimpleNamespace(media_extensions=(".mkv",), video_extensions=(".mkv",))
    return chain


def _context() -> Context:
    """
    构造具有稳定媒体和 torrent 身份的电视剧候选。
    """
    return Context(
        meta_info=MetaInfo("Demo Show S01E01"),
        media_info=MediaInfo(
            media_source=MediaSource.TMDB,
            media_id="77",
            type=MediaType.TV,
            title="Demo Show",
            year="2026",
            tmdb_id=77,
            genre_ids=[18],
        ),
        torrent_info=TorrentInfo(
            site=12,
            site_name="TestSite",
            title="Demo Show S01E01 1080p",
            enclosure="https://example.com/download.php?id=42",
        ),
    )


@pytest.fixture(autouse=True)
def _submission_dependencies(monkeypatch):
    """
    固定目录、媒体补全和种子解析，避免触发外部模块。
    """
    monkeypatch.setattr(
        "app.application.directory.DirectoryHelper.get_download_dirs",
        lambda _self: [SimpleNamespace(
            storage="local",
            download_path="/downloads",
            category=None,
            media_type=None,
        )],
    )
    monkeypatch.setattr(download_submission, "TorrentHelper", _FakeTorrentHelper)
    monkeypatch.setattr(
        download_submission.MediaChain,
        "supplement_tmdb_info",
        lambda _self, media, _meta: media,
    )
