from pathlib import Path
from types import SimpleNamespace

import pytest

from app.helper.directory import DirectoryHelper
from app.schemas import TransferDirectoryConf


def _media(category: str):
    """构造目录匹配测试所需的媒体信息。"""
    return SimpleNamespace(
        type=SimpleNamespace(value="电视剧"),
        category=category,
    )


def _directory(media_category: str) -> TransferDirectoryConf:
    """构造启用自动整理的动漫目录配置。"""
    return TransferDirectoryConf(
        name="动漫",
        priority=1,
        storage="local",
        download_path="/media/anime",
        media_type="电视剧",
        media_category=media_category,
        monitor_type="monitor",
        library_path="/media/link/anime",
        library_storage="local",
    )


@pytest.mark.parametrize("configured_category", ["日番", "日番,日韩剧", "日番，日韩剧", " 日番 , 日韩剧 "])
def test_get_dir_matches_media_category_list(monkeypatch, configured_category):
    """自动整理应匹配目录配置中逗号分隔的任一媒体类别。"""
    directory = _directory(configured_category)
    monkeypatch.setattr(DirectoryHelper, "get_dirs", staticmethod(lambda: [directory]))

    matched = DirectoryHelper().get_dir(
        media=_media("日番"),
        storage="local",
        src_path=Path("/media/anime/demo.mkv"),
    )

    assert matched == directory


def test_get_dir_rejects_unconfigured_media_category(monkeypatch):
    """自动整理不应匹配目录配置中不存在的媒体类别。"""
    directory = _directory("日番,日韩剧")
    monkeypatch.setattr(DirectoryHelper, "get_dirs", staticmethod(lambda: [directory]))

    matched = DirectoryHelper().get_dir(
        media=_media("欧美剧"),
        storage="local",
        src_path=Path("/media/anime/demo.mkv"),
    )

    assert matched is None


def test_get_download_dir_by_save_path_matches_media_category_list(monkeypatch):
    """精确保存根路径应继承逗号分隔的媒体类别规则。"""
    directory = _directory("日番,日韩剧")
    monkeypatch.setattr(DirectoryHelper, "get_download_dirs", lambda _self: [directory])

    matched = DirectoryHelper().get_download_dir_by_save_path(
        media=_media("日韩剧"),
        save_path="/media/anime",
    )

    assert matched == directory
