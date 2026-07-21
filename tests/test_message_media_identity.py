from app.core.context import MediaInfo
from app.helper.message import TemplateContextBuilder
from app.schemas.types import MediaType


def test_message_context_contains_all_media_identity_fields() -> None:
    """消息模板上下文应向插件暴露全部媒体源 ID 和当前主身份。"""
    context = {}
    media = MediaInfo(
        source="anilist",
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
    assert media.to_dict()["mediaid_prefix"] == "anilist"
    assert media.to_dict()["media_id"] == "170942"


def test_media_info_preserves_plugin_source_identity() -> None:
    """核心媒体对象应原样保留插件自定义数据源的原生 ID。"""
    media = MediaInfo(
        source="plugin_source",
        media_id="custom-100",
        type=MediaType.MOVIE,
        title="插件电影",
    )

    assert media.media_id == "custom-100"
    assert media.to_dict()["media_id"] == "custom-100"
