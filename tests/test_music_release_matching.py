"""实际发行身份、曲目覆盖率、候选歧义及 CUE 整专匹配的回归。"""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.audio import AudioMetadataHelper
from app.chain.media import MediaChain
from app.chain.media.album import _album_context_with_resource, _album_directory_cache_key
from app.chain.media.cache import AlbumDirectoryCache
from app.domain.context import MusicAlbumInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.music import expand_music_tracks, music_release_year_matches
from app.modules.musicbrainz import MusicBrainzModule
from tests.test_music_album_match import ALBUM_TRACKS, _local_tracks, _release_detail
from tests.test_music_cue import _image_pair


@pytest.mark.parametrize("asynchronous", [False, True])
def test_release_id_uses_direct_lookup_without_search(monkeypatch, asynchronous):
    """真实发行ID进入同步/异步直查，不重复按文件名搜索候选。"""
    module = MusicBrainzModule()
    detail = _release_detail("release-direct", "七里香", "周杰伦", ALBUM_TRACKS)
    request = AsyncMock(return_value=detail) if asynchronous else Mock(return_value=detail)
    monkeypatch.setattr(module, "_async_request_json" if asynchronous else "_request_json", request)
    meta = MetaMusic(album="七里香", artists=["周杰伦"], musicbrainz_release_id="release-direct")

    album = asyncio.run(module.async_match_music_album(meta, _local_tracks())) if asynchronous else module.match_music_album(meta, _local_tracks())

    assert album is not None
    assert album.musicbrainz_release_id == "release-direct"
    request.assert_called_once()
    assert request.call_args.args[0] == "/release/release-direct"
    assert album.raw_data["match_basis"] == "release_id"


@pytest.mark.parametrize("field,value", [("musicbrainz_release_id", "other"), ("musicbrainz_release_group_id", "other"), ("release_year", 2023)])
def test_identity_and_current_release_year_conflicts_are_not_offset_by_score(field, value):
    """曲目完全一致也不能抵消明确发行身份或当前发行年的冲突。"""
    meta = MetaMusic(album="七里香", artists=["周杰伦"], **{field: value})
    detail = _release_detail("release-1", "七里香", "周杰伦", ALBUM_TRACKS)

    assert MusicBrainzModule._select_release_match(meta, _local_tracks(), [detail]) is None


def test_different_artist_with_identical_titles_and_durations_is_rejected():
    """同名专辑的另一位艺人不能凭相同结构获得高分通过。"""
    detail = _release_detail("release-1", "七里香", "Different Artist", ALBUM_TRACKS)

    assert MusicBrainzModule._select_release_match(MetaMusic(album="七里香", artists=["周杰伦"]), _local_tracks(), [detail]) is None


def test_different_recordings_with_tied_scores_require_confirmation():
    """实际录音序列不同的近分候选不能按搜索返回顺序自动选中。"""
    first = _release_detail("release-1", "七里香", "周杰伦", ALBUM_TRACKS)
    other = deepcopy(first)
    other["id"] = "release-2"
    other["media"][0]["tracks"][1]["recording"]["id"] = "alternate-recording"
    meta = MetaMusic(album="七里香", artists=["周杰伦"])

    assert MusicBrainzModule._select_release_match(meta, _local_tracks(), [first, other]) is None
    assert MusicBrainzModule._select_release_match(meta, _local_tracks(), [other, first]) is None


def test_equivalent_release_content_ignores_response_order():
    """同发行组的录音和位置不变时，响应数组顺序不产生虚假歧义。"""
    first = _release_detail("release-1", "七里香", "周杰伦", ALBUM_TRACKS)
    other = deepcopy(first)
    other["id"] = "release-2"
    other["media"][0]["tracks"].reverse()

    assert MusicBrainzModule._select_release_match(MetaMusic(album="七里香", artists=["周杰伦"]), _local_tracks(), [first, other]) is not None


def test_actual_performer_conflict_is_rejected_but_album_credit_is_not_performer():
    """合辑逐曲艺人必须核对，目录继承的专辑艺人不能冒充每首歌的演唱者。"""
    detail = _release_detail("release-1", "七里香", "Various Artists", ALBUM_TRACKS)
    for track in detail["media"][0]["tracks"]:
        track["recording"]["artist-credit"] = [{"artist": {"id": "guest", "name": "Guest"}}]
    local = _local_tracks()
    for track in local:
        track.artists = ["Another Singer"]
        track.field_sources["artists"] = "tag"
    meta = MetaMusic(album="七里香", album_artist="Various Artists")
    assert MusicBrainzModule._select_release_match(meta, local, [detail]) is None
    for track in local:
        track.artists = ["Various Artists"]
        track.field_sources["artists"] = "directory"
    assert MusicBrainzModule._select_release_match(meta, local, [detail]) is not None


def test_track_coverage_and_matching_are_order_independent():
    """评分实际使用曲目对位，乱序文件不改变匹配结果或分数。"""
    detail = _release_detail("release-1", "七里香", "周杰伦", ALBUM_TRACKS)
    meta = MetaMusic(album="七里香", artists=["周杰伦"])
    first = MusicBrainzModule._select_release_match(meta, _local_tracks(), [detail])
    shuffled = MusicBrainzModule._select_release_match(meta, list(reversed(_local_tracks())), [detail])

    assert first is not None and shuffled is not None
    assert first.raw_data["match_score"] == shuffled.raw_data["match_score"] == 100
    assert first.raw_data["match_coverage"] == 1


def test_unmatched_bonus_and_duplicate_files_reject_entire_candidate():
    """不能先接受高分专辑，再让额外文件随排序绑定或部分冒认身份。"""
    detail = _release_detail("release-1", "七里香", "周杰伦", ALBUM_TRACKS)
    meta = MetaMusic(album="七里香", artists=["周杰伦"])
    for extras in ([MetaMusic(title="Unrelated Bonus")], [_local_tracks()[0]]):
        assert MusicBrainzModule._select_release_match(meta, [*_local_tracks(), *extras], [detail]) is None


def test_partial_album_with_known_titles_still_matches():
    """只下载部分歌曲时要求本地文件全覆盖，不要求必须凑齐完整专辑。"""
    detail = _release_detail("release-1", "七里香", "周杰伦", ALBUM_TRACKS)

    assert MusicBrainzModule._select_release_match(MetaMusic(album="七里香", artists=["周杰伦"]), _local_tracks()[1:], [detail]) is not None


def test_absence_of_bonus_tracks_does_not_prove_standard_edition():
    """已有曲目也可能来自豪华版的部分下载，少了附赠曲本身不能消除发行歧义。"""
    standard = _release_detail("standard", "七里香", "周杰伦", ALBUM_TRACKS)
    deluxe = _release_detail("deluxe", "七里香", "周杰伦", [*ALBUM_TRACKS, ("Bonus", 200)])

    assert MusicBrainzModule._select_release_match(MetaMusic(album="七里香", artists=["周杰伦"]), _local_tracks(), [standard, deluxe]) is None


def test_explicit_live_version_cannot_select_studio_album():
    """已知现场录音版本先参与准入，不能先选录音室版再逐曲失败。"""
    studio = _release_detail("studio", "七里香", "周杰伦", ALBUM_TRACKS)
    live = deepcopy(studio)
    live["id"] = "live"
    live["release-group"]["secondary-types"] = ["Live"]
    meta = MetaMusic(album="七里香", title="七里香", artists=["周杰伦"], version="Live")

    album = MusicBrainzModule._select_release_match(meta, _local_tracks(), [studio, live])

    assert album is not None and album.musicbrainz_release_id == "live"


@pytest.mark.parametrize("name", ["Music", "inbox", "Downloads", "Various Artists"])
def test_generic_directory_does_not_generate_album_search(name):
    """普通目录只能使用实际曲名反查，不能重复查询伪专辑。"""
    queries = MusicBrainzModule._release_queries(MetaMusic(title=name), _local_tracks())

    assert queries
    assert all("release:" not in query for query in queries)


def test_real_tag_album_and_numeric_song_are_searchable():
    """真正名为 Music 的专辑和数字歌曲不会被目录占位规则丢弃。"""
    queries = MusicBrainzModule._release_queries(
        MetaMusic(album="Music", field_sources={"album": "tag"}),
        [MetaMusic(title="22", field_sources={"title": "tag"})],
    )

    assert any('release:"Music"' in query for query in queries)
    assert any('recording:"22"' in query for query in queries)


def test_release_group_id_limits_queries_to_its_namespace():
    """发行组ID只约束发行搜索，不能冒充具体发行或录音ID。"""
    assert MusicBrainzModule._release_queries(MetaMusic(musicbrainz_release_group_id="group-1"), _local_tracks()) == ["rgid:group-1"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_track_title_lookup_uses_recording_index_then_related_releases(monkeypatch, asynchronous):
    """按官方 Recording 搜索响应形状回放，曲名反查不能向 Release 索引发送 recording 字段。"""
    module = MusicBrainzModule()
    detail = _release_detail("related-release", "七里香", "周杰伦", ALBUM_TRACKS)
    recording_payload = {"recordings": [{"id": "rec-1", "score": 100, "title": "我的地盘",
                                        "releases": [{"id": "related-release", "title": "七里香"}]}]}

    def respond(path, params=None):
        """重放搜索和详情，任何错误的实体路由都直接失败。"""
        if path == "/recording":
            assert 'recording:"我的地盘"' in params["query"]
            return recording_payload
        assert path == "/release/related-release"
        return detail

    request = AsyncMock(side_effect=respond) if asynchronous else Mock(side_effect=respond)
    monkeypatch.setattr(module, "_async_request_json" if asynchronous else "_request_json", request)
    meta = MetaMusic(title="Music")
    album = asyncio.run(module.async_match_music_album(meta, _local_tracks())) if asynchronous else module.match_music_album(meta, _local_tracks())

    assert album is not None and album.musicbrainz_release_id == "related-release"
    assert request.call_count == 2


def test_multiple_recording_hits_rank_shared_album_ahead_of_first_songs_single():
    """有限详情预算先用于多首歌共同出现的发行，不被第一首歌的多个单曲版占满。"""
    releases, seen = [], set()
    payload = {"recordings": [
        {"id": "one", "score": "100", "releases": [{"id": "single"}, {"id": "album"}]},
        {"id": "two", "score": "100", "releases": [{"id": "album"}]},
    ]}
    original = deepcopy(payload)
    MusicBrainzModule._merge_release_candidates(releases, seen, {"releases": [{"id": "album", "score": "80"}]})
    MusicBrainzModule._merge_release_candidates(releases, seen, payload)
    ranked = MusicBrainzModule._rank_release_candidates(releases, MusicBrainzModule._release_preference())

    assert ranked[0]["id"] == "album"
    assert ranked[0]["score"] == 100
    assert len(ranked) == 2
    assert payload == original


def test_reissue_keeps_original_and_current_year_distinct():
    """原始2004年与2023再版并不冲突，明确的当前年份仍必须匹配。"""
    detail = _release_detail("reissue", "七里香", "周杰伦", ALBUM_TRACKS)
    detail["date"] = "2023-01-01"
    detail["release-group"]["first-release-date"] = "2004-08-03"
    meta = MetaMusic(album="七里香", artists=["周杰伦"], year=2004, original_year=2004, release_year=2023)

    album = MusicBrainzModule._select_release_match(meta, _local_tracks(), [detail])

    assert album is not None
    assert album.original_year == 2004 and album.year == 2023
    assert music_release_year_matches(album.tracks[0], meta)
    meta.release_year = 2022
    assert not music_release_year_matches(album.tracks[0], meta)


def test_context_and_cache_include_release_identity_and_years(tmp_path):
    """标签和种子传播发行证据，手动改变发行上下文不会命中旧目录缓存。"""
    tracks = [MetaMusic(album="七里香", musicbrainz_release_id="release-1", original_year=2004, release_year=2023)]
    context = _album_context_with_resource(tmp_path, tracks, None)

    assert context.musicbrainz_release_id == "release-1"
    assert (context.original_year, context.release_year) == (2004, 2023)
    first = _album_directory_cache_key(tmp_path, (), (), context)
    context.musicbrainz_release_id = "release-2"
    assert first != _album_directory_cache_key(tmp_path, (), (), context)


def test_conflicting_release_context_never_reaches_network(tmp_path, monkeypatch):
    """直接目录识别遇到多发行强标签冲突时明确保留未识别，不能按多数投票选错版。"""
    files = [tmp_path / f"{index}.flac" for index in (1, 2)]
    metas = [MetaMusic(title=str(index), musicbrainz_release_id=f"release-{index}") for index in (1, 2)]
    monkeypatch.setattr(AudioMetadataHelper, "read_many", Mock(return_value=metas))
    factory = Mock(side_effect=AssertionError("冲突证据不应请求网络"))
    monkeypatch.setattr("app.chain.media.album.MusicBrainzChain", factory)

    assert MediaChain()._match_music_album_directory(tmp_path, files) == {}
    factory.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_single_cue_image_matches_as_two_logical_tracks(tmp_path, monkeypatch, asynchronous):
    """一个整轨文件使用两首逻辑歌曲验证，输出必须仍是专辑而非第一首录音。"""
    audio, cue = _image_pair(tmp_path)
    detail = _release_detail("image-release", "专辑示例", "测试歌手", [("第一首歌曲", 1), ("第二首歌曲", 2)])
    album = MusicBrainzModule._release_to_album(detail)
    request = AsyncMock(return_value=album) if asynchronous else Mock(return_value=album)
    source = Mock(**{"async_match_music_album" if asynchronous else "match_music_album": request})
    monkeypatch.setattr("app.chain.media.album.MusicBrainzChain", Mock(return_value=source))
    chain = MediaChain()
    monkeypatch.setattr(chain, "_album_dir_cache", AlbumDirectoryCache(16))

    result = asyncio.run(chain.async_recognize_music_album_directory(audio.parent)) if asynchronous else chain.recognize_music_album_directory(audio.parent)

    assert [track.title for track in request.call_args.args[1]] == ["第一首歌曲", "第二首歌曲"]
    assert [track.duration for track in request.call_args.args[1]] == [1, 2]
    assert request.call_args.args[0].album == "专辑示例"
    info = result[str(audio.resolve())]
    assert info.music_type == "album" and info.media_id == "rg-1"
    assert info.title == "专辑示例"
    signature = chain._album_directory_signature(audio.parent, [audio])
    cue.write_text(cue.read_text().replace("第二首歌曲", "另一首歌曲"))
    assert signature != chain._album_directory_signature(audio.parent, [audio])


def test_manual_cue_selection_validates_tracks_before_binding_album(tmp_path):
    """手选发行也不能给曲目数量或时长不符的整轨直接绑定任意专辑。"""
    audio, _cue = _image_pair(tmp_path)
    wrong = MusicBrainzModule._release_to_album(_release_detail("wrong", "Wrong", "Other", [("First", 180)]))

    assert isinstance(wrong, MusicAlbumInfo)
    assert MediaChain._align_selected_music_album([audio], wrong) == {}
    logical, owners = expand_music_tracks([AudioMetadataHelper.read(audio)])
    assert len(logical) == 2 and owners == [0, 0]
