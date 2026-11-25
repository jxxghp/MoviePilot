"""
验证站点域名解析、规范化与匹配。
"""

from lxml import etree

from app.domain.site import (
    SiteUtils,
    find_invalid_http_header_names,
    is_valid_http_header_value,
)
from app.foundation.dom import DomUtils
from app.sdk.string import StringUtils


def test_dom_child_element_check_preserves_existing_semantics():
    """
    DOM 基础判断应区分空树和至少包含一个子元素的树。
    """
    empty_tree = etree.HTML("<html></html>")
    populated_tree = etree.HTML("<html><body></body></html>")

    assert DomUtils.has_child_elements(None) is False
    assert DomUtils.has_child_elements(empty_tree) is False
    assert DomUtils.has_child_elements(populated_tree) is True


def test_plugin_string_facade_delegates_to_dom_primitive():
    """
    存量 StringUtils 调用应继续获得与 DOM 原语一致的结果。
    """
    html = etree.HTML("<html><body></body></html>")

    assert StringUtils.is_valid_html_element(html) is DomUtils.has_child_elements(html)


def test_site_login_state_is_derived_from_html_markers():
    """
    站点登录规则应识别退出入口并拒绝密码登录页。
    """
    logged_in_html = '<html><body><a href="/logout.php">退出</a></body></html>'
    login_form_html = '<html><body><input type="password"></body></html>'

    assert SiteUtils.is_logged_in(logged_in_html) is True
    assert SiteUtils.is_logged_in(login_form_html) is False
    assert SiteUtils.is_logged_in("") is False


def test_site_checkin_state_is_derived_from_html_markers():
    """
    站点签到规则应把仍存在签到入口的页面识别为未签到。
    """
    pending_html = '<html><body><a href="/attendance.php">签到</a></body></html>'
    completed_html = '<html><body><a href="/logout.php">退出</a></body></html>'

    assert SiteUtils.is_checkin(pending_html) is False
    assert SiteUtils.is_checkin(completed_html) is True
    assert SiteUtils.is_checkin("") is False


def test_http_header_value_validation_rejects_whitespace_controls_and_non_latin1():
    """
    HTTP 请求头纯规则校验应拒绝首尾空白、控制字符及非 Latin-1 文本，并仅返回出错字段名。
    """
    assert is_valid_http_header_value("") is True
    assert is_valid_http_header_value("Mozilla/5.0 (Windows NT 10.0; Win64; x64)") is True
    assert is_valid_http_header_value(" Mozilla/5.0") is False
    assert is_valid_http_header_value("Mozilla/5.0 ") is False
    assert is_valid_http_header_value("Mozilla/5.0\nBad") is False
    assert is_valid_http_header_value("Mozilla/5.0\x00Bad") is False
    assert is_valid_http_header_value("Mozilla/5.0 中文") is False

    assert find_invalid_http_header_names(None) == []
    assert find_invalid_http_header_names({}) == []
    assert find_invalid_http_header_names(
        {
            "User-Agent": " Mozilla/5.0",
            "Accept": "text/html",
            "X-Empty": None,
            "X-Bad-Type": 123,
        }
    ) == ["User-Agent", "X-Bad-Type"]
