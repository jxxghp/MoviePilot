"""
整理名称与目标目录预览接口及推荐名称扩展名过滤回归测试。
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from app.api.endpoints import transfer as transfer_endpoint
from app.application.classification.reference import ClassificationCategoryResolution
from app.application.directory import DirectoryHelper, build_media_library_path
from app.domain.context import MediaInfo, MusicInfo
from app.domain.metainfo import MetaInfo
from app.modules.filemanager.module import FileManagerModule
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.system import TransferDirectoryConf
from app.schemas.types import MediaSource, MediaType


def _mock_runtime_config(
    *,
    movie_format: str = "{{title}}{% if year %} ({{year}}){% endif %}{% if tmdbid %} {tmdbid={{tmdbid}}}{% endif %}/{{title}}{% if year %} ({{year}}){% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}{{fileExt}}",
    tv_format: str = "{{title}}{% if year %} ({{year}}){% endif %}/Season {{season}}/{{title}} - {{season_episode}}{{fileExt}}",
    music_format: str = "{{album_artist or artist}}/{{album}}/{{title}}{{fileExt}}",
) -> SimpleNamespace:
    """
    构造整理预览使用的轻量运行时配置快照。

    :param movie_format: 电影重命名模板
    :param tv_format: 电视剧重命名模板
    :param music_format: 音乐重命名模板
    :return: 包含扩展名列表和模板解析方法的模拟配置对象
    """

    def _rename_format(media_type: MediaType) -> str:
        """
        按媒体类型返回对应的重命名模板。
        """
        if media_type == MediaType.TV:
            return tv_format
        if media_type == MediaType.MUSIC:
            return music_format
        return movie_format

    return SimpleNamespace(
        media_extensions=(".mkv", ".mp4", ".avi"),
        audio_extensions=(".flac", ".mp3"),
        subtitle_extensions=(".srt", ".ass"),
        lyrics_extensions=(".lrc",),
        rename_format=_rename_format,
    )


def test_recommend_name_ignores_non_media_suffix_from_torrent_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    不带媒体文件后缀的种子名不应把点分发布标签误拼成文件扩展名。
    """
    settings_map: dict[str, Any] = {
        "RENAME_FORMAT": lambda _media_type: "{{title}} ({{year}})/{{title}} ({{year}}){{fileExt}}",
        "RMT_MEDIAEXT": [".mkv", ".mp4"],
        "RMT_AUDIOEXT": [".flac"],
        "RMT_SUBEXT": [".srt"],
        "RMT_LYRICSEXT": [".lrc"],
    }
    monkeypatch.setattr(
        "app.modules.filemanager.module.get_runtime_setting",
        lambda key: settings_map[key],
    )
    captured_exts: list[Optional[str]] = []

    def _fake_naming_dict(
        *,
        meta: Any,
        mediainfo: Any,
        episodes_info: Any = None,
        file_ext: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        记录传入命名上下文的文件扩展名并返回最小模板变量。
        """
        _ = (meta, episodes_info)
        captured_exts.append(file_ext)
        return {
            "title": mediainfo.title,
            "year": mediainfo.year,
            "fileExt": file_ext or "",
        }

    monkeypatch.setattr(TransHandler, "get_naming_dict", staticmethod(_fake_naming_dict))
    monkeypatch.setattr(
        TransHandler,
        "get_rename_path",
        staticmethod(
            lambda template_string, rename_dict, **_kwargs: Path(
                f"{rename_dict['title']} ({rename_dict['year']})/"
                f"{rename_dict['title']} ({rename_dict['year']}){rename_dict['fileExt']}"
            )
        ),
    )

    mediainfo = MediaInfo()
    mediainfo.type = MediaType.MOVIE
    mediainfo.title = "海上钢琴师"
    mediainfo.year = "1998"

    torrent_meta = MetaInfo("The.Legend.of.1900.1998.1080p.BluRay.x264-GROUP")
    torrent_result = FileManagerModule.recommend_name(torrent_meta, mediainfo)
    assert captured_exts[-1] == ""
    assert torrent_result == "海上钢琴师 (1998)/海上钢琴师 (1998)"

    file_meta = MetaInfo("The.Legend.of.1900.1998.1080p.BluRay.mkv")
    file_result = FileManagerModule.recommend_name(file_meta, mediainfo)
    assert captured_exts[-1] == ".mkv"
    assert file_result == "海上钢琴师 (1998)/海上钢琴师 (1998).mkv"


def test_build_media_library_path_matches_transhandler_dest_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    共享媒体库路径构造函数应正确拼接类型与二级分类目录，并与整理处理器保持一致。
    """
    mediainfo = MediaInfo()
    mediainfo.type = MediaType.MOVIE
    mediainfo.title = "海上钢琴师"
    mediainfo.set_library_category("外语电影")

    directory = TransferDirectoryConf(
        name="电影库",
        library_path="/media",
        library_type_folder=True,
        library_category_folder=True,
    )
    monkeypatch.setattr(
        DirectoryHelper,
        "category_path_for_directory",
        lambda self, _dir, _media: ("外语电影",),
    )

    expected = Path("/media/电影/外语电影")
    assert build_media_library_path(directory, mediainfo) == expected
    assert TransHandler.get_dest_dir(mediainfo, directory) == expected


@pytest.mark.asyncio
async def test_preview_name_returns_category_dir_file_and_library_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    预览接口应返回命中的分类路径、目标目录名、最终文件名以及完整目标路径。
    """
    mediainfo = MediaInfo()
    mediainfo.type = MediaType.MOVIE
    mediainfo.title = "海上钢琴师"
    mediainfo.year = "1998"
    mediainfo.tmdb_id = 10376
    mediainfo.set_library_category("外语电影")

    captured_recognize: dict[str, Any] = {}

    async def _fake_recognize_by_meta(
        self: Any,
        metainfo: Any,
        *,
        media_source: Any = None,
        mtype: Any = None,
        music_type: Any = None,
        media_id: Any = None,
    ) -> MediaInfo:
        """
        捕获识别调用参数并返回固定电影信息。
        """
        _ = self
        captured_recognize.update(
            {
                "metainfo": metainfo,
                "media_source": media_source,
                "mtype": mtype,
                "music_type": music_type,
                "media_id": media_id,
            }
        )
        return mediainfo

    monkeypatch.setattr(
        transfer_endpoint,
        "get_api_runtime_config_snapshot",
        _mock_runtime_config,
    )
    monkeypatch.setattr(
        transfer_endpoint.MediaChain,
        "async_recognize_by_meta",
        _fake_recognize_by_meta,
    )
    monkeypatch.setattr(
        transfer_endpoint.TransferChain,
        "recommend_name",
        lambda self, meta, mediainfo: "海上钢琴师 (1998) {tmdbid=10376}/海上钢琴师 (1998) - 1080p.mkv",
    )
    monkeypatch.setattr(
        DirectoryHelper,
        "resolve_media_category",
        lambda self, media: ClassificationCategoryResolution(
            state="resolved",
            category_id=None,
            path=("外语电影",),
        ),
    )
    monkeypatch.setattr(
        DirectoryHelper,
        "category_path_for_directory",
        lambda self, directory, media: ("外语电影",),
    )
    monkeypatch.setattr(
        DirectoryHelper,
        "get_dir",
        lambda self, media=None, src_path=None: TransferDirectoryConf(
            name="电影库",
            library_path="/media",
            library_type_folder=True,
            library_category_folder=True,
        ),
    )

    response = await transfer_endpoint.preview_name(
        title="/downloads/The.Legend.of.1900.1998.1080p.mkv",
        subtitle="海上钢琴师",
        custom_words="屏蔽词",
        media_source=MediaSource.TMDB,
        media_id="10376",
        mtype=MediaType.MOVIE,
    )

    assert response.success is True
    assert captured_recognize["media_source"] == MediaSource.TMDB
    assert captured_recognize["media_id"] == "10376"
    assert captured_recognize["mtype"] == MediaType.MOVIE
    assert response.data is not None
    assert response.data.category == "外语电影"
    assert response.data.target_dir == "海上钢琴师 (1998) {tmdbid=10376}"
    assert response.data.file_name == "海上钢琴师 (1998) - 1080p.mkv"
    assert response.data.rename_path == "海上钢琴师 (1998) {tmdbid=10376}/海上钢琴师 (1998) - 1080p.mkv"
    assert response.data.library_path == "/media/电影/外语电影"
    assert (
        response.data.full_path
        == "/media/电影/外语电影/海上钢琴师 (1998) {tmdbid=10376}/海上钢琴师 (1998) - 1080p.mkv"
    )


@pytest.mark.asyncio
async def test_preview_name_handles_tv_season_and_no_configured_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    电视剧预览应保留季目录层级；未配置匹配整理目录时允许媒体库路径为空。
    """
    mediainfo = MediaInfo()
    mediainfo.type = MediaType.TV
    mediainfo.title = "测试剧集"
    mediainfo.year = "2026"

    async def _fake_recognize_by_meta(self: Any, metainfo: Any, **_kwargs: Any) -> MediaInfo:
        """
        返回测试电视剧信息。
        """
        _ = (self, metainfo)
        return mediainfo

    monkeypatch.setattr(
        transfer_endpoint,
        "get_api_runtime_config_snapshot",
        _mock_runtime_config,
    )
    monkeypatch.setattr(
        transfer_endpoint.MediaChain,
        "async_recognize_by_meta",
        _fake_recognize_by_meta,
    )
    monkeypatch.setattr(
        transfer_endpoint.TransferChain,
        "recommend_name",
        lambda self, meta, mediainfo: "测试剧集 (2026)/Season 1/测试剧集 - S01E01",
    )
    monkeypatch.setattr(
        DirectoryHelper,
        "resolve_media_category",
        lambda self, media: ClassificationCategoryResolution(
            state="resolved",
            category_id=None,
            path=("动漫", "日漫"),
        ),
    )
    monkeypatch.setattr(DirectoryHelper, "get_dir", lambda self, media=None, src_path=None: None)

    response = await transfer_endpoint.preview_name(title="Test.Show.S01E01.1080p")

    assert response.success is True
    assert response.data is not None
    assert response.data.category == "动漫/日漫"
    assert response.data.target_dir == "测试剧集 (2026)"
    assert response.data.file_name == "测试剧集 - S01E01"
    assert response.data.rename_path == "测试剧集 (2026)/Season 1/测试剧集 - S01E01"
    assert response.data.library_path is None
    assert response.data.full_path is None


@pytest.mark.asyncio
async def test_preview_name_handles_music_disc_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    音乐预览遇到碟片子目录时，目标目录应回溯到专辑目录并保留完整相对路径。
    """
    music_info = MusicInfo(
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        media_source=MediaSource.MusicBrainz,
        media_id="8f97b17d-1234-4abc-9def-1234567890ab",
    )

    async def _fake_recognize_by_meta(self: Any, metainfo: Any, **_kwargs: Any) -> MusicInfo:
        """
        返回测试音乐信息。
        """
        _ = (self, metainfo)
        return music_info

    monkeypatch.setattr(
        transfer_endpoint,
        "get_api_runtime_config_snapshot",
        _mock_runtime_config,
    )
    monkeypatch.setattr(
        transfer_endpoint.MediaChain,
        "async_recognize_by_meta",
        _fake_recognize_by_meta,
    )
    monkeypatch.setattr(
        transfer_endpoint.TransferChain,
        "recommend_name",
        lambda self, meta, mediainfo: "周杰伦/叶惠美/Disc 1/01 - 晴天.flac",
    )
    monkeypatch.setattr(
        DirectoryHelper,
        "resolve_media_category",
        lambda self, media: ClassificationCategoryResolution(
            state="empty",
            category_id=None,
            path=(),
        ),
    )
    monkeypatch.setattr(DirectoryHelper, "get_dir", lambda self, media=None, src_path=None: None)

    response = await transfer_endpoint.preview_name(
        title="周杰伦 - 晴天.flac",
        media_source=MediaSource.MusicBrainz,
        media_id="8f97b17d-1234-4abc-9def-1234567890ab",
    )

    assert response.success is True
    assert response.data is not None
    assert response.data.category is None
    assert response.data.target_dir == "周杰伦/叶惠美"
    assert response.data.file_name == "01 - 晴天.flac"
    assert response.data.rename_path == "周杰伦/叶惠美/Disc 1/01 - 晴天.flac"


@pytest.mark.asyncio
async def test_preview_name_reports_unrecognized_and_template_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    未识别媒体或模板渲染失败时应返回结构化失败响应，而非抛出未处理异常。
    """

    async def _fake_unrecognized(self: Any, metainfo: Any, **_kwargs: Any) -> None:
        """
        模拟识别失败返回空媒体信息。
        """
        _ = (self, metainfo)
        return None

    monkeypatch.setattr(
        transfer_endpoint,
        "get_api_runtime_config_snapshot",
        _mock_runtime_config,
    )
    monkeypatch.setattr(
        transfer_endpoint.MediaChain,
        "async_recognize_by_meta",
        _fake_unrecognized,
    )

    unrecognized_response = await transfer_endpoint.preview_name(title="Unknown.Release")
    assert unrecognized_response.success is False
    assert "未识别到媒体信息" in (unrecognized_response.message or "")

    mediainfo = MediaInfo()
    mediainfo.type = MediaType.MOVIE
    mediainfo.title = "海上钢琴师"

    async def _fake_recognized(self: Any, metainfo: Any, **_kwargs: Any) -> MediaInfo:
        """
        模拟识别成功以验证模板渲染失败路径。
        """
        _ = (self, metainfo)
        return mediainfo

    monkeypatch.setattr(
        transfer_endpoint.MediaChain,
        "async_recognize_by_meta",
        _fake_recognized,
    )
    monkeypatch.setattr(
        DirectoryHelper,
        "resolve_media_category",
        lambda self, media: ClassificationCategoryResolution(
            state="empty",
            category_id=None,
            path=(),
        ),
    )

    def _raise_template_error(self: Any, meta: Any, mediainfo: Any) -> str:
        """
        模拟 Jinja2 模板语法错误。
        """
        _ = (self, meta, mediainfo)
        raise ValueError("模板语法错误")

    monkeypatch.setattr(
        transfer_endpoint.TransferChain,
        "recommend_name",
        _raise_template_error,
    )

    error_response = await transfer_endpoint.preview_name(title="海上钢琴师.1998.mkv")
    assert error_response.success is False
    assert "重命名模板渲染失败" in (error_response.message or "")
