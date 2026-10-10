"""下载保存目录支持目录别名：路径优先、别名兜底、错误文案与链路集成。"""
from pathlib import Path
from unittest.mock import patch

import pytest

from app.application.directory import (
    DirectoryHelper,
    specified_download_save_path,
    validate_download_save_path,
)
from app.chain.download.subtitle import DownloadSubtitleOwner
from app.domain.context import MediaInfo
from app.schemas.system import TransferDirectoryConf
from app.schemas.types import MediaType


def _download_dirs() -> list[TransferDirectoryConf]:
    """按 priority 排好序的下载目录配置，覆盖本地、远端、重名、路径型别名和空名称（"" 与 None）。"""
    return [
        TransferDirectoryConf(name="Movies", priority=0, storage="local", download_path="/downloads/en"),
        TransferDirectoryConf(name="可心影视库", priority=1, storage="local", download_path="/downloads/movies"),
        TransferDirectoryConf(name="动漫远程库", priority=2, storage="rclone", download_path="/media/anime"),
        TransferDirectoryConf(name="/downloads/movies", priority=3, storage="local", download_path="/downloads/other"),
        TransferDirectoryConf(name="/legacy/movies", priority=4, storage="local", download_path="/downloads/legacy"),
        TransferDirectoryConf(name="重名目录", priority=5, storage="local", download_path="/downloads/dup-a"),
        TransferDirectoryConf(name="重名目录", priority=6, storage="local", download_path="/downloads/dup-b"),
        TransferDirectoryConf(name="", priority=7, storage="local", download_path="/downloads/unnamed"),
        TransferDirectoryConf(name=None, priority=8, storage="local", download_path="/downloads/noname"),
    ]


@pytest.fixture
def download_dirs():
    """用固定目录配置替换 DirectoryHelper.get_download_dirs。"""
    with patch.object(DirectoryHelper, "get_download_dirs", return_value=_download_dirs()):
        yield


@pytest.mark.usefixtures("download_dirs")
def test_alias_resolves_to_local_root():
    """本地目录别名解析为该目录根路径。"""
    assert validate_download_save_path("可心影视库") == "/downloads/movies"


@pytest.mark.usefixtures("download_dirs")
def test_alias_resolves_to_remote_root_uri():
    """远端目录别名解析为带存储前缀的根 URI，与 /download/paths 的 save_path 一致。"""
    assert validate_download_save_path("动漫远程库") == "rclone:/media/anime"


@pytest.mark.usefixtures("download_dirs")
def test_configured_path_wins_over_same_named_alias():
    """输入既是合法配置路径又是另一目录的别名时，路径语义优先。"""
    assert validate_download_save_path("/downloads/movies") == "/downloads/movies"


@pytest.mark.usefixtures("download_dirs")
def test_path_like_alias_resolves_when_path_is_not_configured():
    """路径形态的别名在路径校验失败后仍按别名解析。"""
    assert validate_download_save_path("/legacy/movies") == "/downloads/legacy"


@pytest.mark.usefixtures("download_dirs")
def test_duplicate_alias_uses_priority_order():
    """重名目录沿用 get_download_dirs 的 priority 顺序取第一个。"""
    assert validate_download_save_path("重名目录") == "/downloads/dup-a"


def test_alias_skips_invalid_directory_roots():
    """同名目录中根路径无效的配置被跳过；只剩无效配置时按别名不存在报错。"""
    invalid = TransferDirectoryConf(name="X", priority=1, storage="local", download_path="relative/path")
    valid = TransferDirectoryConf(name="X", priority=2, storage="local", download_path="/downloads/x")
    with patch.object(DirectoryHelper, "get_download_dirs", return_value=[invalid, valid]):
        assert validate_download_save_path("X") == "/downloads/x"
    with patch.object(DirectoryHelper, "get_download_dirs", return_value=[invalid]):
        with pytest.raises(ValueError, match="未找到名为「X」的下载目录"):
            validate_download_save_path("X")


@pytest.mark.usefixtures("download_dirs")
def test_alias_is_trimmed_and_case_sensitive():
    """别名匹配裁掉首尾空白，但不忽略大小写。"""
    assert validate_download_save_path("  Movies  ") == "/downloads/en"
    with pytest.raises(ValueError, match="未找到名为「movies」的下载目录"):
        validate_download_save_path("movies")


@pytest.mark.usefixtures("download_dirs")
def test_unknown_alias_reports_missing_directory():
    """不像路径的未知输入给出明确的目录不存在文案。"""
    with pytest.raises(ValueError, match="未找到名为「不存在的目录」的下载目录"):
        validate_download_save_path("不存在的目录")


@pytest.mark.usefixtures("download_dirs")
@pytest.mark.parametrize("value", ["", "   ", None])
def test_blank_value_keeps_empty_path_error(value):
    """空值不参与别名匹配，即使存在 name 为空的目录配置。"""
    with pytest.raises(ValueError, match="保存路径不能为空"):
        validate_download_save_path(value)


@pytest.mark.usefixtures("download_dirs")
@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("foo/bar", "保存路径必须是绝对路径"),
        ("/downloads/../etc", "保存路径不能包含上级目录"),
        ("/not/configured", "保存路径不在允许的下载目录范围内"),
        ("rclone:/nowhere", "保存路径不在允许的下载目录范围内"),
        ("rclone:anime", "保存路径必须是绝对路径"),
        ("C:Movies", "保存路径必须是 Windows 绝对路径"),
    ],
)
def test_path_like_input_keeps_original_error(value, message):
    """带路径特征的输入在别名也不命中时保留原始路径校验文案。"""
    with pytest.raises(ValueError, match=message):
        validate_download_save_path(value)


@pytest.mark.parametrize(
    ("storage", "root", "expected_storage"),
    [("local", "/downloads/tv", "local"), ("rclone", "/media/tv", "rclone"), ("local", "D:/Downloads", "local")],
)
def test_resolve_media_download_dir_appends_type_folder_for_alias(storage, root, expected_storage):
    """别名命中后链路按该目录的类型子目录规则追加路径。"""
    dirs = [
        TransferDirectoryConf(
            name="剧集库", priority=1, storage=storage, download_path=root, download_type_folder=True,
        ),
    ]
    media = MediaInfo(type=MediaType.TV, title="测试剧集")
    with patch.object(DirectoryHelper, "get_download_dirs", return_value=dirs):
        result = DownloadSubtitleOwner._resolve_media_download_dir(media, save_path="剧集库")
    assert result == (expected_storage, Path(root) / MediaType.TV.value, "")


def test_resolve_media_download_dir_surfaces_unknown_alias_error():
    """未知别名的错误文案沿链路返回给调用方。"""
    dirs = [TransferDirectoryConf(name="剧集库", priority=1, storage="local", download_path="/downloads/tv")]
    media = MediaInfo(type=MediaType.TV, title="测试剧集")
    with patch.object(DirectoryHelper, "get_download_dirs", return_value=dirs):
        result = DownloadSubtitleOwner._resolve_media_download_dir(media, save_path="不存在的库")
    assert result == (None, None, "未找到名为「不存在的库」的下载目录")


@pytest.mark.parametrize(("value", "expected"), [(None, None), ("", None), ("  ", None), ("剧集库", "剧集库")])
def test_specified_download_save_path_treats_blank_as_unspecified(value, expected):
    """空白保存目录视为未指定，非空值原样交给后续校验。"""
    assert specified_download_save_path(value) == expected


def test_resolve_media_download_dir_uses_default_dir_for_blank_save_path():
    """空字符串保存目录走媒体默认下载目录，而不是报“保存路径不能为空”。"""
    default_dir = TransferDirectoryConf(name="剧集库", priority=1, storage="local", download_path="/downloads/tv")
    media = MediaInfo(type=MediaType.TV, title="测试剧集")
    with patch.object(DirectoryHelper, "get_dir", return_value=default_dir):
        result = DownloadSubtitleOwner._resolve_media_download_dir(media, save_path="")
    assert result == ("local", Path("/downloads/tv"), "")
