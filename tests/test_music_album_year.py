"""
专辑年份只作辅助证据，明确发行年份和真实内容冲突仍须拒绝。
"""

from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.application.audio import AudioMetadataHelper
from app.chain.media import MediaChain
from app.chain.transfer.filter import _MusicFileFilterBase
from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.music import music_album_candidate_matches, music_album_year_matches, music_artist_alias_targets
from app.modules.musicbrainz import MusicBrainzModule
from app.modules.theaudiodb import TheAudioDbModule
from app.schemas.file import FileItem
from app.schemas.types import MediaSource
from tests.test_music_album_match import ALBUM_TRACKS, _local_tracks, _release_detail


@pytest.fixture
def album():
    """
    构造专辑及录音身份一致、可完整对位的再版候选。
    """
    detail = _release_detail("release-2020", "七里香", "周杰伦", ALBUM_TRACKS)
    detail["date"] = "2020-01-01"
    return MusicBrainzModule._release_to_album(detail)


@pytest.mark.parametrize("year", [None, 2004, 2020])
@pytest.mark.parametrize("source", [MediaSource.MusicBrainz, MediaSource.TheAudioDB, MediaSource.DoubanMusic])
def test_album_track_verification_does_not_require_matching_general_year(album, year, source):
    """
    专辑名称、艺人与曲目吻合时，未知或不同普通年份不阻断来源核验。
    """
    album.media_source = source
    for track in album.tracks:
        track.media_source = source
    meta = MetaMusic(album="七里香", artists=["周杰伦"], year=year)

    assert music_album_candidate_matches(album, meta, _local_tracks())


@pytest.mark.parametrize("year,expected", [(None, True), (2004, False), (2020, True)])
def test_album_track_verification_preserves_explicit_release_year(album, year, expected):
    """
    用户指定具体发行年份时，普通曲目吻合不能抵消发行年的明确冲突。
    """
    meta = MetaMusic(album="七里香", artists=["周杰伦"], release_year=year)

    assert music_album_candidate_matches(album, meta, _local_tracks()) is expected


def test_catalog_first_release_year_is_not_a_known_edition_year():
    """
    次级专辑目录只有首发年时，不把它当成已知再版年份制造冲突。
    """
    info = MusicInfo(music_type="album", year=2004, original_year=2004)

    assert music_album_year_matches(info, MetaMusic(release_year=2020))


@pytest.mark.parametrize("source", [MediaSource.MusicBrainz, MediaSource.TheAudioDB])
@pytest.mark.parametrize("year", [None, 2004, 2020])
def test_album_source_selection_accepts_optional_general_year(album, source, year):
    """
    两个内置来源的专辑候选采用同一可选年份规则。
    """
    album.media_source = source
    meta = MetaMusic(title="七里香", album="七里香", artists=["周杰伦"], year=year)

    result = (MusicBrainzModule._select_album_candidate(meta, [album.to_music_info()])
              if source == MediaSource.MusicBrainz else TheAudioDbModule._select_album(meta, [album]))

    assert result is not None


@pytest.mark.parametrize("source", [MediaSource.MusicBrainz, MediaSource.TheAudioDB])
def test_album_source_selection_rejects_explicit_release_year_conflict(album, source):
    """
    专辑搜索不能绕过明确发行年份，再把错误结果交给整理。
    """
    album.media_source = source
    meta = MetaMusic(title="七里香", album="七里香", artists=["周杰伦"], release_year=2004)

    result = (MusicBrainzModule._select_album_candidate(meta, [album.to_music_info()])
              if source == MediaSource.MusicBrainz else TheAudioDbModule._select_album(meta, [album]))

    assert result is None


def test_album_year_difference_does_not_prevent_artist_alias_verification(album):
    """
    普通年份差异不能阻止候选按已知Artist ID补充真实别名。
    """
    meta = MetaMusic(album="七里香", artists=["ZhouJieLun"], year=2004)

    assert music_artist_alias_targets([(meta, album)], MediaSource.MusicBrainz) == {"artist-1": [album]}


@pytest.mark.parametrize("release_year,matched", [(None, True), (2004, False), (2020, True)])
def test_transfer_album_recheck_uses_optional_year(album, monkeypatch, tmp_path, release_year, matched):
    """
    目录已完整匹配后，整理复核不能重新用普通年份否决同一候选。
    """
    path = tmp_path / "我的地盘.flac"
    info = album.tracks[0]
    monkeypatch.setattr(MediaChain, "recognize_music_album_directory", Mock(return_value={str(path.resolve()): info}))
    meta = MetaMusic(title="我的地盘", album="七里香", artists=["周杰伦"], year=2004, release_year=release_year)

    _meta, result = _MusicFileFilterBase._match_music_album_context(FileItem(storage="local", path=str(path)), path, meta)

    assert (result is not None) is matched


@pytest.mark.parametrize("conflict", ["artist", "title", "version", "tracks"])
def test_optional_year_does_not_accept_conflicting_album_content(album, conflict):
    """
    放宽普通年份不能接受不同艺人、作品、录音版本或无法对位的曲目。
    """
    candidate = deepcopy(album)
    meta = MetaMusic(album="七里香", artists=["周杰伦"], year=2004)
    if conflict == "artist":
        candidate.artists = ["另一位歌手"]
        candidate.artist_aliases = []
    elif conflict == "title":
        candidate.title = "另一张专辑"
    elif conflict == "version":
        meta.version = "Live"
    else:
        candidate.tracks[0].title = "另一首歌曲"

    assert not music_album_candidate_matches(candidate, meta, _local_tracks())


def test_confession_balloon_path_retains_album_without_year():
    """
    真实报告中的纯专辑名目录无需年份，即可补足单曲检索的专辑线索。
    """
    path = Path("/音乐/周杰伦 - 全部专辑（无损或320K）/周杰伦的床边故事/周杰伦 - 告白气球.flac")

    meta = AudioMetadataHelper.read_filename(path)

    assert (meta.title, meta.artists, meta.album, meta.year) == ("告白气球", ["周杰伦"], "周杰伦的床边故事", None)
    assert meta.field_sources["album"] == "directory"


@pytest.mark.parametrize("directory", ["Music", "Downloads", "inbox", "音乐", "未分類"])
def test_generic_directory_does_not_become_album(directory):
    """
    文件名已有歌手也不能把通用监控根目录补成专辑名。
    """
    meta = AudioMetadataHelper.read_filename(Path("/data") / directory / "周杰伦 - 告白气球.flac")

    assert meta.album is None


def test_yearless_directory_is_only_candidate_evidence():
    """
    明确专辑线索排除同名曲的其他专辑，仍保留不同录音ID的歧义保护。
    """
    path = Path("/音乐/周杰伦的床边故事/周杰伦 - 告白气球.flac")
    meta = AudioMetadataHelper.read_filename(path)
    correct = MusicInfo(media_source=MediaSource.MusicBrainz, media_id="recording-one", title="告白气球",
                        artists=["周杰伦"], album="周杰伦的床边故事")
    other = MusicInfo(media_source=MediaSource.MusicBrainz, media_id="recording-two", title="告白气球",
                      artists=["周杰伦"], album="另一张专辑")

    assert MusicBrainzModule._select_candidate(meta, [other, correct], MediaSource.MusicBrainz) is correct
    other.album = correct.album
    assert MusicBrainzModule._select_candidate(meta, [other, correct], MediaSource.MusicBrainz) is None
