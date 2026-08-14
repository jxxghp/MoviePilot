"""中文分词工具。"""

from jieba_next import cut as jieba_next_cut


def cut(text: str, HMM: bool = True, cut_all: bool = False) -> list[str]:
    """
    使用 jieba-next 执行中文分词，并兼容 jieba.cut 的常用参数名。
    """
    return list(jieba_next_cut(text, HMM=HMM, cut_all=cut_all))
