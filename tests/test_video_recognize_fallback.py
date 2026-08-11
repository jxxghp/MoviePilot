"""影视自动识别的 TMDB 优先和多源兜底测试。"""

import asyncio
from threading import Barrier

from app.chain import ChainBase
from app.chain.media import MediaChain
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.schemas.types import MediaType


def _video_meta(
        title: str = "流浪地球",
        year: str = "2019",
        mtype: MediaType = MediaType.MOVIE,
) -> MetaInfo:
    """构造包含明确标题、年份和类型的影视解析信息。"""
    meta = MetaInfo(title)
    meta.year = year
    meta.type = mtype
    return meta


def _video_info(
        source: str,
        media_id: int | str,
        title: str = "流浪地球",
        year: str = "2019",
        mtype: MediaType = MediaType.MOVIE,
        **kwargs,
) -> MediaInfo:
    """构造带指定内置来源原生身份的标准影视信息。"""
    identity_fields = {
        "themoviedb": {"tmdb_id": int(media_id)},
        "douban": {"douban_id": str(media_id)},
        "bangumi": {"bangumi_id": int(media_id)},
        "anilist": {"anilist_id": int(media_id)},
    }
    return MediaInfo(
        source=source,
        type=mtype,
        title=title,
        year=year,
        **identity_fields[source],
        **kwargs,
    )


def _module_kwargs(meta: MetaInfo, source: str | None = None) -> dict:
    """构造原生识别路由需要的最小参数。"""
    return {
        "meta": meta,
        "mtype": meta.type,
        "source": source,
        "mediaid": None,
        "tmdbid": None,
        "doubanid": None,
        "bangumiid": None,
        "anilistid": None,
        "episode_group": None,
        "cache": True,
    }


def test_video_auto_recognize_stops_after_reliable_tmdb(monkeypatch) -> None:
    """TMDB 可靠命中时不得查询任何影视副源。"""
    chain = object.__new__(MediaChain)
    meta = _video_meta()
    tmdb = _video_info("themoviedb", 550, names=["The Wandering Earth"])
    calls = []

    def recognize_source(module_kwargs, source, cache):
        """记录单源调用并仅返回 TMDB 测试结果。"""
        calls.append((module_kwargs, source, cache))
        return tmdb if source == "themoviedb" else None

    monkeypatch.setattr(chain, "_recognize_video_from_source", recognize_source)

    result = chain._run_native_media_recognize(_module_kwargs(meta), cache=True)

    assert result is tmdb
    assert [call[1] for call in calls] == ["themoviedb"]


def test_video_auto_recognize_concurrently_scores_fallback_sources(monkeypatch) -> None:
    """TMDB 低置信时应并发查询全部副源，并返回评分最高的候选。"""
    chain = object.__new__(MediaChain)
    meta = _video_meta()
    fallback_barrier = Barrier(3, timeout=2)
    candidates = {
        "douban": _video_info("douban", "26266893"),
        "bangumi": _video_info(
            "bangumi", 302875, mtype=MediaType.TV
        ),
        "anilist": _video_info(
            "anilist", 105333, title="流浪星球"
        ),
    }
    called_sources = []

    def recognize_source(module_kwargs, source, cache):
        """使用线程屏障证明三个副源不是串行执行。"""
        del module_kwargs, cache
        called_sources.append(source)
        if source == "themoviedb":
            return _video_info(
                "themoviedb", 999, title="完全不同的电影"
            )
        fallback_barrier.wait()
        return candidates[source]

    monkeypatch.setattr(chain, "_recognize_video_from_source", recognize_source)

    result = chain._run_native_media_recognize(_module_kwargs(meta), cache=True)

    assert result is candidates["douban"]
    assert called_sources[0] == "themoviedb"
    assert set(called_sources[1:]) == {"douban", "bangumi", "anilist"}


def test_async_video_auto_recognize_concurrently_scores_fallback_sources(
        monkeypatch,
) -> None:
    """异步自动识别应在 TMDB 失败后并发等待全部影视副源。"""
    chain = object.__new__(MediaChain)
    meta = _video_meta()
    candidates = {
        source: _video_info(source, index)
        for index, source in enumerate(("douban", "bangumi", "anilist"), start=1)
    }
    started_sources = set()
    all_started = asyncio.Event()

    async def recognize_source(module_kwargs, source, cache):
        """等三个副源均开始后再放行，串行实现会触发超时。"""
        del module_kwargs, cache
        if source == "themoviedb":
            return None
        started_sources.add(source)
        if len(started_sources) == 3:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=1)
        return candidates[source]

    monkeypatch.setattr(
        chain,
        "_async_recognize_video_from_source",
        recognize_source,
    )

    result = asyncio.run(
        chain._async_run_native_media_recognize(_module_kwargs(meta), cache=True)
    )

    assert result is candidates["douban"]
    assert started_sources == {"douban", "bangumi", "anilist"}


def test_video_candidate_score_uses_requested_season_year() -> None:
    """电视剧应使用请求季年份，而不是整部剧首播年份进行可信度判断。"""
    meta = _video_meta("测试剧", "2024", MediaType.TV)
    meta.begin_season = 2
    candidate = _video_info(
        "themoviedb",
        42,
        title="测试剧",
        year="2020",
        mtype=MediaType.TV,
        seasons={1: [1], 2: [1]},
        season_years={1: "2020", 2: "2024"},
    )

    score = MediaChain._video_candidate_score(meta, candidate, MediaType.TV)

    assert score == 100


def test_video_candidate_score_rejects_type_and_year_conflicts() -> None:
    """类型冲突或明显年份冲突必须在进入副源排名前淘汰。"""
    meta = _video_meta()
    wrong_type = _video_info(
        "bangumi", 1, mtype=MediaType.TV
    )
    wrong_year = _video_info(
        "douban", 2, year="2023"
    )

    assert MediaChain._video_candidate_score(meta, wrong_type) is None
    assert MediaChain._video_candidate_score(meta, wrong_year) is None


def test_video_explicit_source_bypasses_automatic_fallback(monkeypatch) -> None:
    """显式影视来源应继续走通用严格单源分发，不进入自动策略。"""
    chain = object.__new__(MediaChain)
    meta = _video_meta()
    expected = _video_info("douban", 26266893)
    generic_calls = []

    def generic_recognize(_self, module_kwargs, cache):
        """记录父类通用分发调用并返回显式来源结果。"""
        generic_calls.append((module_kwargs, cache))
        return expected

    def unexpected_auto(*_args, **_kwargs):
        """显式来源误入自动策略时立即使测试失败。"""
        raise AssertionError("显式来源不应进入自动影视识别")

    monkeypatch.setattr(
        ChainBase,
        "_run_native_media_recognize",
        generic_recognize,
    )
    monkeypatch.setattr(chain, "_recognize_video_best", unexpected_auto)

    result = chain._run_native_media_recognize(
        _module_kwargs(meta, source="douban"),
        cache=True,
    )

    assert result is expected
    assert generic_calls[0][0]["source"] == "douban"
