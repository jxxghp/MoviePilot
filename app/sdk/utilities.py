"""插件常用的无状态通用工具。"""

from app.foundation.crypto import CryptoJsUtils, HashUtils, RSAUtils
from app.foundation.dom import DomUtils
from app.foundation.text import (
    common_prefix,
    contains_chinese,
    contains_japanese,
    contains_korean,
    cookiejar_to_string,
    count_words,
    cut,
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
from app.foundation.reflection import ModuleHelper, ObjectUtils
from app.foundation.singleton import (
    AbstractSingleton,
    AbstractSingletonClass,
    Singleton,
    SingletonClass,
    WeakSingleton,
)
from app.adapters.system.host import SystemUtils
from app.runtime.execution import log_execution_time, retry
from app.runtime.localization import LocaleHelper
from app.runtime.scheduling import TimerUtils
from app.application.security.otp import OtpUtils
from app.sdk.string import StringUtils


decrypt = CryptoJsUtils.decrypt
encrypt = CryptoJsUtils.encrypt


__all__ = [
    "AbstractSingleton",
    "AbstractSingletonClass",
    "CryptoJsUtils",
    "DomUtils",
    "HashUtils",
    "ModuleHelper",
    "ObjectUtils",
    "OtpUtils",
    "LocaleHelper",
    "RSAUtils",
    "Singleton",
    "SingletonClass",
    "StringUtils",
    "SystemUtils",
    "TimerUtils",
    "WeakSingleton",
    "common_prefix",
    "contains_chinese",
    "contains_japanese",
    "contains_korean",
    "cookiejar_to_string",
    "count_words",
    "cut",
    "decrypt",
    "encrypt",
    "escape_markdown",
    "extract_named_ids",
    "format_amount",
    "is_all_chinese",
    "is_english_word",
    "is_number",
    "log_execution_time",
    "natural_sort_key",
    "normalize_upper",
    "parse_bool",
    "parse_float",
    "parse_int",
    "random_string",
    "remove_punctuation",
    "retry",
    "sanitize_filename",
    "split_by_bytes",
    "strip_optional",
    "title_case",
]
