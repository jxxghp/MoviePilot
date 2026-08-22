from types import SimpleNamespace

from app.application.orchestration.subscribe import SubscribeChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.schemas.types import MediaSource, MediaType


def _target_media(tmdb_id=106449, douban_id=None, **kwargs) -> MediaInfo:
    """构造订阅目标媒体。"""
    media_source = MediaSource.TMDB if tmdb_id is not None else MediaSource.Douban
    media_id = str(tmdb_id) if tmdb_id is not None else douban_id
    defaults = {
        "title": "凡人修仙传",
        "original_title": "凡人修仙传",
        "names": ["A Record Of A Mortals Journey To Immortality"],
        "type": MediaType.TV,
        "year": "2020",
        "season_years": {1: "2020"},
    }
    defaults.update(kwargs)
    return MediaInfo(
        media_source=media_source if media_id is not None else None,
        media_id=media_id,
        tmdb_id=tmdb_id,
        douban_id=douban_id,
        **defaults,
    )


def _candidate_media(tmdb_id=285479, douban_id=None, **kwargs) -> MediaInfo:
    """构造由 RSS 标题推断出的候选媒体。"""
    media_source = MediaSource.TMDB if tmdb_id is not None else MediaSource.Douban
    media_id = str(tmdb_id) if tmdb_id is not None else douban_id
    defaults = {
        "title": "凡人修仙传",
        "type": MediaType.TV,
        "year": "2020",
    }
    defaults.update(kwargs)
    return MediaInfo(
        media_source=media_source if media_id is not None else None,
        media_id=media_id,
        tmdb_id=tmdb_id,
        douban_id=douban_id,
        **defaults,
    )


def _torrent_meta(*, en_name="A Record Of A Mortals Journey To Immortality", tmdbid=None):
    """构造不触发外部识别的标题解析结果。"""
    return SimpleNamespace(
        media_source=MediaSource.TMDB if tmdbid else None,
        media_id=str(tmdbid) if tmdbid else None,
        cn_name="",
        en_name=en_name,
        type=MediaType.TV,
        year=None,
        org_string=f"{en_name} S01E186 2160p WEB-DL",
    )


def _context(meta, candidate, torrent) -> Context:
    """构造 RSS 候选上下文。"""
    return Context(
        meta_info=meta,
        media_info=candidate,
        torrent_info=torrent,
        resource_source="rss",
        match_source="tmdbid",
        candidate_recognized=True,
        media_info_is_target=False,
    )


def _reconcile(target, candidate, meta, torrent, context):
    """调用订阅链的候选媒体冲突复核。"""
    return SubscribeChain._SubscribeChain__reconcile_candidate_media(
        target_mediainfo=target,
        candidate_mediainfo=candidate,
        torrent_meta=meta,
        torrent_info=torrent,
        context=context,
    )


def test_inferred_tmdb_conflict_falls_back_to_strict_alias_match():
    """标题推断到重复 TMDB 条目时应允许订阅目标别名复核。"""
    target = _target_media()
    candidate = _candidate_media()
    meta = _torrent_meta()
    torrent = TorrentInfo(
        title="A Record Of A Mortals Journey To Immortality S01E186 2160p WEB-DL",
        site_name="测试站点",
        category=MediaType.TV.value,
    )
    context = _context(meta, candidate, torrent)

    result = _reconcile(target, candidate, meta, torrent, context)

    assert result is target
    assert context.media_info is target
    assert context.match_source == "title"
    assert context.candidate_recognized is False
    assert context.media_info_is_target is True


def test_no_year_alias_rejects_candidate_with_different_first_air_year():
    """无年份别名命中另一个首播年份的同名作品时应拒绝。"""
    target = _target_media(
        tmdb_id=236356,
        title="家族计划",
        original_title="가족계획",
        names=["Family Matters"],
        year="2024",
        original_language="ko",
        season_years={1: "2024"},
    )
    candidate = _candidate_media(
        tmdb_id=30161,
        title="Family Matters",
        original_title="Family Matters",
        year="2008",
        original_language="en",
    )
    meta = _torrent_meta(en_name="Family Matters")
    torrent = TorrentInfo(
        title="Family Matters S01 1080p WEBRip DD2.0 x264-TrollHD",
        site_name="测试站点",
        category=MediaType.TV.value,
    )
    context = _context(meta, candidate, torrent)

    assert _reconcile(target, candidate, meta, torrent, context) is None
    assert context.media_info is candidate
    assert context.match_source == "tmdbid"


def test_explicit_target_year_can_override_wrong_same_name_candidate():
    """资源明确携带目标年份时应允许纠正同名候选的错误识别。"""
    target = _target_media(
        tmdb_id=236356,
        title="家族计划",
        original_title="가족계획",
        names=["Family Matters"],
        year="2024",
        original_language="ko",
        season_years={1: "2024"},
    )
    candidate = _candidate_media(
        tmdb_id=30161,
        title="Family Matters",
        original_title="Family Matters",
        year="2008",
        original_language="en",
    )
    meta = _torrent_meta(en_name="Family Matters")
    meta.year = "2024"
    torrent = TorrentInfo(
        title="Family Matters 2024 S01 1080p WEB-DL",
        site_name="测试站点",
        category=MediaType.TV.value,
    )
    context = _context(meta, candidate, torrent)

    assert _reconcile(target, candidate, meta, torrent, context) is target
    assert context.media_info is target
    assert context.match_source == "title"


def test_inferred_tmdb_conflict_rejects_nonmatching_title():
    """ID 冲突且标题和别名不匹配时应继续拒绝候选。"""
    target = _target_media()
    candidate = _candidate_media()
    meta = _torrent_meta(en_name="Different Series")
    torrent = TorrentInfo(
        title="Different Series S01E186 2160p WEB-DL",
        site_name="测试站点",
        category=MediaType.TV.value,
    )
    context = _context(meta, candidate, torrent)

    assert _reconcile(target, candidate, meta, torrent, context) is None
    assert context.media_info is candidate
    assert context.match_source == "tmdbid"
    assert context.candidate_recognized is True
    assert context.media_info_is_target is False


def test_inferred_douban_conflict_falls_back_to_strict_alias_match():
    """豆瓣 ID 推断冲突时应使用同一套严格标题复核。"""
    target = _target_media(tmdb_id=None, douban_id="30170816")
    candidate = _candidate_media(tmdb_id=None, douban_id="36612345")
    meta = _torrent_meta()
    torrent = TorrentInfo(
        title="A Record Of A Mortals Journey To Immortality S01E186 2160p WEB-DL",
        site_name="测试站点",
        category=MediaType.TV.value,
    )
    context = _context(meta, candidate, torrent)

    result = _reconcile(target, candidate, meta, torrent, context)

    assert result is target
    assert context.media_info is target
    assert context.match_source == "title"


def test_explicit_tmdb_identity_keeps_strict_conflict_rejection():
    """标题显式携带媒体 ID 时不得被标题别名覆盖。"""
    target = _target_media()
    candidate = _candidate_media()
    meta = _torrent_meta(tmdbid=285479)
    torrent = TorrentInfo(
        title="A Record Of A Mortals Journey To Immortality S01E186 {tmdbid=285479}",
        site_name="测试站点",
        category=MediaType.TV.value,
    )
    context = _context(meta, candidate, torrent)

    assert _reconcile(target, candidate, meta, torrent, context) is None
    assert context.media_info is candidate
    assert context.match_source == "tmdbid"


def test_conflict_rejects_target_without_same_source_identity():
    """订阅目标缺少候选同来源 ID 时不得用标题跨来源放行。"""
    target = _target_media(tmdb_id=None)
    candidate = _candidate_media()
    meta = _torrent_meta()
    torrent = TorrentInfo(
        title="A Record Of A Mortals Journey To Immortality S01E186",
        site_name="测试站点",
        category=MediaType.TV.value,
    )
    context = _context(meta, candidate, torrent)

    assert _reconcile(target, candidate, meta, torrent, context) is None
    assert context.media_info is candidate


def test_matching_media_id_preserves_candidate_identity():
    """媒体 ID 原本一致时应保持候选识别上下文。"""
    target = _target_media()
    candidate = _candidate_media(tmdb_id=106449)
    meta = _torrent_meta()
    torrent = TorrentInfo(
        title="A Record Of A Mortals Journey To Immortality S01E186",
        site_name="测试站点",
        category=MediaType.TV.value,
    )
    context = _context(meta, candidate, torrent)

    result = _reconcile(target, candidate, meta, torrent, context)

    assert result is candidate
    assert context.media_info is candidate
    assert context.match_source == "tmdbid"
    assert context.candidate_recognized is True
    assert context.media_info_is_target is False
