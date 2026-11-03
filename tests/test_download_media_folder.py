"""
下载媒体目录与历史命名快照的行为回归。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.application.directory import DirectoryHelper, build_media_download_path
from app.application.download.classification import resolve_download_source_classification
from app.application.history import DownloadHistorySnapshot
from app.chain.download.history import DownloadHistoryOwner
from app.chain.download.subtitle import DownloadSubtitleOwner
from app.domain.context import MediaInfo, MusicInfo
from app.domain.metainfo import MetaInfo
from app.runtime.config import settings
from app.schemas.system import TransferDirectoryConf
from app.schemas.transfer import DownloaderTorrent
from app.schemas.types import MediaType


@pytest.fixture
def rename_formats(monkeypatch):
    """
    使用确定的影视模板，并在测试结束时恢复进程配置。
    """
    template = "{{title}}{% if year %} ({{year}}){% endif %}/{{title}}{{fileExt}}"
    monkeypatch.setattr(settings, "MOVIE_RENAME_FORMAT", template)
    monkeypatch.setattr(settings, "TV_RENAME_FORMAT", template)


def _media(media_type=MediaType.MOVIE, **overrides):
    """
    构造可跨季、跨版本稳定命名的媒体事实。
    """
    return MediaInfo(type=media_type, title="示例", year="2026", **overrides)


def _directory(**overrides):
    """
    构造默认启用媒体子目录的测试目录。
    """
    return TransferDirectoryConf(download_path="/downloads", download_media_folder=True, **overrides)


@pytest.mark.usefixtures("rename_formats")
@pytest.mark.parametrize("media_type", [MediaType.MOVIE, MediaType.TV])
def test_media_folder_is_appended_for_movies_and_series(media_type):
    """
    不启用类型、分类时仍须追加影视媒体目录。
    """
    assert build_media_download_path(Path("/downloads"), _directory(), _media(media_type)) == (
        Path("/downloads/示例 (2026)")
    )


@pytest.mark.usefixtures("rename_formats")
@pytest.mark.parametrize("enabled", [False, None])
def test_disabled_media_folder_keeps_existing_path(enabled):
    """
    关闭或未设置开关不得改变原路径。
    """
    directory = TransferDirectoryConf(download_path="/downloads", download_media_folder=enabled)
    assert build_media_download_path(Path("/downloads"), directory, _media()) == Path("/downloads")
    assert TransferDirectoryConf().model_dump()["download_media_folder"] is False


@pytest.mark.usefixtures("rename_formats")
@pytest.mark.parametrize("options,expected", [
    ({"download_type_folder": True}, "/downloads/电影/亚洲/华语/示例 (2026)"),
    ({"media_type": "电影", "download_type_folder": True}, "/downloads/亚洲/华语/示例 (2026)"),
    ({"media_category": "华语"}, "/downloads/示例 (2026)"),
])
def test_media_folder_follows_existing_type_and_nested_category_rules(options, expected):
    """
    媒体目录位于动态分类之后，固定分类不重复生成子目录。
    """
    helper = MagicMock()
    helper.has_fixed_category.return_value = bool(options.get("media_category"))
    helper.resolve_media_category.return_value = SimpleNamespace(path=("亚洲", "华语"))
    result = build_media_download_path(
        Path("/downloads"), _directory(download_category_folder=True, **options), _media(), helper,
    )
    assert result == Path(expected)


@pytest.mark.usefixtures("rename_formats")
def test_music_and_unknown_media_do_not_gain_media_folders():
    """
    影视开关不改变音乐专辑或未知媒体目录。
    """
    for media in (MusicInfo(title="专辑"), _media(MediaType.UNKNOWN)):
        assert build_media_download_path(Path("/downloads"), _directory(), media) == Path("/downloads")


def test_media_folder_uses_last_title_directory_without_prefix_or_season(monkeypatch):
    """
    年份前缀、嵌套标题和季目录沿用媒体库根目录的选择规则。
    """
    template = "{{year}}/{{title}}/{{en_title}} {tmdb-{{tmdbid}}}/Season {{season}}/{{title}}{{fileExt}}"
    monkeypatch.setattr(settings, "TV_RENAME_FORMAT", template)
    media = _media(MediaType.TV, en_title="Example", tmdb_id=123, season=1)
    first = build_media_download_path(Path("/downloads"), _directory(), media)
    media.season = 2
    assert first == build_media_download_path(Path("/downloads"), _directory(), media)
    assert first == Path("/downloads/Example {tmdb-123}")
    library_file = Path("/library/2026/示例/Example {tmdb-123}/Season 1/episode.mkv")
    assert DirectoryHelper.get_media_root_path(template, library_file) == Path("/library/2026/示例/Example {tmdb-123}")


@pytest.mark.parametrize("template", [
    "{{title}}/{{title}}{{fileExt}}",
    "{{title}}{% if year %} ({{year}}){% endif %}{% if tmdbid %} {tmdb-{{tmdbid}}}{% endif %}/file.mkv",
    "{{title}}{{imdbid}}/file.mkv",
    "{{title}}{% if year is defined %} ({{year}}){% endif %}{% if tmdbid is defined %} ID{{tmdbid}}{% endif %}/file.mkv",
])
def test_known_missing_metadata_never_renders_none(monkeypatch, template):
    """
    无年份和辅助标识时可使用条件模板，缺失值不变成字面 None。
    """
    monkeypatch.setattr(settings, "MOVIE_RENAME_FORMAT", template)
    media = MediaInfo(type=MediaType.MOVIE, title="示例")
    assert build_media_download_path(Path("/downloads"), _directory(), media) == Path("/downloads/示例")


@pytest.mark.parametrize("template", [
    "{{title}}-{{videoFormat}}/file.mkv",
    "{{title}}{% if season_year %}-{{season_year}}{% endif %}/file.mkv",
    "{{title}}-{{season_fmt}}/file.mkv",
    "{{title}}-{{instance_name}}/file.mkv",
    "{{title}}-{{cycler('a', 'b').next()}}/file.mkv",
    "{{title}}-{{1 % 0}}/file.mkv",
    "{{title}}{% if missing %}/file.mkv",
    "{{title}}{{fileExt}}",
    "{{title | replace('示例', '..')}}/file.mkv",
    "{{title | replace('示例', '')}}/file.mkv",
    "{{title | replace('示例', 'C:evil')}}/file.mkv",
    "{{title | replace('示例', '\\\\')}}/file.mkv",
    "{{title | replace('示例', '\\u0000')}}/file.mkv",
])
def test_unsafe_or_file_dependent_template_fails_explicitly(monkeypatch, template):
    """
    不能从媒体事实安全计算单目录的模板须返回明确错误。
    """
    monkeypatch.setattr(settings, "MOVIE_RENAME_FORMAT", template)
    with pytest.raises(ValueError, match="媒体目录"):
        build_media_download_path(Path("/downloads"), _directory(), _media())


@pytest.mark.usefixtures("rename_formats")
@pytest.mark.parametrize("save_path,storage", [(None, "local"), ("影库", "local"), ("云盘", "rclone")])
def test_download_path_resolution_supports_default_alias_and_remote(monkeypatch, save_path, storage):
    """
    默认选择及显式别名共享媒体目录规则，并保留远端存储。
    """
    directories = [
        _directory(name="影库", storage="local"),
        TransferDirectoryConf(
            name="云盘", storage="rclone", download_path="/remote", download_media_folder=True,
        ),
    ]
    monkeypatch.setattr(DirectoryHelper, "get_download_dirs", lambda _: directories)
    monkeypatch.setattr(DirectoryHelper, "get_download_dir", lambda _self, _media: directories[0])
    result = DownloadSubtitleOwner._resolve_media_download_dir(_media(), save_path)
    assert result == (storage, Path("/remote/示例 (2026)" if storage == "rclone" else "/downloads/示例 (2026)"), "")


@pytest.mark.parametrize("save_path", [None, "/downloads"])
def test_download_path_resolution_returns_template_error(monkeypatch, save_path):
    """
    默认和显式下载路径都应把模板错误转为链路错误结果。
    """
    monkeypatch.setattr(settings, "MOVIE_RENAME_FORMAT", "{{title}}-{{resolution}}/file.mkv")
    monkeypatch.setattr(DirectoryHelper, "get_download_dirs", lambda _: [_directory()])
    monkeypatch.setattr(DirectoryHelper, "get_download_dir", lambda _self, _media: _directory())
    storage, path, error = DownloadSubtitleOwner._resolve_media_download_dir(_media(), save_path)
    assert storage is None
    assert path is None
    assert "媒体目录" in error


@pytest.mark.parametrize("template", [
    "{{title}} {tmdb-{{tmdbid}}}/file.mkv",
    "{{en_title}} ({{year}})/file.mkv",
])
def test_history_reclassification_reuses_download_naming_facts(monkeypatch, template):
    """
    下载历史保存有限命名快照，后续分类不能丢失英文标题或辅助标识。
    """
    monkeypatch.setattr(settings, "MOVIE_RENAME_FORMAT", template)
    media = _media(en_title="Example", tmdb_id=123)
    note = DownloadHistoryOwner._build_download_note("Subscribe", media, MetaInfo("Example"))
    directory = _directory(download_category_folder=True)
    helper = MagicMock()
    helper.get_download_dir_by_task_path.return_value = directory
    helper.has_fixed_category.return_value = False
    helper.resolve_media_category.return_value = SimpleNamespace(usable=True, path=("华语",))
    monkeypatch.setattr("app.application.download.classification.validate_download_save_path", lambda value: value)
    original = build_media_download_path(Path("/downloads"), directory, media, helper)
    history = DownloadHistorySnapshot(
        id=1, path=original.as_posix(), type="电影", title="示例", year="2026",
        media_category="华语", note=note,
    )
    result = resolve_download_source_classification(
        DownloaderTorrent(hash="a" * 40, save_path="/downloads"),
        history, directory_helper=helper,
    )
    assert result.target_save_path == original.as_posix()
    assert note["source"] == "Subscribe"
    assert note["media_folder"]["version"] == 1
    assert "raw_data" not in note["media_folder"]


def test_legacy_history_rejects_unknown_alternate_title(monkeypatch):
    """
    旧历史缺少英文标题时不能静默生成不同媒体目录。
    """
    monkeypatch.setattr(settings, "MOVIE_RENAME_FORMAT", "{{en_title}}/file.mkv")
    helper = MagicMock()
    helper.get_download_dir_by_task_path.return_value = _directory(download_category_folder=True)
    helper.has_fixed_category.return_value = False
    helper.resolve_media_category.return_value = SimpleNamespace(usable=True, path=("华语",))
    history = DownloadHistorySnapshot(id=1, path="/downloads", type="电影", title="示例", year="2026", media_category="华语")
    with pytest.raises(ValueError, match="媒体目录"):
        resolve_download_source_classification(
            DownloaderTorrent(hash="a" * 40, save_path="/downloads"), history, directory_helper=helper,
        )


@pytest.mark.parametrize("note", [
    {"media_folder": {"version": 2, "context": {"title": "示例"}}},
    {"media_folder": {"version": 1, "context": {"title": ["伪造"]}}},
    {"media_folder": {"version": 1, "context": {"title": "示例", "url": "https://secret"}}},
])
def test_malformed_naming_snapshot_only_blocks_enabled_media_folders(monkeypatch, note):
    """
    损坏或越界的历史快照只阻止启用媒体子目录的路径计划。
    """
    monkeypatch.setattr(settings, "MOVIE_RENAME_FORMAT", "{{title}}/file.mkv")
    monkeypatch.setattr("app.application.download.classification.validate_download_save_path", lambda value: value)
    directory = _directory(download_category_folder=True)
    helper = MagicMock()
    helper.get_download_dir_by_task_path.return_value = directory
    helper.has_fixed_category.return_value = False
    helper.resolve_media_category.return_value = SimpleNamespace(usable=True, path=("华语",))
    history = DownloadHistorySnapshot(
        id=1, path="/downloads", type="电影", title="示例", year="2026", media_category="华语", note=note,
    )
    torrent = DownloaderTorrent(hash="a" * 40, save_path="/downloads")
    with pytest.raises(ValueError, match="媒体目录"):
        resolve_download_source_classification(torrent, history, directory_helper=helper)
    directory.download_media_folder = False
    assert resolve_download_source_classification(
        torrent, history, directory_helper=helper,
    ).target_save_path == "/downloads/华语"


def test_legacy_history_can_recover_primary_tmdb_identity(monkeypatch):
    """
    旧历史中的标准 TMDB 主身份足以恢复常用媒体目录。
    """
    monkeypatch.setattr(settings, "MOVIE_RENAME_FORMAT", "{{title}} {tmdb-{{tmdbid}}}/file.mkv")
    monkeypatch.setattr("app.application.download.classification.validate_download_save_path", lambda value: value)
    helper = MagicMock()
    helper.get_download_dir_by_task_path.return_value = _directory(download_category_folder=True)
    helper.has_fixed_category.return_value = False
    helper.resolve_media_category.return_value = SimpleNamespace(usable=True, path=("华语",))
    history = DownloadHistorySnapshot(
        id=1, path="/downloads", type="电影", title="示例", year="2026",
        media_source="themoviedb", media_id="123", media_category="华语",
    )
    assert resolve_download_source_classification(
        DownloaderTorrent(hash="a" * 40, save_path="/downloads"), history, directory_helper=helper,
    ).target_save_path == "/downloads/华语/示例 {tmdb-123}"


def test_oversized_optional_metadata_does_not_break_existing_history():
    """
    可选命名字段过长时保留普通历史结算，不保存无界快照。
    """
    note = DownloadHistoryOwner._build_download_note(
        "Subscribe", _media(en_title="a" * 2049), MetaInfo("Example"),
    )
    assert note == {"source": "Subscribe"}


@pytest.mark.parametrize("overrides", [{"en_title": ["bad"]}, {"actors": None}])
def test_malformed_optional_metadata_does_not_break_existing_history(overrides):
    """
    插件提供的异常可选字段不能让原本可结算的下载因新增快照失败。
    """
    assert DownloadHistoryOwner._build_download_note(
        "Subscribe", _media(**overrides), MetaInfo("Example"),
    ) == {"source": "Subscribe"}
