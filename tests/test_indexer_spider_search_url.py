from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from app.modules.indexer.spider import SiteSpider
from app.modules.indexer.spider.haidan import HaiDanSpider
from app.schemas.types import MediaType


def _build_indexer(**kwargs):
    """
    构造 SiteSpider 生成搜索 URL 所需的最小站点配置。
    """
    indexer = {
        "id": "test",
        "name": "测试站点",
        "domain": "https://example.com/",
        "search": {
            "paths": [{"path": "torrents.php"}],
            "params": {"search": "{keyword}"},
        },
        "torrents": {"list": {}, "fields": {}},
    }
    indexer.update(kwargs)
    return indexer


def _get_search_url(indexer: dict, keyword: str | list[str], mtype: MediaType = None) -> str:
    """
    调用 SiteSpider 私有 URL 构造逻辑，避免真实请求站点。
    """
    spider = SiteSpider(indexer=indexer, keyword=keyword, mtype=mtype)
    return spider._SiteSpider__get_search_url()


def _get_haidan_params(keyword: str | None, mtype: MediaType = None) -> dict:
    """
    调用 HaiDanSpider 私有参数构造逻辑，避免真实请求站点。
    """
    spider = HaiDanSpider(indexer={"domain": "https://www.haidan.video/", "name": "海胆"})
    params = parse_qs(spider._HaiDanSpider__get_params(keyword, mtype), keep_blank_values=True)
    return {key: values[0] for key, values in params.items()}


def test_eastgame_imdb_search_uses_imdb_area():
    """
    TLF 支持 IMDb ID 搜索时应使用站点配置的 IMDb 搜索区域。
    """
    indexer = _build_indexer(
        id="eastgame",
        domain="https://pt.eastgame.org/",
        search={
            "paths": [{"path": "torrents.php"}],
            "params": {
                "search_area": 4,
                "search": "{keyword}",
            },
        },
    )

    parsed_url = urlparse(_get_search_url(indexer, "tt16311594"))
    query = parse_qs(parsed_url.query)

    assert parsed_url.geturl().startswith("https://pt.eastgame.org/torrents.php?")
    assert query["search"] == ["tt16311594"]
    assert query["search_area"] == ["4"]


def test_eastgame_title_search_keeps_title_area():
    """
    TLF 普通标题搜索不应误用 IMDb 搜索区域。
    """
    indexer = _build_indexer(
        id="eastgame",
        domain="https://pt.eastgame.org/",
        search={
            "paths": [{"path": "torrents.php"}],
            "params": {
                "search_area": 4,
                "search": "{keyword}",
            },
        },
    )

    query = parse_qs(urlparse(_get_search_url(indexer, "普通标题")).query)

    assert query["search"] == ["普通标题"]
    assert query["search_area"] == ["0"]


def test_eastgame_batch_search_keeps_title_area():
    """
    TLF 批量搜索不是单个 IMDb ID，不能触发 IMDb 搜索区域。
    """
    indexer = _build_indexer(
        id="eastgame",
        domain="https://pt.eastgame.org/",
        search={
            "paths": [{"path": "torrents.php"}],
            "params": {
                "search_area": 4,
                "search": "{keyword}",
            },
        },
    )

    query = parse_qs(urlparse(_get_search_url(indexer, ["tt1234567", "tt7654321"])).query)

    assert query["search"] == ["tt1234567 tt7654321"]
    assert query["search_mode"] == ["1"]
    assert query["search_area"] == ["0"]


def test_ttg_imdb_search_formats_keyword_and_keeps_existing_query():
    """
    TTG 的 IMDb 搜索需要 tt 前缀转换，并且路径自带查询参数不能生成双问号。
    """
    indexer = _build_indexer(
        id="ttg",
        domain="https://totheglory.im/",
        search={
            "paths": [{"path": "browse.php?c=M"}],
            "params": {
                "search_field": "{keyword}",
                "c": "M",
            },
            "imdbid_format": "imdb{imdbid_num}",
        },
        category={
            "field": "search_field",
            "delimiter": " 分类:",
            "movie": [{"id": "电影DVDRip", "cat": "Movies/SD"}],
        },
    )

    search_url = _get_search_url(indexer, "tt0049406", MediaType.MOVIE)
    query = parse_qs(urlparse(search_url).query)

    assert search_url.count("?") == 1
    assert query["c"] == ["M"]
    assert query["search_field"] == ["imdb0049406 分类:电影DVDRip"]


def test_ttg_title_search_does_not_format_keyword():
    """
    TTG 普通标题搜索不能被 IMDb ID 格式化规则影响。
    """
    indexer = _build_indexer(
        id="ttg",
        domain="https://totheglory.im/",
        search={
            "paths": [{"path": "browse.php?c=M"}],
            "params": {
                "search_field": "{keyword}",
                "c": "M",
            },
            "imdbid_format": "imdb{imdbid_num}",
        },
        category={
            "field": "search_field",
            "delimiter": " 分类:",
            "movie": [{"id": "电影DVDRip", "cat": "Movies/SD"}],
        },
    )

    query = parse_qs(urlparse(_get_search_url(indexer, "The Movie", MediaType.MOVIE)).query)

    assert query["search_field"] == ["The Movie 分类:电影DVDRip"]


def test_music_search_uses_dedicated_path_and_repeated_category_parameter():
    """
    音乐分支使用独立页面时应选择 music 路径，并按配置生成可重复的分类参数。
    """
    indexer = _build_indexer(
        id="hhanclub",
        domain="https://hhanclub.net/",
        search={
            "paths": [
                {"path": "torrents.php", "type": "all"},
                {"path": "special.php", "type": "music"},
            ],
            "params": {"search": "{keyword}"},
        },
        category={
            "param": "cat[]",
            "music": [
                {"id": 410, "cat": "Music"},
                {"id": 411, "cat": "MusicVideo"},
            ],
        },
    )

    parsed_url = urlparse(_get_search_url(indexer, "周杰伦", MediaType.MUSIC))
    query = parse_qs(parsed_url.query)

    assert parsed_url.path == "/special.php"
    assert query["cat[]"] == ["410", "411"]
    assert query["search"] == ["周杰伦"]


def test_typed_search_path_falls_back_to_all_path():
    """
    站点只为音乐定义专用路径时，影视搜索仍应回退到通用路径。
    """
    indexer = _build_indexer(
        search={
            "paths": [
                {"path": "torrents.php", "type": "all"},
                {"path": "special.php", "type": "music"},
            ],
            "params": {"search": "{keyword}"},
        },
    )

    parsed_url = urlparse(_get_search_url(indexer, "电影", MediaType.MOVIE))

    assert parsed_url.path == "/torrents.php"


def test_category_item_can_use_distinct_search_parameter_value():
    """
    DiscuzX 子分类可以用展示分类 ID 解析结果，同时用父分类值构造搜索参数。
    """
    indexer = _build_indexer(
        search={
            "paths": [{"path": "forum.php?mod=torrents&cat=1"}],
            "params": {"search": "{keyword}"},
        },
        category={
            "music": [
                {"id": 20, "value": 1, "param": "cat_1_18", "cat": "Music"},
                {"id": 21, "value": 1, "param": "cat_1_18", "cat": "Music"},
            ]
        },
    )

    query = parse_qs(urlparse(_get_search_url(indexer, "FLAC", MediaType.MUSIC)).query)

    assert query["cat_1_18"] == ["1", "1"]


def test_haidan_empty_keyword_uses_blank_search_value():
    """
    海胆空关键词浏览不能把 Python None 编码进 search 参数。
    """
    params = _get_haidan_params(None)

    assert params["search"] == ""
    assert params["search_area"] == "0"


def test_python_spider_remove_does_not_pollute_other_fields():
    """
    Python fallback 解析带 remove 的字段时不能影响同一行后续字段选择。
    """
    indexer = _build_indexer(
        torrents={
            "list": {"selector": "table.torrents > tr"},
            "fields": {
                "title": {"selector": "a.title"},
                "description": {
                    "selector": "td.desc",
                    "remove": "span.noise",
                },
                "imdbid": {"selector": "span.noise"},
            },
        },
    )
    html = """
    <table class="torrents">
      <tr>
        <td><a class="title">Movie.Title</a></td>
        <td class="desc">Main description <span class="noise">tt1234567</span></td>
      </tr>
    </table>
    """

    with patch("app.modules.indexer.spider.rust_accel.parse_indexer_torrents", return_value=None):
        result = SiteSpider(indexer).parse(html)

    assert result == [{
        "title": "Movie.Title",
        "description": "Main description",
        "imdbid": "tt1234567",
    }]


def test_python_spider_parses_nexus_php_occurrence_time_cell():
    """
    Python 兜底解析应兼容 NexusPHP 发生时间模式下没有 span 的时间单元格。
    """
    indexer = _build_indexer(
        torrents={
            "list": {"selector": 'table.torrents > tr:has("table.torrentname")'},
            "fields": {
                "title": {"selector": 'a[href*="details.php?id="]'},
                "date_elapsed": {"selector": "td:nth-child(4) > span", "optional": True},
                "date_added": {
                    "selector": "td:nth-child(4) > span",
                    "attribute": "title",
                    "optional": True,
                },
                "date": {
                    "text": "{% if fields['date_elapsed'] or fields['date_added'] %}"
                            "{{ fields['date_elapsed'] if fields['date_elapsed'] else fields['date_added'] }}"
                            "{% else %}now{% endif %}",
                    "filters": [{"name": "dateparse", "args": "%Y-%m-%d %H:%M:%S"}],
                },
            },
        },
    )
    html = """
    <table class="torrents">
      <tr>
        <td></td>
        <td><table class="torrentname"><tr><td><a href="details.php?id=1">Movie.Title</a></td></tr></table></td>
        <td></td>
        <td class="rowfollow nowrap">2025-05-01<br/>12:13:14</td>
      </tr>
    </table>
    """

    with patch("app.modules.indexer.spider.rust_accel.parse_indexer_torrents", return_value=None):
        result = SiteSpider(indexer).parse(html)

    assert result[0]["pubdate"] == "2025-05-01 12:13:14"


def test_python_spider_does_not_use_relative_date_as_pubdate():
    """
    Python 兜底解析不能把相对时间写入 pubdate。
    """
    indexer = _build_indexer(
        torrents={
            "list": {"selector": "table.torrents > tr"},
            "fields": {
                "title": {"selector": "a.title"},
                "date_elapsed": {"selector": "span.elapsed"},
                "date": {
                    "text": "{% if fields['date_elapsed'] or fields['date_added'] %}"
                            "{{ fields['date_elapsed'] if fields['date_elapsed'] else fields['date_added'] }}"
                            "{% else %}now{% endif %}",
                    "filters": [{"name": "dateparse", "args": "%Y-%m-%d %H:%M:%S"}],
                },
            },
        },
    )
    html = """
    <table class="torrents">
      <tr><td><a class="title">Movie.Title</a><span class="elapsed">1小时</span></td></tr>
    </table>
    """

    with patch("app.modules.indexer.spider.rust_accel.parse_indexer_torrents", return_value=None):
        result = SiteSpider(indexer).parse(html)

    assert "pubdate" not in result[0] or result[0]["pubdate"] is None


def test_python_spider_does_not_use_invalid_date_as_pubdate():
    """
    Python 兜底解析不能把列错位的无效日期写入 pubdate。
    """
    indexer = _build_indexer(
        torrents={
            "list": {"selector": "table.torrents > tr"},
            "fields": {
                "title": {"selector": "a.title"},
                "date_added": {"selector": "td:nth-child(4) > span", "attribute": "title"},
                "date_elapsed": {"selector": "td:nth-child(4) > span"},
                "date": {
                    "text": "{% if fields['date_elapsed'] or fields['date_added'] %}"
                            "{{ fields['date_elapsed'] if fields['date_elapsed'] else fields['date_added'] }}"
                            "{% else %}now{% endif %}",
                    "filters": [{"name": "dateparse", "args": "%Y-%m-%d %H:%M:%S"}],
                },
            },
        },
    )
    html = """
    <table class="torrents">
      <tr>
        <td><a class="title">Movie.Title</a></td>
        <td></td>
        <td></td>
        <td>0</td>
      </tr>
    </table>
    """

    with patch("app.modules.indexer.spider.rust_accel.parse_indexer_torrents", return_value=None):
        result = SiteSpider(indexer).parse(html)

    assert "pubdate" not in result[0] or result[0]["pubdate"] is None


def test_nexus_php_subtitle_table_parse_extracts_common_fields():
    """
    NexusPHP 字幕表格应解析出下载链接、语言、标题、时间、大小、点击、上传者等字段。
    """
    indexer = _build_indexer(
        subtitles={
            "search": {
                "paths": [{"path": "subtitles.php?search={keyword}&lang_id=0"}],
            },
            "list": {"selector": "table tr:has(td.rowfollow)"},
            "fields": {
                "language": {"selector": "td:nth-child(1) img", "attribute": "title"},
                "language_icon": {"selector": "td:nth-child(1) img", "attribute": "src"},
                "title": {"selector": "td:nth-child(2) a"},
                "download": {"selector": "td:nth-child(2) a", "attribute": "href"},
                "date_added": {"selector": "td:nth-child(3) span", "attribute": "title"},
                "date_elapsed": {"selector": "td:nth-child(3) span"},
                "size": {"selector": "td:nth-child(4)"},
                "grabs": {"selector": "td:nth-child(5)"},
                "uploader": {"selector": "td:nth-child(6)"},
                "report": {"selector": "td:nth-child(7) a", "attribute": "href"},
            },
        },
    )
    html = """
    <table width="940" border="1" cellspacing="0" cellpadding="5">
    <tbody><tr><td class="colhead">语言</td><td width="100%" class="colhead" align="center">标题</td></tr>
    <tr><td class="rowfollow" align="center" valign="middle"><img border="0" src="pic/flag/japan.gif" alt="日本語" title="日本語"></td>
    <td class="rowfollow" align="left"><a href="downloadsubs.php?torrentid=514068&amp;subid=2179">739437-second-to-last-love-s03-2025-1080p-fod-web-dl-aac20-h264-magicstar-japanese-subtitle</a></td>
    <td class="rowfollow" align="center"><nobr><span title="2026-03-17 19:48:55">2月23天</span></nobr></td>
    <td class="rowfollow" align="center">233.19&nbsp;KB</td>
    <td class="rowfollow" align="center">0</td>
    <td class="rowfollow" align="center"><i>匿名</i></td>
    <td class="rowfollow" align="center"><a href="report.php?subtitle=2179"><img class="f_report" src="pic/trans.gif" alt="Report" title="举报该字幕"></a></td>
    </tr>
    </tbody></table>
    """

    result = SiteSpider(indexer, keyword="love", search_type="subtitles").parse(html)

    assert result == [{
        "title": "739437-second-to-last-love-s03-2025-1080p-fod-web-dl-aac20-h264-magicstar-japanese-subtitle",
        "enclosure": "https://example.com/downloadsubs.php?torrentid=514068&subid=2179",
        "size": 238787,
        "pubdate": "2026-03-17 19:48:55",
        "date_elapsed": "2月23天",
        "grabs": 0,
        "language_icon": "https://example.com/pic/flag/japan.gif",
        "report_url": "https://example.com/report.php?subtitle=2179",
        "language": "日本語",
        "uploader": "匿名",
        "torrent_id": "514068",
        "subtitle_id": "2179",
    }]
