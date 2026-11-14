"""
日期、时间戳和时长的无状态转换能力。
"""

import bisect
import datetime
from typing import Any, Optional, Union, overload

import dateparser
import dateutil.parser

# dateparser 自由文本解析的候选语言，覆盖站点与存储返回的简繁中文、日文和英文日期。
# 不传 languages 时，无法识别的文本会让 dateparser 逐个尝试并常驻全部 205 种语言数据，
# 进程多占约 35MB；限定后约 4MB。其他语言的日期文本不再被识别，按无法解析处理。
_DATE_TEXT_LANGUAGES = ["zh", "zh-Hant", "ja", "en"]


def format_approx_duration(seconds: Union[str, int, float]) -> str:
    """
    把秒数格式化为单一最大单位的近似时长。
    """
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return ""
    thresholds = [(0, "秒"), (60 - 1, "分"), (3600 - 1, "小时"), (86400 - 1, "天")]
    index = bisect.bisect_left(
        [threshold for threshold, _unit in thresholds], seconds
    ) - 1
    if index == -1:
        return str(seconds)
    threshold, unit = thresholds[index]
    return f"{round(seconds / (threshold + 1))}{unit}"


def format_duration(seconds: Union[int, float]) -> str:
    """
    把秒数格式化为时分秒组合文本。
    """
    hours = seconds // 3600
    remainder_seconds = seconds % 3600
    minutes = remainder_seconds // 60
    seconds = remainder_seconds % 60

    result = f"{int(seconds)}秒"
    if minutes:
        result = f"{int(minutes)}分{result}"
    if hours:
        result = f"{int(hours)}时{result}"
    return result


def parse_datetime(value: Any) -> Optional[datetime.datetime]:
    """
    将常见日期表达解析为 datetime，无法解析时返回 None。
    """
    try:
        return dateutil.parser.parse(value)
    except (TypeError, ValueError, dateutil.parser.ParserError):
        return None


@overload
def normalize_datetime(value: str) -> str:
    ...


@overload
def normalize_datetime(value: None) -> None:
    ...


def normalize_datetime(value: Optional[str]) -> Optional[str]:
    """
    把常见绝对或相对日期文本统一为本地日期时间格式。

    :param value: 待解析的日期文本；未提供时为 None
    :return: 标准格式文本；空值或无法解析时返回原值
    """
    if not value:
        return value
    try:
        parsed = dateparser.parse(value, languages=_DATE_TEXT_LANGUAGES)
        return parsed.strftime("%Y-%m-%d %H:%M:%S") if parsed else value
    except (TypeError, ValueError, OverflowError):
        return value


@overload
def format_timestamp(timestamp: str, date_format: str = "%Y-%m-%d %H:%M:%S") -> str:
    ...


@overload
def format_timestamp(timestamp: int, date_format: str = "%Y-%m-%d %H:%M:%S") -> Union[str, int]:
    ...


@overload
def format_timestamp(timestamp: float, date_format: str = "%Y-%m-%d %H:%M:%S") -> Union[str, float]:
    ...


@overload
def format_timestamp(timestamp: None, date_format: str = "%Y-%m-%d %H:%M:%S") -> None:
    ...


def format_timestamp(
    timestamp: Optional[Union[str, int, float]],
    date_format: str = "%Y-%m-%d %H:%M:%S",
) -> Optional[Union[str, int, float]]:
    """
    把 Unix 时间戳转换为指定格式的本地日期文本。

    :param timestamp: 字符串或数值时间戳；None 原样返回
    :param date_format: 成功转换时使用的本地日期格式
    :return: 成功时为日期文本；非数字文本或转换失败时返回原输入
    """
    if timestamp is None:
        return None
    if isinstance(timestamp, str) and not timestamp.isdigit():
        return timestamp
    try:
        return datetime.datetime.fromtimestamp(int(timestamp)).strftime(date_format)
    except (TypeError, ValueError, OverflowError, OSError):
        return timestamp


def parse_timestamp(value: str) -> float:
    """
    把日期表达转换为 Unix 时间戳，无法解析时返回零。
    """
    if not value:
        return 0
    try:
        parsed = dateparser.parse(value, languages=_DATE_TEXT_LANGUAGES)
        return parsed.timestamp() if parsed else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def format_minutes(minutes: int) -> str:
    """
    把分钟数格式化为小时和分钟组合文本。
    """
    if not minutes:
        return ""
    hours, remaining_minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}小时{remaining_minutes}分"
    return f"{remaining_minutes}分钟"


def format_remaining(value: str) -> str:
    """
    把本地日期时间文本格式化为距当前时间的剩余时长。
    """
    if not value:
        return ""
    try:
        target = datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value
    difference = target - datetime.datetime.now()
    seconds = difference.seconds
    days = difference.days
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if days > 0:
        return f"{days}天{hours}小时{minutes}分钟"
    if hours > 0:
        return f"{hours}小时{minutes}分钟"
    if minutes > 0:
        return f"{minutes}分钟"
    return ""
