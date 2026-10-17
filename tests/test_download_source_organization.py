"""
隔离验证下载源目录整理的预览、重命名、迁移及回滚约束。
"""

import importlib.util
import sys
from enum import Enum
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest


class MediaType(str, Enum):
    """
    源目录整理测试使用的媒体类型。
    """

    # 音乐资源。
    MUSIC = "音乐"
    # 电影资源。
    MOVIE = "电影"
    # 电视剧资源。
    TV = "电视剧"


class SystemConfigKey(str, Enum):
    """
    模拟系统配置读取时使用的配置键。
    """

    # 已配置的下载器列表。
    Downloaders = "Downloaders"


class MediaSource(str, Enum):
    """
    媒体识别测试支持的外部来源。
    """

    # 音乐专辑和艺术家识别来源。
    MusicBrainz = "musicbrainz"


directory_module = ModuleType("app.application.directory")
directory_module.DirectoryHelper = Mock()
directory_module.validate_download_save_path = lambda value: str(value)
configuration_module = ModuleType("app.application.configuration")
configuration_module.get_configured_system_config = lambda: {
    SystemConfigKey.Downloaders: [{"name": "qb", "type": "qbittorrent"}]
}
metabase_module = ModuleType("app.domain.meta.metabase")
metabase_module.MetaBase = object
metamusic_module = ModuleType("app.domain.meta.metamusic")
metamusic_module.MetaMusic = NS(parse_query=lambda _value: NS())
metainfo_module = ModuleType("app.domain.metainfo")
metainfo_module.MetaInfo = lambda **_kwargs: NS()
types_module = ModuleType("app.schemas.types")
types_module.MediaSource = MediaSource
types_module.MediaType = MediaType
types_module.SystemConfigKey = SystemConfigKey
types_module.MUSIC_ARTIST_COLLECTION_CATEGORY = "Artist Collection"

with patch.dict(
    sys.modules,
    {
        "app.application.configuration": configuration_module,
        "app.application.directory": directory_module,
        "app.domain.meta.metabase": metabase_module,
        "app.domain.meta.metamusic": metamusic_module,
        "app.domain.metainfo": metainfo_module,
        "app.schemas.types": types_module,
    },
):
    spec = importlib.util.spec_from_file_location(
        "source_organization",
        Path(__file__).parent.parent / "app/application/download/organization.py",
    )
    organization = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(organization)


@pytest.fixture
def source_context(monkeypatch):
    """
    构造音乐下载记录和下载器替身，每个用例独立维护目录配置。

    :param monkeypatch: 恢复共享替身模块中的目录帮助器
    :return: 包含请求、媒体、下载器和业务链替身的测试上下文
    """
    source_context = NS()
    directory_helper = Mock()
    monkeypatch.setattr(directory_module, "DirectoryHelper", directory_helper)
    monkeypatch.setattr(organization, "DirectoryHelper", directory_helper)
    source_context.hash_value = "a" * 40
    source_context.request = NS(
        downloader=None,
        execute=False,
        mode="recognize",
        target_path=None,
        type_name="音乐",
        media_source=None,
        media_id=None,
        music_type="album",
        episode_group=None,
        media_category=None,
        smart_rename=True,
        expected_current_path=None,
        expected_target_path=None,
        expected_content_path=None,
        expected_root_name=None,
    )
    source_context.history = NS(
        downloader="qb",
        type="音乐",
        media_source=None,
        media_id=None,
        music_type=None,
        media_category=None,
        episode_group=None,
        torrent_name="Karen Mok - Loving Gaze 2002",
        torrent_description=None,
        title="含情脉脉",
    )
    source_context.torrent = NS(
        hash=source_context.hash_value,
        downloader="qb",
        title="Karen Mok - Loving Gaze 2002",
        save_path="/volume1/UT/Musics",
        content_path="/volume1/UT/Musics/Karen Mok - Loving Gaze 2002 FLAC",
        path=None,
    )
    source_context.media = NS(
        type=MediaType.MUSIC,
        album_type="Album",
        secondary_types=["Compilation"],
        classification_path=("Album", "Compilation"),
        album="含情脉脉",
        title="含情脉脉",
        album_artist="莫文蔚",
        artist="莫文蔚",
        year="2002",
        media_source="musicbrainz",
        media_id="release-group-id",
    )
    source_context.media_chain = Mock()
    source_context.media_chain.recognize_by_meta.return_value = source_context.media
    source_context.qbc = Mock()
    source_context.torrent_files = [NS(name="Karen Mok - Loving Gaze 2002 FLAC/01.flac")]
    module = NS(
        get_instance=lambda _name: NS(qbc=source_context.qbc),
        torrent_files=lambda **_kwargs: source_context.torrent_files,
    )
    source_context.chain = Mock()
    source_context.chain.download_history_repository.get_by_hash.return_value = source_context.history
    source_context.chain.list_torrents.return_value = [source_context.torrent]
    source_context.chain.modulemanager.get_running_module.return_value = module
    source_context.chain.update_torrent.return_value = {"save_path": True}
    directory_module.DirectoryHelper.return_value.get_download_dirs.return_value = [
        NS(
            storage="local",
            download_path="/volume1/UT/Musics",
            media_type="音乐",
            media_category="",
            download_type_folder=False,
            download_category_folder=True,
            priority=2,
        )
    ]
    directory_module.DirectoryHelper.return_value.classification_category_paths.return_value = (
        ("Album",),
        ("Album", "Compilation"),
        ("EP",),
        ("Action",),
    )
    directory_module.DirectoryHelper.return_value.resolve_media_category.side_effect = lambda media: NS(
        path=getattr(media, "classification_path", ())
    )

    return source_context


def _preview(source_context):
    """
    使用当前替身状态执行源目录预览或已确认的整理。

    :param source_context: 当前用例的请求及依赖替身
    :return: 整理服务返回的计划或执行结果
    """
    return organization.organize_existing_source(
        source_context.hash_value,
        source_context.request,
        source_context.chain,
        source_context.media_chain,
    )


def test_music_effective_classification_path_drives_source_directory(source_context) -> None:
    """
    音乐完整分类路径决定源目录层级。
    """
    result = _preview(source_context)
    assert result["category"] == "Album/Compilation"
    assert result["secondary_categories"] == ["Compilation"]
    assert result["target_save_path"] == "/volume1/UT/Musics/Album/Compilation"


def test_music_category_falls_back_to_primary_type_without_classification(source_context) -> None:
    """
    没有完整分类时回退到音乐主类型。
    """
    source_context.media.classification_path = ()
    result = _preview(source_context)
    assert result["category"] == "Album"
    assert result["target_save_path"] == "/volume1/UT/Musics/Album"


def test_artist_collection_uses_one_dedicated_source_category(source_context) -> None:
    """
    艺术家合集统一使用专用目录分类。
    """
    source_context.request.music_type = "artist"
    source_context.history.music_type = "artist"
    source_context.media.music_type = "artist"
    source_context.media.name = "许嵩"
    source_context.media.classification_path = ("未分类",)
    source_context.torrent.title = "许嵩[2006-2022]录音室专辑合集"

    result = _preview(source_context)

    assert result["category"] == "Artist Collection"
    assert result["target_save_path"] == "/volume1/UT/Musics/Artist Collection"
    assert result["proposed_root_name"] == "许嵩 - 艺术家合集"


def test_preview_is_read_only_and_includes_qb_root_rename(source_context) -> None:
    """
    预览仅返回根目录改名计划，不执行下载器写入。
    """
    result = _preview(source_context)
    assert result["proposed_root_name"] == "莫文蔚 - 含情脉脉 (2002)"
    assert result["rename_required"]
    source_context.chain.update_torrent.assert_not_called()
    source_context.qbc.torrents_rename_folder.assert_not_called()


def test_execution_requires_exact_preview_replay(source_context) -> None:
    """
    执行前必须准确回传预览结果。
    """
    source_context.request.execute = True
    with pytest.raises(ValueError, match="重新预览"):
        _preview(source_context)
    source_context.chain.update_torrent.assert_not_called()


def test_confirmed_execution_uses_qb_rename_then_set_location(source_context) -> None:
    """
    确认执行时先重命名根目录，再修改保存位置。
    """
    plan = _preview(source_context)
    source_context.request.execute = True
    source_context.request.expected_current_path = plan["current_save_path"]
    source_context.request.expected_target_path = plan["target_save_path"]
    source_context.request.expected_content_path = plan["current_content_path"]
    source_context.request.expected_root_name = plan["proposed_root_name"]
    result = _preview(source_context)
    assert result["renamed"]
    assert result["relocated"]
    source_context.qbc.torrents_rename_folder.assert_called_once_with(
        torrent_hash=source_context.hash_value,
        old_path="Karen Mok - Loving Gaze 2002 FLAC",
        new_path="莫文蔚 - 含情脉脉 (2002)",
    )
    source_context.chain.update_torrent.assert_called_once_with(
        hash_string=source_context.hash_value,
        downloader="qb",
        save_path="/volume1/UT/Musics/Album/Compilation",
    )


def test_manual_directory_without_rename_skips_recognition(source_context) -> None:
    """
    手动目录且关闭智能改名时跳过媒体识别。
    """
    source_context.request.mode = "manual"
    source_context.request.target_path = "/volume1/UT/Musics/EP"
    source_context.request.smart_rename = False
    result = _preview(source_context)
    assert not result["recognized"]
    assert result["target_save_path"] == "/volume1/UT/Musics/EP"
    source_context.media_chain.recognize_by_meta.assert_not_called()


def test_failed_relocation_rolls_back_root_rename(source_context) -> None:
    """
    迁移失败时回滚已经完成的根目录重命名。
    """
    plan = _preview(source_context)
    source_context.request.execute = True
    source_context.request.expected_current_path = plan["current_save_path"]
    source_context.request.expected_target_path = plan["target_save_path"]
    source_context.request.expected_content_path = plan["current_content_path"]
    source_context.request.expected_root_name = plan["proposed_root_name"]
    source_context.chain.update_torrent.return_value = {"save_path": False}
    with pytest.raises(ValueError, match="已回滚"):
        _preview(source_context)
    assert source_context.qbc.torrents_rename_folder.call_count == 2
    source_context.qbc.torrents_rename_folder.assert_called_with(
        torrent_hash=source_context.hash_value,
        old_path="莫文蔚 - 含情脉脉 (2002)",
        new_path="Karen Mok - Loving Gaze 2002 FLAC",
    )


def test_smart_rename_rejects_multi_root_tasks(source_context) -> None:
    """
    智能改名拒绝包含多个根项的下载任务。
    """
    source_context.torrent.content_path = source_context.torrent.save_path
    with pytest.raises(ValueError, match="单文件或散列文件"):
        _preview(source_context)


def test_dotted_folder_name_is_not_treated_as_a_file(source_context) -> None:
    """
    含点号的目录名仍按目录处理。
    """
    folder = "Eagles.2011 - Hotel California SACD"
    source_context.torrent.content_path = f"/volume1/UT/Musics/{folder}"
    source_context.torrent_files = [NS(name=f"{folder}/01.dsf"), NS(name=f"{folder}/02.dsf")]
    result = _preview(source_context)
    assert result["current_root_name"] == folder
    assert result["rename_supported"]


def test_single_file_task_is_not_treated_as_a_folder(source_context) -> None:
    """
    单文件任务不能作为目录重命名。
    """
    source_context.torrent.content_path = "/volume1/UT/Musics/Hotel California.dsf"
    source_context.torrent_files = [NS(name="Hotel California.dsf")]
    with pytest.raises(ValueError, match="单文件或散列文件"):
        _preview(source_context)


def test_changing_source_does_not_reuse_history_media_id(source_context) -> None:
    """
    切换识别来源后不能复用历史媒体标识。
    """
    source_context.history.media_source = "other-source"
    source_context.history.media_id = "other-id"
    source_context.request.media_source = MediaSource.MusicBrainz
    _preview(source_context)
    source_context.media_chain.recognize_media.assert_not_called()
    source_context.media_chain.recognize_by_meta.assert_called_once()


def test_manual_category_must_exist_in_active_policy(source_context) -> None:
    """
    手动分类必须属于当前启用的分类策略。
    """
    source_context.request.type_name = "电影"
    source_context.request.media_category = "Unlisted"
    source_context.media.type = MediaType.MOVIE
    source_context.media.category = "Action"
    with pytest.raises(ValueError, match="不存在、已停用"):
        _preview(source_context)


def test_active_manual_category_overrides_recognized_music_category(source_context) -> None:
    """
    有效的手动分类覆盖音乐识别结果。
    """
    source_context.request.media_category = "EP"
    result = _preview(source_context)
    assert result["category"] == "EP"
    assert result["target_save_path"] == "/volume1/UT/Musics/EP"


def test_windows_downloader_paths_are_parsed_by_path_style(source_context) -> None:
    """
    按下载器路径格式解析 Windows 路径并统一输出分隔符。
    """
    folder = "Eagles.2011 - Hotel California SACD"
    source_context.torrent.save_path = r"D:\Downloads"
    source_context.torrent.content_path = rf"D:\Downloads\{folder}"
    source_context.torrent_files = [NS(name=f"{folder}/01.dsf"), NS(name=f"{folder}/02.dsf")]
    directory_module.DirectoryHelper.return_value.get_download_dirs.return_value[0].download_path = "D:/Downloads"
    result = _preview(source_context)
    assert result["current_save_path"] == "D:/Downloads"
    assert result["target_save_path"] == "D:/Downloads/Album/Compilation"
    assert result["current_root_name"] == folder
