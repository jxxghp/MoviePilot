"""
验证文本工具的历史调用与格式兼容。
"""

import importlib
import inspect

from lxml import etree

from app.sdk.string import StringUtils

EXPECTED_METHODS = {
    "clear",
    "clear_file_name",
    "clear_upper",
    "compare_version",
    "count_words",
    "diff_time_str",
    "escape_markdown",
    "find_common_prefix",
    "format_ep",
    "format_size",
    "format_timestamp",
    "generate_random_str",
    "get_base_url",
    "get_domain_address",
    "get_idlist",
    "get_keyword",
    "get_time",
    "get_url_domain",
    "get_url_host",
    "get_url_netloc",
    "get_url_sld",
    "is_all_chinese",
    "is_chinese",
    "is_english_word",
    "is_japanese",
    "is_korean",
    "is_link",
    "is_magnet_link",
    "is_media_title_like",
    "is_number",
    "is_valid_html_element",
    "md5_hash",
    "natural_sort_key",
    "num_filesize",
    "safe_strip",
    "split_text",
    "str_amount",
    "str_filesize",
    "str_float",
    "str_from_cookiejar",
    "str_int",
    "str_secends",
    "str_series",
    "str_timehours",
    "str_timelong",
    "str_title",
    "str_to_timestamp",
    "to_bool",
    "unify_datetime_str",
    "url_equal",
}


def test_string_utils_keeps_complete_plugin_method_surface():
    """
    SDK 门面必须保留拆分前全部静态方法名称。
    """
    assert EXPECTED_METHODS <= set(dir(StringUtils))


def test_legacy_string_modules_share_sdk_facade_identity():
    """
    旧 utils/domain 路径与 SDK 应解析到同一个轻量兼容模块。
    """
    sdk_module = importlib.import_module("app.sdk.string")
    legacy_utils = importlib.import_module("app.utils.string")
    legacy_domain = importlib.import_module("app.domain.string")

    assert legacy_utils is sdk_module
    assert legacy_domain is sdk_module
    assert legacy_utils.StringUtils is StringUtils
    assert legacy_domain.StringUtils is StringUtils


def test_string_utils_preserves_legacy_keyword_arguments():
    """
    插件按旧参数名调用时应正确转交到拆分后的实现。
    """
    assert StringUtils.clear(text="A.B C", replace_word="-", allow_space=True) == "A-B C"
    assert StringUtils.str_filesize(size=1024 ** 3, pre=2) == "1.0G"
    assert StringUtils.url_equal(url1="https://www.example.com", url2="example.com") is True
    assert StringUtils.generate_random_str(randomlength=8)
    assert StringUtils.to_bool(text="", default_val=True) is True
    assert StringUtils.str_amount(amount=1234, curr="¥") == "¥1,234"
    assert StringUtils.get_domain_address(
        address="example.com:8080", prefix=True
    ) == ("http://example.com", 8080)
    assert StringUtils.compare_version(
        v1="1.2.0", compare_type="<", v2="1.3.0"
    ) is True


def test_string_utils_preserves_legacy_method_signatures():
    """
    反射静态方法签名时也应继续看到插件熟悉的旧参数名。
    """
    assert list(inspect.signature(StringUtils.clear).parameters) == [
        "text",
        "replace_word",
        "allow_space",
    ]
    assert list(inspect.signature(StringUtils.compare_version).parameters) == [
        "v1",
        "compare_type",
        "v2",
        "verbose",
    ]


def test_string_utils_routes_representative_capabilities():
    """
    容量、站点、媒体、剧集、种子和 DOM 能力应保持历史结果。
    """
    assert StringUtils.num_filesize("10.150 TB") == 11160043021926
    assert StringUtils.get_url_domain("https://u2.dmhy.org/torrents.php") == "u2.dmhy.org"
    assert StringUtils.is_media_title_like("The Office S01E01") is True
    assert StringUtils.format_ep([1, 2, 3, 5]) == "E01-E03、E05"
    assert StringUtils.is_magnet_link("magnet:?xt=urn:btih:abc") is True
    assert StringUtils.is_valid_html_element(
        etree.HTML("<html><body></body></html>")
    ) is True


def test_string_utils_keeps_empty_and_nullable_text_results():
    """
    插件依赖的空值返回形状不能被类型治理改为空字符串或二元组。
    """
    assert StringUtils.get_idlist(content="", dicts=[{"name": "A", "id": 1}]) == []
    assert StringUtils.get_idlist(content="   ", dicts=[]) == ([], "")
    assert StringUtils.str_title(s=None) is None
    assert StringUtils.str_title(s="") == ""
    assert StringUtils.str_title(s="hello WORLD") == "Hello World"
    assert StringUtils.safe_strip(None) is None
    assert StringUtils.safe_strip("  value  ") == "value"


def test_string_utils_preserves_named_id_order_and_replacement():
    """
    名称按原始词列表匹配，ID 去重且保留类型，替换沿用整串规则。
    """
    entries = [
        {}, {"name": None}, {"name": "A", "id": 1}, {"name": "B", "id": 1},
        {"name": "B", "id": "second"}, {"name": "C"},
    ]
    assert StringUtils.get_idlist(content="A A+B B C", dicts=entries) == ([1, "second", None], "+")
    assert entries[-1] == {"name": "C"}


def test_string_utils_keeps_recursive_list_cleaning_defaults():
    """
    列表递归沿用默认清理参数，保留历史的嵌套列表和空元素。
    """
    assert StringUtils.clear(text="A.B C", replace_word="-", allow_space=True) == "A-B C"
    assert StringUtils.clear(text=["A.B C"], replace_word="-", allow_space=True) == ["ABC"]
    assert StringUtils.clear(text=[["A.B"], None, "C-D"]) == [["AB"], None, "CD"]
