"""种子链接内容的纯领域判断规则。"""

from typing import Union


def is_magnet_link(content: Union[str, bytes]) -> bool:
    """判断字符串或字节内容是否为磁力链接。"""
    if not content:
        return False
    if isinstance(content, str):
        return content.startswith("magnet:")
    if isinstance(content, bytes):
        return content.startswith(b"magnet:")
    return False
