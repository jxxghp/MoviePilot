import pytest

from app.api.endpoints.search import _parse_media_type, _resolve_media_season
from app.chain.search import SearchChain
from app.core.context import MediaInfo, SubtitleInfo
from app.modules.indexer import IndexerModule
from app.modules.indexer.spider import SiteSpider
from app.schemas.types import MediaType
from app.utils import rust_accel


AUDIENCES_SUBTITLE_HTML = """
<table width="940" border="1" cellspacing="0" cellpadding="5">
<tbody><tr><td class="colhead">语言</td><td width="100%" class="colhead" align="center">标题</td><td class="colhead" align="center"><img class="time" src="pic/trans.gif" alt="time" title="添加时间"></td>
        <td class="colhead" align="center"><img class="size" src="pic/trans.gif" alt="size" title="大小"></td><td class="colhead" align="center">点击</td><td class="colhead" align="center">上传者</td><td class="colhead" align="center">举报</td></tr>
<tr><td class="rowfollow" align="center" valign="middle"><img border="0" src="pic/flag/uk.gif" alt="English" title="English"></td>
<td class="rowfollow" align="left"><a href="downloadsubs.php?torrentid=61964&amp;subid=394" <b="">The.Capture.S02E05.2022.1080p.iP.WEB-DL.x264.AAC-ADWeb</a></td>
<td class="rowfollow" align="center"><nobr><span title="2022-09-18 19:33:11">3年9月</span></nobr></td>
<td class="rowfollow" align="center">96.69&nbsp;KB</td>
<td class="rowfollow" align="center">1</td>
<td class="rowfollow" align="center"><i>匿名</i></td>
<td class="rowfollow" align="center"><a href="report.php?subtitle=394"><img class="f_report" src="pic/trans.gif" alt="Report" title="举报该字幕"></a></td>
</tr>
</tbody></table>
"""


def test_explicit_special_season_zero_overrides_recognized_season():
    """精确搜索中显式季 0 必须优先于跨源识别返回的其它季号。"""
    assert _resolve_media_season(explicit_season=0, recognized_season=1) == 0
    assert _resolve_media_season(explicit_season=None, recognized_season=1) == 1

HHANCLUB_SUBTITLE_HTML = """
<div class="flex flex-col w-full items-center mt-[25px] gap-y-[10px] bg-[#F1F3F5] !rounded-md p-5" id="subtitles-table">
  <div class="grid grid-cols-[10%_60%_10%_10%_10%] w-[95%] !rounded-md py-1 items-center bg-[#FFFFFF]/[0.7]">
    <div><img class="w-[70px] h-[46px] pl-5" src="pic/flag/china.gif"></div>
    <div class="flex flex-col">
      <div class="flex flex-row gap-x-[45px]">
        <a href="downloadsubs.php?torrentid=1435&amp;subid=1733" class="!text-[#000000] !text-[16px] !font-[700px] leading-[24px] hover:!text-orange-400 w-[80%]">The.Capture.S01.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb[chs&amp;eng]</a>
      </div>
      <div class="flex flex-row items-center !text-[#888A8D] !text-[15px] !font-[500px] leading-[22px]">
        <div class="flex flex-wrap items-center"><a href="https://hhanclub.net/userdetails.php?id=26202" class="User_Name"><b>qfsong</b></a></div>
      </div>
    </div>
    <div><div class="!text-[#000000] !text-[16px] !font-[700px] leading-[24px]">180.47&nbsp;KB</div></div>
    <div><div class="!text-[#000000] !text-[16px] !font-[700px] leading-[24px]"><span title="2026-03-25 23:26:37">2月15天</span></div></div>
    <div><div><a href="report.php?subtitle=1733"><img src="styles/HHan/icons/icon-report.svg" alt="举报"></a></div></div>
  </div>
</div>
"""

PTTIME_SUBTITLE_HTML = """
<table>
  <tr>
    <td class="rowfollow">简体中文</td>
    <td class="rowfollow"><a href="downloadsubs.php?torrentid=33968&amp;subid=1242">The.Capture.S02.1080p.iP.WEBRip.AAC2.0.x264-PlayWEB.zip</a></td>
    <td class="rowfollow"><a href="/details.php?id=33968">33968</a></td>
    <td class="rowfollow">2022-09-25 13:36:44</td>
    <td class="rowfollow">248KB</td>
    <td class="rowfollow">27</td>
    <td class="rowfollow">匿名</td>
    <td class="rowfollow"><a href="report.php?subtitle=1242">举报</a></td>
  </tr>
</table>
"""


def _audiences_indexer():
    """
    构造 audiences 字幕解析配置。
    """
    return {
        "id": 1,
        "name": "观众",
        "domain": "https://audiences.me/",
        "subtitles": {
            "list": {"selector": "tr:has(td.rowfollow)"},
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
    }


def _hhanclub_indexer():
    """
    构造 hhanclub 字幕解析配置。
    """
    return {
        "id": 2,
        "name": "憨憨",
        "domain": "https://hhanclub.net/",
        "subtitles": {
            "list": {"selector": "#subtitles-table > div"},
            "fields": {
                "language": {
                    "selector": "div:nth-child(1) img",
                    "attribute": "title",
                    "default_value": "简体中文",
                },
                "language_icon": {"selector": "div:nth-child(1) img", "attribute": "src"},
                "title": {"selector": 'div:nth-child(2) a[href*="downloadsubs.php"]'},
                "download": {
                    "selector": 'div:nth-child(2) a[href*="downloadsubs.php"]',
                    "attribute": "href",
                },
                "date_added": {"selector": "div:nth-child(4) span", "attribute": "title"},
                "date_elapsed": {"selector": "div:nth-child(4) span"},
                "size": {"selector": "div:nth-child(3)"},
                "grabs": {"default_value": 0},
                "uploader": {"selector": 'div:nth-child(2) a[href*="userdetails.php"]'},
                "report": {"selector": 'div:nth-child(5) a[href*="report.php"]', "attribute": "href"},
            },
        },
    }


def _pttime_indexer():
    """
    构造 PT时间 字幕解析配置。
    """
    return {
        "id": 3,
        "name": "PT时间",
        "domain": "https://www.pttime.org/",
        "subtitles": {
            "list": {"selector": "table tr:has(td.rowfollow)"},
            "fields": {
                "language": {"selector": "td:nth-child(1)"},
                "language_icon": {
                    "selector": "td:nth-child(1) img",
                    "attribute": "src",
                    "optional": True,
                },
                "title": {"selector": "td:nth-child(2) a"},
                "download": {"selector": "td:nth-child(2) a", "attribute": "href"},
                "date_added": {"selector": "td:nth-child(4)", "optional": True},
                "date_elapsed": {"selector": "td:nth-child(4)", "optional": True},
                "size": {"selector": "td:nth-child(5)"},
                "grabs": {"selector": "td:nth-child(6)"},
                "uploader": {"selector": "td:nth-child(7)"},
                "report": {"selector": "td:nth-child(8) a", "attribute": "href"},
            },
        },
    }


def test_search_media_type_parser_accepts_agent_values():
    """
    搜索入口应兼容前端使用的 movie/tv 媒体类型值。
    """
    assert _parse_media_type("movie") == MediaType.MOVIE
    assert _parse_media_type("tv") == MediaType.TV
    assert _parse_media_type("电影") == MediaType.MOVIE
    assert _parse_media_type("电视剧") == MediaType.TV


def test_exact_subtitle_match_keeps_same_tv_episode(monkeypatch):
    """
    精确字幕搜索应识别字幕名称，并只保留同一剧集的字幕结果。
    """
    chain = object.__new__(SearchChain)

    def fail_filter(*_args, **_kwargs):
        """
        字幕精确搜索不能调用资源过滤规则。
        """
        pytest.fail("字幕精确搜索不应调用过滤规则")

    monkeypatch.setattr(chain, "filter_torrents", fail_filter)

    mediainfo = MediaInfo(
        type=MediaType.TV,
        title="Example Show",
        original_title="Example Show",
        en_title="Example Show",
        year="2024",
        season=1,
        names=["Example Show"],
        season_years={1: "2024"},
    )
    subtitles = [
        SubtitleInfo(site_name="SiteA", title="Example Show S01E03 1080p WEB-DL CHS", subtitle_id="1"),
        SubtitleInfo(site_name="SiteA", title="Example Show S01E04 1080p WEB-DL CHS", subtitle_id="2"),
        SubtitleInfo(site_name="SiteA", title="Example Show S02E03 1080p WEB-DL CHS", subtitle_id="3"),
        SubtitleInfo(site_name="SiteA", title="Other Show S01E03 1080p WEB-DL CHS", subtitle_id="4"),
    ]

    result = chain._SearchChain__parse_subtitle_result(
        subtitles=subtitles,
        mediainfo=mediainfo,
        season_episodes={1: [3]},
        episode=3,
    )

    assert [item.subtitle_id for item in result] == ["1"]


def test_exact_subtitle_match_uses_file_name_candidate():
    """
    精确字幕搜索应同时识别字幕标题和下载文件名。
    """
    chain = object.__new__(SearchChain)
    mediainfo = MediaInfo(
        type=MediaType.TV,
        title="Example Show",
        original_title="Example Show",
        en_title="Example Show",
        year="2024",
        season=1,
        names=["Example Show"],
        season_years={1: "2024"},
    )
    subtitles = [
        SubtitleInfo(
            site_name="SiteA",
            title="Example Show subtitle package",
            file_name="Example.Show.S01E03.1080p.WEB-DL.CHS.srt",
            subtitle_id="1",
        ),
        SubtitleInfo(
            site_name="SiteA",
            title="Example Show subtitle package",
            file_name="Example.Show.S01E04.1080p.WEB-DL.CHS.srt",
            subtitle_id="2",
        ),
    ]

    result = chain._SearchChain__parse_subtitle_result(
        subtitles=subtitles,
        mediainfo=mediainfo,
        season_episodes={1: [3]},
        episode=3,
    )

    assert [item.subtitle_id for item in result] == ["1"]


def test_subtitle_search_params_keep_episode():
    """
    精确字幕搜索缓存参数时应保留集数，便于前端刷新后继续按同一集搜索。
    """
    params = SearchChain._normalize_search_params(
        {
            "keyword": "tmdb:123",
            "type": MediaType.TV,
            "season": 1,
            "episode": 3,
            "sites": "1,2",
            "result_type": "subtitle",
        }
    )

    assert params["episode"] == "3"
    assert params["result_type"] == "subtitle"


def test_subtitle_info_serializes_title_season_episode():
    """
    字幕结果序列化应返回从字幕名称中识别出的季集信息。
    """
    subtitle = SubtitleInfo(
        site_name="观众",
        title="The.Capture.S02E05.2022.1080p.iP.WEB-DL.x264.AAC-ADWeb",
    )

    result = subtitle.to_dict()

    assert result["season_episode"] == "S02 E05"
    assert result["meta_info"]["season_episode"] == "S02 E05"
    assert result["episode_list"] == [5]


@pytest.mark.parametrize(
    ("indexer", "html", "expected"),
    [
        (
            _audiences_indexer(),
            AUDIENCES_SUBTITLE_HTML,
            {
                "title": "The.Capture.S02E05.2022.1080p.iP.WEB-DL.x264.AAC-ADWeb",
                "language": "English",
                "language_icon": "https://audiences.me/pic/flag/uk.gif",
                "enclosure": "https://audiences.me/downloadsubs.php?torrentid=61964&subid=394",
                "pubdate": "2022-09-18 19:33:11",
                "date_elapsed": "3年9月",
                "size": 99011,
                "grabs": 1,
                "uploader": "匿名",
                "report_url": "https://audiences.me/report.php?subtitle=394",
                "torrent_id": "61964",
                "subtitle_id": "394",
            },
        ),
        (
            _hhanclub_indexer(),
            HHANCLUB_SUBTITLE_HTML,
            {
                "title": "The.Capture.S01.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb[chs&eng]",
                "language_icon": "https://hhanclub.net/pic/flag/china.gif",
                "enclosure": "https://hhanclub.net/downloadsubs.php?torrentid=1435&subid=1733",
                "pubdate": "2026-03-25 23:26:37",
                "date_elapsed": "2月15天",
                "size": 184801,
                "grabs": 0,
                "uploader": "qfsong",
                "report_url": "https://hhanclub.net/report.php?subtitle=1733",
                "torrent_id": "1435",
                "subtitle_id": "1733",
            },
        ),
        (
            _pttime_indexer(),
            PTTIME_SUBTITLE_HTML,
            {
                "title": "The.Capture.S02.1080p.iP.WEBRip.AAC2.0.x264-PlayWEB.zip",
                "language": "简体中文",
                "enclosure": "https://www.pttime.org/downloadsubs.php?torrentid=33968&subid=1242",
                "pubdate": "2022-09-25 13:36:44",
                "date_elapsed": "2022-09-25 13:36:44",
                "size": 253952,
                "grabs": 27,
                "uploader": "匿名",
                "report_url": "https://www.pttime.org/report.php?subtitle=1242",
                "torrent_id": "33968",
                "subtitle_id": "1242",
            },
        ),
    ],
)
def test_subtitle_site_spider_parses_standard_fields(monkeypatch, indexer, html, expected):
    """
    Python 字幕解析应把站点配置字段归一化为前端需要的标准字段。
    """
    monkeypatch.setattr(rust_accel, "parse_indexer_subtitles", lambda **_kwargs: None)

    result = SiteSpider(indexer, keyword="The.Capture", search_type="subtitles").parse(html)

    assert len(result) == 1
    for field, value in expected.items():
        assert result[0][field] == value


def test_subtitle_site_spider_skips_empty_rows(monkeypatch):
    """
    Python 字幕解析应丢弃没有标题或下载链接的表格杂项行。
    """
    monkeypatch.setattr(rust_accel, "parse_indexer_subtitles", lambda **_kwargs: None)
    html = """
    <table>
    <tr><td class="rowfollow">not language</td><td class="rowfollow">not title</td></tr>
    <tr><td class="rowfollow"><img src="pic/flag/uk.gif" title="English"></td>
    <td class="rowfollow"><a href="downloadsubs.php?torrentid=1&amp;subid=2">The.Capture.S01</a></td>
    <td class="rowfollow"><span title="2026-01-01 00:00:00">1天</span></td>
    <td class="rowfollow">1 KB</td><td class="rowfollow">0</td><td class="rowfollow">匿名</td>
    <td class="rowfollow"><a href="report.php?subtitle=2">report</a></td></tr>
    </table>
    """

    result = SiteSpider(_audiences_indexer(), keyword="The.Capture", search_type="subtitles").parse(html)

    assert len(result) == 1
    assert result[0]["title"] == "The.Capture.S01"


def test_subtitle_site_spider_marks_login_page_as_error():
    """
    Python 字幕解析遇到登录页时应标记站点错误，避免误判为无字幕。
    """
    html = """
    <html><head><title>1PTBA.COM :: 登录 - Powered by NexusPHP</title></head>
    <body>未登录! 错误: 该页面必须在登录后才能访问 你需要启用cookies才能登录</body></html>
    """
    spider = SiteSpider(_audiences_indexer(), keyword="The.Capture", search_type="subtitles")

    result = spider.parse(html)

    assert result == []
    assert spider.is_error


def test_sync_subtitle_search_reports_spider_error(monkeypatch):
    """
    同步字幕搜索应在解析完成后上报爬虫错误状态。
    """
    captured = {}

    def fake_check(_site, _search_word=None):
        """
        跳过站点流控检查。
        """
        return True

    def fake_get_torrents(self):
        """
        模拟登录页解析后设置错误状态。
        """
        self.is_error = True
        return []

    def fake_statistic(site, error_flag=False, seconds=0):
        """
        捕获同步搜索传给统计的错误状态。
        """
        captured["site"] = site
        captured["error_flag"] = error_flag
        captured["seconds"] = seconds

    monkeypatch.setattr(IndexerModule, "_IndexerModule__search_check", staticmethod(fake_check))
    monkeypatch.setattr(SiteSpider, "get_torrents", fake_get_torrents)
    monkeypatch.setattr(IndexerModule, "_IndexerModule__indexer_statistic", staticmethod(fake_statistic))

    site = _audiences_indexer()
    site["subtitles"]["search"] = {"paths": [{"path": "subtitles.php?search={keyword}"}]}

    result = IndexerModule().search_subtitles(site=site, keyword="The.Capture")

    assert result == []
    assert captured["site"] == site
    assert captured["error_flag"] is True


def test_subtitle_site_spider_keeps_parseable_nested_nexus_rows(monkeypatch):
    """
    Python 字幕解析应保留可解析的 NexusPHP 嵌套行结果。
    """
    monkeypatch.setattr(rust_accel, "parse_indexer_subtitles", lambda **_kwargs: None)
    html = """
    <table><tr><td class="rowfollow">
      <table>
        <tr>
          <td class="rowfollow"><img src="data:image/svg+xml;base64,xxx" title="添加时间"></td>
          <td class="rowfollow"><a href="downloadsubs.php?torrentid=1&amp;subid=2">The.Capture.S01</a></td>
          <td class="rowfollow"><span title="2026-01-01 00:00:00">1天</span></td>
          <td class="rowfollow">1 KB</td><td class="rowfollow">0</td><td class="rowfollow">上传者</td>
          <td class="rowfollow"><a href="report.php?subtitle=2">report</a></td>
        </tr>
        <tr>
          <td class="rowfollow"><img src="pic/flag/uk.gif" title="English"></td>
          <td class="rowfollow"><a href="downloadsubs.php?torrentid=3&amp;subid=4">The.Capture.S02</a></td>
          <td class="rowfollow"><span title="2026-01-02 00:00:00">2天</span></td>
          <td class="rowfollow">2 KB</td><td class="rowfollow">1</td><td class="rowfollow">匿名</td>
          <td class="rowfollow"><a href="report.php?subtitle=4">report</a></td>
        </tr>
      </table>
    </td></tr></table>
    """

    result = SiteSpider(_audiences_indexer(), keyword="The.Capture", search_type="subtitles").parse(html)

    assert [item["title"] for item in result] == [
        "The.Capture.S01",
        "The.Capture.S01",
        "The.Capture.S02",
    ]
    assert result[0]["language"] == "添加时间"
    assert result[0]["language_icon"] == "data:image/svg+xml;base64,xxx"
    assert result[1]["language"] == "添加时间"
    assert result[2]["language"] == "English"


def test_exact_subtitle_match_uses_torrent_helper_for_media_names():
    """
    精确字幕搜索应复用资源匹配逻辑识别字幕标题中的媒体名称。
    """
    chain = object.__new__(SearchChain)
    mediainfo = MediaInfo(
        type=MediaType.TV,
        title="真相捕捉",
        original_title="The Capture",
        en_title="The Capture",
        year="2019",
        season=1,
        names=["The.Capture", "The Capture"],
        season_years={1: "2019"},
    )
    subtitles = [
        SubtitleInfo(
            site_name="憨憨",
            title="The.Capture.S01.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb[chs&eng]",
            subtitle_id="1733",
        ),
        SubtitleInfo(
            site_name="观众",
            title="Other.Show.S01.1080p.WEB-DL.CHS",
            subtitle_id="404",
        ),
    ]

    result = chain._SearchChain__parse_subtitle_result(
        subtitles=subtitles,
        mediainfo=mediainfo,
        season_episodes={1: []},
    )

    assert [item.subtitle_id for item in result] == ["1733"]


def test_exact_subtitle_match_rejects_partial_name_match():
    """
    精确字幕搜索应避免媒体名称只在中间出现时误判。
    """
    chain = object.__new__(SearchChain)
    mediainfo = MediaInfo(
        type=MediaType.TV,
        title="真相捕捉",
        original_title="The Capture",
        en_title="The Capture",
        year="2019",
        season=1,
        names=["The.Capture", "The Capture"],
        season_years={1: "2019"},
    )
    subtitles = [
        SubtitleInfo(
            site_name="观众",
            title="Not.The.Capture.S01.1080p.WEB-DL.CHS",
            subtitle_id="404",
        ),
    ]

    result = chain._SearchChain__parse_subtitle_result(
        subtitles=subtitles,
        mediainfo=mediainfo,
        season_episodes={1: []},
    )

    assert result == []
