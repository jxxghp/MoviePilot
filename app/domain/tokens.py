"""
将媒体标题拆分为词元流，并提供顺序读取与向前查看。
"""

import re
from typing import Optional


class Tokens:
    """
    将媒体文本拆分为可顺序读取的简单词元流。
    """

    _text: str = ""
    _index: int = 0
    _tokens: list[str] = []

    def __init__(self, text: str) -> None:
        """
        使用原始文本初始化词元流。
        """
        self._text = text
        self._tokens = []
        self.load_text(text)

    def load_text(self, text: str) -> None:
        """
        拆分文本并追加有效词元。
        """
        splitted_text = re.split(r"\.|\s+|\(|\)|\[|]|-|【|】|/|～|;|&|\||#|_|「|」|~", text)
        for sub_text in splitted_text:
            if sub_text:
                self._tokens.append(sub_text)

    def cur(self) -> Optional[str]:
        """
        返回当前位置的词元。
        """
        if self._index >= len(self._tokens):
            return None
        else:
            token = self._tokens[self._index]
            return token

    def get_next(self) -> Optional[str]:
        """
        返回当前位置词元并将游标后移。
        """
        token = self.cur()
        if token:
            self._index = self._index + 1
        return token

    def prev(self) -> Optional[str]:
        """
        返回正在处理词元的前一个词元但不移动游标。

        主循环以 get_next() 先消费当前词元再交给各识别阶段，因此游标已指向
        下一个词元；`cur()` 是后继，本方法的偏移量与之相差一位。
        """
        index = self._index - 2
        if index < 0:
            return None
        else:
            return self._tokens[index]

    def peek(self) -> Optional[str]:
        """
        返回下一位置词元但不移动游标。
        """
        index = self._index + 1
        if index >= len(self._tokens):
            return None
        else:
            return self._tokens[index]

    @property
    def tokens(self) -> list[str]:
        """
        返回当前词元列表。
        """
        return self._tokens
