#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
TemplateContextBuilder 的并发安全单元测试。

历史上 builder 持有 ``self._context`` 实例字段，``build()`` 内 ``clear()`` →
``_add_*`` → 推导式返回这一序列在 ``TRANSFER_THREADS > 1`` 下会被多线程相互
覆盖，导致同一 builder 实例并发调用产生互相串味的 rename_dict。本测试在多
线程下连续调用 ``build()``，校验每个线程拿到的字典只反映自己的入参。
"""
import threading

from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.domain.meta.metamusic import MetaMusic
from app.application.messaging.message import TemplateContextBuilder
from app.application.transferhandler import TransHandler
from app.schemas.types import MediaSource, MediaType
from app.schemas.tmdb import TmdbEpisode


THREAD_COUNT = 8
ITERATIONS_PER_THREAD = 200


def _build_fake_meta():
    """
    构造模板上下文测试所需的最小元数据对象。
    """
    meta = type("FakeMeta", (), {})()
    meta.begin_episode = None
    meta.title = "Movie.2024.1080p.x265.10bit.mkv"
    meta.name = "Movie"
    meta.en_name = "Movie"
    meta.year = "2024"
    meta.season_seq = ""
    meta.season = ""
    meta.episode_seqs = ""
    meta.episode = ""
    meta.part = None
    meta.customization = None
    meta.fps = None
    meta.resource_type = None
    meta.resource_effect = None
    meta.edition = ""
    meta.resource_pix = "1080p"
    meta.resource_term = "1080p"
    meta.resource_team = None
    meta.video_encode = "x265 10bit"
    meta.video_bit = "10bit"
    meta.audio_encode = "AAC"
    meta.web_source = None
    return meta


def test_concurrent_build_no_cross_contamination() -> None:
    """
    使用 8 个线程并发调用同一 TemplateContextBuilder 实例的 build()，
    确保各自的 file_extension / 自定义 kwargs 不会被其它线程覆盖。
    """
    builder = TemplateContextBuilder()
    errors = []

    def worker(tag: int) -> None:
        try:
            for _ in range(ITERATIONS_PER_THREAD):
                ctx = builder.build(
                    file_extension=f".{tag}",
                    marker=tag,
                )
                assert ctx.get("fileExt") == f".{tag}"
                assert ctx.get("marker") == tag
        except AssertionError as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i,), name=f"builder-{i}")
        for i in range(THREAD_COUNT)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"检测到并发串味，共 {len(errors)} 条；首个错误：{errors[0] if errors else ''}"


def test_build_returns_independent_dicts() -> None:
    """
    连续两次 build() 应返回相互独立的 dict 实例，避免调用方误用共享结果。
    """
    builder = TemplateContextBuilder()
    first = builder.build(file_extension=".a", marker=1)
    second = builder.build(file_extension=".b", marker=2)

    assert first is not second
    assert first.get("fileExt") == ".a"
    assert second.get("fileExt") == ".b"
    assert first.get("marker") == 1


def test_build_exposes_video_bit_from_meta() -> None:
    """
    模板上下文应提供独立 videoBit 字段，避免用户只能从 videoCodec 中手工拆位深。
    """
    context = TemplateContextBuilder().build(meta=_build_fake_meta())

    assert context.get("videoCodec") == "x265 10bit"
    assert context.get("videoBit") == "10bit"


def test_build_exposes_total_episodes_from_current_season() -> None:
    """
    模板上下文应提供当前季总集数，供入库通知模板直接引用。
    """
    context = TemplateContextBuilder().build(
        meta=_build_fake_meta(),
        episodes_info=[
            TmdbEpisode(episode_number=1, name="第一集"),
            TmdbEpisode(episode_number=2, name="第二集"),
            TmdbEpisode(episode_number=3, name="第三集"),
        ],
    )

    assert context.get("total_episodes") == 3


def test_build_preserves_special_season_context() -> None:
    """显式 S00 必须优先于媒体回退季，并使用特别季年份。"""
    meta = MetaInfo("Test Show S00E01")
    mediainfo = MediaInfo(
        title="Test Show",
        type=MediaType.TV,
        season=1,
        season_years={0: "2024", 1: "2025"},
    )

    context = TemplateContextBuilder().build(meta=meta, mediainfo=mediainfo)

    assert context["season"] == "0"
    assert context["season_fmt"] == "S00"
    assert context["season_year"] == "2024"


def test_file_rename_context_keeps_source_specific_id_variables() -> None:
    """文件重命名沿用原专用ID变量，不暴露统一身份字段。"""
    mediainfo = MediaInfo(
        media_source=MediaSource.AniList,
        media_id="170942",
        type=MediaType.TV,
        title="测试动画",
        tmdb_id=24680,
        imdb_id="tt1234567",
        douban_id="35000000",
        bangumi_id=499390,
        anilist_id=170942,
    )

    context = TransHandler.get_naming_dict(
        meta=MetaInfo("Test.Show.S01E01"),
        mediainfo=mediainfo,
    )

    assert context["tmdbid"] == 24680
    assert context["imdbid"] == "tt1234567"
    assert context["doubanid"] == "35000000"
    assert context["bangumiid"] == 499390
    assert context["anilistid"] == 170942
    assert "media_source" not in context
    assert "media_id" not in context


def test_build_exposes_music_audio_specs_for_notifications() -> None:
    """下载和整理通知上下文应包含格式化音质及可独立引用的技术参数。"""
    meta = MetaMusic(
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        track_number=3,
        audio_format="FLAC",
        bit_depth=24,
        sample_rate=96000,
        bitrate=2304000,
    )

    context = TemplateContextBuilder().build(meta=meta)

    assert context["audio_quality"] == "hires"
    assert context["audio_specs"] == "FLAC · 24-bit · 96 kHz · 2,304 kbps"
    assert context["bitrate_kbps"] == 2304
    assert context["sample_rate_khz"] == "96"


def _build_music_context(album, year):
    """
    构造音乐重命名上下文：标签专辑名带年份、文件/目录年份可独立渲染的场景。
    """
    meta = MetaMusic(
        title="欲望反光",
        album=album,
        artists=["萧敬腾"],
        album_artist="萧敬腾",
        year=year,
        track_number=1,
        audio_format="FLAC",
    )
    return TemplateContextBuilder().build(meta=meta)


def test_music_rename_strips_duplicate_album_year() -> None:
    """专辑名尾部的年份标记不应与模板追加的年份重复（issue #6355）。"""
    context = _build_music_context(album="欲望反光 (2018)", year=2018)

    assert context["album"] == "欲望反光"
    assert context["year"] == 2018


def test_music_rename_strips_album_year_different_from_media_year() -> None:
    """标签专辑名年份与识别年份不一致时，仅保留模板追加的识别年份。"""
    context = _build_music_context(
        album="洛克先生Mr.Rock演唱会Live纪实 (2010)", year=2009
    )

    assert context["album"] == "洛克先生Mr.Rock演唱会Live纪实"
    assert context["year"] == 2009


def test_music_rename_strips_repeated_album_year_suffixes() -> None:
    """历史整理已生成的重复年份目录再次重命名时，所有尾部年份都应被剥离。"""
    context = _build_music_context(album="爱的时刻自选辑 (2009) (2009)", year=2015)

    assert context["album"] == "爱的时刻自选辑"
    assert context["year"] == 2015


def test_music_rename_keeps_album_year_when_no_standalone_year() -> None:
    """没有独立年份可渲染时保留专辑名自带的年份，避免信息丢失。"""
    context = _build_music_context(album="欲望反光 (2018)", year=None)

    assert context["album"] == "欲望反光 (2018)"


def test_music_rename_keeps_plain_album_title() -> None:
    """不含年份的普通专辑名不受影响。"""
    context = _build_music_context(album="叶惠美", year=2003)

    assert context["album"] == "叶惠美"
