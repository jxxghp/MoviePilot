"""
知行解析器的数值属性与对外 Schema 兼容合同。
"""

from unittest.mock import Mock

import pytest

from app.modules.indexer.parser import SiteParserBase
from app.modules.indexer.parser.zhixing import ZhixingSiteUserInfo
from app.schemas.site import SiteUserData


@pytest.mark.parametrize("seeding_page,seeding,seeding_size", [
    ("", 3, 2 * 1024 ** 3),
    ('<table><tr id="t1"><td class="r"><a>1 GiB</a></td></tr></table>', 1, 1024 ** 3),
])
def test_numeric_properties_preserve_site_schema_and_seeding_fallback(
    monkeypatch, seeding_page, seeding, seeding_size,
):
    """
    离线解析保留页面统计和详情回退，属性采用基类数值类型，API 数值不变。
    """
    parser = ZhixingSiteUserInfo(
        site_name="offline", url="https://zhixing.invalid/", site_cookie="", apikey=None, token=None,
        request_utils=Mock(),
    )
    monkeypatch.setattr(SiteParserBase, "parse", lambda _self: None)
    parser._parse_site_page("")
    parser._index_html = '<a href="/user/42/">offline</a>'
    basic = """<div class="user-details-tabs">
        <p>UID：42</p><p>用户名：offline</p><p>用户组：Power User</p>
        <p>上传流量：2048</p><p>下载流量：1024</p><p>共享率：2</p><p>保种积分：1.5</p>
        <p>当前保种数量：3</p><p>当前保种容量：2 GiB</p>
    </div>"""
    pages = {
        "https://zhixing.invalid/user/42/": basic,
        "https://zhixing.invalid/user/42/seeding/p1": seeding_page,
        "https://zhixing.invalid/user/42/seeding/p2": "",
    }
    reader = Mock(side_effect=lambda *, url: pages[url])
    monkeypatch.setattr(parser, "_get_page_content", reader)
    parser.parse()

    expected = {
        "upload": 2048, "download": 1024, "ratio": 2.0, "bonus": 1.5,
        "message_unread": 0, "seeding": seeding, "seeding_size": seeding_size,
    }
    public_data = SiteUserData.model_validate(parser).model_dump(include=set(expected))
    assert public_data == expected
    assert parser.userid == "42"
    assert parser.username == "offline"
    assert parser.user_level == "Power User"
    for field, value in expected.items():
        assert type(getattr(parser, field)) is type(value), field
    assert reader.call_count == (3 if seeding_page else 2)
