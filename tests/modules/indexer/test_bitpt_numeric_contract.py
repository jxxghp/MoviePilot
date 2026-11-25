"""
极速之星解析器的数值属性与基类合同。
"""

from unittest.mock import Mock

from app.modules.indexer.parser import SiteParserBase
from app.modules.indexer.parser.bitpt import BitptSiteUserInfo
from app.schemas.site import SiteUserData


def test_numeric_properties_preserve_site_schema(monkeypatch):
    """
    解析用户资料时保留基类数值类型并维持对外数据。
    """
    parser = BitptSiteUserInfo(
        site_name="offline",
        url="https://bitpt.invalid/",
        site_cookie="",
        apikey=None,
        token=None,
        request_utils=Mock(),
    )
    monkeypatch.setattr(SiteParserBase, "parse", lambda _self: None)
    parser._parse_site_page("")
    parser._index_html = '<a href="userdetails.php?uid=42">offline</a>'
    basic = """<table class="frmtable">
        <tr><td>UID</td><td>42</td></tr>
        <tr><td>用户名</td><td>offline&nbsp; </td></tr>
        <tr><td>用户级别</td><td>Power User</td></tr>
        <tr><td>上传流量</td><td>2 GiB</td></tr>
        <tr><td>下载流量</td><td>1 GiB</td></tr>
        <tr><td>共享率</td><td>2</td></tr>
        <tr><td>星辰</td><td>累计1.5</td></tr>
    </table>"""
    pages = {
        "https://bitpt.invalid/userdetails.php?uid=42": basic,
        "https://bitpt.invalid/browse.php?t=myseed&st=2&d=desc&p=1": "",
    }
    reader = Mock(side_effect=lambda *, url: pages[url])
    monkeypatch.setattr(parser, "_get_page_content", reader)
    parser.parse()

    expected = {
        "upload": 2 * 1024 ** 3,
        "download": 1024 ** 3,
        "ratio": 2.0,
        "bonus": 1.5,
        "message_unread": 0,
        "seeding": 0,
        "seeding_size": 0,
    }
    assert SiteUserData.model_validate(parser).model_dump(include=set(expected)) == expected
    assert parser.userid == "42"
    assert parser.username == "offline"
    assert parser.user_level == "Power User"
    for field, value in expected.items():
        assert type(getattr(parser, field)) is type(value), field
    assert reader.call_count == 2
