"""指纹多候选、占位文件名、失败缓存与录音/发行边界回归。"""

import asyncio
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from mutagen.flac import FLAC

from app.application.audio import AudioMetadataHelper
from app.application.music.observation import (
    capture_music_recognition,
    report_music_fingerprints,
    report_music_recognition,
)
from app.chain.acoustid import AcoustIdChain
from app.chain.media import MediaChain
from app.chain.media.path import (
    _async_recognize_fingerprints,
    _fingerprint_info_matches_evidence,
    _recognize_fingerprints,
    _reconcile_fingerprint_release,
    _select_fingerprint_info,
)
from app.chain.transfer.facade import TransferChain
from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.music import music_tags_are_usable, music_track_title_is_weak
from app.modules.acoustid import AcoustIdModule
from tests.test_acoustid_module import RECORDING_ID, FakeResponse
from tests.test_transfer_sync_extra_files import make_fileitem

OTHER_ID = "70b71113-b078-4f07-9d24-374482016a41"


def _audio(tmp_path, name="01.flac"):
    """创建只有时长证据、没有名称标签的真实音频。"""
    path = tmp_path / name
    shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", path)
    return path


@pytest.mark.parametrize("asynchronous", [False, True])
def test_all_native_candidates_are_checked_before_selecting_recording(tmp_path, monkeypatch, asynchronous):
    """首个高分映射不符合时长时，后续正确候选仍可命中，纯编号不能否决真实指纹。"""
    path = _audio(tmp_path)
    chain = object.__new__(MediaChain)
    candidates = [{"recording_id": RECORDING_ID, "score": 0.99, "duration": 3},
                  {"recording_id": OTHER_ID, "score": 0.99, "duration": 3}]

    def identify(_path):
        """模拟旧指纹接口，同时通过观察上下文保留原生多候选证据。"""
        report_music_fingerprints(candidates)
        return RECORDING_ID

    monkeypatch.setattr(AcoustIdChain, "async_identify_music_by_fingerprint" if asynchronous else "identify_music_by_fingerprint",
                        AsyncMock(side_effect=identify) if asynchronous else Mock(side_effect=identify))
    infos = {RECORDING_ID: MusicInfo(media_source="musicbrainz", media_id=RECORDING_ID, title="Wrong Recording", duration=200),
             OTHER_ID: MusicInfo(media_source="musicbrainz", media_id=OTHER_ID, title="Actual Song", artists=["Artist"], duration=3)}
    recognize = AsyncMock(side_effect=lambda _meta, key: infos[key]) if asynchronous else Mock(side_effect=lambda _meta, key: infos[key])
    monkeypatch.setattr(chain, "_async_recognize_musicbrainz_recording" if asynchronous else "_recognize_musicbrainz_recording", recognize)
    monkeypatch.setattr(chain, "_async_finalize_recognition_result", AsyncMock(side_effect=lambda info, **_kwargs: info))
    monkeypatch.setattr(chain, "_finalize_recognition_result", lambda info, **_kwargs: info)
    text_lookup = Mock(side_effect=AssertionError("唯一指纹已命中，不应继续文本查询"))
    monkeypatch.setattr(chain, "recognize_media", text_lookup)
    monkeypatch.setattr(chain, "async_recognize_media", AsyncMock(side_effect=text_lookup))

    _meta, info = asyncio.run(chain.async_recognize_music_by_path(path)) if asynchronous else chain.recognize_music_by_path(path)

    assert info.media_id == OTHER_ID and info.title == "Actual Song"
    assert recognize.call_count == 2
    text_lookup.assert_not_called()


def test_equal_fingerprint_candidates_stay_pending_and_do_not_fall_through(tmp_path, monkeypatch):
    """两个合理录音不能由返回顺序决定，也不能用后续弱搜索掩盖歧义。"""
    path = _audio(tmp_path)
    chain = object.__new__(MediaChain)

    def identify(_path):
        """提供两个同样高分且时长相同的录音关联。"""
        report_music_fingerprints([{"recording_id": item, "score": 0.99, "duration": 3} for item in (RECORDING_ID, OTHER_ID)])
        return RECORDING_ID

    monkeypatch.setattr(AcoustIdChain, "identify_music_by_fingerprint", Mock(side_effect=identify))
    monkeypatch.setattr(chain, "_recognize_musicbrainz_recording", lambda _meta, key: MusicInfo(
        media_source="musicbrainz", media_id=key, title="Song", artists=["Artist"], duration=3))
    monkeypatch.setattr(chain, "_finalize_recognition_result", lambda info, **_kwargs: info)
    text_lookup = Mock(side_effect=AssertionError("不得继续弱搜索"))
    monkeypatch.setattr(chain, "recognize_media", text_lookup)

    meta, info = chain.recognize_music_by_path(path)
    monkeypatch.setattr(MediaChain, "recognize_music_by_path", Mock(return_value=(meta, info)))
    _meta, transfer_info = TransferChain._match_music_recording_context(make_fileitem(str(path)), path, meta)

    assert info.media_id is None
    assert info.raw_data["recognition"]["status"] == "ambiguous"
    assert transfer_info.raw_data["recognition"]["status"] == "ambiguous"
    text_lookup.assert_not_called()


def test_fingerprint_bound_marks_unchecked_candidates_instead_of_claiming_unique():
    """超过五个关联时保留截断事实，不能将未查完的候选称为唯一匹配。"""
    payload = {"status": "ok", "results": [{"score": 1.0, "recordings": [
        {"id": f"00000000-0000-4000-8000-{index:012d}"} for index in range(10)
    ]}]}

    candidates = AcoustIdModule._select_recording_candidates(payload)

    assert len(candidates) == 5
    assert all(item["truncated"] for item in candidates)
    info = MusicInfo(media_source="musicbrainz", media_id=RECORDING_ID, title="Song")
    pending = _select_fingerprint_info(MetaMusic(title="01"), [(1.0, info)], None, None, truncated=True)
    assert pending.media_id is None and pending.raw_data["recognition"]["status"] == "ambiguous"


def test_tagged_recording_identity_disambiguates_supported_fingerprint_mapping():
    """当音频和文本均支持已存Recording ID时，保留标签身份，不任意替换为重复记录。"""
    tagged = MetaMusic(title="Song", artists=["Artist"], media_source="musicbrainz", media_id=OTHER_ID)
    infos = [MusicInfo(media_source="musicbrainz", media_id=value, title="Song", artists=["Artist"]) for value in (RECORDING_ID, OTHER_ID)]

    chosen = _select_fingerprint_info(tagged, [(0.99, info) for info in infos], tagged, None)

    assert chosen.media_id == OTHER_ID
    assert _select_fingerprint_info(tagged, [(0.99, infos[0])], tagged, None) is None


def test_fingerprint_keeps_tagged_release_ids_and_does_not_invent_missing_ones():
    """同名同年的远端发行也不证明实际版本；标签发行ID保留，缺失时不猜。"""
    info = MusicInfo(media_source="musicbrainz", media_id=RECORDING_ID, title="Song", album="Album", year=2004,
                     album_id="remote-group", musicbrainz_release_id="remote-edition", musicbrainz_release_group_id="remote-group")
    tags = MetaMusic(title="Song", album="Album", year=2004, musicbrainz_release_id="tagged-edition")

    selected = _select_fingerprint_info(tags, [(0.99, info)], tags, None)

    assert selected.musicbrainz_release_id == "tagged-edition"
    assert selected.musicbrainz_release_group_id is None
    assert selected.album_id is None
    assert selected.raw_data["recognition"]["release_verified"] is False
    assert info.musicbrainz_release_id == "remote-edition"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_incomplete_fingerprint_details_do_not_select_first_success(tmp_path, monkeypatch, asynchronous):
    """后续候选查询故障时，首个成功详情不能冒充已经排除全部歧义。"""
    path = _audio(tmp_path)

    def identify(_path):
        """返回两个需要核验的高分录音。"""
        report_music_fingerprints([{"recording_id": item, "score": 0.99, "duration": 3} for item in (RECORDING_ID, OTHER_ID)])
        return RECORDING_ID

    def recognize(_meta, key):
        """只完成第一个详情，第二个模拟上游临时故障。"""
        if key == OTHER_ID:
            report_music_recognition("service_error", "详情服务临时失败")
            return None
        return MusicInfo(media_source="musicbrainz", media_id=key, title="Song", duration=3)

    monkeypatch.setattr(AcoustIdChain, "async_identify_music_by_fingerprint" if asynchronous else "identify_music_by_fingerprint",
                        AsyncMock(side_effect=identify) if asynchronous else Mock(side_effect=identify))
    meta = MetaMusic(title="01")
    result = asyncio.run(_async_recognize_fingerprints(path, meta, None, meta, AsyncMock(side_effect=recognize))) if asynchronous else (
        _recognize_fingerprints(path, meta, None, meta, recognize))
    assert result is None


@pytest.mark.parametrize("artist", ["Unknown Artist", "未知艺术家", "www.example.com", "\ufffd"])
def test_placeholder_artist_does_not_reject_unique_native_fingerprint(artist):
    """占位和宣传署名不是强证据，真实唯一指纹可补齐缺失艺人。"""
    tags = MetaMusic(title="01", artists=[artist], field_sources={"title": "tag", "artists": "tag"})
    info = MusicInfo(media_source="musicbrainz", media_id=RECORDING_ID, title="Song", artists=["Artist"], duration=180)
    assert _fingerprint_info_matches_evidence(info, tags, MetaMusic(title="01"), fingerprint_score=0.99, fingerprint_duration=180)


def test_placeholder_title_does_not_hide_explicit_live_version_conflict():
    """即使文件名和曲名是占位，标签明确声明的现场版本仍必须核验。"""
    tags = MetaMusic(title="01", version="Live", artists=["Artist"], field_sources={"title": "tag"})
    info = MusicInfo(media_source="musicbrainz", media_id=RECORDING_ID, title="Song (Studio Version)", artists=["Artist"], duration=180)
    assert not _fingerprint_info_matches_evidence(info, tags, MetaMusic(title="01"), fingerprint_score=0.99, fingerprint_duration=180)


def test_fingerprint_budget_prevents_queued_requests_and_cleans_context(monkeypatch):
    """超出时限不占据未来限流时隙，预算退出后不污染下一次独立识别。"""
    module = AcoustIdModule()
    monkeypatch.setattr(AcoustIdModule, "_last_request_at", 10.0)
    monkeypatch.setattr("app.modules.acoustid.time.monotonic", lambda: 10.0)
    monkeypatch.setattr("app.application.music.observation.monotonic", lambda: 10.0)
    with capture_music_recognition(seconds=0.1) as first:
        assert module._reserve_request_delay() == -1
        assert first.status == "budget_exhausted"
        assert AcoustIdModule._last_request_at == 10.0
    with capture_music_recognition(seconds=2) as second:
        assert 0 < module._reserve_request_delay() < 1
        assert second.status == "not_found"


@pytest.mark.parametrize("payload", [
    {"status": "ok", "results": 12},
    {"status": "ok", "results": [{"score": 1, "recordings": 12}]},
])
def test_malformed_candidate_collections_are_not_recordings(payload):
    """响应结构异常不应崩溃或伪造有效指纹候选。"""
    assert AcoustIdModule._select_recording_candidates(payload) == []


def test_whole_album_numeric_cue_title_is_not_a_track_placeholder():
    """整轨专辑的数字标题与抓轨序号不同，不能因前导零丢掉专辑名。"""
    meta = MetaMusic(title="01", album="01", artists=["Artist"], music_type="album", field_sources={"title": "cue"})
    assert music_tags_are_usable(meta)


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.1, 1.1, "invalid"])
def test_invalid_fingerprint_score_cannot_become_confidence(score):
    """非有限值及越界分数不能被截成满分录音证据。"""
    assert AcoustIdModule._select_recording_candidates({"status": "ok", "results": [
        {"score": score, "recordings": [{"id": RECORDING_ID}]},
    ]}) == []


def test_candidates_survive_cache_and_failures_expire(tmp_path, monkeypatch):
    """缓存完整候选，并让服务故障在短冷却后恢复，不能永久缓存无结果。"""
    path = _audio(tmp_path)
    module = AcoustIdModule()
    module._fpcalc_path = "/fpcalc"
    now = [100.0]
    monkeypatch.setattr("app.modules.acoustid.time.monotonic", lambda: now[0])
    monkeypatch.setattr(module, "_generate_fingerprint", Mock(return_value=(3, "fingerprint")))
    monkeypatch.setattr(module, "_wait_for_rate_limit", lambda: None)
    post = Mock(return_value=FakeResponse({}, status_code=503))
    monkeypatch.setattr("app.modules.acoustid.RequestUtils.post_res", post)
    with capture_music_recognition() as first:
        assert module.identify_music_by_fingerprint(path) is None
    assert first.status == "service_error"
    with capture_music_recognition():
        assert module.identify_music_by_fingerprint(path) is None
    assert post.call_count == 1
    now[0] += 16
    post.return_value = FakeResponse({"status": "ok", "results": [{"score": 0.99, "recordings": [
        {"id": RECORDING_ID}, {"id": OTHER_ID},
    ]}]})
    for _ in range(2):
        with capture_music_recognition() as recovered:
            assert module.identify_music_by_fingerprint(path) == RECORDING_ID
        assert [item["recording_id"] for item in recovered.fingerprint_candidates] == [RECORDING_ID, OTHER_ID]
        assert all(item["duration"] == 3 for item in recovered.fingerprint_candidates)
    assert post.call_count == 2


def test_legacy_plugin_id_without_score_does_not_gain_audio_only_trust():
    """旧插件回执保持兼容，但没有原生分数和时长时不能冒充强指纹证据。"""
    info = MusicInfo(media_source="musicbrainz", media_id=RECORDING_ID, title="Actual Song", duration=180)
    assert not _fingerprint_info_matches_evidence(info, None, MetaMusic(title="01", duration=180))


@pytest.mark.parametrize("title,usable", [("22", True), ("1979", True), ("01", False), ("Track 01", False)])
def test_real_numeric_tags_and_placeholders_use_consistent_rules(tmp_path, title, usable):
    """真实数字歌名可凭完整标签整理，抓轨序号和Track占位仍需补充识别。"""
    path = _audio(tmp_path)
    tags = FLAC(path)
    tags.update(title=[title], artist=["Artist"], album=["Album"], tracknumber=["1"])
    tags.save()

    meta = AudioMetadataHelper.read_tags(path)

    assert music_tags_are_usable(meta) is usable
    assert music_track_title_is_weak(meta) is not usable


def test_recording_fingerprint_cannot_assign_an_unverified_compilation_release():
    """确认录音不等于确认发行；本地专辑保留来源，移除无关远端发行标识。"""
    info = MusicInfo(media_source="musicbrainz", media_id=RECORDING_ID, title="Song", artists=["Artist"],
                     album="Random Compilation", album_id="wrong-group", musicbrainz_release_id="wrong-release",
                     musicbrainz_release_group_id="wrong-group", musicbrainz_release_track_id="wrong-slot",
                     year=2023, release_year=2023, original_year=2004)
    tags = MetaMusic(title="Song", artists=["Artist"], album="Actual Album", album_artist="Artist", year=2004,
                     field_sources={"title": "tag", "artists": "tag", "album": "tag", "year": "tag"})

    corrected = _reconcile_fingerprint_release(info, tags, None, verified_audio=True)

    assert corrected.media_id == RECORDING_ID
    assert corrected.album == "Actual Album" and corrected.year == 2004
    assert corrected.album_id is None and corrected.musicbrainz_release_id is None
    assert corrected.musicbrainz_release_group_id is None and corrected.musicbrainz_release_track_id is None
    assert corrected.field_sources["album"] == "tag"
    assert info.album == "Random Compilation"
