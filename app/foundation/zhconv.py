"""中文简繁转换工具。"""

from zhconv_rs import zhconv as _zhconv  # pylint: disable=no-name-in-module


def convert(text: str, target: str) -> str:
    """
    使用 zhconv-rs 执行中文简繁转换，并隔离第三方包的函数名差异。
    """
    return _zhconv(text, target)
