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


def test_schema_metainfo_season_and_episode_properties() -> None:
    """Schema MetaInfo 应具备与领域 MetaBase 一致的季、集属性和序列化兼容性。"""
    from app.schemas.types import MediaType

    # 单季剧集（未显式指定季）
    tv_no_season = MetaInfo(type="电视剧", begin_episode=1, end_episode=2)
    assert tv_no_season.season_list == [1]
    assert tv_no_season.season == "S01"
    assert tv_no_season.season_seq == "1"
    assert tv_no_season.episode == "E01-E02"

    # 指定单季剧集
    tv_single_season = MetaInfo(type=MediaType.TV, begin_season=2, begin_episode=5)
    assert tv_single_season.season_list == [2]
    assert tv_single_season.season == "S02"
    assert tv_single_season.season_seq == "2"
    assert tv_single_season.episode == "E05"

    # 多季剧集
    tv_multi_season = MetaInfo(type="电视剧", begin_season=1, end_season=3)
    assert tv_multi_season.season_list == [1, 2, 3]
    assert tv_multi_season.season == "S01-S03"

    # 电影
    movie = MetaInfo(type="电影")
    assert movie.season_list == []
    assert movie.season == ""
    assert movie.season_seq == ""
    assert movie.episode == ""
