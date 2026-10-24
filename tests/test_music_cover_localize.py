"""
音乐封面本地化回归：Cover Art Archive 封面必须转换为本站可直连地址。

Cover Art Archive 的封面请求会 307 跳转到 ``archive.org``，浏览器在网络受限时
无法直连该归档节点。封面必须由后端取图后落到静态目录，接口返回本站地址。
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from app.application import music_cover
from app.modules.listenbrainz import ListenBrainzModule
from app.modules.musicbrainz import MusicBrainzModule

_RELEASE_URL = "https://coverartarchive.org/release/rel-1/front-500"
_GROUP_URL = "https://coverartarchive.org/release-group/group-1/front-500"
_IMAGE_BYTES = b"fake-image-bytes"


def _static_root(tmp_path: Path) -> Path:
    """
    建立临时静态根目录，隔离真实 /public 写入。
    """
    root = tmp_path / "public"
    root.mkdir()
    return root


def test_localize_downloads_cover_into_static_dir(tmp_path, monkeypatch):
    """
    本地缺失时应取图并写入静态目录，返回可直连的本站路径。
    """
    root = _static_root(tmp_path)
    monkeypatch.setattr(music_cover, "_static_root", lambda: root)
    with patch("app.application.image.ImageHelper") as helper:
        helper.return_value.fetch_image.return_value = _IMAGE_BYTES
        result = music_cover.localize_music_cover(_RELEASE_URL)

    assert result is not None
    assert result.startswith("/static/music/cover_")
    assert result.endswith(".jpg")
    saved = root / result.lstrip("/")
    assert saved.read_bytes() == _IMAGE_BYTES
    helper.return_value.fetch_image.assert_called_once_with(_RELEASE_URL, use_cache=True)


def test_localize_reuses_existing_file_without_network(tmp_path, monkeypatch):
    """
    已本地化的封面应直接复用，避免每次刷新重复下载。
    """
    root = _static_root(tmp_path)
    monkeypatch.setattr(music_cover, "_static_root", lambda: root)
    with patch("app.application.image.ImageHelper") as helper:
        helper.return_value.fetch_image.return_value = _IMAGE_BYTES
        first = music_cover.localize_music_cover(_RELEASE_URL)
        second = music_cover.localize_music_cover(_RELEASE_URL)

    assert first == second
    assert helper.return_value.fetch_image.call_count == 1


def test_localize_returns_none_on_fetch_failure(tmp_path, monkeypatch):
    """
    取图失败不得抛出异常，也不得写入残缺文件。
    """
    root = _static_root(tmp_path)
    monkeypatch.setattr(music_cover, "_static_root", lambda: root)
    with patch("app.application.image.ImageHelper") as helper:
        helper.return_value.fetch_image.return_value = None
        assert music_cover.localize_music_cover(_RELEASE_URL) is None

    assert not any((root / "static").glob("*")) if (root / "static").exists() else True


def test_localize_skips_non_http_url():
    """
    非 HTTP 地址不参与本地化，交由调用方原样处理。
    """
    assert music_cover.localize_music_cover("/local/cover.jpg") is None
    assert music_cover.localize_music_cover("") is None
    assert music_cover.localize_music_cover(None) is None


def test_listenbrainz_cover_prefers_localized_url(monkeypatch):
    """
    ListenBrainz 单曲与专辑封面都应转换为本站地址，而不是 CAA 官方地址。
    """
    monkeypatch.setattr(
        "app.modules.listenbrainz.localize_music_cover",
        lambda url: f"/static/music/{url.rsplit('/', 3)[-3]}-{url.rsplit('/', 1)[-1]}",
    )
    assert ListenBrainzModule._release_cover("rel-1") == "/static/music/release-front-500"
    assert ListenBrainzModule._release_group_cover("group-1") == "/static/music/release-group-front-500"


def test_listenbrainz_cover_falls_back_to_source_when_localize_fails(monkeypatch):
    """
    本地化失败时退回原始地址，不得返回无法解析的相对接口地址。
    """
    monkeypatch.setattr("app.modules.listenbrainz.localize_music_cover", lambda url: None)
    cover = ListenBrainzModule._release_cover("rel-1")
    assert cover == _RELEASE_URL
    assert cover.startswith("https://")


def test_musicbrainz_cover_prefers_localized_url(monkeypatch):
    """
    MusicBrainz 识别结果的封面同样应转换为本站地址。
    """
    monkeypatch.setattr(
        "app.modules.musicbrainz.localize_music_cover",
        lambda url: "/static/music/group-cover.jpg",
    )
    assert MusicBrainzModule._build_cover_url("group-1") == "/static/music/group-cover.jpg"


def test_musicbrainz_cover_falls_back_to_source_when_localize_fails(monkeypatch):
    """
    MusicBrainz 本地化失败时保持官方地址，且空 ID 仍返回 None。
    """
    monkeypatch.setattr("app.modules.musicbrainz.localize_music_cover", lambda url: None)
    assert MusicBrainzModule._build_cover_url("group-1") == _GROUP_URL
    assert MusicBrainzModule._build_cover_url(None) is None
