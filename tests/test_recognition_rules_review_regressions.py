"""
识别、分类、搜索和刮削审查发现的真实行为回归。
"""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock
from xml.dom import minidom

import pytest
from pyparsing import ParseBaseException

from app.api.endpoints import media as media_endpoint
from app.api.endpoints import recommend
from app.application import rules
from app.application.classification.configuration import build_default_classification_policy
from app.chain.media import MediaChain
from app.chain.scraping import ScrapingChain, ScrapingConfig
from app.chain.tmdb import TmdbChain
from app.domain.classification.evaluator import ClassificationEvaluator
from app.domain.classification.facts import build_classification_facts
from app.domain.classification.validation import ClassificationPolicyValidator
from app.domain.context import MediaInfo, TorrentInfo
from app.domain.meta.words import WordsMatcher
from app.domain.metainfo import MetaInfo
from app.domain.scraper import MediaScraperHelper
from app.modules.douban.apiv2 import DoubanApi
from app.modules.filter import FilterModule
from app.modules.imdb import ImdbModule
from app.modules.imdb.api import ImdbEpisode, ImdbTitle
from app.modules.musicbrainz import MusicBrainzModule
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.schemas.category import ClassificationCategory, ClassificationRule
from app.schemas.file import FileItem
from app.schemas.types import MediaSource, MediaType


def test_album_signature_preserves_case_sensitive_track_paths(tmp_path) -> None:
    """
    同大小、同时间戳的大小写重命名必须使旧路径缓存失效。
    """
    old = tmp_path / "track.flac"
    old.write_bytes(b"isolated-audio")
    first = MediaChain._album_directory_signature(tmp_path, [old])
    new = old.rename(tmp_path / "Track.flac")
    second = MediaChain._album_directory_signature(tmp_path, [new])
    assert first != second


def test_fin_partial_range_uses_count_in_real_size_filter(monkeypatch) -> None:
    """
    非首集开头的完结合集按实际集数计算每集大小。
    """
    monkeypatch.setattr("app.domain.metainfo.get_metainfo_accelerator", lambda: None)
    monkeypatch.setattr("app.modules.filter.rust_accel.filter_torrents", lambda *args, **kwargs: None)
    torrent = TorrentInfo(title="Review Show S01 2026 1080p WEB-DL H264",
                          description="Review Show [11-26Fin]", size=16 * 1024**3)
    meta = MetaInfo(torrent.title, torrent.description)
    module = object.__new__(FilterModule)
    module.rule_set = {"R": {"size_range": ">800"}}
    module.rulehelper = SimpleNamespace(get_rule_group_by_media=lambda **_kw: [
        SimpleNamespace(name="review size", rule_string="R")])
    assert meta.total_episode == 16
    assert module.filter_torrents(["review size"], [torrent]) == [torrent]


@pytest.mark.parametrize("title,expected", [
    ("Review E1-E11", "Review E2-E12"),
    ("Review E9-E99", "Review E10-E100"),
    ("Review E09-E10", "Review E10-E11"),
])
def test_episode_offset_never_rewrites_its_output(title, expected) -> None:
    """
    不同位数及重叠前缀的集号均只基于原始匹配计算一次。
    """
    prepared, _ = WordsMatcher().prepare(title, ["E <> $ >> EP+1"])
    assert prepared == expected


def test_wallpaper_without_any_backdrop_is_bounded(monkeypatch) -> None:
    """
    全部候选缺少背景图时不再无限抽取同一批结果。
    """
    chain = object.__new__(TmdbChain)
    info = MediaInfo(title="No artwork")
    chain.tmdb_trending = lambda: [info]
    monkeypatch.setattr("app.chain.tmdb.random.choice", Mock(side_effect=[info, AssertionError("重复抽取空背景图")]))
    assert chain.get_random_wallpager() is None


@pytest.mark.parametrize("title", ["Single Ladies", "The Album", "EP Collection"])
def test_musicbrainz_preserves_natural_release_title_words(title) -> None:
    """
    真实作品名称中的发行词不被当作资源规格全局剥离。
    """
    assert MusicBrainzModule._search_title(title) == title


def _classification_policy(field, operator, value, *, aliases=None):
    """
    构造经过标准 Schema 的最小分类规则和别名策略。
    """
    policy = build_default_classification_policy()
    policy.categories.append(ClassificationCategory(id="review.hit", name="命中", path=["命中"], media_type="电影"))
    policy.rules = [ClassificationRule.model_validate({
        "id": "review.rule", "name": "审查", "kind": "category", "media_types": ["电影"],
        "when": {"field": field, "operator": operator, "value": value}, "target": {"category_id": "review.hit"}})]
    policy.field_aliases = {field: aliases} if aliases else {}
    return policy


def test_classification_resolves_valid_alias_chain() -> None:
    """
    发布允许的无环多级别名在实际事实求值中命中规范值。
    """
    policy = _classification_policy("media.language", "equals", "zh", aliases={"普通话": "中文", "中文": "zh"})
    assert ClassificationPolicyValidator().validate(policy).valid
    media = MediaInfo(media_source=MediaSource.TMDB, media_id="1", type=MediaType.MOVIE,
                     title="Review", original_language="普通话")
    result = ClassificationEvaluator().evaluate(policy, build_classification_facts(media))
    assert result.result.effective.category_id == "review.hit"


@pytest.mark.parametrize("field,operator,value,aliases", [
    ("media.year", "in", [2026], {"2026": "错误字符串替换"}),
    ("media.adult", "is_true", None, {"True": "错误字符串替换"}),
])
def test_classification_aliases_preserve_numeric_and_boolean_facts(field, operator, value, aliases) -> None:
    """
    有效别名策略只映射字符串，数字与布尔事实不得被字符串化或改变比较类型。
    """
    policy = _classification_policy(field, operator, value, aliases=aliases)
    assert ClassificationPolicyValidator().validate(policy).valid
    facts = build_classification_facts(MediaInfo(
        media_source=MediaSource.TMDB, media_id="1", type=MediaType.MOVIE,
        title="Review", year="2026", adult=True,
    ))
    before = facts.model_dump()
    result = ClassificationEvaluator().evaluate(policy, facts, trace=True)
    assert result.result.effective.category_id == "review.hit"
    assert facts.model_dump() == before
    actual = result.trace[0].conditions[0].actual
    assert type(actual) is (int if field == "media.year" else bool)


def test_classification_collection_alias_chains_preserve_original_trace() -> None:
    """
    集合事实和期望成员均按多级别名命中，同时保留原始事实与解释轨迹。
    """
    policy = _classification_policy("media.countries", "contains_any", ["中国"], aliases={
        "CHN": "PRC", "PRC": "CN", "中国": "中华", "中华": "CN",
    })
    assert ClassificationPolicyValidator().validate(policy).valid
    facts = build_classification_facts(MediaInfo(
        media_source=MediaSource.TMDB, media_id="1", type=MediaType.MOVIE,
        title="Review", origin_country=["CHN", "US"],
    ))
    result = ClassificationEvaluator().evaluate(policy, facts, trace=True)
    assert result.result.effective.category_id == "review.hit"
    assert facts.media.countries == ["CHN", "US"]
    assert result.trace[0].conditions[0].actual == ["CHN", "US"]
    assert result.trace[0].conditions[0].expected == ["中国"]


@pytest.mark.parametrize("value", [["2024"], [True], [2024.5]])
def test_classification_rejects_wrong_numeric_member_types(value) -> None:
    """
    数值年份成员列表在发布前拒绝字符串、布尔值和非整数。
    """
    policy = _classification_policy("media.year", "in", value)
    assert not ClassificationPolicyValidator().validate(policy).valid


@pytest.mark.parametrize("genre,expected", [
    (10759, {"action", "adventure"}), (10765, {"science_fiction", "fantasy"}),
    (10768, {"war"}),
])
def test_classification_normalizes_composite_tmdb_tv_genres(genre, expected) -> None:
    """
    电视剧复合来源类型展开为可跨来源匹配的标准键。
    """
    media = MediaInfo(tmdb_info={"id": 2, "media_type": MediaType.TV, "name": "Review", "genre_ids": [genre]})
    assert expected <= set(build_classification_facts(media).media.genre_keys or [])


@pytest.mark.parametrize("mtype,title_field,original_field", [
    (MediaType.MOVIE, "title", "original_title"), (MediaType.TV, "name", "original_name")])
def test_tmdb_localized_candidates_keep_original_title_matches(mtype, title_field, original_field) -> None:
    """
    已由来源搜索命中的原文结果不能因本地化展示标题被排除。
    """
    candidate = {"id": 603, title_field: "黑客帝国", original_field: "The Matrix"}
    assert TmdbApi._project_search_results("the matrix", [candidate], mtype) == [candidate]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("status,payload", [(404, {"code": 1001, "msg": "not found"}),
                                           (200, {"code": 1001, "msg": "not found"})])
async def test_douban_failed_detail_does_not_poison_recovery(monkeypatch, asynchronous, status, payload) -> None:
    """
    失败响应不进入长期详情缓存，远端恢复后同一请求立即得到合法身份。
    """
    api = object.__new__(DoubanApi)
    responses = [SimpleNamespace(status_code=status, json=lambda: copy.deepcopy(payload)),
                 SimpleNamespace(status_code=200, json=lambda: {"id": "123456", "title": "Review", "type": "movie"})]
    request = Mock(side_effect=responses)
    api._request = SimpleNamespace(get_res=request)
    async def get_res(**kwargs):
        """
        在异步入口复用失败后恢复的同一响应序列。
        """
        return request(**kwargs)
    monkeypatch.setattr("app.modules.douban.apiv2.AsyncRequestUtils", lambda **_kwargs: SimpleNamespace(get_res=get_res))
    api.clear_cache()
    try:
        operation = api.async_movie_detail if asynchronous else api.movie_detail
        first = await operation("123456") if asynchronous else operation("123456")
        second = await operation("123456") if asynchronous else operation("123456")
        assert first is None
        assert second["id"] == "123456"
        assert request.call_count == 2
    finally:
        api.clear_cache()


@pytest.mark.asyncio
async def test_unified_music_search_can_return_second_page(monkeypatch) -> None:
    """
    音乐来源请求足够的候选，统一接口再对完整集合分页。
    """
    async def search(**kwargs):
        """
        按真实请求上限返回候选，以核对接口的第二页切片。
        """
        return [SimpleNamespace(to_dict=lambda i=i: {"title": f"Song {i}", "year": "2024"})
                for i in range(min(kwargs["limit"], 6))]
    monkeypatch.setattr(media_endpoint, "MediaChain", lambda: SimpleNamespace(async_search_music=search))
    result = await media_endpoint.search(title="Review", type="music", page=2, count=2, media_source=())
    assert [item["title"] for item in result] == ["Song 2", "Song 3"]


def test_imdb_episode_nfo_has_episode_identity() -> None:
    """
    两个单集各自使用 IMDb 原生身份，不能写入同一剧集主 ID。
    """
    module = object.__new__(ImdbModule)
    module.scraper = MediaScraperHelper()
    episodes = [ImdbEpisode(id=f"tt100000{i}", title=f"Episode {i}", season="1", episode_number=i) for i in (1, 2)]
    show = module._to_media_info(ImdbTitle(id="tt1000000", type="tvSeries", primary_title="Review"), episodes=episodes)
    show.scrape_source = "imdb"
    for item in episodes:
        document = minidom.parseString(module.metadata_nfo(show, season=1, episode=item.episode_number))
        identity = document.getElementsByTagName("uniqueid")[0]
        assert identity.firstChild.data == item.id
    assert show.media_id == "tt1000000"
    missing = minidom.parseString(module.metadata_nfo(show, season=1, episode=99))
    assert not missing.getElementsByTagName("uniqueid")


@pytest.mark.parametrize("extension,failed_upload", [(".txt", False), (".mkv", True)])
def test_manual_scrape_propagates_rejection_and_failed_upload(monkeypatch, tmp_path, extension, failed_upload) -> None:
    """
    真实刮削拒绝或 NFO 上传失败时，API 不再返回完成。
    """
    path = tmp_path / ("Review" + extension)
    path.write_bytes(b"review-fixture")
    chain = object.__new__(ScrapingChain)
    config = SimpleNamespace(audio_extensions=(".flac",), video_extensions=(".mkv",))
    monkeypatch.setattr(ScrapingChain, "runtime_config", PropertyMock(return_value=config))
    info = MediaInfo(media_source=MediaSource.TMDB, media_id="1", type=MediaType.MOVIE, title="Review")
    provider = Mock(return_value=SimpleNamespace(
        recognize_media=lambda **_kwargs: info, obtain_images=lambda **_kwargs: info))
    provider.is_audio_path.return_value = False
    monkeypatch.setattr(media_endpoint, "MediaChain", provider)
    monkeypatch.setattr(media_endpoint, "ScrapingChain", lambda: chain)
    upload = Mock(return_value=None)
    chain.storagechain = SimpleNamespace(
        get_parent_item=lambda _item: FileItem(storage="local", type="dir", path=tmp_path.as_posix()),
        get_file_item=lambda **_kwargs: None, upload_file=upload)
    chain.scraping_policies = ScrapingConfig()
    chain.metadata_nfo = lambda **kwargs: MediaScraperHelper.get_metadata_nfo(kwargs["mediainfo"])
    chain.metadata_img = lambda **_kwargs: None
    response = media_endpoint._scrape_impl(FileItem(storage="local", type="file", path=path.as_posix()),
                                          storage="local", media_source=MediaSource.TMDB, media_id="1", type_name=MediaType.MOVIE)
    assert not response.success
    assert upload.called is failed_upload


def test_directory_scrape_propagates_child_upload_failure(monkeypatch, tmp_path) -> None:
    """
    子文件的 NFO 上传失败传到目录结果，普通附属文件仍允许跳过。
    """
    video = tmp_path / "Review.mkv"
    video.write_bytes(b"isolated-video")
    note = tmp_path / "readme.txt"
    note.write_text("isolated-note")
    chain = object.__new__(ScrapingChain)
    monkeypatch.setattr(ScrapingChain, "runtime_config", PropertyMock(return_value=SimpleNamespace(
        audio_extensions=(".flac",), video_extensions=(".mkv",))))
    directory = FileItem(storage="local", type="dir", path=tmp_path.as_posix())
    children = [FileItem(storage="local", type="file", path=path.as_posix()) for path in (note, video)]
    chain.storagechain = SimpleNamespace(
        list_files=lambda **_kwargs: children, contains_bluray_subdirectories=lambda _files: False,
        get_parent_item=lambda _item: directory, get_file_item=lambda **_kwargs: None,
        upload_file=lambda **_kwargs: None)
    chain.scraping_policies = ScrapingConfig()
    chain.metadata_nfo = lambda **_kwargs: "<movie/>"
    chain.metadata_img = lambda **_kwargs: None
    result = chain.scrape_metadata(fileitem=directory, mediainfo=MediaInfo(type=MediaType.MOVIE, title="Review"))
    assert result[0] is False


@pytest.mark.parametrize("expression", ["4K &", "4K & !BLU)", "4K garbage"])
def test_filter_rule_parser_requires_complete_input(monkeypatch, expression) -> None:
    """
    Python 回退解析器不接受仅合法前缀，错误尾部必须显式失败。
    """
    monkeypatch.setattr(rules, "_filter_rule_parser", None)
    with pytest.raises(ParseBaseException):
        rules.RuleParser().parse(expression)


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,expected", [("movie", "movie"), ("tv", "tv")])
async def test_recommendation_filters_mixed_source_media_types(monkeypatch, requested, expected) -> None:
    """
    混合来源在裁剪数量之前应用请求的媒体类型过滤。
    """
    chain = SimpleNamespace(async_tmdb_trending=AsyncMock(return_value=[
        {"title": "TV", "type": "电视剧", "media_id": "1"},
        {"title": "Movie", "type": "电影", "media_id": "2"}]))
    monkeypatch.setattr(recommend, "RecommendChain", lambda: chain)
    response = await recommend.agent_recommendations(source="tmdb_trending", media_type=requested)
    assert response.success
    assert [item["type"] for item in response.data] == [expected]
