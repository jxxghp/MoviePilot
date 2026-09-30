"""真实古典角色标签、发行演奏阵容及序列化合同，不依赖在线服务。"""

import asyncio
import hashlib
import os
import pickle
import shutil
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from mutagen import File as MutagenFile
from mutagen.asf import ASFTags
from mutagen.id3 import ID3, TALB, TCOM, TIPL, TIT2, TMCL, TPE1, TPE3, TXXX
from mutagen.mp4 import MP4Tags

from app import schemas
from app.application.audio import AudioMetadataHelper
from app.application.music.projection import simplify_music_info
from app.chain.media.album import MediaAlbumOwner, _album_context_with_resource, _album_directory_cache_key
from app.chain.media.path import _finalize_music_path_info, _music_info_matches_text_evidence
from app.chain.transfer.music import _local_music_context, prepare_music_batch_context
from app.domain.context import MusicAlbumInfo, MusicInfo
from app.domain.meta.metamusic import MUSIC_CREDIT_FIELDS, MetaMusic, music_credit_values
from app.domain.music import (
    align_music_tracks,
    match_music_resource,
    music_album_candidate_matches,
    music_artist_evidence_matches,
    music_query_artists,
)
from app.modules.musicbrainz import MusicBrainzModule
from app.modules.musicbrainz.cache import MusicBrainzCache
from app.modules.theaudiodb import TheAudioDbModule
from app.schemas.types import MediaSource, MediaType
from tests.test_audio_containers import _write_aiff, _write_dff, _write_dsf, _write_wave
from tests.test_transfer_sync_extra_files import make_fileitem, make_transfer_chain

CREDITS = {"composers": ["Ludwig van Beethoven"], "conductors": ["Herbert von Karajan"],
           "orchestras": ["Berliner Philharmoniker"], "performers": {"violin": ["Anne-Sophie Mutter"]}}
TITLE = "Violin Concerto in D major, op. 61: I. Allegro ma non troppo"


def _native_audio(tmp_path, container):
    """使用独立Mutagen原生字段制作音频，不能用被测写入器生成读测试输入。"""
    path = tmp_path / f"01.{container}"
    if container == "wav":
        _write_wave(path)
    elif container == "dsf":
        _write_dsf(path)
    elif container == "aiff":
        _write_aiff(path)
    elif container == "dff":
        _write_dff(path)
    else:
        shutil.copyfile(Path(__file__).parent / f"fixtures/audio/silence.{container}", path)
    audio = MutagenFile(path)
    if audio.tags is None:
        audio.add_tags()
    if isinstance(audio.tags, ID3):
        for frame in (TIT2(encoding=3, text=[TITLE]), TPE1(encoding=3, text=CREDITS["composers"]),
                      TALB(encoding=3, text=["Violin Concerto"]), TCOM(encoding=3, text=CREDITS["composers"]),
                      TPE3(encoding=3, text=CREDITS["conductors"]), TXXX(encoding=3, desc="ORCHESTRA", text=CREDITS["orchestras"]),
                      TMCL(encoding=3, people=[["violin", "Anne-Sophie Mutter"]]), TIPL(encoding=3, people=[["producer", "Producer"]])):
            audio.tags.add(frame)
    elif isinstance(audio.tags, MP4Tags):
        audio.tags.update({"\xa9nam": [TITLE], "\xa9ART": CREDITS["composers"], "\xa9alb": ["Violin Concerto"],
                           "\xa9wrt": CREDITS["composers"], "----:com.apple.iTunes:CONDUCTOR": [b"Herbert von Karajan"],
                           "----:com.apple.iTunes:ORCHESTRA": [b"Berliner Philharmoniker"],
                           "----:com.apple.iTunes:PERFORMER": [b"Anne-Sophie Mutter (violin)"]})
    elif isinstance(audio.tags, ASFTags):
        audio.tags.update({"Title": [TITLE], "Author": CREDITS["composers"], "WM/AlbumTitle": ["Violin Concerto"],
                           "WM/Composer": CREDITS["composers"], "WM/Conductor": CREDITS["conductors"],
                           "WM/Orchestra": CREDITS["orchestras"], "WM/Performer": ["Anne-Sophie Mutter (violin)"]})
    else:
        audio.tags.update({"title": [TITLE], "artist": CREDITS["composers"], "album": ["Violin Concerto"],
                           "composer": CREDITS["composers"], "conductor": CREDITS["conductors"],
                           "orchestra": CREDITS["orchestras"], "performer": ["Anne-Sophie Mutter (violin)"]})
    audio.save()
    return path


@pytest.mark.parametrize("container", ["flac", "mp3", "wav", "dsf", "m4a", "aiff", "dff", "wma"])
def test_native_credits_survive_read_models_and_write_without_touching_seed(tmp_path, container):
    """原生角色到模型/API/pickle均不丢失，库内改写指挥不会修改硬链接做种源或主署名。"""
    source = _native_audio(tmp_path, container)
    original = hashlib.sha256(source.read_bytes()).digest()
    meta = AudioMetadataHelper.read_tags(source)
    assert music_credit_values(meta) == CREDITS
    assert all(meta.field_sources[key] == "tag" for key in MUSIC_CREDIT_FIELDS)
    assert meta.artists == CREDITS["composers"]
    info = MusicInfo.from_meta(MetaMusic.from_dict(meta.to_dict()))
    for model in (pickle.loads(pickle.dumps(meta)), MusicInfo.from_dict(info.to_dict()),
                  schemas.MusicMeta.model_validate(meta.to_dict()), schemas.MusicInfo.model_validate(info.to_dict())):
        assert music_credit_values(model) == CREDITS
    assert all(simplify_music_info(info)[key] == value for key, value in CREDITS.items())
    target = tmp_path / f"library.{container}"
    os.link(source, target)
    info.conductors = ["Another Conductor"]
    assert AudioMetadataHelper.write(target, info)
    actual = AudioMetadataHelper.read_tags(target)
    assert actual.conductors == ["Another Conductor"]
    assert actual.performers == CREDITS["performers"] and actual.orchestras == CREDITS["orchestras"]
    assert actual.artists == CREDITS["composers"]
    assert hashlib.sha256(source.read_bytes()).digest() == original


def test_legacy_id3_personnel_keep_instruments_but_not_producers(tmp_path):
    """ID3v2.3的IPLS经Mutagen转换后仍分离演奏和制作，并支持纠正旧演奏者。"""
    path = _native_audio(tmp_path, "mp3")
    tags = ID3(path)
    tags.update_to_v23()
    tags.save(path, v2_version=3)
    meta = AudioMetadataHelper.read_tags(path)
    assert meta.performers == CREDITS["performers"]
    meta.performers = {"violin": ["New Soloist"]}
    assert AudioMetadataHelper.write(path, meta)
    assert AudioMetadataHelper.read_tags(path).performers == meta.performers
    assert any(person == "Producer" for frame in ID3(path).getall("TIPL") for _role, person in frame.people)


def test_existing_credit_order_does_not_detach_identical_hardlink(tmp_path):
    """乐器署名的原生顺序不影响语义，完整标签写回不能触发额外大文件复制。"""
    source = _native_audio(tmp_path, "mp3")
    target = tmp_path / "library.mp3"
    os.link(source, target)
    info = AudioMetadataHelper.read_tags(target)
    assert AudioMetadataHelper.write(target, info)
    assert target.stat().st_ino == source.stat().st_ino


def test_classical_tags_survive_offline_transfer_batch(tmp_path):
    """完整古典标签走真实批次扫描和本地准入，保留主署名与角色且不伪造在线身份。"""
    source = _native_audio(tmp_path, "flac")
    owner = make_transfer_chain()
    item = make_fileitem(str(source))
    batch = prepare_music_batch_context(owner, [(item, False)], MediaType.MUSIC)
    meta, info = _local_music_context(owner, item, source, batch, AudioMetadataHelper.read(source))
    assert music_credit_values(meta) == music_credit_values(info) == CREDITS
    assert info.artists == CREDITS["composers"] and info.media_id is None


@pytest.mark.parametrize("container", ["mp3", "flac"])
def test_custom_performer_tags_are_replaced_without_stale_names(tmp_path, container):
    """支持读取的自定义演奏标签在纠正时也必须替换，不能把新旧独奏者合并。"""
    path = _native_audio(tmp_path, container)
    audio = MutagenFile(path)
    if container == "mp3":
        audio.tags.delall("TMCL")
        audio.tags.add(TXXX(encoding=3, desc="PERFORMER", text=["Old Soloist (violin)"]))
    else:
        del audio.tags["performer"]
        audio.tags["performer:violin"] = ["Old Soloist"]
    audio.save()
    meta = AudioMetadataHelper.read_tags(path)
    assert meta.performers == {"violin": ["Old Soloist"]}
    meta.performers = {"violin": ["New Soloist"]}
    assert AudioMetadataHelper.write(path, meta)
    assert AudioMetadataHelper.read_tags(path).performers == meta.performers


def test_mp4_role_correction_removes_case_variants_and_ensemble_alias(tmp_path):
    """MP4自由字段可有不同大小写及乐团别名，纠正后不能读回混杂的新旧阵容。"""
    path = _native_audio(tmp_path, "m4a")
    audio = MutagenFile(path)
    del audio.tags["----:com.apple.iTunes:ORCHESTRA"]
    del audio.tags["----:com.apple.iTunes:CONDUCTOR"]
    audio.tags["----:com.apple.iTunes:ensemble"] = [b"Old Orchestra"]
    audio.tags["----:com.apple.iTunes:conductor"] = [b"Old Conductor"]
    audio.save()
    meta = AudioMetadataHelper.read_tags(path)
    assert meta.orchestras == ["Old Orchestra"] and meta.conductors == ["Old Conductor"]
    meta.orchestras, meta.conductors = ["New Orchestra"], ["New Conductor"]
    assert AudioMetadataHelper.write(path, meta)
    actual = AudioMetadataHelper.read_tags(path)
    assert actual.orchestras == meta.orchestras and actual.conductors == meta.conductors


@pytest.mark.parametrize("model", [MetaMusic, MusicInfo, MusicAlbumInfo])
def test_old_pickles_default_to_empty_roles(model):
    """已有缓存缺少新增角色字段时仍可恢复，不能出现None映射或共享可变默认值。"""
    old = model()
    for key in MUSIC_CREDIT_FIELDS:
        old.__dict__.pop(key, None)
    restored = pickle.loads(pickle.dumps(old))
    assert music_credit_values(restored) == {"composers": [], "conductors": [], "orchestras": [], "performers": {}}
    assert restored.performers == {}


def test_explicit_torrent_roles_remain_independent_and_change_cache_identity():
    """PT副标题的明确角色可作证据；更换演奏阵容必须隔离正负缓存。"""
    meta = MetaMusic.parse_resource("Beethoven - Concerto FLAC", "作曲家：Beethoven；指挥：Karajan；乐团：Berlin；演奏者：Mutter")
    assert meta.artists == ["Beethoven"]
    assert meta.composers == ["Beethoven"] and meta.conductors == ["Karajan"] and meta.orchestras == ["Berlin"]
    assert meta.performers == {"performer": ["Mutter"]}
    assert music_query_artists(meta)[0] == "Mutter"
    other = MetaMusic.from_dict(meta.to_dict())
    other.performers = {"performer": ["Other"]}
    assert MusicBrainzCache._MusicBrainzCache__get_key(meta) != MusicBrainzCache._MusicBrainzCache__get_key(other)
    assert _album_directory_cache_key(Path("/music"), (), (), meta) != _album_directory_cache_key(Path("/music"), (), (), other)


def _recording(identity="recording-one", soloist="Anne-Sophie Mutter"):
    """按MusicBrainz实际的录音→作品→作曲家及录音→演奏者结构构造公共响应形状。"""
    return {"id": identity, "title": TITLE, "length": 3000,
            "artist-credit": [{"artist": {"id": "soloist-id", "name": soloist}}],
            "relations": [
                {"type": "performance", "work": {"id": "work-id", "relations": [
                    {"type": "composer", "artist": {"id": "composer-id", "name": "Ludwig van Beethoven"}}]}},
                {"type": "conductor", "artist": {"id": "conductor-id", "name": "Herbert von Karajan"}},
                {"type": "orchestra", "artist": {"id": "orchestra-id", "name": "Berliner Philharmoniker"}},
                {"type": "instrument", "attributes": ["violin"], "artist": {"id": "soloist-id", "name": soloist}},
                {"type": "producer", "artist": {"id": "producer-id", "name": "Producer"}},
            ]}


def test_same_composer_different_performance_is_rejected():
    """同作品、曲序和时长仍无法抵消独奏者差异；明确角色不进入主艺人别名。"""
    meta = MetaMusic(title=TITLE, artists=CREDITS["composers"], track_number=1, duration=3, **CREDITS)
    correct = MusicBrainzModule._recording_to_info(_recording())
    wrong = MusicBrainzModule._recording_to_info(_recording("other", "Other Soloist"))
    assert correct.composers == CREDITS["composers"] and correct.artist_aliases == ["Anne-Sophie Mutter"]
    assert correct.artists == ["Anne-Sophie Mutter"]
    assert music_artist_evidence_matches(correct, meta)
    assert not music_artist_evidence_matches(wrong, meta)
    assert align_music_tracks([meta], [wrong, correct]) == {0: 1}
    assert match_music_resource(wrong, TITLE, meta=meta).reason == "performance_mismatch"
    assert MusicBrainzModule._select_candidate(meta, [wrong, correct], MediaSource.MusicBrainz) is correct


def test_composer_credit_alone_cannot_confirm_a_recording():
    """只有作曲家相同仍缺乏演奏版本证据；明确录音身份可补足这个缺口。"""
    meta = MetaMusic(title=TITLE, artists=CREDITS["composers"], composers=CREDITS["composers"])
    candidate = MusicBrainzModule._recording_to_info(_recording())
    candidate.artists = list(meta.artists)
    assert not music_artist_evidence_matches(candidate, meta)
    meta.media_source, meta.media_id = candidate.media_source, candidate.media_id
    assert music_artist_evidence_matches(candidate, meta)


def test_singer_songwriter_remains_a_valid_primary_artist():
    """普通歌曲的歌手同时作曲不应因新增角色字段被拒绝，但作曲关系不等于翻唱艺人。"""
    meta = MetaMusic(title="Song", artists=["Singer"], composers=["Singer"])
    original = MusicInfo(title="Song", artists=["Singer"], composers=["Singer"])
    cover = MusicInfo(title="Song", artists=["Cover Artist"], composers=["Singer"])
    assert music_artist_evidence_matches(original, meta)
    assert not music_artist_evidence_matches(cover, meta)
    original.performers = {"vocal": ["Singer"]}
    assert music_artist_evidence_matches(original, meta)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_search_fetches_bounded_roles_and_rejects_wrong_recording_detail(monkeypatch, asynchronous):
    """同步异步实际识别都核验角色详情，错误详情ID不应让同作品候选冒充正确演奏。"""
    module = object.__new__(MusicBrainzModule)
    monkeypatch.setattr(module, "_cached_recognition", lambda _plan: None)
    monkeypatch.setattr(module, "_update_recognize_cache", lambda *_args, **_kwargs: None)
    wrong, correct = _recording("wrong", "Other Soloist"), _recording()
    cards = [{key: value for key, value in item.items() if key != "relations"} for item in [wrong, correct]]

    def request(path, params):
        """只替换HTTP边界；检索、投影、角色补证和候选选择仍走生产实现。"""
        if path == "/recording":
            assert "Anne-Sophie Mutter" in params["query"]
            return {"recordings": cards}
        assert "work-level-rels" in params["inc"]
        return wrong if path == "/recording/wrong" else correct

    sync, async_request = Mock(side_effect=request), AsyncMock(side_effect=request)
    monkeypatch.setattr(module, "_request_json", sync)
    monkeypatch.setattr(module, "_async_request_json", async_request)
    kwargs = dict(meta=MetaMusic(title=TITLE, artists=CREDITS["composers"], **CREDITS),
                  media_source=MediaSource.MusicBrainz, music_type="recording", cache=False)
    result = asyncio.run(module.async_recognize_media(**kwargs)) if asynchronous else module.recognize_media(**kwargs)
    assert result.media_id == "recording-one" and music_credit_values(result) == CREDITS
    assert (async_request.call_count if asynchronous else sync.call_count) == 3
    untouched = MusicInfo(media_source=MediaSource.MusicBrainz, media_id="unrelated", title=TITLE)
    module._apply_recording_credits("unrelated", [untouched], correct)
    assert untouched.composers == []


def test_recording_detail_budget_is_deduplicated_and_ordinary_music_needs_none():
    """最多补证三个录音身份，已含完整角色的摘要及普通音乐不增加详情请求。"""
    candidates = [MusicInfo(media_source=MediaSource.MusicBrainz, media_id=str(index // 2 + 1), title=TITLE) for index in range(10)]
    meta = MetaMusic(title=TITLE, **CREDITS)
    targets = MusicBrainzModule._recording_credit_targets(meta, candidates)
    assert list(targets) == ["1", "2", "3"] and all(len(items) == 2 for items in targets.values())
    assert MusicBrainzModule._recording_credit_targets(MetaMusic(title=TITLE), candidates) == {}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_role_detail_budget_is_shared_across_search_relaxations(monkeypatch, asynchronous):
    """放宽搜索词不能恢复三个详情额度，重复候选或失败详情也不能重复消耗请求。"""
    module = object.__new__(MusicBrainzModule)
    monkeypatch.setattr(module, "_recording_queries", lambda _meta: ["strict", "loose"])

    def request(path, params):
        """首轮返回三个不完整录音，次轮重复一个并引入新身份，所有角色仍无法确认。"""
        if path != "/recording":
            return None
        ids = ["1", "2", "3"] if params["query"] == "strict" else ["1", "4", "5"]
        return {"recordings": [{"id": identity, "title": TITLE} for identity in ids]}

    operation = AsyncMock(side_effect=request) if asynchronous else Mock(side_effect=request)
    monkeypatch.setattr(module, "_async_request_json" if asynchronous else "_request_json", operation)
    meta = MetaMusic(title=TITLE, **CREDITS)
    result = (asyncio.run(module._async_search_recordings(meta, 10, require_match=True)) if asynchronous
              else module._search_recordings(meta, 10, require_match=True))
    assert result == []
    assert [call.args[0] for call in operation.call_args_list] == [
        "/recording", "/recording/1", "/recording/2", "/recording/3", "/recording"]


def test_album_track_projection_and_local_merge_preserve_each_performance(tmp_path):
    """整专逐曲角色不从专辑角色扩散，API卡片与路径合并保留本地明确角色。"""
    detail = {"id": "release", "title": "Violin Concerto", "artist-credit": [{"artist": {"name": "Ludwig van Beethoven"}}],
              "media": [{"position": 1, "tracks": [{"id": "track", "position": 1, "recording": _recording()}]}]}
    album = MusicBrainzModule._release_to_album(detail)
    assert album.composers == [] and music_credit_values(album.tracks[0]) == CREDITS
    card = MusicAlbumInfo(title="Album", **CREDITS).to_music_info()
    assert music_credit_values(schemas.MusicAlbumInfo.model_validate(MusicAlbumInfo(**CREDITS).to_dict())) == CREDITS
    assert music_credit_values(card) == CREDITS
    local = MetaMusic(title=TITLE, artists=CREDITS["composers"], duration=3, **CREDITS,
                      field_sources={key: "tag" for key in MUSIC_CREDIT_FIELDS})
    mapping = MediaAlbumOwner._album_track_map([tmp_path / "01.flac"], [local], album)
    assert music_credit_values(next(iter(mapping.values()))) == CREDITS
    bare = MusicInfo(media_source=MediaSource.TheAudioDB, media_id="101", title=TITLE, artists=["Anne-Sophie Mutter"])
    assert music_credit_values(_finalize_music_path_info(local, bare)) == CREDITS
    assert deepcopy(local).artists == CREDITS["composers"]


def test_album_roles_are_verified_by_actual_tracks_without_broadcasting(tmp_path):
    """专辑层不含角色时可用真实曲目关系核验PT阵容，换演奏版本必须拒绝。"""
    local = MetaMusic(title=TITLE, album="Violin Concerto", artists=CREDITS["composers"], duration=3, **CREDITS,
                      field_sources={"album": "tag"})
    context = MetaMusic(album=local.album, artists=local.artists, **CREDITS)
    meta = _album_context_with_resource(tmp_path, [local], context)
    assert music_credit_values(meta) == CREDITS
    correct = MusicBrainzModule._recording_to_info(_recording())
    album = MusicAlbumInfo(title=local.album, artists=local.artists, tracks=[correct])
    assert music_album_candidate_matches(album, meta, [local])
    assert MusicBrainzModule._score_release(meta, [local], {}, [{}], album=album) > 0
    album.tracks[0] = MusicBrainzModule._recording_to_info(_recording("other", "Other Soloist"))
    assert not music_album_candidate_matches(album, meta, [local])
    assert MusicBrainzModule._score_release(meta, [local], {}, [{}], album=album) == 0
    # 不把单曲角色推成整张合辑的公共角色。
    assert not any(music_credit_values(_album_context_with_resource(tmp_path, [local], None)).values())
    corrected = MediaAlbumOwner._album_track_map([tmp_path / "01.flac"], [local], album, allow_title_override=True)
    assert next(iter(corrected.values())).performers == {"violin": ["Other Soloist"]}


def test_artistless_roles_and_plugin_candidates_still_require_performance_evidence():
    """没有ARTIST不能绕过角色校验；插件和各来源同样拒绝未知或冲突阵容。"""
    meta = MetaMusic(title=TITLE, performers=CREDITS["performers"])
    wrong = MusicInfo(media_source=MediaSource.TheAudioDB, media_id="123", title=TITLE, artists=["Other Soloist"])
    correct = MusicBrainzModule._recording_to_info(_recording())
    assert not _music_info_matches_text_evidence(wrong, meta)
    assert _music_info_matches_text_evidence(correct, meta)
    assert TheAudioDbModule._select_track(meta, [wrong]) is None
    assert TheAudioDbModule._select_album(meta, [MusicAlbumInfo(title=TITLE, artists=wrong.artists)]) is None
    composer_only = MetaMusic(title=TITLE, artists=CREDITS["composers"], composers=CREDITS["composers"],
                              media_source=wrong.media_source, media_id=wrong.media_id)
    assert _music_info_matches_text_evidence(wrong, composer_only)
    wrong.composers = ["Different Composer"]
    assert not _music_info_matches_text_evidence(wrong, composer_only)
