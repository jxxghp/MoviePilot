"""音乐识别的故障语义、短负缓存、请求预算和手动刷新回归。"""

import asyncio
from contextvars import copy_context
from copy import deepcopy
from threading import Event, Thread, get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from mutagen.flac import FLAC

from app.api.endpoints import music as music_endpoint
from app.application import audio as audio_module
from app.application.audio import AudioMetadataHelper, capture_audio_metadata
from app.application.music import observation as observation_module
from app.application.music.observation import capture_music_recognition, report_music_recognition
from app.chain.media import MediaChain
from app.chain.media.cache import AlbumDirectoryCache
from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.music import MusicDirectoryMatch
from app.modules.musicbrainz import MusicBrainzModule
from app.schemas.types import MediaType
from tests.test_music_album_match import _release_detail
from tests.test_music_cue import _image_pair
from tests.test_music_recognize_cache import _build_music_cache, _TTLCacheStub
from tests.test_music_resource_context import _audio_files
from tests.test_music_storage_context import _setup_transfer
from tests.test_musicbrainz_module import _FakeMusicBrainzResponse
from tests.test_transfer_sync_extra_files import make_fileitem

SIGNATURE = (("one.flac", 100, 1),)


@pytest.fixture(autouse=True)
def isolated_http_cache():
    """每条用例隔离共享HTTP缓存，不让一次故障或成功污染后续判断。"""
    MusicBrainzModule._request_json.cache_clear()
    yield
    MusicBrainzModule._request_json.cache_clear()


def test_directory_success_no_match_and_failure_have_separate_expiry():
    """成功、无匹配与临时故障各自过期，空字典不再永久有效。"""
    now = [0.0]
    cache = AlbumDirectoryCache(4, ttl=10, negative_ttl=3, failure_ttl=1, clock=lambda: now[0])
    cache.put("positive", SIGNATURE, {"one": MusicInfo(title="Song")})
    cache.put("empty", SIGNATURE, {})
    cache.put("failed", SIGNATURE, MusicDirectoryMatch(recognition={"status": "service_error"}))
    now[0] = 1
    assert cache.get("failed", SIGNATURE) is None
    assert cache.get("empty", SIGNATURE) == {}
    now[0] = 3
    assert cache.get("empty", SIGNATURE) is None
    assert cache.get("positive", SIGNATURE)
    now[0] = 10
    assert cache.get("positive", SIGNATURE) is None


def test_clear_during_sync_lookup_does_not_join_or_cache_old_flight():
    """刷新后的请求独立执行，旧请求稍后完成也不能覆盖新结果。"""
    cache = AlbumDirectoryCache(4)
    started, release = Event(), Event()
    old_results = []

    def old_lookup():
        """把旧识别保持在刷新动作之前开始、之后结束。"""
        started.set()
        assert release.wait(2)
        return {"one": MusicInfo(title="Old")}

    worker = Thread(target=lambda: old_results.append(cache.resolve("album", SIGNATURE, old_lookup)))
    worker.start()
    try:
        assert started.wait(2)
        cache.clear()
        fresh = cache.resolve("album", SIGNATURE, lambda: {"one": MusicInfo(title="New")})
        assert fresh["one"].title == "New"
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert old_results[0]["one"].title == "Old"
    assert cache.get("album", SIGNATURE)["one"].title == "New"


@pytest.mark.asyncio
async def test_clear_during_async_lookup_fences_old_result():
    """异步刷新使用同一代际规则，也不取消刷新前已开始的等待者。"""
    cache = AlbumDirectoryCache(4)
    started, release = asyncio.Event(), asyncio.Event()

    async def old_lookup():
        """等待受控事件再返回旧数据。"""
        started.set()
        await release.wait()
        return {"one": MusicInfo(title="Old")}

    old = asyncio.create_task(cache.async_resolve("album", SIGNATURE, old_lookup))
    await started.wait()
    cache.clear()
    await cache.async_resolve("album", SIGNATURE, AsyncMock(return_value={"one": MusicInfo(title="New")}))
    release.set()
    assert (await old)["one"].title == "Old"
    assert cache.get("album", SIGNATURE)["one"].title == "New"


def test_http_budget_counts_misses_but_cached_reads_are_free(monkeypatch):
    """额度用于真实请求；缓存命中无需额外请求，超时取当前剩余预算。"""
    network = Mock(return_value=_FakeMusicBrainzResponse({"id": "one"}))
    monkeypatch.setattr(MusicBrainzModule, "_get_request", lambda: SimpleNamespace(get_res=network))
    monkeypatch.setattr(MusicBrainzModule, "_wait_for_rate_limit", lambda: None)
    with capture_music_recognition(seconds=5, request_limit=2) as observed:
        assert MusicBrainzModule._request_json("/one")
        assert MusicBrainzModule._request_json("/two")
        assert MusicBrainzModule._request_json("/three") is None
        assert MusicBrainzModule._request_json("/one")
    assert observed.requests == network.call_count == 2
    assert observed.status == "budget_exhausted"
    assert all(0 < call.kwargs["timeout"] <= 5 for call in network.call_args_list)


def test_deadline_stops_wait_without_reserving_future_slots(monkeypatch):
    """请求排队时间超预算时直接停止，不能再占用未来时隙拖慢其他专辑。"""
    monkeypatch.setattr(observation_module, "monotonic", lambda: 10)
    monkeypatch.setattr("app.modules.musicbrainz.time.monotonic", lambda: 10)
    monkeypatch.setattr(MusicBrainzModule, "_last_request_at", 100)
    with capture_music_recognition(seconds=5) as observed:
        assert MusicBrainzModule._wait_for_rate_limit() is False
    assert MusicBrainzModule._last_request_at == 100
    assert observed.status == "budget_exhausted"


def test_nested_observation_shares_budget_and_restores_after_exception():
    """嵌套入口共享诊断，异常退出后下一个请求不继承上次的失败。"""
    with pytest.raises(ValueError), capture_music_recognition() as first:
        with capture_music_recognition() as nested:
            assert nested is first
            raise ValueError("provider failure")
    assert first.status == "service_error"
    with capture_music_recognition() as following:
        assert following.status == "not_found"
        assert following is not first


@pytest.mark.parametrize("status,payload", [(200, []), (403, {}), (503, {})])
def test_http_failures_are_not_cached_as_empty_search(monkeypatch, status, payload):
    """错误响应保留故障语义，下一次请求能够在服务恢复后重新获取数据。"""
    network = Mock(return_value=_FakeMusicBrainzResponse(payload, status_code=status))
    monkeypatch.setattr(MusicBrainzModule, "_get_request", lambda: SimpleNamespace(get_res=network))
    monkeypatch.setattr(MusicBrainzModule, "_wait_for_rate_limit", lambda: None)
    monkeypatch.setattr("app.modules.musicbrainz.time.sleep", lambda _seconds: None)
    with capture_music_recognition() as observed:
        assert MusicBrainzModule._request_json("/failure") is None
    assert observed.status == "service_error"
    network.return_value = _FakeMusicBrainzResponse({"id": "recovered"})
    with capture_music_recognition():
        assert MusicBrainzModule._request_json("/failure") == {"id": "recovered"}


def test_negative_metadata_has_short_ttl_and_failures_are_not_persisted():
    """无匹配只有五分钟TTL；服务故障不进入无身份音乐的负缓存。"""
    cache = _build_music_cache({})
    backend = _TTLCacheStub()
    cache._cache = backend
    meta = MetaMusic(title="Song")
    cache.update(meta, MusicInfo(title="Song"))
    assert list(backend.ttls.values()) == [300]
    cache.clear()
    cache.update(meta, MusicInfo(title="Song", raw_data={"recognition": {"status": "service_error"}}))
    assert backend.data == {}


def test_recording_network_failure_does_not_become_negative_metadata(monkeypatch):
    """实际单曲识别中的连接失败保留诊断，不能写成五分钟的无匹配条目。"""
    module = MusicBrainzModule()
    cache = _build_music_cache({})
    monkeypatch.setattr(module, "cache", cache)
    network = Mock(return_value=None)
    monkeypatch.setattr(MusicBrainzModule, "_get_request", lambda: SimpleNamespace(get_res=network))
    monkeypatch.setattr(MusicBrainzModule, "_wait_for_rate_limit", lambda: None)

    info = module.recognize_media(meta=MetaMusic(title="Unique Song", artists=["Artist"]))

    assert info is not None and not info.media_id
    assert info.raw_data["recognition"]["status"] == "service_error"
    assert cache._cache.data == {}
    assert network.call_count == 1


@pytest.mark.asyncio
async def test_async_http_budget_and_clear_share_the_same_response_cache(monkeypatch):
    """异步请求同样受额度限制，现有同步清理入口可以清掉它的来源响应缓存。"""
    response = SimpleNamespace(status_code=200, text="", json=lambda: {"id": "one"}, aclose=AsyncMock())
    network = AsyncMock(return_value=response)
    monkeypatch.setattr("app.modules.musicbrainz.AsyncRequestUtils", lambda **_kwargs: SimpleNamespace(get_res=network))
    monkeypatch.setattr(MusicBrainzModule, "_async_wait_for_rate_limit", AsyncMock(return_value=None))
    with capture_music_recognition(seconds=5, request_limit=1) as observed:
        assert await MusicBrainzModule._async_request_json("/async-one")
        assert await MusicBrainzModule._async_request_json("/async-two") is None
    assert observed.status == "budget_exhausted" and network.call_count == 1
    MusicBrainzModule().music_cache_clear()
    with capture_music_recognition():
        assert await MusicBrainzModule._async_request_json("/async-one")
    assert network.call_count == 2


@pytest.mark.parametrize("asynchronous", [False, True])
def test_directory_failure_recovers_after_short_cooldown(tmp_path, monkeypatch, asynchronous):
    """同批文件复用故障状态，短暂冷却后可自动恢复，不能永久缓存空结果。"""
    files = _audio_files(tmp_path / "Artist - Album (2004)")
    detail = _release_detail("release", "Album", "Artist", [("First Song", 3), ("Second Song", 3)])
    album = MusicBrainzModule._release_to_album(detail)
    now = [0.0]
    chain = MediaChain()
    monkeypatch.setattr(chain, "_album_dir_cache", AlbumDirectoryCache(4, clock=lambda: now[0], failure_ttl=1))

    def unavailable(*_args, **_kwargs):
        """模拟正常传回的服务故障诊断。"""
        report_music_recognition("service_error", "服务暂时不可用")
        return None

    request = AsyncMock(side_effect=unavailable) if asynchronous else Mock(side_effect=unavailable)
    source = Mock(**{"async_match_music_album" if asynchronous else "match_music_album": request})
    monkeypatch.setattr("app.chain.media.album.MusicBrainzChain", Mock(return_value=source))
    monkeypatch.setattr(chain, "_finalize_recognition_result", lambda info: info)
    monkeypatch.setattr(chain, "_async_finalize_recognition_result", AsyncMock(side_effect=lambda info: info))

    def recognize():
        """调用同一公开目录识别入口，覆盖同步与异步结果传递。"""
        return asyncio.run(chain.async_recognize_music_album_directory(files[0].parent)) if asynchronous else chain.recognize_music_album_directory(files[0].parent)

    assert recognize().recognition["status"] == "service_error"
    assert recognize().recognition["status"] == "service_error"
    assert request.call_count == 1
    now[0] = 2
    request.side_effect, request.return_value = None, album
    assert len(recognize()) == 2
    assert request.call_count == 2


def test_ambiguous_album_blocks_recording_fallback_and_file_operations(tmp_path, monkeypatch):
    """真实整理入口保留发行歧义，不再靠逐曲搜索把同一包悄悄绑到不同发行。"""
    paths = _audio_files(tmp_path / "Artist - Album (2004)")
    items = [make_fileitem(str(path)) for path in paths]
    owner = _setup_transfer(monkeypatch, items)
    chain = MediaChain()
    monkeypatch.setattr(chain, "_album_dir_cache", AlbumDirectoryCache(4))
    detail = _release_detail("release-a", "Album", "Artist", [("First Song", 3), ("Second Song", 3)])
    other = deepcopy(detail)
    other["id"] = "release-b"
    other["media"][0]["tracks"][0]["recording"]["id"] = "other-recording"
    source = Mock(match_music_album=lambda meta, tracks, **_kwargs: MusicBrainzModule._select_release_match(meta, tracks, [detail, other]))
    monkeypatch.setattr("app.chain.media.album.MusicBrainzChain", Mock(return_value=source))
    fallback = Mock(side_effect=AssertionError("歧义不能转入逐曲猜测"))
    monkeypatch.setattr(MediaChain, "recognize_by_meta", fallback)
    plan = Mock(side_effect=AssertionError("歧义不能执行文件操作"))
    monkeypatch.setattr(owner, "_plan_checkpoint_and_execute", plan)
    root = make_fileitem(str(paths[0].parent)).model_copy(update={"type": "dir"})

    state, result = owner.do_transfer(fileitem=root, mtype=MediaType.MUSIC, preview=True, force=True, library_category_folder=False)

    assert state is False
    assert "手动选择专辑版本" in str(result)
    fallback.assert_not_called()
    plan.assert_not_called()


def test_music_cache_endpoint_clears_http_and_directory_layers(monkeypatch):
    """用户的清缓存操作同时移除来源响应和目录状态，保留原管理端点合同。"""
    network = Mock(return_value=_FakeMusicBrainzResponse({"id": "one"}))
    monkeypatch.setattr(MusicBrainzModule, "_get_request", lambda: SimpleNamespace(get_res=network))
    monkeypatch.setattr(MusicBrainzModule, "_wait_for_rate_limit", lambda: None)
    module = MusicBrainzModule()
    monkeypatch.setattr(module, "cache", _build_music_cache({}))
    monkeypatch.setattr(music_endpoint, "MusicBrainzChain", lambda: SimpleNamespace(clear_cache=module.music_cache_clear))
    cache = AlbumDirectoryCache(4)
    monkeypatch.setattr(MediaChain, "_album_dir_cache", cache)
    cache.put("album", SIGNATURE, {})
    MusicBrainzModule._request_json("/cached")

    assert asyncio.run(music_endpoint.clear_music_recognition_cache()).success
    assert cache.get("album", SIGNATURE) is None
    MusicBrainzModule._request_json("/cached")
    assert network.call_count == 2


def test_audio_scan_reuses_isolated_tags_and_invalidates_changed_files(tmp_path, monkeypatch):
    """预览和匹配复用一次容器读取，路径补写不污染纯标签，写标签后须重新读取。"""
    path = _audio_files(tmp_path / "Artist - Album (2004)")[0]
    read = Mock(wraps=audio_module.MutagenFile)
    monkeypatch.setattr(audio_module, "MutagenFile", read)
    with capture_audio_metadata():
        assert AudioMetadataHelper.read(path).title == "First Song"
        assert AudioMetadataHelper.read_tags(path).title is None
        AudioMetadataHelper.read_evidence(path)
        assert read.call_count == 1
        tags = FLAC(path)
        tags["title"] = ["Changed"]
        tags.save()
        assert AudioMetadataHelper.read_tags(path).title == "Changed"
        assert read.call_count == 2
        inherited = copy_context()
    assert inherited.run(AudioMetadataHelper.read_tags, path).title == "Changed"
    assert read.call_count == 3


def test_audio_scan_reuses_cue_text_but_reads_edited_indexes(tmp_path, monkeypatch):
    """同一批次的CUE重复使用只读文本，索引编辑会立即使快照失效。"""
    path, cue = _image_pair(tmp_path)
    read = Mock(wraps=audio_module._load_cue_text)
    monkeypatch.setattr(audio_module, "_load_cue_text", read)
    with capture_audio_metadata():
        AudioMetadataHelper.read(path)
        AudioMetadataHelper.read_evidence(path)
        assert read.call_count == 1
        cue.write_text(cue.read_text().replace("第二首歌曲", "修改后的歌曲"))
        assert AudioMetadataHelper.read(path).cue_tracks[1]["title"] == "修改后的歌曲"
        assert read.call_count == 2


def test_full_transfer_batch_reads_each_audio_container_once(tmp_path, monkeypatch):
    """候选规划、分组、专辑匹配走真实整理入口，并共享批次只读标签快照。"""
    paths = _audio_files(tmp_path / "Artist - Album (2004)")
    owner = _setup_transfer(monkeypatch, [make_fileitem(str(path)) for path in paths])
    monkeypatch.setattr(MediaChain(), "_album_dir_cache", AlbumDirectoryCache(4))
    detail = _release_detail("release", "Album", "Artist", [("First Song", 3), ("Second Song", 3)])
    source = Mock(match_music_album=Mock(return_value=MusicBrainzModule._release_to_album(detail)))
    monkeypatch.setattr("app.chain.media.album.MusicBrainzChain", Mock(return_value=source))
    monkeypatch.setattr(owner, "_TransferChain__handle_transfer", lambda task, **_kwargs: (bool(task), ""))
    read = Mock(wraps=audio_module.MutagenFile)
    monkeypatch.setattr(audio_module, "MutagenFile", read)
    root = make_fileitem(str(paths[0].parent)).model_copy(update={"type": "dir"})

    state, _result = owner.do_transfer(fileitem=root, mtype=MediaType.MUSIC, background=False, force=True)

    assert state is True
    assert read.call_count == len(paths)
    source.match_music_album.assert_called_once()


@pytest.mark.asyncio
async def test_async_directory_scan_and_cue_signatures_run_off_event_loop(tmp_path, monkeypatch):
    """音频枚举与新增的CUE签名读取都在工作线程，不阻塞异步API的事件循环。"""
    paths = _audio_files(tmp_path / "Artist - Album")
    threads = []

    def collect(_directory, _scope=None):
        """记录目录扫描实际执行的线程。"""
        threads.append(get_ident())
        return paths

    def signature(_directory, _files):
        """记录文件签名读取实际执行的线程。"""
        threads.append(get_ident())
        return SIGNATURE

    chain = MediaChain()
    monkeypatch.setattr(MediaChain, "_directory_audio_files", staticmethod(collect))
    monkeypatch.setattr(MediaChain, "_album_directory_signature", staticmethod(signature))
    monkeypatch.setattr(chain, "_album_dir_cache", AlbumDirectoryCache(4))
    monkeypatch.setattr(chain, "_async_match_music_album_directory", AsyncMock(return_value={}))

    assert await chain.async_recognize_music_album_directory(paths[0].parent) == {}
    assert len(threads) == 2 and threads[0] == threads[1]
    assert threads[0] != get_ident()
