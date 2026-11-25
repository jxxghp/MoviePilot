# -*- coding: utf-8 -*-
"""
验证 Discuz 用户信息解析的默认值、流量和页面字段。
"""

from collections.abc import Iterator

import pytest

from app.modules.indexer.parser.discuz import DiscuzUserInfo


@pytest.fixture
def parser() -> Iterator[DiscuzUserInfo]:
    """
    创建不访问外部服务的 Discuz 解析器，并在用例结束后关闭。

    :yields: 使用空凭据和示例地址的解析器
    """
    instance = DiscuzUserInfo(
        site_name="Discuz",
        url="https://example.com/",
        site_cookie="",
        apikey=None,
        token=None,
    )
    yield instance
    instance.close()


def test_discuz_traffic_defaults_use_integer_bytes(parser: DiscuzUserInfo) -> None:
    """
    上传/下载量使用整数字节，分享率保持浮点数。
    """
    for value in (parser.upload, parser.download):
        assert value == 0
        assert type(value) is int
    assert parser.ratio == 0.0
    assert isinstance(parser.ratio, float)


@pytest.mark.parametrize("include_menu", [True, False])
def test_discuz_skyeysnow_ratio(parser: DiscuzUserInfo, include_menu: bool) -> None:
    """
    天雪菜单和资料页的无括号分享率均应解析，且不影响积分和流量。
    """
    menu = """
    <ul id="ratio_menu">
      <li><a>上传量 : 845.25 GB</a></li>
      <li><a>分享率 : 24.12</a></li>
      <li><a>保种率 : 31.91</a></li>
    </ul>
    """ if include_menu else ""
    html_text = f"""
    <html><body>
      {menu}
      <ul class="cl bbda pbm mbm">
        <li>
          <em class="xg2">统计信息</em>
          <a>好友数 0</a><span class="pipe">|</span><a>回帖数 1033</a>
          <br /><em class="xg2">PT信息</em>
          保种率 32.09 <span class="pipe">|</span> 分享率 24.12
          <span class="pipe">|</span> 预期保种率 31.91
        </li>
      </ul>
      <ul class="pf_l">
        <li><em>积分</em>58065</li>
        <li><em>上传积分/上传量</em>343.62 G / 845.25 GB</li>
        <li><em>下载积分/下载量</em>35.04 G / 35.04 GB</li>
      </ul>
    </body></html>
    """

    parser._parse_user_detail_info(html_text)

    assert parser.ratio == 24.12
    assert parser.to_dict()["ratio"] == 24.12
    assert parser.bonus == 58065
    assert parser.upload == round(845.25 * 1024 ** 3)
    assert parser.download == round(35.04 * 1024 ** 3)


@pytest.mark.parametrize(("ratio_html", "expected"), [
    ("<li>分享率 (24.12)</li>", 24.12),
    ("<li><em>分享率</em>24.12</li>", 24.12),
    ("<li>\n<em>分享率</em>：<span>24.12</span></li>", 24.12),
    ("<li><em>分享率</em>（1,234.56）</li>", 1234.56),
    ("<li><a>分享率 : 0.00</a></li>", 0),
    ("<li>分享率 (24.12)</li><li>分享率 (31.91)</li>", 24.12),
    ("<li>保种率 31.91</li>", 0),
    ("<li>分享率 -- <span>保种率 (31.91)</span></li>", 0),
])
def test_discuz_ratio_formats_keep_bonus(parser: DiscuzUserInfo, ratio_html: str, expected: float) -> None:
    """
    分享率应读取标签后的数值，不误取其他指标或覆盖已有积分。
    """
    parser.bonus = 123

    parser._parse_user_detail_info(f"<html><body><ul>{ratio_html}</ul></body></html>")

    assert parser.ratio == expected
    assert parser.bonus == 123
