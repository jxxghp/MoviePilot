"""下载器文件项宿主投影的跨 provider 契约测试。"""

from types import SimpleNamespace

from app.modules._base.downloader import _DownloaderModuleBase
from app.schemas.transfer import DownloaderFile


def test_normalize_torrent_files_accepts_mapping_object_and_sdk_wrapper() -> None:
    """共同适配器应归一字典、属性对象和带 data 的 SDK 集合。"""
    files = SimpleNamespace(
        data=[
            {"id": 1, "name": "Movie.mkv", "size": 1024},
            SimpleNamespace(id="2", name="Subtitle.srt", progress=100),
        ]
    )

    result = _DownloaderModuleBase._normalize_torrent_files(
        files, DownloaderFile.model_validate
    )

    assert result == [
        DownloaderFile(id=1, name="Movie.mkv", size=1024),
        DownloaderFile(id="2", name="Subtitle.srt", progress=100),
    ]


def test_normalize_torrent_files_preserves_none_and_empty_collections() -> None:
    """未命中 provider 与已命中空集合必须继续保持不同结果。"""
    assert _DownloaderModuleBase._normalize_torrent_files(
        None, DownloaderFile.model_validate
    ) is None
    assert _DownloaderModuleBase._normalize_torrent_files(
        [], DownloaderFile.model_validate
    ) == []
