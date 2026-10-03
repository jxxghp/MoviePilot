"""录音标识与发行来源的一致性必须贯穿自动匹配、缓存及文件名回退。"""

import asyncio
import shutil
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from mutagen.flac import FLAC

from app.application.audio import AudioMetadataHelper
from app.chain.acoustid import AcoustIdChain
from app.chain.media import MediaChain
from app.chain.media.path import _fingerprint_info_matches_evidence
from app.chain.musicbrainz import MusicBrainzChain
from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.music import align_music_tracks, music_isrc_conflicts, music_isrc_matches
from app.modules.musicbrainz import MusicBrainzModule
from app.schemas.types import MediaSource
from tests.test_music_album_match import ALBUM_TRACKS, _local_tracks, _release_detail
from tests.test_music_recognize_cache import _build_module_with_cache, _build_music_cache
from tests.test_music_source_fallback import _album, _backend, _directory_setup

ISRC = "USABC2400001"
OTHER_ISRC = "USABC2400002"


def _recording(isrc=OTHER_ISRC, **kwargs):
    """构造同名歌曲以保证测试只由身份约束决定结果。"""
    return MusicInfo(title="Song", artists=["Artist"], media_source=MediaSource.MusicBrainz,
                     media_id="recording", isrc=isrc, track_number=1, **kwargs)


@pytest.mark.parametrize("local,remote,extras,matched,conflict", [
    (ISRC, OTHER_ISRC, [], False, True),
    ("ISRC: US-ABC-24-00001", "usabc2400001", [], True, False),
    (ISRC, OTHER_ISRC, [OTHER_ISRC, ISRC], True, False),
    (ISRC, None, ["unknown", ISRC], True, False),
    (ISRC, None, [], False, False),
    (None, OTHER_ISRC, [], False, False),
    ("Unknown", OTHER_ISRC, [], False, False),
    (ISRC, "Unknown", [], False, False),
    (ISRC, None, {"isrc": ISRC}, False, False),
])
def test_isrc_evidence_validates_all_recording_codes(local, remote, extras, matched, conflict):
    """有效列表、展示分隔符和缺失值有不同语义，异常列表不得当成已验证身份。"""
    info = _recording(remote, raw_data={"isrcs": extras})
    meta = MetaMusic(title="Song", isrc=local)
    assert music_isrc_matches(info, meta) is matched
    assert music_isrc_conflicts(info, meta) is conflict


@pytest.mark.parametrize("reverse", [False, True])
def test_recording_candidates_cannot_outvote_explicit_isrc(reverse):
    """明确冲突的唯一同名候选被拒绝，完整ISRC列表命中不受列表顺序干扰。"""
    meta = MetaMusic(title="Song", artists=["Artist"], isrc=ISRC)
    wrong = _recording()
    assert MusicBrainzModule._select_candidate(meta, [wrong], MediaSource.MusicBrainz) is None
    right = _recording(raw_data={"isrcs": [OTHER_ISRC, ISRC]})
    right.media_id, right.title = "right-recording", "Different Display Title"
    candidates = [wrong, right] if reverse else [right, wrong]
    assert MusicBrainzModule._select_candidate(meta, candidates, MediaSource.MusicBrainz) is right


def test_album_alignment_uses_isrc_and_preserves_manual_correction():
    """自动对位拒绝冲突；人工选版可纠正错误旧标签，但仍不能覆盖严重时长冲突。"""
    local = MetaMusic(title="Song", track_number=1, isrc=ISRC)
    wrong = _recording()
    assert align_music_tracks([local], [wrong]) == {}
    assert align_music_tracks([local], [wrong], allow_title_override=True) == {0: 0}
    right = _recording(raw_data={"isrcs": [ISRC]})
    right.title, right.track_number = "Different Display Title", 2
    assert align_music_tracks([local], [wrong, right]) == {0: 1}
    local.duration, right.duration = 180, 300
    assert align_music_tracks([local], [right], allow_title_override=True) == {}


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("same_recording", [False, True])
def test_cache_rechecks_isrc_including_secondary_codes(monkeypatch, asynchronous, same_recording):
    """旧缓存冲突须重新查询，同一录音的第二个ISRC则可直接复用缓存。"""
    meta = MetaMusic(title="Song", artists=["Artist"], isrc=ISRC)
    cache = _build_music_cache({})
    module = _build_module_with_cache(cache)
    cached = _recording(raw_data={"isrcs": [OTHER_ISRC, ISRC] if same_recording else [OTHER_ISRC]})
    cached.raw_data["unrelated_payload"] = "must not persist"
    cache.update(meta, cached, music_type="recording")
    assert set(cache.get(meta, music_type="recording").raw_data) == {"isrcs"}
    fresh = _recording(ISRC)
    fresh.media_id = "fresh-recording"
    lookup = AsyncMock(return_value=[fresh]) if asynchronous else Mock(return_value=[fresh])
    monkeypatch.setattr(module, "_async_search_recordings" if asynchronous else "_search_recordings", lookup)
    result = asyncio.run(module.async_recognize_media(meta=meta, music_type="recording")) if asynchronous else (
        module.recognize_media(meta=meta, music_type="recording"))
    assert result.media_id == (cached.media_id if same_recording else fresh.media_id)
    assert lookup.call_count == (0 if same_recording else 1)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("same_recording", [False, True])
def test_release_lookup_preserves_isrc_for_track_alignment(monkeypatch, asynchronous, same_recording):
    """真正的Release响应投影后仍保留全部ISRC，自动整专不能接受错误录音。"""
    module = MusicBrainzModule()
    detail = _release_detail("release", "七里香", "周杰伦", ALBUM_TRACKS)
    codes = [OTHER_ISRC, ISRC] if same_recording else [OTHER_ISRC]
    detail["media"][0]["tracks"][0]["recording"]["isrcs"] = codes
    local = _local_tracks()
    local[0].isrc = ISRC
    request = AsyncMock(return_value=detail) if asynchronous else Mock(return_value=detail)
    monkeypatch.setattr(module, "_async_request_json" if asynchronous else "_request_json", request)
    meta = MetaMusic(album="七里香", artists=["周杰伦"], musicbrainz_release_id="release")
    result = asyncio.run(module.async_match_music_album(meta, local)) if asynchronous else module.match_music_album(meta, local)
    assert (result is not None) is same_recording
    assert "isrcs" in request.call_args.kwargs["params"]["inc"].split("+")
    if result:
        assert music_isrc_matches(result.tracks[0], local[0])
        assert result.tracks[0].raw_data["isrcs"] == codes


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("field,value", [
    ("media_source", "theaudiodb"), ("album_id", "other-group"),
    ("musicbrainz_release_id", "other-release"), ("musicbrainz_release_group_id", "other-group"),
])
def test_automatic_album_source_and_directory_reject_conflicting_tracks(tmp_path, monkeypatch, asynchronous, field, value):
    """来源链和整专编排均拒绝混源或混版曲目；不依赖用户显式指定发行ID才核验。"""
    album = MusicBrainzModule._release_to_album(_release_detail("release", "七里香", "周杰伦", ALBUM_TRACKS))
    setattr(album.tracks[1], field, value)
    source = MusicBrainzChain()
    monkeypatch.setattr(source, "async_run_module" if asynchronous else "run_module",
                        AsyncMock(return_value=album) if asynchronous else Mock(return_value=album))
    meta = MetaMusic(album="七里香", artists=["周杰伦"])
    result = asyncio.run(source.async_match_music_album(meta, _local_tracks())) if asynchronous else source.match_music_album(meta, _local_tracks())
    assert result is None
    files = [tmp_path / f"{index}.flac" for index in range(3)]
    monkeypatch.setattr(AudioMetadataHelper, "read_many", Mock(return_value=_local_tracks()))
    monkeypatch.setattr(source, "async_match_music_album" if asynchronous else "match_music_album",
                        AsyncMock(return_value=album) if asynchronous else Mock(return_value=album))
    monkeypatch.setattr("app.chain.media.album.MusicBrainzChain", lambda: source)
    chain = MediaChain()
    method = chain._async_match_music_album_directory if asynchronous else chain._match_music_album_directory
    result = asyncio.run(method(tmp_path, files)) if asynchronous else method(tmp_path, files)
    assert not result and result.recognition["status"] == "conflict"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_secondary_album_cannot_mix_source_namespaces(tmp_path, monkeypatch, asynchronous):
    """次级来源的专辑壳也不能让其它来源的同名曲目取得当前来源身份。"""
    directory, chain, _config, _primary = _directory_setup(tmp_path, monkeypatch, "theaudiodb")
    album = _album(MediaSource.TheAudioDB)
    album.tracks[0].media_source = MediaSource.DoubanMusic
    monkeypatch.setattr(chain, "_music_source_chain", lambda _source: _backend([album]))
    result = asyncio.run(chain.async_recognize_music_album_directory(directory)) if asynchronous else chain.recognize_music_album_directory(directory)
    assert not result


@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_tags_keep_isrc_constraint_through_filename_fallback(tmp_path, monkeypatch, asynchronous):
    """读取真实FLAC后，标签层被拒绝不能通过文件名层丢弃ISRC约束后重新误命中。"""
    path = tmp_path / "01 - Artist - Song.flac"
    shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", path)
    audio = FLAC(path)
    audio.update(title="Song", artist="Artist", isrc=ISRC)
    audio.save()
    chain = object.__new__(MediaChain)
    lookup = AsyncMock(side_effect=lambda **_kwargs: deepcopy(_recording())) if asynchronous else Mock(side_effect=lambda **_kwargs: deepcopy(_recording()))
    monkeypatch.setattr(chain, "async_recognize_media" if asynchronous else "recognize_media", lookup)
    monkeypatch.setattr(AcoustIdChain, "async_identify_music_by_fingerprint" if asynchronous else "identify_music_by_fingerprint",
                        AsyncMock(return_value=None) if asynchronous else Mock(return_value=None))
    monkeypatch.setattr(chain, "_async_music_album_dir_fallback" if asynchronous else "_music_album_dir_fallback",
                        AsyncMock(return_value=None) if asynchronous else Mock(return_value=None))
    monkeypatch.setattr(chain, "_async_finalize_recognition_result" if asynchronous else "_finalize_recognition_result",
                        AsyncMock(side_effect=lambda info, **_kwargs: info) if asynchronous else lambda info, **_kwargs: info)
    _meta, info = asyncio.run(chain.async_recognize_music_by_path(path)) if asynchronous else chain.recognize_music_by_path(path)
    assert info.media_id is None
    assert lookup.call_count == 2
    assert all(call.kwargs["meta"].isrc == ISRC for call in lookup.call_args_list)


@pytest.mark.parametrize("title", ["Song", "Track 01"])
def test_high_fingerprint_score_cannot_hide_isrc_conflict(title):
    """强指纹也须与实际标签的明确录音身份相容，占位曲名不能使ISRC被忽略。"""
    tags = MetaMusic(title=title, artists=["Artist"], isrc=ISRC, duration=180)
    info = _recording(duration=180)
    assert not _fingerprint_info_matches_evidence(info, tags, MetaMusic(title="Song"), fingerprint_score=.99)
