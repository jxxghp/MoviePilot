"""音乐专辑详情与整理命名共享简体开关，并保留原始来源证据。"""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest
from jinja2 import Template

from app.application.messaging.message import TemplateContextBuilder
from app.chain.media import MediaChain
from app.domain.context import MusicAlbumInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.runtime.config import ConfigModel, settings


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("simplified", [False, True])
@pytest.mark.parametrize("album_title", ["歲月", "Album"])
def test_album_details_simplify_tracks_without_mutating_source(
        monkeypatch, asynchronous, simplified, album_title,
):
    """同步、异步详情均转换曲目；仅曲目含繁体时也不能污染来源缓存。"""
    source = MusicAlbumInfo(
        media_source="musicbrainz", media_id="album-1", title=album_title,
        artists=["優里"] if album_title != "Album" else ["Artist"],
        artist_ids=["artist-1"], release_date="2026-09-02",
        musicbrainz_release_id="release-1",
        tracks=[MusicInfo(
            media_source="musicbrainz", media_id="recording-1",
            title="後來", artists=["優里", "UMI"], artist_ids=["artist-1", "artist-2"],
            album="歲月", album_artist="優里 / UMI", album_id="album-1",
            lyrics="後來的歌詞", track_number=1, disc_number=1,
            musicbrainz_release_id="release-1", musicbrainz_release_track_id="track-1",
            raw_data={"title": "後來", "artist-credit": [{"name": "優里"}]},
        )],
        raw_data={"title": album_title},
    )
    original = deepcopy(source)
    provider = Mock()
    provider.get_music_album.return_value = source
    provider.async_get_music_album = AsyncMock(return_value=source)
    chain = MediaChain()
    monkeypatch.setattr(chain, "_music_source_chain", Mock(return_value=provider))
    monkeypatch.setattr(settings, "MUSIC_METADATA_TO_SIMPLIFIED", simplified)

    kwargs = {"media_source": "musicbrainz", "media_id": "album-1",
              "musicbrainz_release_id": "release-1"}
    result = (asyncio.run(chain.async_get_music_album(**kwargs)) if asynchronous
              else chain.get_music_album(**kwargs))

    assert result.title == ("岁月" if simplified and album_title != "Album" else album_title)
    assert result.artists == (["优里"] if simplified and album_title != "Album" else original.artists)
    assert result.artist_ids == original.artist_ids
    assert result.musicbrainz_release_id == "release-1"
    track = result.tracks[0]
    assert track.title == ("后来" if simplified else "後來")
    assert track.artists == (["优里", "UMI"] if simplified else ["優里", "UMI"])
    assert track.album == ("岁月" if simplified else "歲月")
    assert track.album_artist == ("优里 / UMI" if simplified else "優里 / UMI")
    assert (track.media_id, track.album_id, track.artist_ids) == ("recording-1", "album-1", ["artist-1", "artist-2"])
    assert (track.musicbrainz_release_id, track.musicbrainz_release_track_id) == ("release-1", "track-1")
    assert (track.track_number, track.disc_number) == (1, 1)
    assert track.lyrics == original.tracks[0].lyrics
    assert track.raw_data == original.tracks[0].raw_data
    assert source == original
    if simplified:
        assert result is not source
        assert "後來" in track.title_aliases
        if album_title != "Album":
            assert "歲月" in result.title_aliases
            assert "優里" in result.artist_aliases


@pytest.mark.parametrize("music_type", ["local", "recording", "album"])
@pytest.mark.parametrize("simplified", [False, True])
def test_music_naming_converts_selected_fields_without_changing_identity(
        monkeypatch, music_type, simplified,
):
    """本地标签、录音与专辑命名同样转简体，录音身份不替换本地发行专辑。"""
    meta = MetaMusic(
        title="後來", artists=["優里", "UMI"], album="歲月",
        album_artist="優里", year=2026, track_number=1, disc_number=2, total_discs=2,
    )
    info = MusicInfo.from_meta(meta)
    if music_type != "local":
        info.media_source, info.media_id, info.music_type = "musicbrainz", "selected-1", music_type
        info.album, info.album_artist, info.year = "精選", "合輯藝人", 2020
    original_meta, original_info = deepcopy(meta.to_dict()), deepcopy(info)
    monkeypatch.setattr(settings, "MUSIC_METADATA_TO_SIMPLIFIED", simplified)

    context = TemplateContextBuilder().build(
        meta=meta, mediainfo=info, file_extension=".flac", include_raw_objects=False,
    )
    path = Template(ConfigModel().MUSIC_RENAME_FORMAT).render(context)

    expected_artist = "合辑艺人" if music_type == "album" else "优里"
    expected_album = "精选" if music_type == "album" else "岁月"
    if not simplified:
        expected_artist = "合輯藝人" if music_type == "album" else "優里"
        expected_album = "精選" if music_type == "album" else "歲月"
    expected_title = "后来" if simplified else "後來"
    expected_year = 2020 if music_type == "album" else 2026
    assert path == f"{expected_artist}/{expected_album} ({expected_year})/Disc 2/01 - {expected_title}.flac"
    assert context["artists"] == (["优里", "UMI"] if simplified else ["優里", "UMI"])
    assert context["artist"] == ("优里 ／ UMI" if simplified else "優里 ／ UMI")
    assert context["title_year"] == f"{expected_title} ({expected_year})"
    assert meta.to_dict() == original_meta
    assert info == original_info
