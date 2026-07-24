from types import SimpleNamespace

from app.chain.media import MediaChain
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.schemas.types import MediaType


class _FakeTmdbModule:
    """返回固定 TMDB 结果，避免测试访问外部元数据服务。"""

    def __init__(self, result: MediaInfo):
        """保存测试需要返回的 TMDB 媒体信息。"""
        self.result = result

    def recognize_media(self, **_kwargs):
        """同步返回固定 TMDB 媒体信息。"""
        return self.result


def _make_chain(tmdb_media: MediaInfo) -> MediaChain:
    """构造不加载真实模块的媒体处理链。"""
    chain = object.__new__(MediaChain)
    module = _FakeTmdbModule(tmdb_media)
    chain.modulemanager = SimpleNamespace(
        get_running_module=lambda module_id: (
            module if module_id == "TheMovieDbModule" else None
        )
    )
    return chain


def test_supplement_tmdb_keeps_custom_source_identity() -> None:
    """自定义识别源补充 TMDB 后，主身份和展示字段必须保持不变。"""
    primary = MediaInfo(
        source="plugin-anime",
        media_id="subject-42",
        type=MediaType.TV,
        title="原识别标题",
        year="2024",
        category="",
    )
    tmdb_media = MediaInfo(
        tmdb_info={
            "id": 12345,
            "media_type": MediaType.TV,
            "name": "TMDB 标题",
            "genre_ids": [16, 18],
            "external_ids": {"imdb_id": "tt12345", "tvdb_id": 6789},
        }
    )
    tmdb_media.category = "日本动画"

    result = _make_chain(tmdb_media).supplement_tmdb_info(
        primary, MetaInfo("原识别标题 2024")
    )

    assert result is primary
    assert result.source == "plugin-anime"
    assert result.media_id == "subject-42"
    assert result.title == "原识别标题"
    assert result.tmdb_id == 12345
    assert result.genre_ids == [16, 18]
    assert result.category == "日本动画"


def test_supplement_tmdb_does_not_override_custom_category() -> None:
    """下载历史或目录指定的自定义分类优先于 TMDB 自动分类。"""
    primary = MediaInfo(
        source="douban",
        media_id="35593344",
        douban_id="35593344",
        type=MediaType.MOVIE,
        title="测试电影",
        category="纪录片",
    )
    tmdb_media = MediaInfo(
        tmdb_info={
            "id": 9876,
            "media_type": MediaType.MOVIE,
            "title": "Test Movie",
            "genre_ids": [28],
        }
    )
    tmdb_media.category = "动作片"

    result = _make_chain(tmdb_media).supplement_tmdb_info(primary)

    assert result.source == "douban"
    assert result.media_id == "35593344"
    assert result.category == "纪录片"
    assert result.tmdb_id == 9876


def test_tmdb_supplement_uses_current_season_year_and_keeps_season_zero() -> None:
    """电视剧优先使用当前季年份，并且特别季季号不能退化为空。"""
    media = MediaInfo(
        source="bangumi",
        media_id="42",
        type=MediaType.TV,
        title="测试动画",
        year="2020",
        season=0,
        season_years={0: "2024"},
    )

    tmdb_meta = MediaChain._build_tmdb_supplement_meta(
        media, MetaInfo("测试动画 S00 2020")
    )

    assert tmdb_meta.begin_season == 0
    assert tmdb_meta.year == "2024"
