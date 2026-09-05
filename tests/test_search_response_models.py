"""搜索接口响应模型测试。"""

from app.schemas.context import Context, MetaInfo, SubtitleInfo, TorrentInfo
from app.schemas.response import Response
from app.schemas.search import SearchLastContextData


def test_last_search_context_preserves_nested_torrent_context() -> None:
    """最近搜索响应必须保留资源上下文的嵌套元数据和种子信息。"""
    context = Context(
        meta_info=MetaInfo(title="Example", name="Example"),
        torrent_info=TorrentInfo(title="Example torrent", site_name="Site A"),
    )

    response = Response[SearchLastContextData](
        success=True,
        data={
            "params": {"keyword": "Example", "result_type": "torrent"},
            "results": [context.model_dump(mode="json")],
        },
    ).model_dump(mode="json")

    result = response["data"]["results"][0]
    assert result["meta_info"]["name"] == "Example"
    assert result["torrent_info"]["title"] == "Example torrent"
    assert result["torrent_info"]["site_name"] == "Site A"


def test_last_search_context_keeps_subtitle_result_shape() -> None:
    """修正资源上下文类型后仍必须完整保留字幕搜索结果。"""
    subtitle = SubtitleInfo(
        title="Example subtitle",
        site_name="Subtitle Site",
        enclosure="https://example.test/subtitle.srt",
    )

    response = Response[SearchLastContextData](
        success=True,
        data={
            "params": {"keyword": "Example", "result_type": "subtitle"},
            "results": [subtitle.model_dump(mode="json")],
        },
    ).model_dump(mode="json")

    result = response["data"]["results"][0]
    assert result["title"] == "Example subtitle"
    assert result["site_name"] == "Subtitle Site"
    assert result["enclosure"] == "https://example.test/subtitle.srt"
