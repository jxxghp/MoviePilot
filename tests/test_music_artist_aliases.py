"""按来源真实Artist身份补证别名，不把拼音或同名结果猜成另一艺人。"""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.music.observation import capture_music_recognition, music_request_timeout, report_music_recognition
from app.application.music.recognition import async_enrich_music_artist_aliases, enrich_music_artist_aliases
from app.domain.context import MusicAlbumInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.music import music_album_lookup_plan, music_album_with_artist_aliases, music_artist_matches
from app.modules.musicbrainz import MusicBrainzModule
from app.modules.theaudiodb import TheAudioDbModule
from app.schemas.types import MediaSource

ARTIST_ID = "a223958d-5c56-4b2c-a30a-87e357bc121b"


def _module(source, monkeypatch, *, wrong_id=False, alias="Zhōu Jié Lún"):
    """构造真实来源投影与确认入口，仅替换HTTP边界，避免单例和真实服务污染。"""
    module = object.__new__(MusicBrainzModule if source == MediaSource.MusicBrainz else TheAudioDbModule)
    if source == MediaSource.MusicBrainz:
        monkeypatch.setattr(module, "_cached_recognition", lambda _plan: None)
        monkeypatch.setattr(module, "_update_recognize_cache", lambda *_args, **_kwargs: None)
        entity = {"id": "recording-one", "title": "晴天", "artist-credit": [{"artist": {"id": ARTIST_ID, "name": "周杰倫"}}]}
        payload = {"recordings": [entity]}
        artist_payload = {"id": "other" if wrong_id else ARTIST_ID, "name": "周杰倫", "aliases": [{"name": alias}]}
    else:
        entity = {"idTrack": "101", "strTrack": "晴天", "strArtist": "周杰倫", "idArtist": "201"}
        payload = {"track": [entity]}
        artist_payload = {"artists": [{"idArtist": "other" if wrong_id else "201", "strArtist": "周杰倫", "strArtistAlternate": alias}]}

    def request(path, _params=None, **_kwargs):
        """同一搜索实体及其Artist详情可重复读取，响应保持原样以检验缓存隔离。"""
        return artist_payload if path.startswith("/artist/") or path == "artist.php" else payload

    sync, asynchronous = Mock(side_effect=request), AsyncMock(side_effect=request)
    monkeypatch.setattr(module, "_request_json", sync)
    monkeypatch.setattr(module, "_async_request_json", asynchronous)
    return module, sync, asynchronous, payload


@pytest.mark.parametrize("source", [MediaSource.MusicBrainz, MediaSource.TheAudioDB])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("wrong_id", [False, True])
def test_recording_verifies_real_artist_alias_before_accepting_pinyin(monkeypatch, source, asynchronous, wrong_id):
    """实际来源入口可用有声调/空格别名确认拼音；错误Artist响应不得建立身份。"""
    module, sync, async_request, payload = _module(source, monkeypatch, wrong_id=wrong_id)
    original = deepcopy(payload)
    kwargs = {"meta": MetaMusic(title="晴天", artists=["ZhouJieLun"]), "media_source": source, "music_type": "recording"}
    result = asyncio.run(module.async_recognize_media(**kwargs)) if asynchronous else module.recognize_media(**kwargs)
    assert bool(result and result.media_id) is not wrong_id
    if not wrong_id:
        assert music_artist_matches(result, ["ZhouJieLun"])
        assert result.artists == ["周杰倫"]
        assert result.artist_ids == ([ARTIST_ID] if source == MediaSource.MusicBrainz else ["201"])
        assert result.media_source == source
    assert payload == original
    assert (sync.call_count == 0) is asynchronous
    assert (async_request.await_count == 0) is not asynchronous


@pytest.mark.parametrize("source", [MediaSource.MusicBrainz, MediaSource.TheAudioDB])
def test_candidate_without_source_alias_does_not_guess_transliteration(monkeypatch, source):
    """中文名字相似或拼音相近不构成来源别名证据。"""
    module, _sync, _async, _payload = _module(source, monkeypatch, alias="Another Person")
    result = module.recognize_media(meta=MetaMusic(title="晴天", artists=["ZhouJieLun"]), media_source=source, music_type="recording")
    assert not result or result.media_id is None


@pytest.mark.parametrize("asynchronous", [False, True])
def test_alias_lookup_deduplicates_ids_and_keeps_candidate_roles(asynchronous):
    """同一Artist只查一次、批次最多三人；版本冲突和跨来源候选不参与补证。"""
    meta = MetaMusic(title="Song", artists=["Romanized"], version="Live")
    candidates = [MusicInfo(media_source=MediaSource.TheAudioDB, media_id=str(index + 1), title="Song (Live)",
                            artists=["真实署名"], artist_ids=[str(index // 2 + 1)]) for index in range(8)]
    wrong_version = MusicInfo(media_source=MediaSource.TheAudioDB, media_id="wrong", title="Song", artists=["Other"], artist_ids=["99"])
    wrong_source = MusicInfo(media_source=MediaSource.DoubanMusic, media_id="wrong", title="Song (Live)", artists=["Other"], artist_ids=["98"])
    pairs = [(meta, item) for item in [*candidates, wrong_version, wrong_source]]
    lookup = (AsyncMock if asynchronous else Mock)(return_value=["Romanized"])
    if asynchronous:
        asyncio.run(async_enrich_music_artist_aliases(pairs, MediaSource.TheAudioDB, lookup))
    else:
        enrich_music_artist_aliases(pairs, MediaSource.TheAudioDB, lookup)
    assert [call.args[0] for call in lookup.call_args_list] == ["1", "2", "3"]
    assert all(item.artist_aliases == ["Romanized"] and item.artists == ["真实署名"] for item in candidates[:6])
    assert all(not item.artist_aliases for item in [*candidates[6:], wrong_version, wrong_source])


def test_known_artist_does_not_spend_alias_requests():
    """原署名已能通过繁简归一时无需额外查询，保留预算供整专曲目核验。"""
    item = MusicInfo(media_source=MediaSource.TheAudioDB, media_id="track", title="Song", artists=["周杰倫"], artist_ids=["201"])
    lookup = Mock()
    enrich_music_artist_aliases([(MetaMusic(title="Song", artists=["周杰伦"]), item)], MediaSource.TheAudioDB, lookup)
    lookup.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_release_album_and_each_track_share_verified_artist_alias(monkeypatch, asynchronous):
    """具体发行及逐曲模型都消费同一Artist证据，原始详情响应不被修改。"""
    module = object.__new__(MusicBrainzModule)
    credit = [{"artist": {"id": ARTIST_ID, "name": "周杰倫"}}]
    detail = {"id": "release-one", "title": "Album", "artist-credit": credit,
              "release-group": {"id": "group-one", "primary-type": "Album"},
              "media": [{"position": 1, "tracks": [
                  {"position": index, "title": name, "length": 180000, "recording": {
                      "id": f"recording-{index}", "title": name, "artist-credit": credit}}
                  for index, name in enumerate(["First", "Second"], 1)]}]}
    original = deepcopy(detail)

    def request(path, **_kwargs):
        """发行详情和精确Artist查询都走真实投影层。"""
        if path == f"/artist/{ARTIST_ID}":
            return {"id": ARTIST_ID, "name": "周杰倫", "aliases": [{"name": "Zhou Jie Lun"}]}
        return detail

    sync, async_request = Mock(side_effect=request), AsyncMock(side_effect=request)
    monkeypatch.setattr(module, "_request_json", sync)
    monkeypatch.setattr(module, "_async_request_json", async_request)
    meta = MetaMusic(title="Album", album="Album", album_artist="ZhouJieLun", musicbrainz_release_id="release-one")
    tracks = [MetaMusic(title=name, artists=["ZhouJieLun"], duration=180, track_number=index)
              for index, name in enumerate(["First", "Second"], 1)]
    result = asyncio.run(module.async_match_music_album(meta, tracks)) if asynchronous else module.match_music_album(meta, tracks)
    assert result and result.musicbrainz_release_id == "release-one"
    assert all(music_artist_matches(track, ["ZhouJieLun"]) for track in result.tracks)
    assert music_artist_matches(result.to_music_info(), ["ZhouJieLun"])
    calls = async_request.call_args_list if asynchronous else sync.call_args_list
    assert [call.args[0] for call in calls] == ["/release/release-one", f"/artist/{ARTIST_ID}"]
    assert detail == original


def test_album_lookup_keeps_search_aliases_bound_to_same_artist():
    """整专重新读取详情后仍保留搜索补证，其他曲目表演者不继承专辑艺人别名。"""
    card = MusicInfo(media_source=MediaSource.TheAudioDB, media_id="album", music_type="album", title="Album",
                     artists=["周杰倫"], artist_ids=["201"], artist_aliases=["ZhouJieLun"])
    album = MusicAlbumInfo(media_source=MediaSource.TheAudioDB, media_id="album", title="Album", artists=["周杰倫"], artist_ids=["201"],
                           tracks=[MusicInfo(media_source=MediaSource.TheAudioDB, media_id="101", title="First", artists=["周杰倫"], artist_ids=["201"]),
                                   MusicInfo(media_source=MediaSource.TheAudioDB, media_id="102", title="Second", artists=["Other"], artist_ids=["202"]),
                                   MusicInfo(media_source=MediaSource.DoubanMusic, media_id="103", title="Third", artists=["Other"], artist_ids=["201"])])
    plan = music_album_lookup_plan(MediaSource.TheAudioDB, MetaMusic(title="Album", album="Album", artists=["ZhouJieLun"]), [])
    next(plan)
    assert plan.send([card]).album_id == "album"
    with pytest.raises(StopIteration) as finished:
        plan.send(album)
    result = finished.value.value[0]
    assert result.artist_aliases == result.tracks[0].artist_aliases == ["ZhouJieLun"]
    assert result.tracks[1].artist_aliases == []
    assert result.tracks[2].artist_aliases == []
    assert album.artist_aliases == album.tracks[0].artist_aliases == []
    assert music_album_with_artist_aliases(album, {}).artist_ids == ["201"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_musicbrainz_ambiguous_recordings_keep_diagnostics(monkeypatch, asynchronous):
    """同证据的不同Recording不因列表顺序或后续查询变成成功身份。"""
    module, _sync, _async, payload = _module(MediaSource.MusicBrainz, monkeypatch)
    payload["recordings"].append({**payload["recordings"][0], "id": "recording-two"})
    kwargs = {"meta": MetaMusic(title="晴天", artists=["ZhouJieLun"]), "media_source": MediaSource.MusicBrainz, "music_type": "recording"}
    result = asyncio.run(module.async_recognize_media(**kwargs)) if asynchronous else module.recognize_media(**kwargs)
    assert result and result.media_id is None
    assert result.raw_data["recognition"]["status"] == "ambiguous"
    assert {row["media_id"] for row in result.raw_data["recognition"]["candidates"]} == {"recording-one", "recording-two"}


def test_alias_failure_is_not_cached_as_missing_identity(monkeypatch):
    """核验别名时服务失败，候选保持未确认并携带可重试原因。"""
    module, _sync, _async, _payload = _module(MediaSource.MusicBrainz, monkeypatch)
    cache_write = Mock()
    monkeypatch.setattr(module, "_update_recognize_cache", cache_write)

    def fail_alias(_ids, _aliases):
        """模拟真实请求额度耗尽并保持来源故障诊断。"""
        assert music_request_timeout() is None
        report_music_recognition("budget_exhausted", "别名核验预算耗尽")
        return []

    monkeypatch.setattr(module, "_lookup_artist_aliases", fail_alias)
    with capture_music_recognition(request_limit=0):
        result = module.recognize_media(meta=MetaMusic(title="晴天", artists=["ZhouJieLun"]), media_source=MediaSource.MusicBrainz,
                                         music_type="recording")
    assert result.media_id is None and result.raw_data["recognition"]["status"] == "budget_exhausted"
    cache_write.assert_not_called()


@pytest.mark.parametrize("music_type", ["recording", "album"])
def test_musicbrainz_alias_collision_is_not_broken_by_response_order(music_type):
    """不同Artist身份共享别名时，只凭同曲名同别名不足以确认某一实体。"""
    meta = MetaMusic(title="Song", artists=["Romanized"], music_type=music_type)
    candidates = [MusicInfo(media_source=MediaSource.MusicBrainz, media_id=f"entity-{index}", music_type=music_type,
                            title="Song", artists=[f"Artist {index}"], artist_ids=[f"artist-{index}"], artist_aliases=["Romanized"])
                  for index in (1, 2)]
    for ordered in [candidates, list(reversed(candidates))]:
        with capture_music_recognition() as observation:
            result = MusicBrainzModule._select_album_candidate(meta, ordered) if music_type == "album" else (
                MusicBrainzModule._select_candidate(meta, ordered, MediaSource.MusicBrainz))
        assert result is None and observation.status == "ambiguous"


def test_musicbrainz_duplicate_isrc_cannot_select_first_identity():
    """同一ISRC出现在不同Recording ID时保留歧义，重复行仍可去重。"""
    meta = MetaMusic(title="Local Name", isrc="USAAA0100001")
    candidates = [MusicInfo(media_source=MediaSource.MusicBrainz, media_id=identity, title="Remote Name", isrc=meta.isrc)
                  for identity in ("first", "second")]
    with capture_music_recognition() as observation:
        assert MusicBrainzModule._select_candidate(meta, candidates, MediaSource.MusicBrainz) is None
    assert observation.status == "ambiguous"
    assert MusicBrainzModule._select_candidate(meta, [candidates[0], candidates[0]], MediaSource.MusicBrainz) is candidates[0]


def test_real_album_directory_retains_verified_source_aliases(tmp_path, monkeypatch):
    """真实无标签文件经来源搜索、专辑详情、曲目对位后仍能用实际Artist别名整理。"""
    import shutil
    from pathlib import Path
    from types import SimpleNamespace

    from app.chain.media import MediaChain
    from app.chain.media.cache import AlbumDirectoryCache

    directory = tmp_path / "ZhouJieLun - Album"
    directory.mkdir()
    for index, title in enumerate(["First", "Second"], 1):
        shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", directory / f"{index:02} - {title}.flac")
    module = object.__new__(TheAudioDbModule)
    album = {"idAlbum": "301", "strAlbum": "Album", "strArtist": "周杰倫", "idArtist": "201"}
    responses = {"searchalbum.php": {"album": [album]}, "album.php": {"album": [album]},
                 "artist.php": {"artists": [{"idArtist": "201", "strArtist": "周杰倫", "strArtistAlternate": "Zhou Jie Lun"}]},
                 "track.php": {"track": [{"idTrack": str(index + 400), "strTrack": title, "strArtist": "周杰倫",
                                           "idArtist": "201", "idAlbum": "301", "strAlbum": "Album", "intDuration": "3000"}
                                          for index, title in enumerate(["First", "Second"], 1)]}}
    network = Mock(side_effect=lambda endpoint, _params: responses[endpoint])
    monkeypatch.setattr(module, "_request_json", network)
    chain = object.__new__(MediaChain)
    chain._album_dir_cache = AlbumDirectoryCache(8)
    monkeypatch.setattr("app.chain.media.album.get_chain_runtime_config_snapshot", lambda: SimpleNamespace(
        search_source="theaudiodb", audio_extensions=(".flac",), music_release_region_priority=(), music_release_script_priority=()))
    monkeypatch.setattr(chain, "_finalize_recognition_result", lambda info, **_kwargs: info)

    def search(meta, **kwargs):
        """保持真实来源搜索和别名查询，只替换模块调度边界。"""
        return module.search_music(meta, media_source=MediaSource.TheAudioDB, **kwargs)

    backend = SimpleNamespace(search_music=search, get_music_album=lambda identity: module.music_album(MediaSource.TheAudioDB, identity))
    monkeypatch.setattr(chain, "_music_source_chain", lambda _source: backend)
    result = chain.recognize_music_album_directory(directory)
    assert len(result) == 2 and result.recognition["status"] == "matched"
    assert all(info.media_source == MediaSource.TheAudioDB and music_artist_matches(info, ["ZhouJieLun"]) for info in result.values())
    assert [call.args[0] for call in network.call_args_list].count("artist.php") == 1
    assert all(info.media_id in {"401", "402"} and info.artist_ids == ["201"] for info in result.values())
