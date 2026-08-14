"""无业务状态的中文分词与简繁转换工具。"""

from jieba_next import cut as jieba_next_cut
from zhconv_rs import zhconv as _zhconv  # pylint: disable=no-name-in-module


def cut(text: str, HMM: bool = True, cut_all: bool = False) -> list[str]:
    """
    使用 jieba-next 执行中文分词，并兼容 jieba.cut 的常用参数名。
    """
    return list(jieba_next_cut(text, HMM=HMM, cut_all=cut_all))


def convert(text: str, target: str) -> str:
    """使用 zhconv-rs 执行中文简繁转换，并隔离第三方包的函数名差异。"""
    return _zhconv(text, target)
