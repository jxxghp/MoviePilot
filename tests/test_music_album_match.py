import asyncio
import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from app.chain.media import MediaChain
from app.chain.media.cache import AlbumDirectoryCache
from app.domain.context import MusicAlbumInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.modules.musicbrainz import MusicBrainzModule


def _release_detail(release_id: str, title: str, artist: str, tracks: list[tuple[str, int]]):
    """构造 MusicBrainz Release 详情响应，tracks 为 (曲名, 时长秒) 列表。"""
    return {
        "id": release_id,
        "title": title,
        "date": "2004-08-03",
        "artist-credit": [{"artist": {"id": "artist-1", "name": artist}}],
        "release-group": {"id": "rg-1", "primary-type": "Album", "secondary-types": []},
        "media": [
            {
                "position": 1,
                "track-count": len(tracks),
                "tracks": [
                    {
                        "position": index + 1,
                        "length": length * 1000,
                        "title": name,
                        "recording": {"id": f"rec-{index + 1}", "title": name, "length": length * 1000},
                    }
                    for index, (name, length) in enumerate(tracks)
                ],
            }
        ],
    }


ALBUM_TRACKS = [("我的地盘", 215), ("七里香", 299), ("借口", 265)]


def _local_tracks():
    """构造无标签整专目录读取得到的本地曲目元数据。"""
    return [
        MetaMusic(title=name, track_number=index + 1, duration=length, audio_format="WAV")
        for index, (name, length) in enumerate(ALBUM_TRACKS)
    ]


def test_match_music_album_selects_release_by_count_and_duration(monkeypatch):
    """曲目数和时长一致的发行版本应被选中并返回曲目表。"""
    module = MusicBrainzModule()
    detail = _release_detail("release-1", "七里香", "周杰伦", ALBUM_TRACKS)

    def fake_request(path, params=None):
        if path == "/release":
            return {"releases": [{"id": "release-1", "title": "七里香"}]}
        if path == "/release/release-1":
            return detail
        return None

    monkeypatch.setattr(module, "_request_json", fake_request)

    album = module.match_music_album(
        MetaMusic(album="七里香", artists=["周杰伦"]),
        _local_tracks(),
    )

    assert album is not None
    assert album.media_id == "rg-1"
    assert album.title == "七里香"
    assert album.artists == ["周杰伦"]
    assert [track.media_id for track in album.tracks] == ["rec-1", "rec-2", "rec-3"]
    assert album.tracks[0].track_number == 1
    assert album.tracks[0].album == "七里香"


def test_match_music_album_rejects_mismatched_trackset(monkeypatch):
    """曲目数和时长都对不上的候选应被拒绝，避免写错标签。"""
    module = MusicBrainzModule()
    # 候选只有 1 首歌且时长差异巨大
    detail = _release_detail("release-1", "七里香", "周杰伦", [("七里香", 60)])

    def fake_request(path, params=None):
        if path == "/release":
            return {"releases": [{"id": "release-1", "title": "七里香"}]}
        if path == "/release/release-1":
            return detail
        return None

    monkeypatch.setattr(module, "_request_json", fake_request)

    album = module.match_music_album(
        MetaMusic(album="七里香", artists=["周杰伦"]),
        _local_tracks(),
    )

    assert album is None


def test_release_queries_fallback_to_track_titles():
    """目录名没有专辑线索时应使用代表性曲名反查发行版本。"""
    queries = MusicBrainzModule._release_queries(
        MetaMusic(title="Various"),
        [MetaMusic(title="晴天"), MetaMusic(title="七里香"), MetaMusic(title="03")],
    )

    assert any("recording:" in query for query in queries)
    # 纯数字文件名不能作为曲名线索
    assert all('"03"' not in query for query in queries)


@pytest.fixture()
def media_chain():
    """构造媒体链并清理目录匹配缓存。"""
    chain = MediaChain()
    MediaChain._album_dir_cache.clear()
    yield chain
    MediaChain._album_dir_cache.clear()


def test_recognize_album_directory_maps_files(tmp_path, media_chain, monkeypatch):
    """目录级匹配应把每个音频文件对位到专辑曲目并缓存结果。"""
    album_dir = tmp_path / "周杰伦 - 七里香 (2004)"
    album_dir.mkdir()
    files = []
    for index, (name, length) in enumerate(ALBUM_TRACKS):
        file = album_dir / f"{index + 1:02d}.{name}.wav"
        file.write_bytes(b"RIFF")
        files.append(file)

    album = MusicAlbumInfo(
        media_source="musicbrainz",
        media_id="rg-1",
        title="七里香",
        artists=["周杰伦"],
        tracks=[
            MusicInfo(
                media_source="musicbrainz",
                media_id=f"rec-{index + 1}",
                title=name,
                artists=["周杰伦"],
                album="七里香",
                track_number=index + 1,
                duration=length,
            )
            for index, (name, length) in enumerate(ALBUM_TRACKS)
        ],
    )
    source_chain = Mock()
    source_chain.match_music_album.side_effect = lambda *_args, **_kwargs: album
    monkeypatch.setattr(
        "app.chain.media.album.MusicBrainzChain",
        Mock(return_value=source_chain),
    )

    matched = media_chain.recognize_music_album_directory(album_dir)

    assert len(matched) == len(files)
    for index, file in enumerate(files):
        info = matched[str(file.resolve())]
        assert info.media_id == f"rec-{index + 1}"
        assert info.title == ALBUM_TRACKS[index][0]
    # 同一目录再次识别直接命中缓存，不重复请求模块
    assert media_chain.recognize_music_album_directory(album_dir) == matched
    source_chain.match_music_album.assert_called_once()


def test_align_album_tracks_prefers_exact_titles_over_conflicting_positions():
    """本地曲序与发行版本冲突时，精确曲名必须优先，避免整张专辑错位。"""
    files = [
        Path("费玉清-真的好想你.flac"),
        Path("费玉清-冬之夜.flac"),
        Path("费玉清-愛是一個圓.flac"),
        Path("04.flac"),
    ]
    metas = [
        MetaMusic(title="真的好想你", track_number=1),
        MetaMusic(title="冬之夜", track_number=2),
        MetaMusic(title="愛是一個圓", track_number=3),
        MetaMusic(title="04", track_number=4),
    ]
    tracks = [
        MusicInfo(
            media_source="musicbrainz",
            media_id="winter",
            title="冬之夜",
            track_number=1,
        ),
        MusicInfo(
            media_source="musicbrainz",
            media_id="circle",
            title="爱是一个圆",
            track_number=2,
        ),
        MusicInfo(
            media_source="musicbrainz",
            media_id="miss",
            title="真的好想你",
            track_number=3,
        ),
        MusicInfo(
            media_source="musicbrainz",
            media_id="fallback",
            title="一生的朋友",
            track_number=4,
        ),
    ]

    matched = MediaChain._align_music_album_tracks(files, metas, tracks)

    assert [matched[file].media_id for file in files] == [
        "miss",
        "winter",
        "circle",
        "fallback",
    ]


def test_async_recognize_album_directory_calls_async_module(
        tmp_path,
        media_chain,
        monkeypatch,
):
    """异步目录识别应直接调用模块异步接口，并兼容单个专辑返回值。"""
    album_dir = tmp_path / "周杰伦 - 七里香 (2004)"
    album_dir.mkdir()
    files = []
    for index, (name, _length) in enumerate(ALBUM_TRACKS):
        file = album_dir / f"{index + 1:02d}.{name}.wav"
        file.write_bytes(b"RIFF")
        files.append(file)

    album = MusicAlbumInfo(
        media_source="musicbrainz",
        media_id="rg-1",
        title="七里香",
        artists=["周杰伦"],
        tracks=[
            MusicInfo(
                media_source="musicbrainz",
                media_id=f"rec-{index + 1}",
                title=name,
                artists=["周杰伦"],
                album="七里香",
                track_number=index + 1,
                duration=length,
            )
            for index, (name, length) in enumerate(ALBUM_TRACKS)
        ],
    )
    async_run_module = AsyncMock(return_value=album)
    run_module = Mock(side_effect=AssertionError("异步目录识别不应调用同步模块接口"))
    source_chain = Mock()
    source_chain.async_match_music_album = async_run_module
    source_chain.match_music_album = run_module
    monkeypatch.setattr(
        "app.chain.media.album.MusicBrainzChain", Mock(return_value=source_chain)
    )
    monkeypatch.setattr(
        "app.chain.media.album.AudioMetadataHelper.read_many",
        lambda _files: _local_tracks(),
    )

    matched = asyncio.run(media_chain.async_recognize_music_album_directory(album_dir))

    assert [matched[str(file.resolve())].media_id for file in files] == [
        "rec-1",
        "rec-2",
        "rec-3",
    ]
    async_run_module.assert_awaited_once()
    assert async_run_module.await_args.args[0].album == "七里香"
    run_module.assert_not_called()


def test_async_recognize_album_directory_checks_path_in_threadpool(
        tmp_path,
        media_chain,
        monkeypatch,
):
    """异步专辑识别应把目录元数据检查移出事件循环。"""
    check_directory = AsyncMock(return_value=False)
    monkeypatch.setattr("app.chain.media.album.run_in_threadpool", check_directory)

    result = asyncio.run(
        media_chain.async_recognize_music_album_directory(tmp_path / "missing")
    )

    assert result == {}
    check_directory.assert_awaited_once()
    assert check_directory.await_args.args[1] == tmp_path / "missing"


def test_async_album_fallback_propagates_cancellation_during_path_check(
        tmp_path,
        media_chain,
        monkeypatch,
):
    """异步专辑兜底不得吞掉文件检查被取消的信号。"""
    started = asyncio.Event()

    async def wait_for_check(*_args, **_kwargs):
        """模拟慢文件系统检查，直到调用方取消。"""
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("app.chain.media.path.run_in_threadpool", wait_for_check)

    async def exercise_cancellation():
        """在同一事件循环中取消正在等待文件检查的调用。"""
        task = asyncio.create_task(
            media_chain._async_music_album_dir_fallback(tmp_path / "track.flac")
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise_cancellation())


def test_recognize_album_directory_skips_single_file(tmp_path, media_chain, monkeypatch):
    """单文件目录不走专辑匹配，交给单曲识别链路。"""
    album_dir = tmp_path / "单曲"
    album_dir.mkdir()
    (album_dir / "晴天.wav").write_bytes(b"RIFF")

    def fake_run_module(method, **kwargs):
        raise AssertionError("单文件目录不应触发专辑匹配")

    monkeypatch.setattr(media_chain, "_match_music_album_directory", fake_run_module)

    assert media_chain.recognize_music_album_directory(album_dir) == {}


def test_recognize_album_directory_invalidates_cache_after_same_count_rename(
        tmp_path,
        media_chain,
        monkeypatch,
):
    """目录内文件数量不变但文件名变化时，专辑曲目映射缓存必须失效。"""
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    first = album_dir / "01 - First.wav"
    second = album_dir / "02 - Second.wav"
    first.write_bytes(b"RIFF")
    second.write_bytes(b"RIFF")
    calls = []

    def fake_match(_dir_path, files):
        """记录目录匹配输入，返回空映射以专注验证缓存签名。"""
        calls.append([file.name for file in files])
        return {}

    monkeypatch.setattr(media_chain, "_match_music_album_directory", fake_match)

    media_chain.recognize_music_album_directory(album_dir)
    first.rename(album_dir / "01 - Renamed.wav")
    media_chain.recognize_music_album_directory(album_dir)

    assert calls == [
        ["01 - First.wav", "02 - Second.wav"],
        ["01 - Renamed.wav", "02 - Second.wav"],
    ]


def test_recognize_album_directory_invalidates_cache_after_content_change(
        tmp_path,
        media_chain,
        monkeypatch,
):
    """同名文件内容变化时应通过大小和纳秒时间戳使目录缓存失效。"""
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    files = [album_dir / "01.wav", album_dir / "02.wav"]
    for file in files:
        file.write_bytes(b"RIFF")
    calls = 0

    def fake_match(_directory, _files):
        """记录缓存未命中的目录识别次数。"""
        nonlocal calls
        calls += 1
        return {}

    monkeypatch.setattr(media_chain, "_match_music_album_directory", fake_match)

    media_chain.recognize_music_album_directory(album_dir)
    files[0].write_bytes(b"RIFF-data")
    os.utime(files[0], ns=(files[0].stat().st_atime_ns, files[0].stat().st_mtime_ns + 1))
    media_chain.recognize_music_album_directory(album_dir)

    assert calls == 2


def test_album_directory_cache_returns_isolated_results(tmp_path, media_chain, monkeypatch):
    """调用方修改识别结果不得污染后续目录缓存命中。"""
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    files = [album_dir / "01.wav", album_dir / "02.wav"]
    for file in files:
        file.write_bytes(b"RIFF")
    canonical = MusicInfo(media_source="musicbrainz", media_id="track-1", title="Original")
    monkeypatch.setattr(
        media_chain,
        "_match_music_album_directory",
        Mock(return_value={str(files[0].resolve()): canonical}),
    )

    first = media_chain.recognize_music_album_directory(album_dir)
    first[str(files[0].resolve())].title = "Mutated"
    second = media_chain.recognize_music_album_directory(album_dir)

    assert second[str(files[0].resolve())].title == "Original"


def test_album_directory_cache_keeps_symbolic_link_directory_aliases_distinct(
    tmp_path, media_chain, monkeypatch
):
    """不同符号链接目录名提供不同专辑线索时不得共享同步缓存结果。"""
    physical = tmp_path / "physical"
    physical.mkdir()
    for name in ("01.wav", "02.wav"):
        (physical / name).write_bytes(b"RIFF")
    first_alias = tmp_path / "Album A"
    second_alias = tmp_path / "Album B"
    first_alias.symlink_to(physical, target_is_directory=True)
    second_alias.symlink_to(physical, target_is_directory=True)
    calls = []

    def fake_match(directory, files):
        """用目录别名生成结果，暴露错误共享物理路径缓存的行为。"""
        calls.append(directory.name)
        return {
            str(files[0].resolve()): MusicInfo(
                media_source="musicbrainz",
                media_id=directory.name,
                title=directory.name,
            )
        }

    monkeypatch.setattr(media_chain, "_match_music_album_directory", fake_match)

    first = media_chain.recognize_music_album_directory(first_alias)
    second = media_chain.recognize_music_album_directory(second_alias)

    assert next(iter(first.values())).title == "Album A"
    assert next(iter(second.values())).title == "Album B"
    assert calls == ["Album A", "Album B"]


@pytest.mark.asyncio
async def test_async_album_directory_cache_keeps_symbolic_link_aliases_distinct(
    tmp_path, media_chain, monkeypatch
):
    """异步目录识别同样必须按符号链接别名隔离专辑缓存。"""
    physical = tmp_path / "physical"
    physical.mkdir()
    for name in ("01.wav", "02.wav"):
        (physical / name).write_bytes(b"RIFF")
    first_alias = tmp_path / "Album A"
    second_alias = tmp_path / "Album B"
    first_alias.symlink_to(physical, target_is_directory=True)
    second_alias.symlink_to(physical, target_is_directory=True)
    calls = []

    async def fake_match(directory, files):
        """用目录别名生成异步结果，验证两个别名分别执行。"""
        calls.append(directory.name)
        return {
            str(files[0].resolve()): MusicInfo(
                media_source="musicbrainz",
                media_id=directory.name,
                title=directory.name,
            )
        }

    monkeypatch.setattr(media_chain, "_async_match_music_album_directory", fake_match)

    first = await media_chain.async_recognize_music_album_directory(first_alias)
    second = await media_chain.async_recognize_music_album_directory(second_alias)

    assert next(iter(first.values())).title == "Album A"
    assert next(iter(second.values())).title == "Album B"
    assert calls == ["Album A", "Album B"]


def test_album_directory_cache_evicts_only_least_recently_used_entry():
    """缓存满额时只淘汰最久未使用目录，避免全量缓存断崖。"""
    cache = AlbumDirectoryCache(capacity=2)
    signature = (("01.wav", 4, 1),)
    cache.put("first", signature, {})
    cache.put("second", signature, {})
    assert cache.get("first", signature) == {}

    cache.put("third", signature, {})

    assert cache.get("first", signature) == {}
    assert cache.get("second", signature) is None
    assert cache.get("third", signature) == {}


def test_album_directory_cache_singleflights_concurrent_sync_resolvers():
    """同一目录的并发同步未命中只允许首个解析器执行。"""
    cache = AlbumDirectoryCache(capacity=2)
    signature = (("01.wav", 4, 1),)
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def resolver():
        """阻塞首个解析器，让第二个调用进入单飞等待。"""
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=2)
        return {}

    results = []
    first = threading.Thread(target=lambda: results.append(cache.resolve("album", signature, resolver)))
    second = threading.Thread(target=lambda: results.append(cache.resolve("album", signature, resolver)))
    first.start()
    assert started.wait(timeout=2)
    second.start()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert calls == 1
    assert results == [{}, {}]


@pytest.mark.asyncio
async def test_album_directory_cache_isolates_shared_flight_from_follower_cancellation():
    """取消一个异步等待者不得取消 leader 或其他等待者共享的解析凭据。"""
    cache = AlbumDirectoryCache(capacity=2)
    signature = (("01.wav", 4, 1),)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def resolver():
        """保持 leader 运行，直到两个 follower 都进入共享等待。"""
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {}

    leader = asyncio.create_task(cache.async_resolve("album", signature, resolver))
    await started.wait()
    cancelled_follower = asyncio.create_task(
        cache.async_resolve("album", signature, resolver)
    )
    surviving_follower = asyncio.create_task(
        cache.async_resolve("album", signature, resolver)
    )
    await asyncio.sleep(0)

    cancelled_follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_follower
    release.set()

    assert await leader == {}
    assert await surviving_follower == {}
    assert calls == 1
    assert cache.get("album", signature) == {}


def test_recognize_music_by_path_falls_back_to_album_match(tmp_path, monkeypatch):
    """单曲识别无远端身份时应用目录级匹配结果兜底。"""
    album_dir = tmp_path / "周杰伦 - 七里香 (2004)"
    album_dir.mkdir()
    file = album_dir / "01.我的地盘.wav"
    file.write_bytes(b"RIFF")

    matched_info = MusicInfo(
        media_source="musicbrainz",
        media_id="rec-1",
        title="我的地盘",
        artists=["周杰伦"],
        album="七里香",
        track_number=1,
    )
    # recognize_music_by_path 已下沉至 MediaChain 统一识别入口
    chain = MediaChain()
    monkeypatch.setattr(chain, "recognize_media", lambda **kwargs: None)
    monkeypatch.setattr(
        chain,
        "_music_album_dir_fallback",
        lambda path: matched_info,
    )

    meta, info = chain.recognize_music_by_path(file)

    assert info.media_id == "rec-1"
    assert info.title == "我的地盘"
    assert info.album == "七里香"
    # 本地音频参数应保留在识别结果中
    assert meta.audio_format == "WAV"


def test_async_music_album_fallback_calls_async_directory_match(tmp_path, monkeypatch):
    """异步路径回退应等待目录异步识别，不得调用同步目录识别。"""
    album_dir = tmp_path / "七里香"
    album_dir.mkdir()
    file = album_dir / "01.我的地盘.wav"
    file.write_bytes(b"RIFF")
    matched_info = MusicInfo(
        media_source="musicbrainz",
        media_id="rec-1",
        title="我的地盘",
    )
    async_recognize = AsyncMock(
        return_value={str(file.resolve()): matched_info}
    )
    sync_recognize = Mock(side_effect=AssertionError("异步回退不应调用同步目录识别"))
    monkeypatch.setattr(
        MediaChain,
        "async_recognize_music_album_directory",
        async_recognize,
    )
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_album_directory",
        sync_recognize,
    )

    result = asyncio.run(MediaChain()._async_music_album_dir_fallback(file))

    assert result is matched_info
    async_recognize.assert_awaited_once_with(album_dir)
    sync_recognize.assert_not_called()
