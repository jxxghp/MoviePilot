from app.core.context import MediaInfo
from app.helper.message import TemplateContextBuilder
from app.schemas.types import MediaSource, MediaType


def test_message_context_contains_primary_and_auxiliary_media_fields() -> None:
    """消息上下文应区分规范主身份与仅供展示的辅助来源 ID。"""
    context = {}
    media = MediaInfo(
        media_source=MediaSource.AniList,
        media_id="170942",
        type=MediaType.TV,
        title="测试动画",
        tmdb_id=24680,
        douban_id="35000000",
        bangumi_id=499390,
        anilist_id=170942,
    )

    TemplateContextBuilder._add_media_info(context, media)

    assert context["tmdbid"] == 24680
    assert context["doubanid"] == "35000000"
    assert context["bangumiid"] == 499390
    assert context["anilistid"] == 170942
    assert context["media_source"] == "anilist"
    assert context["media_id"] == "170942"
    assert "mediaid_prefix" not in media.to_dict()
    assert media.to_dict()["media_id"] == "170942"


def test_media_info_preserves_plugin_source_identity() -> None:
    """核心媒体对象应保留格式合法的插件自定义来源。"""
    media = MediaInfo(
        media_source="plugin_source",
        media_id="custom-100",
        type=MediaType.MOVIE,
        title="插件电影",
    )

    assert media.media_source == MediaSource("plugin_source")
    assert media.media_id == "custom-100"
    assert media.to_dict()["media_source"] == "plugin_source"
