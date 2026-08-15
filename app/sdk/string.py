"""为插件保留历史 StringUtils 类的轻量兼容门面。"""

from functools import wraps
from inspect import signature
from typing import Callable

from app.domain.episode import compact_numbers, format_ranges
from app.domain.site import extract_domain, urls_match
from app.domain.title import is_media_title_like, parse_search_keyword
from app.domain.torrent import is_magnet_link
from app.foundation.crypto import HashUtils
from app.foundation.dom import DomUtils
from app.foundation.size import format_compact_size, format_size, parse_size
from app.foundation.temporal import (
    format_approx_duration,
    format_duration,
    format_minutes,
    format_remaining,
    format_timestamp,
    normalize_datetime,
    parse_datetime,
    parse_timestamp,
)
from app.foundation.text import (
    common_prefix,
    contains_chinese,
    contains_japanese,
    contains_korean,
    cookiejar_to_string,
    count_words,
    escape_markdown,
    extract_named_ids,
    format_amount,
    is_all_chinese,
    is_english_word,
    is_number,
    natural_sort_key,
    normalize_upper,
    parse_bool,
    parse_float,
    parse_int,
    random_string,
    remove_punctuation,
    sanitize_filename,
    split_by_bytes,
    strip_optional,
    title_case,
)
from app.foundation.url import (
    base_url,
    host_label,
    is_link,
    parse_address,
    second_level_label,
    split_netloc,
)
from app.foundation.version import compare_version


def _legacy_alias(function: Callable, **keyword_aliases: str) -> Callable:
    """创建支持旧关键字名称的静态方法转发器。"""

    @wraps(function)
    def call(*args, **kwargs):
        """把旧关键字转换为 canonical 参数后调用真实实现。"""
        for legacy_name, canonical_name in keyword_aliases.items():
            if legacy_name in kwargs:
                kwargs[canonical_name] = kwargs.pop(legacy_name)
        return function(*args, **kwargs)

    canonical_to_legacy = {
        canonical_name: legacy_name
        for legacy_name, canonical_name in keyword_aliases.items()
    }
    call.__signature__ = signature(function).replace(
        parameters=[
            parameter.replace(
                name=canonical_to_legacy.get(parameter.name, parameter.name)
            )
            for parameter in signature(function).parameters.values()
        ]
    )
    return call


def _legacy_md5_hash(data) -> str:
    """保持 StringUtils.md5_hash 对空值和对象文本化的历史语义。"""
    if not data:
        return ""
    return HashUtils.md5(str(data))


class StringUtils:
    """组合已拆分实现，保持插件使用的历史静态方法接口。"""

    num_filesize = staticmethod(parse_size)
    str_timelong = staticmethod(_legacy_alias(format_approx_duration, time_sec="seconds"))
    str_secends = staticmethod(_legacy_alias(format_duration, time_sec="seconds"))
    is_chinese = staticmethod(_legacy_alias(contains_chinese, word="value"))
    is_japanese = staticmethod(_legacy_alias(contains_japanese, word="value"))
    is_korean = staticmethod(_legacy_alias(contains_korean, word="value"))
    is_all_chinese = staticmethod(_legacy_alias(is_all_chinese, word="value"))
    is_english_word = staticmethod(_legacy_alias(is_english_word, word="value"))
    str_int = staticmethod(_legacy_alias(parse_int, text="value"))
    str_float = staticmethod(_legacy_alias(parse_float, text="value"))
    clear = staticmethod(
        _legacy_alias(
            remove_punctuation,
            text="value",
            replace_word="replacement",
        )
    )
    clear_upper = staticmethod(_legacy_alias(normalize_upper, text="value"))
    str_filesize = staticmethod(_legacy_alias(format_compact_size, pre="precision"))
    format_size = staticmethod(format_size)
    url_equal = staticmethod(_legacy_alias(urls_match, url1="first", url2="second"))
    get_url_netloc = staticmethod(split_netloc)
    get_url_domain = staticmethod(extract_domain)
    get_url_sld = staticmethod(second_level_label)
    get_url_host = staticmethod(host_label)
    get_base_url = staticmethod(base_url)
    clear_file_name = staticmethod(_legacy_alias(sanitize_filename, name="value"))
    generate_random_str = staticmethod(_legacy_alias(random_string, randomlength="length"))
    get_time = staticmethod(_legacy_alias(parse_datetime, date="value"))
    unify_datetime_str = staticmethod(_legacy_alias(normalize_datetime, datetime_str="value"))
    format_timestamp = staticmethod(format_timestamp)
    str_to_timestamp = staticmethod(_legacy_alias(parse_timestamp, date_str="value"))
    to_bool = staticmethod(_legacy_alias(parse_bool, text="value", default_val="default"))
    str_from_cookiejar = staticmethod(_legacy_alias(cookiejar_to_string, cj="cookiejar"))
    get_idlist = staticmethod(_legacy_alias(extract_named_ids, dicts="entries"))
    md5_hash = staticmethod(_legacy_md5_hash)
    str_timehours = staticmethod(format_minutes)
    str_amount = staticmethod(_legacy_alias(format_amount, curr="currency"))
    count_words = staticmethod(_legacy_alias(count_words, text="value"))
    is_media_title_like = staticmethod(_legacy_alias(is_media_title_like, text="value"))
    split_text = staticmethod(_legacy_alias(split_by_bytes, text="value"))
    get_keyword = staticmethod(parse_search_keyword)
    str_title = staticmethod(_legacy_alias(title_case, s="value"))
    escape_markdown = staticmethod(_legacy_alias(escape_markdown, content="value"))
    get_domain_address = staticmethod(
        _legacy_alias(parse_address, prefix="include_scheme")
    )
    str_series = staticmethod(_legacy_alias(compact_numbers, array="numbers"))
    format_ep = staticmethod(_legacy_alias(format_ranges, nums="numbers"))
    is_number = staticmethod(_legacy_alias(is_number, text="value"))
    find_common_prefix = staticmethod(
        _legacy_alias(common_prefix, str1="first", str2="second")
    )
    compare_version = staticmethod(
        _legacy_alias(
            compare_version,
            v1="source",
            compare_type="comparison",
            v2="target",
        )
    )
    diff_time_str = staticmethod(_legacy_alias(format_remaining, time_str="value"))
    safe_strip = staticmethod(strip_optional)
    is_valid_html_element = staticmethod(_legacy_alias(DomUtils.has_child_elements, elem="element"))
    is_link = staticmethod(_legacy_alias(is_link, text="value"))
    is_magnet_link = staticmethod(is_magnet_link)
    natural_sort_key = staticmethod(_legacy_alias(natural_sort_key, text="value"))


__all__ = ["StringUtils"]
