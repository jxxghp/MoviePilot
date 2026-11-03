"""
媒体目录模板选择与单层目录名校验，不读取运行时配置。
"""

import re
from typing import Optional

# 与媒体库根目录定位保持一致，只检查输出表达式中的标题和季变量。
_TEMPLATE_VARIABLE = re.compile(r"\{\{.*?}}", re.DOTALL)


def media_folder_template_index(rename_format: str) -> Optional[int]:
    """
    找出最后一个含标题且不含季表达式的目录段，排除文件名。

    :param rename_format: 已按斜杠分隔的重命名模板
    :return: 媒体目录在模板中的下标；无法识别时返回 None
    """
    directories = rename_format.split("/")[:-1]
    for index in range(len(directories) - 1, -1, -1):
        expressions = _TEMPLATE_VARIABLE.findall(directories[index])
        if any("title" in value for value in expressions) and not any("season" in value for value in expressions):
            return index
    return None


def validate_media_folder_name(value: str) -> str:
    """
    保证渲染结果是非空的单层相对目录名。

    :param value: 模板渲染得到的目录名
    :return: 校验后的原目录名，不改变与媒体库模板一致的空格
    :raises ValueError: 目录名为空、跨层级或包含控制字符及盘符
    """
    if (
        not value.strip()
        or value.strip() in {".", ".."}
        or "/" in value
        or "\\" in value
        or re.match(r"^[A-Za-z]:", value)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("媒体目录模板必须生成一个安全的非空目录名")
    return value
