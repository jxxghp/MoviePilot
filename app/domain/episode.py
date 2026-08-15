"""剧集编号列表的业务显示规则。"""

from typing import List


def compact_numbers(numbers: List[int]) -> str:
    """把连续剧集编号压缩为逗号分隔的数字区间。"""
    numbers.sort()
    result = []
    start = numbers[0]
    end = numbers[0]
    for number in numbers[1:]:
        if number == end + 1:
            end = number
            continue
        result.append(str(start) if start == end else f"{start}-{end}")
        start = end = number
    result.append(str(start) if start == end else f"{start}-{end}")
    return ",".join(result)


def format_ranges(numbers: List[int]) -> str:
    """把剧集编号格式化为带 E 前缀和中文顿号的连续区间。"""
    if not numbers:
        return ""
    if len(numbers) == 1:
        return f"E{numbers[0]:02d}"
    numbers.sort()
    ranges = []
    start = numbers[0]
    end = numbers[0]
    for number in numbers[1:]:
        if number == end + 1:
            end = number
            continue
        ranges.append(f"E{start:02d}" if start == end else f"E{start:02d}-E{end:02d}")
        start = end = number
    ranges.append(f"E{start:02d}" if start == end else f"E{start:02d}-E{end:02d}")
    return "、".join(ranges)
