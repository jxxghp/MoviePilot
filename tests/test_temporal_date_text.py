"""自由文本日期解析的语言范围与结果契约。"""

import pytest
from dateparser.languages.loader import LocaleDataLoader

from app.foundation import temporal


@pytest.mark.parametrize("text, expected", [
    ("2026-09-30T12:31:15Z", "2026-09-30 12:31:15"),
    ("2026-09-30T12:31:15.123456789+08:00", "2026-09-30 12:31:15"),
    ("2026/09/30 12:31", "2026-09-30 12:31:00"),
    ("Tue, 30 Sep 2026 12:31:15 GMT", "2026-09-30 12:31:15"),
    ("September 30 2026 10:00 PM", "2026-09-30 22:00:00"),
    ("2026年09月30日 12:30:00", "2026-09-30 12:30:00"),
])
def test_absolute_date_text_keeps_parsed_value(text, expected):
    """站点与存储常见的绝对时间格式照常归一。"""
    assert temporal.normalize_datetime(text) == expected


@pytest.mark.parametrize("text", ["3 小时前", "3 小時前", "2 天前", "昨天", "3 hours ago", "2 days ago", "9月30日"])
def test_relative_and_partial_date_text_still_resolves(text):
    """简繁中文、日文写法和英文的相对或缺年份日期仍能解析。"""
    assert temporal.parse_timestamp(text) > 0


@pytest.mark.parametrize("text", ["not a date", "N/A", "未知", "--"])
def test_unparseable_text_loads_only_candidate_languages(text, monkeypatch):
    """无法识别的文本原样返回，且不会把全部语言数据载入常驻内存。"""
    monkeypatch.setattr(LocaleDataLoader, "_loaded_locales", {})
    monkeypatch.setattr(LocaleDataLoader, "_loaded_languages", {})

    assert temporal.normalize_datetime(text) == text
    assert temporal.parse_timestamp(text) == 0
    assert len(LocaleDataLoader._loaded_locales) <= len(temporal._DATE_TEXT_LANGUAGES)
