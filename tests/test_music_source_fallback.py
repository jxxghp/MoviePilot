"""自动音乐来源顺序、隔离诊断和不可重置的请求预算。"""

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.music.observation import (
    capture_music_recognition,
    capture_music_source,
    music_request_timeout,
    report_music_recognition,
)
from app.application.music.recognition import async_recognize_music_sources, recognize_music_sources
from app.chain.media import MediaChain
from app.chain.media.cache import AlbumDirectoryCache
from app.chain.media.path import _music_path_plan
from app.domain.context import MusicAlbumInfo, MusicInfo
from app.domain.media import music_recognition_sources
from app.domain.meta.metamusic import MetaMusic
from app.modules.douban.apiv2 import DoubanApi
from app.modules.theaudiodb import TheAudioDbModule
from app.schemas.types import MediaSource, MediaType


def _accepts(info, source):
    """测试入口采用实际的来源与主身份判据，不凭对象非空判定远端命中。"""
    return bool(info and info.media_source == source and info.media_id)


@pytest.mark.parametrize("configured,expected", [
    ("themoviedb", (MediaSource.MusicBrainz,)),
    ("theaudiodb,musicbrainz,theaudiodb,doubanmusic", (MediaSource.TheAudioDB, MediaSource.MusicBrainz, MediaSource.DoubanMusic)),
    ("", (MediaSource.MusicBrainz,)),
    ("invalid,musicbrainz,anilist", (MediaSource.MusicBrainz,)),
])
def test_configured_music_sources_keep_order_and_ignore_video_sources(configured, expected):
    """混合来源设置不会把影视来源派发为音乐服务，默认设置仍保持MusicBrainz。"""
    assert music_recognition_sources(configured) == expected


@pytest.mark.parametrize("asynchronous", [False, True])
def test_source_failure_does_not_poison_following_source(asynchronous):
    """首源故障不阻断后源的实际HTTP额度，两次请求仍计入同一操作总量。"""
    calls = []

    def recognize(source):
        """第一来源记录服务故障，第二来源以自己的身份成功命中。"""
        calls.append(source)
        assert music_request_timeout() is not None
        if source == MediaSource.MusicBrainz:
            report_music_recognition("service_error", "服务暂不可用")
            return None
        return MusicInfo(media_source=source, media_id="audio-db-track", title="Song")

    sources = (MediaSource.MusicBrainz, MediaSource.TheAudioDB, MediaSource.DoubanMusic)
    result, diagnostic = asyncio.run(async_recognize_music_sources(sources, AsyncMock(side_effect=recognize), _accepts)) if asynchronous else (
        recognize_music_sources(sources, recognize, _accepts))

    assert calls == list(sources[:2])
    assert result.media_source == MediaSource.TheAudioDB
    assert diagnostic["status"] == "matched" and diagnostic["requests"] == 2
    assert [row["status"] for row in diagnostic["sources"]] == ["service_error", "matched"]
    assert [row["requests"] for row in diagnostic["sources"]] == [1, 1]


@pytest.mark.parametrize("state", ["ambiguous", "conflict"])
def test_ambiguous_or_conflicting_source_stops_weaker_fallback(state):
    """已知冲突不能因为另一个来源愿意返回首个同名结果而被掩盖。"""
    pending = MusicInfo(title="Song", raw_data={"recognition": {"status": state, "message": "需要确认"}})
    recognize = Mock(side_effect=[pending, MusicInfo(media_source="theaudiodb", media_id="wrong", title="Song")])

    result, diagnostic = recognize_music_sources((MediaSource.MusicBrainz, MediaSource.TheAudioDB), recognize, _accepts)

    assert result is None and diagnostic["status"] == state
    assert recognize.call_count == 1


def test_outer_budget_cannot_be_reset_by_changing_source():
    """来源范围独立失败，但不能通过新建子范围恢复已用完的外层请求额度。"""
    with capture_music_recognition(seconds=5, request_limit=1) as total:
        with capture_music_source("first") as first:
            assert music_request_timeout() is not None
            report_music_recognition("service_error", "失败")
        with capture_music_source("second") as second:
            assert music_request_timeout() is None
    assert total.requests == first.requests == 1
    assert second.requests == 0 and second.status == "budget_exhausted"


@pytest.mark.asyncio
async def test_concurrent_source_scopes_claim_shared_budget_atomically():
    """并行继承同一观察范围时也只能消耗总额度，不能分别领取一份完整预算。"""
    async def claim(source):
        """让两个来源交错执行后领取共享HTTP名额。"""
        with capture_music_source(source):
            await asyncio.sleep(0)
            return music_request_timeout()

    with capture_music_recognition(seconds=5, request_limit=1) as total:
        allowed = await asyncio.gather(claim("one"), claim("two"))
    assert sum(value is not None for value in allowed) == total.requests == 1


def test_all_empty_sources_keep_service_error_and_fresh_operation_recovers():
    """没有成功回退时保留故障语义，下一次独立操作不继承上次状态。"""
    def failed(source):
        """只有第一个来源出现故障，后面的来源正常无匹配。"""
        if source == MediaSource.MusicBrainz:
            raise OSError("offline")
        return None

    sources = (MediaSource.MusicBrainz, MediaSource.TheAudioDB)
    result, diagnostic = recognize_music_sources(sources, failed, _accepts)
    assert result is None and diagnostic["status"] == "service_error"
    expected = MusicInfo(media_source="musicbrainz", media_id="recording", title="Song")
    result, diagnostic = recognize_music_sources(sources, lambda _source: expected, _accepts)
    assert result is expected and diagnostic["status"] == "matched"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_recognition_uses_configured_fallback_without_mutating_provider_result(monkeypatch, asynchronous):
    """自动识别使用配置顺序；成功保留来源ID，附加诊断不会污染来源缓存对象。"""
    chain = object.__new__(MediaChain)
    monkeypatch.setattr("app.chain.media.recognition.get_chain_runtime_config_snapshot",
                        lambda: SimpleNamespace(search_source="musicbrainz,theaudiodb,doubanmusic"))
    expected = MusicInfo(media_source="theaudiodb", media_id="track-42", title="Song")
    callback = AsyncMock(side_effect=[None, expected]) if asynchronous else Mock(side_effect=[None, expected])
    monkeypatch.setattr(chain, "async_recognize_music_from_source" if asynchronous else "recognize_music_from_source", callback)
    kwargs = {"meta": MetaMusic(title="Song", artists=["Artist"]), "mtype": MediaType.MUSIC}

    result = asyncio.run(chain._async_run_native_media_recognize(kwargs, True)) if asynchronous else chain._run_native_media_recognize(kwargs, True)

    assert result.media_source == MediaSource.TheAudioDB and result.media_id == "track-42"
    assert [call.kwargs["media_source"] for call in callback.call_args_list] == [MediaSource.MusicBrainz, MediaSource.TheAudioDB]
    assert all(call.kwargs.get("media_id") is None for call in callback.call_args_list)
    assert result.raw_data["recognition"]["status"] == "matched"
    assert expected.raw_data == {} and result is not expected


@pytest.mark.parametrize("explicit", [False, True])
def test_bound_or_explicit_identity_does_not_cross_sources(monkeypatch, explicit):
    """已绑定身份经过真实请求规划后仍固定来源，不用其它来源解释同一个ID。"""
    chain = object.__new__(MediaChain)
    monkeypatch.setattr("app.chain.media.recognition.get_chain_runtime_config_snapshot",
                        lambda: SimpleNamespace(search_source="theaudiodb,musicbrainz,doubanmusic"))
    meta = MetaMusic(title="Song", media_source=None if explicit else "musicbrainz", media_id=None if explicit else "recording")
    plan = chain._build_recognition_plan(meta=meta, mtype=MediaType.MUSIC,
                                        media_source=MediaSource.MusicBrainz if explicit else None,
                                        media_id="recording" if explicit else None, episode_group=None,
                                        cache=True, share_meta=None, music_type="recording")
    expected = MusicInfo(media_source="musicbrainz", media_id="recording", title="Song")
    callback = Mock(return_value=expected)
    monkeypatch.setattr(chain, "recognize_music_from_source", callback)

    assert chain._run_native_media_recognize(plan.module_kwargs(), True) is expected
    callback.assert_called_once()
    assert callback.call_args.kwargs["media_source"] == MediaSource.MusicBrainz
    assert callback.call_args.kwargs["media_id"] == "recording"


def test_unbound_track_with_explicit_release_evidence_stays_in_musicbrainz(monkeypatch):
    """只有发行MBID时也不能将其映射到其它来源的同名专辑。"""
    chain = object.__new__(MediaChain)
    monkeypatch.setattr("app.chain.media.recognition.get_chain_runtime_config_snapshot",
                        lambda: SimpleNamespace(search_source="theaudiodb,musicbrainz"))
    callback = Mock(return_value=None)
    monkeypatch.setattr(chain, "recognize_music_from_source", callback)
    chain._run_native_media_recognize({"meta": MetaMusic(title="Song", musicbrainz_release_id="known-release")}, True)
    callback.assert_called_once()
    assert callback.call_args.kwargs["media_source"] == MediaSource.MusicBrainz


@pytest.mark.parametrize("state", ["ambiguous", "conflict", "service_error", "budget_exhausted"])
def test_path_tiers_preserve_unresolved_diagnostics(monkeypatch, state):
    """无远端ID的诊断不能在版本校验中丢失，再用文件名启动一轮弱查询。"""
    chain = object.__new__(MediaChain)
    meta = MetaMusic(title="Song")
    pending = MusicInfo(title="Song", raw_data={"recognition": {"status": state}})
    monkeypatch.setattr(chain, "recognize_media", Mock(return_value=pending))
    assert chain._recognize_music_meta_tier(meta, None, "标签") is pending
    plan = _music_path_plan(meta, MetaMusic(title="filename"), None)
    next(plan)
    plan.send(None)
    with pytest.raises(StopIteration) as finished:
        plan.send(pending)
    assert finished.value.value is pending


def test_ambiguity_cannot_be_overwritten_by_plugin_or_shared_identity():
    """原生已判定歧义时，真实插件规划和共享状态机都必须保留待确认结果。"""
    chain = object.__new__(MediaChain)
    chain.eventmanager = Mock()
    meta = MetaMusic(title="Song")
    pending = MusicInfo(title="Song", raw_data={"recognition": {"status": "ambiguous"}})
    assert chain._supplement_media_recognize(meta, MediaType.MUSIC, None, None, pending) is pending
    chain.eventmanager.check.assert_not_called()
    plan = chain._build_recognition_plan(meta=meta, mtype=MediaType.MUSIC, media_source=None, media_id=None,
                                        episode_group=None, cache=True, share_meta=None, music_type="recording")
    steps = chain._recognition_steps(plan, True)
    next(steps)
    steps.send(pending)
    with pytest.raises(StopIteration) as finished:
        steps.send(pending)
    assert finished.value.value is pending


class _Response:
    """模拟HTTP响应及明确的资源释放，不允许测试访问真实服务。"""

    def __init__(self, payload, status=200):
        """保存响应内容及关闭状态。"""
        self.payload, self.status_code, self.closed = payload, status, False
        self.content, self.headers, self.text = b"json", {}, ""

    def json(self):
        """返回已设定的JSON结构。"""
        return self.payload

    def close(self):
        """标记同步响应释放。"""
        self.closed = True

    async def aclose(self):
        """标记异步响应释放。"""
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("source", ["theaudiodb", "doubanmusic"])
async def test_secondary_provider_counts_requests_and_recovers_uncached_failure(monkeypatch, source, asynchronous):
    """次级来源真实HTTP边界也服从预算，故障后同一请求可恢复而不会命中长负缓存。"""
    failed, recovered = _Response({}, 503), _Response({"album": [{"id": "ok"}]})
    network = (AsyncMock if asynchronous else Mock)(side_effect=[failed, recovered])
    client = SimpleNamespace(get_res=network)
    if source == "theaudiodb":
        getattr(TheAudioDbModule._request_json, "cache_clear")()
        monkeypatch.setattr("app.modules.theaudiodb.AsyncRequestUtils" if asynchronous else "app.modules.theaudiodb.RequestUtils",
                            lambda **_kwargs: client)

        async def request(query):
            """通过实际同步或异步请求入口验证共享缓存。"""
            return await TheAudioDbModule._async_request_json("searchalbum.php", {"a": query}) if asynchronous else (
                TheAudioDbModule._request_json("searchalbum.php", {"a": query}))
    else:
        api = object.__new__(DoubanApi)
        api._request = client
        api._user_agents = ["test"]
        monkeypatch.setattr(api, "_prepare_get_request", lambda url, **kwargs: (url, kwargs))
        getattr(api._music_get, "cache_clear")()
        monkeypatch.setattr("app.modules.douban.apiv2.AsyncRequestUtils", lambda **_kwargs: client)

        async def request(query):
            """音乐API请求仍经过实际解析和关闭逻辑。"""
            return await api.async_music_search(query) if asynchronous else api.music_search(query)
    try:
        with capture_music_recognition(seconds=5, request_limit=1) as first:
            assert await request("same") is None
        assert first.requests == 1 and first.status == "service_error" and failed.closed
        with capture_music_recognition(seconds=5, request_limit=1) as second:
            assert await request("same") == recovered.payload
            assert await request("other") is None
            assert await request("same") == recovered.payload
        assert second.requests == 1 and second.status == "budget_exhausted"
        assert network.call_count == 2 and recovered.closed
    finally:
        if source == "theaudiodb":
            getattr(TheAudioDbModule._request_json, "cache_clear")()
        else:
            getattr(api._music_get, "cache_clear")()


def _directory_setup(tmp_path, monkeypatch, sources, *, known_album=True):
    """使用真实无标签FLAC和独立媒体链测试整专来源选择，不注册全局单例。"""
    directory = tmp_path / ("Artist - Album" if known_album else "Music")
    directory.mkdir()
    for index, title in enumerate(("First", "Second"), 1):
        name = f"{index:02} - {title}.flac" if known_album else f"{index:02} - Artist - {title}.flac"
        shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", directory / name)
    config = {"sources": sources}
    monkeypatch.setattr("app.chain.media.album.get_chain_runtime_config_snapshot", lambda: SimpleNamespace(
        search_source=config["sources"], audio_extensions=(".flac",), music_cue_enable=True,
        music_release_region_priority=("CN",), music_release_script_priority=("Hans",)))
    chain = object.__new__(MediaChain)
    chain._album_dir_cache = AlbumDirectoryCache(8)
    monkeypatch.setattr(chain, "_finalize_recognition_result", lambda info, **_kwargs: info)
    monkeypatch.setattr(chain, "_async_finalize_recognition_result", AsyncMock(side_effect=lambda info, **_kwargs: info))
    primary = SimpleNamespace(match_music_album=Mock(return_value=None), async_match_music_album=AsyncMock(return_value=None))
    monkeypatch.setattr("app.chain.media.album.MusicBrainzChain", lambda: primary)
    return directory, chain, config, primary


def _album(source, identity="album-one", *, tracks=True):
    """构造同一来源命名空间内的完整专辑和原生曲目身份。"""
    album = MusicAlbumInfo(media_source=source, media_id=identity, title="Album", artists=["Artist"])
    if tracks:
        album.tracks = [MusicInfo(media_source=source, media_id=f"{identity}-track-{index}", title=title,
                                  artists=["Artist"], album="Album", album_id=identity,
                                  track_number=index, disc_number=1, duration=3)
                        for index, title in enumerate(("First", "Second"), 1)]
    return album


def _backend(albums):
    """模拟来源链的读边界，真正的搜索计划、候选核验和文件对位继续运行。"""
    by_id = {album.media_id: album for album in albums}
    cards = [album.to_music_info() for album in albums]
    return SimpleNamespace(search_music=Mock(return_value=cards), async_search_music=AsyncMock(return_value=cards),
                           get_music_album=Mock(side_effect=by_id.get), async_get_music_album=AsyncMock(side_effect=by_id.get))


@pytest.mark.parametrize("asynchronous", [False, True])
def test_directory_fallback_requires_complete_tracks_and_keeps_source_identity(tmp_path, monkeypatch, asynchronous):
    """首源故障时可按完整次级曲目表整理；匹配身份始终属于该次级来源。"""
    directory, chain, _config, primary = _directory_setup(tmp_path, monkeypatch, "musicbrainz,theaudiodb")

    def fail_primary(*_args, **_kwargs):
        """复现主元数据源短暂不可用。"""
        report_music_recognition("service_error", "临时不可用")
        return None

    primary.match_music_album.side_effect = fail_primary
    primary.async_match_music_album.side_effect = fail_primary
    backend = _backend([_album(MediaSource.TheAudioDB)])
    monkeypatch.setattr(chain, "_music_source_chain", lambda _source: backend)

    result = asyncio.run(chain.async_recognize_music_album_directory(directory)) if asynchronous else chain.recognize_music_album_directory(directory)

    assert len(result) == 2 and result.recognition["status"] == "matched"
    assert [row["status"] for row in result.recognition["sources"]] == ["service_error", "matched"]
    assert {info.media_source for info in result.values()} == {MediaSource.TheAudioDB}
    assert {info.media_id for info in result.values()} == {"album-one-track-1", "album-one-track-2"}
    assert all(info.musicbrainz_release_id is None and info.raw_data["recognition"]["release_verified"] is False for info in result.values())


@pytest.mark.parametrize("case", ["no_tracks", "ambiguous", "wrong_source", "wrong_duration"])
def test_directory_does_not_invent_alignment_or_pick_first_album(tmp_path, monkeypatch, case):
    """没有曲目、等价多候选、混源ID及明显时长冲突都不能产生自动文件映射。"""
    directory, chain, _config, _primary = _directory_setup(tmp_path, monkeypatch, "theaudiodb")
    album = _album(MediaSource.TheAudioDB, tracks=case != "no_tracks")
    albums = [album, _album(MediaSource.TheAudioDB, "other")] if case == "ambiguous" else [album]
    backend = _backend(albums)
    if case == "wrong_source":
        backend.get_music_album.side_effect = lambda _id: _album(MediaSource.DoubanMusic)
    if case == "wrong_duration":
        album.tracks[0].duration = 200
    monkeypatch.setattr(chain, "_music_source_chain", lambda _source: backend)

    result = chain.recognize_music_album_directory(directory)

    assert not result
    assert result.recognition["status"] == ("ambiguous" if case == "ambiguous" else "not_found")


def test_source_configuration_change_invalidates_directory_negative_cache(tmp_path, monkeypatch):
    """切换来源后必须重新匹配，不能复用同一目录在旧来源中的无匹配结果。"""
    directory, chain, config, primary = _directory_setup(tmp_path, monkeypatch, "musicbrainz")
    backend = _backend([_album(MediaSource.TheAudioDB)])
    monkeypatch.setattr(chain, "_music_source_chain", lambda _source: backend)
    assert not chain.recognize_music_album_directory(directory)
    config["sources"] = "theaudiodb"

    result = chain.recognize_music_album_directory(directory)

    assert len(result) == 2
    primary.match_music_album.assert_called_once()
    backend.search_music.assert_called_once()


def test_directory_lookup_freezes_source_choice_with_cache_key(tmp_path, monkeypatch):
    """配置在缓存键生成后改变，也不能将新来源的结果存入旧来源键。"""
    directory, chain, config, _primary = _directory_setup(tmp_path, monkeypatch, "theaudiodb")
    backends = {source: _backend([_album(source)]) for source in (MediaSource.TheAudioDB, MediaSource.DoubanMusic)}
    monkeypatch.setattr(chain, "_music_source_chain", backends.get)
    original = chain._album_dir_cache.resolve

    def change_config(key, signature, resolver):
        """在真实缓存准备加载时切换下一次请求的来源。"""
        config["sources"] = "doubanmusic"
        return original(key, signature, resolver)

    monkeypatch.setattr(chain._album_dir_cache, "resolve", change_config)
    first = chain.recognize_music_album_directory(directory)
    second = chain.recognize_music_album_directory(directory)

    assert {info.media_source for info in first.values()} == {MediaSource.TheAudioDB}
    assert {info.media_source for info in second.values()} == {MediaSource.DoubanMusic}


def test_unknown_album_can_be_discovered_from_two_track_names(tmp_path, monkeypatch):
    """专辑目录名无意义时，使用真实曲名返回的同来源album_id汇总候选再完整核验。"""
    directory, chain, _config, _primary = _directory_setup(tmp_path, monkeypatch, "theaudiodb", known_album=False)
    album = _album(MediaSource.TheAudioDB)
    backend = _backend([album])
    backend.search_music.side_effect = lambda meta, **_kwargs: [track for track in album.tracks if track.title == meta.title]
    monkeypatch.setattr(chain, "_music_source_chain", lambda _source: backend)

    result = chain.recognize_music_album_directory(directory)

    assert len(result) == 2
    assert backend.search_music.call_count == 2
    backend.get_music_album.assert_called_once_with("album-one")


def test_secondary_recording_candidates_are_not_selected_by_response_order():
    """同名同艺人的不同原生ID没有额外证据时必须保持歧义。"""
    candidates = [MusicInfo(media_source="theaudiodb", media_id=identity, title="Song", artists=["Artist"])
                  for identity in ("one", "two")]
    with capture_music_recognition() as observed:
        assert TheAudioDbModule._select_track(MetaMusic(title="Song", artists=["Artist"]), candidates) is None
    assert observed.status == "ambiguous" and len(observed.candidates) == 2


@pytest.mark.parametrize("asynchronous", [False, True])
def test_secondary_catalog_preserves_local_multidisc_edition(tmp_path, monkeypatch, asynchronous):
    """真实标签的再版年及双碟曲序不能被次级目录的单碟首版布局覆盖。"""
    from mutagen.flac import FLAC

    directory, chain, _config, _primary = _directory_setup(tmp_path, monkeypatch, "theaudiodb")
    files = sorted(directory.glob("*.flac"))
    for disc, path in enumerate(files, 1):
        tags = FLAC(path)
        tags.update(title=["First" if disc == 1 else "Second"], album=["Album"], artist=["Unknown Artist"],
                    albumartist=["Artist"], discnumber=[str(disc)], disctotal=["2"], tracknumber=["1"],
                    tracktotal=["1"], date=["2020-09-01"], originaldate=["2001"])
        tags.save()
    album = _album(MediaSource.TheAudioDB)
    album.release_date = "2001"
    for track in album.tracks:
        track.year, track.release_year, track.original_year = 2001, None, 2001
        track.total_tracks, track.total_discs = 2, 1
    backend = _backend([album])
    monkeypatch.setattr(chain, "_music_source_chain", lambda _source: backend)

    result = asyncio.run(chain.async_recognize_music_album_directory(directory)) if asynchronous else chain.recognize_music_album_directory(directory)

    assert len(result) == 2
    for disc, path in enumerate(files, 1):
        info = result[str(path.resolve())]
        assert (info.disc_number, info.track_number, info.total_tracks, info.total_discs) == (disc, 1, 1, 2)
        assert (info.year, info.release_year, info.original_year) == (2020, 2020, 2001)
        assert info.field_sources["release_year"] == "tag"
        assert info.media_id == f"album-one-track-{disc}"
        assert info.musicbrainz_release_id is None
    assert [track.track_number for track in album.tracks] == [1, 2]
    assert [track.year for track in album.tracks] == [2001, 2001]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("music_type", ["album", "recording"])
def test_douban_distinct_matching_identities_require_confirmation(asynchronous, music_type):
    """同步和异步豆瓣搜索遇到同名同署名的不同身份时都不能取第一个。"""
    from app.modules.douban import DoubanModule

    module = object.__new__(DoubanModule)
    cards = [{"id": identity, "title": "Album", "artists": [{"name": "Artist"}]} for identity in ("100", "200")]

    def detail(subject_id):
        """提供同证据的两个专辑曲目表。"""
        return {"id": subject_id, "title": "Album", "singer": [{"name": "Artist"}],
                "songs": [{"title": "First", "track_number": 1}]}

    module.doubanapi = SimpleNamespace(music_search=Mock(return_value={"items": cards}),
                                      async_music_search=AsyncMock(return_value={"items": cards}),
                                      music_detail=Mock(side_effect=detail), async_music_detail=AsyncMock(side_effect=detail))
    meta = MetaMusic(title="First", album="Album", artists=["Artist"], track_number=1, music_type=music_type)
    with capture_music_recognition() as observation:
        result = asyncio.run(module._async_recognize_music_media(meta, MediaSource.DoubanMusic, None, music_type)) if asynchronous else (
            module._recognize_music_media(meta, MediaSource.DoubanMusic, None, music_type))
    assert result is None and observation.status == "ambiguous"
    assert len(observation.candidates) == 2
    if music_type == "album":
        module.doubanapi.music_detail.assert_not_called()
        module.doubanapi.async_music_detail.assert_not_called()


@pytest.mark.parametrize("state", ["budget_exhausted", "service_error", "ambiguous", "conflict"])
def test_source_cache_cannot_turn_failed_parent_into_success(state):
    """外层已中断的识别不能因后续来源命中缓存而返回成功身份。"""
    with capture_music_recognition() as observation:
        report_music_recognition(state, "外层已中断")
        result, diagnostic = recognize_music_sources((MediaSource.TheAudioDB,), lambda source: MusicInfo(
            media_source=source, media_id="cached", title="First"), _accepts)
    assert result is None
    assert diagnostic["status"] == observation.status == state


@pytest.mark.parametrize("asynchronous", [False, True])
def test_directory_does_not_confirm_truncated_candidates(tmp_path, monkeypatch, asynchronous):
    """超过有界详情核验范围时保留歧义，不能仅凭前五个候选宣布唯一。"""
    directory, chain, _config, _primary = _directory_setup(tmp_path, monkeypatch, "theaudiodb")
    backend = _backend([_album(MediaSource.TheAudioDB, str(index + 10)) for index in range(6)])
    monkeypatch.setattr(chain, "_music_source_chain", lambda _source: backend)
    result = asyncio.run(chain.async_recognize_music_album_directory(directory)) if asynchronous else chain.recognize_music_album_directory(directory)
    assert not result and result.recognition["status"] == "ambiguous"
    backend.get_music_album.assert_not_called()
    backend.async_get_music_album.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_empty_directory_diagnostics_survive_single_file_fallback(tmp_path, monkeypatch, asynchronous):
    """空目录映射仍须把待确认原因送到单文件识别，并保留本地展示信息。"""
    from app.chain.media.path import _finalize_music_path_info
    from app.domain.music import MusicDirectoryMatch

    path = tmp_path / "First.flac"
    path.write_bytes(b"test")
    chain = object.__new__(MediaChain)
    pending = MusicDirectoryMatch(recognition={"status": "ambiguous", "message": "多个专辑"})
    monkeypatch.setattr(chain, "recognize_music_album_directory", Mock(return_value=pending))
    monkeypatch.setattr(chain, "async_recognize_music_album_directory", AsyncMock(return_value=pending))
    result = asyncio.run(chain._async_music_album_dir_fallback(path)) if asynchronous else chain._music_album_dir_fallback(path)
    result = _finalize_music_path_info(MetaMusic(title="First", artists=["Artist"]), result)
    assert result.title == "First" and result.artists == ["Artist"]
    assert result.media_id is None and result.raw_data["recognition"] == pending.recognition
