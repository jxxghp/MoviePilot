"""媒体搜索、路径识别与歌词聚合的同步异步同形回归。"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from app.chain.acoustid import AcoustIdChain
from app.chain.lyrics import LyricsChain
from app.chain.media import MediaChain
from app.domain.context import MediaInfo, MusicInfo, MusicLyrics
from app.domain.meta.metamusic import MetaMusic
from app.schemas.types import MediaSource, MediaType


def _remote_music(stage: str) -> MusicInfo:
    """构造指定回退层命中的标准远端音乐结果。"""
    return MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id=f"recording-{stage}",
        title=f"Track {stage}",
    )


@pytest.mark.parametrize("empty", [False, True])
def test_media_search_sync_async_share_projection_and_empty_result(
    monkeypatch: pytest.MonkeyPatch,
    empty: bool,
) -> None:
    """搜索双入口应共享要素投影，并将所有空结果规范为新列表。"""
    chain = MediaChain()
    source = (MediaSource.TMDB,)
    medias = [] if empty else [MediaInfo(type=MediaType.TV, title="Result")]
    sync_search = Mock(return_value=medias or None)
    async_search = AsyncMock(return_value=medias or None)
    monkeypatch.setattr(
        "app.chain.media.search.title_rules.parse_search_keyword",
        Mock(
            return_value=(
                MediaType.TV,
                "ignored",
                2,
                3,
                2024,
                "Projected Title",
            )
        ),
    )
    monkeypatch.setattr(MediaChain, "search_medias", sync_search)
    monkeypatch.setattr(MediaChain, "async_search_medias", async_search)

    sync_meta, sync_result = chain.search("raw title", media_source=source)
    async_meta, async_result = asyncio.run(chain.async_search("raw title", media_source=source))

    assert sync_meta is not None and async_meta is not None
    assert (
        (
            sync_meta.name,
            sync_meta.type,
            sync_meta.begin_season,
            sync_meta.begin_episode,
            sync_meta.year,
        )
        == (
            async_meta.name,
            async_meta.type,
            async_meta.begin_season,
            async_meta.begin_episode,
            async_meta.year,
        )
        == ("Projected Title", MediaType.TV, 2, 3, 2024)
    )
    assert sync_result == async_result == medias
    assert (sync_result is not async_result) is empty
    assert sync_search.call_args.kwargs["media_source"] == source
    assert async_search.await_args.kwargs["media_source"] == source


def test_media_search_sync_async_propagate_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """搜索提供方异常不得被任一入口伪装为空结果。"""
    chain = MediaChain()
    monkeypatch.setattr(
        MediaChain,
        "search_medias",
        Mock(side_effect=RuntimeError("sync search failed")),
    )
    monkeypatch.setattr(
        MediaChain,
        "async_search_medias",
        AsyncMock(side_effect=RuntimeError("async search failed")),
    )

    with pytest.raises(RuntimeError, match="sync search failed"):
        chain.search("Title")
    with pytest.raises(RuntimeError, match="async search failed"):
        asyncio.run(chain.async_search("Title"))


@pytest.mark.parametrize(
    ("hit_stage", "expected_order"),
    [
        ("fingerprint", ["fingerprint"]),
        ("tag", ["fingerprint", "tag"]),
        ("filename", ["fingerprint", "tag", "filename"]),
        ("album", ["fingerprint", "tag", "filename", "album"]),
        (None, ["fingerprint", "tag", "filename", "album"]),
    ],
)
def test_music_path_sync_async_follow_one_fallback_state_machine(
    monkeypatch: pytest.MonkeyPatch,
    hit_stage: str | None,
    expected_order: list[str],
) -> None:
    """路径识别双入口应在同一层终止，并保持每层调用次数一致。"""
    chain = MediaChain()
    path = Path("Track.flac")
    merged = MetaMusic(title="Track", audio_format="FLAC")
    tag_meta = MetaMusic(title="Tagged")
    filename_meta = MetaMusic(title="Filename")
    sync_order: list[str] = []
    async_order: list[str] = []
    monkeypatch.setattr(
        "app.chain.media.path.AudioMetadataHelper.read_evidence",
        Mock(return_value=(merged, tag_meta, filename_meta)),
    )

    def sync_fingerprint(_self: AcoustIdChain, _path: Path) -> str | None:
        """记录同步指纹层并按场景返回 Recording ID。"""
        sync_order.append("fingerprint")
        return "fingerprint-id" if hit_stage == "fingerprint" else None

    async def async_fingerprint(_self: AcoustIdChain, _path: Path) -> str | None:
        """记录异步指纹层并按场景返回 Recording ID。"""
        async_order.append("fingerprint")
        return "fingerprint-id" if hit_stage == "fingerprint" else None

    def sync_tier(
        _chain: MediaChain,
        *,
        meta: MetaMusic,
        media_source: MediaSource | None,
        tier_name: str,
    ):
        """模拟同步标签或文件名层的远端命中。"""
        del meta, media_source
        stage = "tag" if tier_name == "文件标签" else "filename"
        sync_order.append(stage)
        return _remote_music(stage) if hit_stage == stage else None

    async def async_tier(
        _chain: MediaChain,
        *,
        meta: MetaMusic,
        media_source: MediaSource | None,
        tier_name: str,
    ):
        """模拟异步标签或文件名层的远端命中。"""
        del meta, media_source
        stage = "tag" if tier_name == "文件标签" else "filename"
        async_order.append(stage)
        return _remote_music(stage) if hit_stage == stage else None

    def sync_album(_chain: MediaChain, _path: Path) -> MusicInfo | None:
        """记录同步专辑目录兜底。"""
        sync_order.append("album")
        return _remote_music("album") if hit_stage == "album" else None

    async def async_album(_chain: MediaChain, _path: Path) -> MusicInfo | None:
        """记录异步专辑目录兜底。"""
        async_order.append("album")
        return _remote_music("album") if hit_stage == "album" else None

    monkeypatch.setattr(AcoustIdChain, "identify_music_by_fingerprint", sync_fingerprint)
    monkeypatch.setattr(AcoustIdChain, "async_identify_music_by_fingerprint", async_fingerprint)
    monkeypatch.setattr(
        MediaChain,
        "_recognize_musicbrainz_recording",
        Mock(return_value=_remote_music("fingerprint")),
    )
    monkeypatch.setattr(
        MediaChain,
        "_async_recognize_musicbrainz_recording",
        AsyncMock(return_value=_remote_music("fingerprint")),
    )
    monkeypatch.setattr(MediaChain, "_recognize_music_meta_tier", sync_tier)
    monkeypatch.setattr(MediaChain, "_async_recognize_music_meta_tier", async_tier)
    monkeypatch.setattr(MediaChain, "_music_album_dir_fallback", sync_album)
    monkeypatch.setattr(MediaChain, "_async_music_album_dir_fallback", async_album)

    _, sync_info = chain.recognize_music_by_path(path)
    _, async_info = asyncio.run(chain.async_recognize_music_by_path(path))

    assert sync_order == async_order == expected_order
    expected_id = f"recording-{hit_stage}" if hit_stage else None
    assert sync_info.media_id == async_info.media_id == expected_id
    assert sync_info.audio_format == async_info.audio_format == "FLAC"


@pytest.mark.parametrize(
    ("failed_stage", "expected_order"),
    [
        ("fingerprint", ["fingerprint"]),
        ("tag", ["fingerprint", "tag"]),
        ("filename", ["fingerprint", "tag", "filename"]),
    ],
)
def test_music_path_sync_async_stop_at_same_failing_tier(
    monkeypatch: pytest.MonkeyPatch,
    failed_stage: str,
    expected_order: list[str],
) -> None:
    """指纹、标签或文件名 I/O 异常时双入口应在相同层停止。"""
    chain = MediaChain()
    meta = MetaMusic(title="Track")
    sync_order: list[str] = []
    async_order: list[str] = []
    monkeypatch.setattr(
        "app.chain.media.path.AudioMetadataHelper.read_evidence",
        Mock(return_value=(meta, meta, meta)),
    )

    def sync_fingerprint(_self: AcoustIdChain, _path: Path) -> None:
        """记录同步指纹动作并按场景抛错。"""
        sync_order.append("fingerprint")
        if failed_stage == "fingerprint":
            raise RuntimeError("sync fingerprint failed")

    async def async_fingerprint(_self: AcoustIdChain, _path: Path) -> None:
        """记录异步指纹动作并按场景抛错。"""
        async_order.append("fingerprint")
        if failed_stage == "fingerprint":
            raise RuntimeError("async fingerprint failed")

    def sync_tier(
        _chain: MediaChain,
        *,
        meta: MetaMusic,
        media_source: MediaSource | None,
        tier_name: str,
    ):
        """记录同步元数据层并在指定层抛错。"""
        del meta, media_source
        stage = "tag" if tier_name == "文件标签" else "filename"
        sync_order.append(stage)
        if failed_stage == stage:
            raise RuntimeError(f"sync {stage} failed")
        return None

    async def async_tier(
        _chain: MediaChain,
        *,
        meta: MetaMusic,
        media_source: MediaSource | None,
        tier_name: str,
    ):
        """记录异步元数据层并在指定层抛错。"""
        del meta, media_source
        stage = "tag" if tier_name == "文件标签" else "filename"
        async_order.append(stage)
        if failed_stage == stage:
            raise RuntimeError(f"async {stage} failed")
        return None

    monkeypatch.setattr(AcoustIdChain, "identify_music_by_fingerprint", sync_fingerprint)
    monkeypatch.setattr(AcoustIdChain, "async_identify_music_by_fingerprint", async_fingerprint)
    monkeypatch.setattr(MediaChain, "_recognize_music_meta_tier", sync_tier)
    monkeypatch.setattr(MediaChain, "_async_recognize_music_meta_tier", async_tier)

    with pytest.raises(RuntimeError, match=f"sync {failed_stage} failed"):
        chain.recognize_music_by_path("Track.flac")
    with pytest.raises(RuntimeError, match=f"async {failed_stage} failed"):
        asyncio.run(chain.async_recognize_music_by_path("Track.flac"))

    assert sync_order == async_order == expected_order


@pytest.mark.parametrize(
    ("source", "direct_hit", "has_title", "expected_calls"),
    [
        (None, True, True, ["direct"]),
        (None, False, True, ["direct", "search"]),
        (MediaSource.TMDB, False, True, ["search"]),
        (None, False, False, ["direct"]),
    ],
)
def test_music_meta_tier_sync_async_share_direct_and_search_decisions(
    monkeypatch: pytest.MonkeyPatch,
    source: MediaSource | None,
    direct_hit: bool,
    has_title: bool,
    expected_calls: list[str],
) -> None:
    """标签 MBID 直查、身份清理和标题回退在双入口中必须同形。"""
    chain = MediaChain()
    meta = MetaMusic(
        title="Tagged" if has_title else None,
        media_source=MediaSource.MusicBrainz,
        media_id="recording-tagged",
    )
    sync_calls: list[str] = []
    async_calls: list[str] = []
    direct_result = _remote_music("direct") if direct_hit else None
    search_result = _remote_music("search")

    def sync_direct(_chain: MediaChain, **_kwargs) -> MusicInfo | None:
        """记录同步 MBID 直查。"""
        sync_calls.append("direct")
        return direct_result

    async def async_direct(_chain: MediaChain, **_kwargs) -> MusicInfo | None:
        """记录异步 MBID 直查。"""
        async_calls.append("direct")
        return direct_result

    def sync_search(_chain: MediaChain, **kwargs) -> MusicInfo:
        """记录同步标题搜索并验证身份已清理。"""
        sync_calls.append("search")
        assert kwargs["meta"].media_source is None
        assert kwargs["meta"].media_id is None
        return search_result

    async def async_search(_chain: MediaChain, **kwargs) -> MusicInfo:
        """记录异步标题搜索并验证身份已清理。"""
        async_calls.append("search")
        assert kwargs["meta"].media_source is None
        assert kwargs["meta"].media_id is None
        return search_result

    monkeypatch.setattr(MediaChain, "_recognize_musicbrainz_recording", sync_direct)
    monkeypatch.setattr(MediaChain, "_async_recognize_musicbrainz_recording", async_direct)
    monkeypatch.setattr(MediaChain, "recognize_media", sync_search)
    monkeypatch.setattr(MediaChain, "async_recognize_media", async_search)

    sync_result = chain._recognize_music_meta_tier(meta, source, "文件标签")
    async_result = asyncio.run(chain._async_recognize_music_meta_tier(meta, source, "文件标签"))

    assert sync_calls == async_calls == expected_calls
    expected = direct_result if direct_hit else (search_result if has_title else None)
    assert sync_result is expected
    assert async_result is expected


def test_music_album_fallback_sync_async_isolate_directory_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """专辑目录识别异常在同步和异步入口都应降级为空结果。"""
    path = tmp_path / "Track.flac"
    path.write_bytes(b"audio")
    chain = MediaChain()
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_album_directory",
        Mock(side_effect=RuntimeError("sync album failed")),
    )
    monkeypatch.setattr(
        MediaChain,
        "async_recognize_music_album_directory",
        AsyncMock(side_effect=RuntimeError("async album failed")),
    )

    assert chain._music_album_dir_fallback(path) is None
    assert asyncio.run(chain._async_music_album_dir_fallback(path)) is None


@pytest.mark.parametrize("recognized", [False, True])
def test_video_path_sync_async_share_route_and_failure_context(
    monkeypatch: pytest.MonkeyPatch,
    recognized: bool,
) -> None:
    """影视路径双入口应共享路由参数，并在失败时都保留元数据 Context。"""
    chain = MediaChain()
    info = MediaInfo(type=MediaType.MOVIE, title="Movie") if recognized else None
    sync_recognize = Mock(return_value=info)
    async_recognize = AsyncMock(return_value=info)
    monkeypatch.setattr(MediaChain, "_is_music_path_request", Mock(return_value=False))
    monkeypatch.setattr(MediaChain, "_recognize_with_fallback_by_meta", sync_recognize)
    monkeypatch.setattr(MediaChain, "_async_recognize_with_fallback_by_meta", async_recognize)

    sync_context = chain.recognize_by_path(
        "Movie.2024.mkv",
        media_source=MediaSource.TMDB,
        episode_group="group-1",
        obtain_images=True,
    )
    async_context = asyncio.run(
        chain.async_recognize_by_path(
            "Movie.2024.mkv",
            media_source=MediaSource.TMDB,
            episode_group="group-1",
            obtain_images=True,
        )
    )

    assert sync_context is not None and async_context is not None
    assert sync_context.media_info is async_context.media_info is info
    assert sync_context.meta_info.title == async_context.meta_info.title
    for call in (sync_recognize.call_args, async_recognize.await_args):
        assert call.kwargs["media_source"] == MediaSource.TMDB
        assert call.kwargs["episode_group"] == "group-1"
        assert call.kwargs["obtain_images"] is True


def test_music_path_route_sync_async_skip_video_recognition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """音乐路由双入口应直接返回音乐 Context，且不调用影视识别。"""
    chain = MediaChain()
    meta = MetaMusic(title="Track")
    info = _remote_music("route")
    sync_music = Mock(return_value=(meta, info))
    async_music = AsyncMock(return_value=(meta, info))
    sync_video = Mock()
    async_video = AsyncMock()
    monkeypatch.setattr(MediaChain, "_is_music_path_request", Mock(return_value=True))
    monkeypatch.setattr(MediaChain, "recognize_music_by_path", sync_music)
    monkeypatch.setattr(MediaChain, "async_recognize_music_by_path", async_music)
    monkeypatch.setattr(MediaChain, "_recognize_with_fallback_by_meta", sync_video)
    monkeypatch.setattr(MediaChain, "_async_recognize_with_fallback_by_meta", async_video)

    sync_context = chain.recognize_by_path("Track.flac", media_source=MediaSource.MusicBrainz)
    async_context = asyncio.run(chain.async_recognize_by_path("Track.flac", media_source=MediaSource.MusicBrainz))

    assert sync_context is not None and async_context is not None
    assert sync_context.meta_info is async_context.meta_info is meta
    assert sync_context.media_info is async_context.media_info is info
    sync_music.assert_called_once_with("Track.flac", media_source=MediaSource.MusicBrainz)
    async_music.assert_awaited_once_with("Track.flac", media_source=MediaSource.MusicBrainz)
    sync_video.assert_not_called()
    async_video.assert_not_awaited()


def test_lyrics_sync_async_share_deadline_fallback_dedup_and_call_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """歌词双入口应按旧、新、本地顺序聚合，并稳定替换重复候选。"""
    low = MusicLyrics(provider="same", plain_lyrics="duplicate", match_score=10)
    high = MusicLyrics(provider="same", plain_lyrics="duplicate", match_score=90)
    new = MusicLyrics(provider="new", synced_lyrics="[00:01]new", match_score=80)
    music = MusicInfo(title="Track", lyrics="fallback")
    sync_chain = LyricsChain()
    async_chain = LyricsChain()
    sync_calls: list[str] = []
    async_calls: list[str] = []

    def sync_module(method: str, **_kwargs):
        """记录同步歌词接口顺序并返回对应候选。"""
        sync_calls.append(method)
        return high if method == "music_lyrics" else [new]

    async def async_module(method: str, **_kwargs):
        """记录异步歌词接口顺序并返回对应候选。"""
        async_calls.append(method)
        return high if method == "music_lyrics" else [new]

    monkeypatch.setattr(sync_chain, "run_module", sync_module)
    monkeypatch.setattr(async_chain, "async_run_module", async_module)

    sync_result = sync_chain.get_music_lyrics_candidates(music, [low])
    async_result = asyncio.run(async_chain.async_get_music_lyrics_candidates(music, [low]))

    assert sync_calls == async_calls == ["music_lyrics", "music_lyrics_candidates"]
    assert [item.provider for item in sync_result] == ["same", "new", "theaudiodb"]
    assert [item.provider for item in async_result] == ["same", "new", "theaudiodb"]
    assert sync_result[0] is async_result[0] is high


def test_lyrics_sync_async_deadline_skips_modules_and_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预算耗尽时双入口只返回现有本地候选，不再调用模块或追加来源兜底。"""
    monkeypatch.setattr("app.chain.lyrics.time.monotonic", Mock(return_value=10.0))
    local = MusicLyrics(provider="embedded", plain_lyrics="local")
    music = MusicInfo(title="Track", lyrics="fallback")
    sync_chain = LyricsChain(deadline=10.0)
    async_chain = LyricsChain(deadline=10.0)
    sync_run = Mock()
    async_run = AsyncMock()
    monkeypatch.setattr(sync_chain, "run_module", sync_run)
    monkeypatch.setattr(async_chain, "async_run_module", async_run)

    sync_result = sync_chain.get_music_lyrics_candidates(music, [local])
    async_result = asyncio.run(async_chain.async_get_music_lyrics_candidates(music, [local]))

    assert sync_result == async_result == [local]
    assert sync_chain.budget_exceeded is async_chain.budget_exceeded is True
    sync_run.assert_not_called()
    async_run.assert_not_awaited()


def test_lyrics_sync_async_empty_modules_without_fallback_return_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新旧接口均为空且无本地歌词时，双入口应稳定返回空列表。"""
    music = MusicInfo(title="Track")
    sync_chain = LyricsChain()
    async_chain = LyricsChain()
    sync_run = Mock(side_effect=[None, []])
    async_run = AsyncMock(side_effect=[None, []])
    monkeypatch.setattr(sync_chain, "run_module", sync_run)
    monkeypatch.setattr(async_chain, "async_run_module", async_run)

    assert sync_chain.get_music_lyrics_candidates(music) == []
    assert asyncio.run(async_chain.async_get_music_lyrics_candidates(music)) == []
    assert sync_run.call_count == async_run.await_count == 2


@pytest.mark.parametrize(
    ("failed_method", "expected_calls"),
    [
        ("music_lyrics", ["music_lyrics"]),
        (
            "music_lyrics_candidates",
            ["music_lyrics", "music_lyrics_candidates"],
        ),
    ],
)
def test_lyrics_sync_async_propagate_same_module_exception(
    monkeypatch: pytest.MonkeyPatch,
    failed_method: str,
    expected_calls: list[str],
) -> None:
    """旧或新模块接口异常时双入口应在同一步停止且不伪造兜底成功。"""
    music = MusicInfo(title="Track", lyrics="fallback")
    sync_chain = LyricsChain()
    async_chain = LyricsChain()
    sync_calls: list[str] = []
    async_calls: list[str] = []

    def sync_module(method: str, **_kwargs):
        """在指定同步歌词接口抛出异常。"""
        sync_calls.append(method)
        if method == failed_method:
            raise RuntimeError("sync failed")
        return None

    async def async_module(method: str, **_kwargs):
        """在指定异步歌词接口抛出异常。"""
        async_calls.append(method)
        if method == failed_method:
            raise RuntimeError("async failed")
        return None

    monkeypatch.setattr(sync_chain, "run_module", sync_module)
    monkeypatch.setattr(async_chain, "async_run_module", async_module)

    with pytest.raises(RuntimeError, match="sync failed"):
        sync_chain.get_music_lyrics_candidates(music)
    with pytest.raises(RuntimeError, match="async failed"):
        asyncio.run(async_chain.async_get_music_lyrics_candidates(music))

    assert sync_calls == async_calls == expected_calls
