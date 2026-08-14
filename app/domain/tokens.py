import re


class Tokens:
    """将媒体文本拆分为可顺序读取的简单词元流。"""

    _text: str = ""
    _index: int = 0
    _tokens: list = []

    def __init__(self, text):
        """使用原始文本初始化词元流。"""
        self._text = text
        self._tokens = []
        self.load_text(text)

    def load_text(self, text):
        """拆分文本并追加有效词元。"""
        splitted_text = re.split(r"\.|\s+|\(|\)|\[|]|-|【|】|/|～|;|&|\||#|_|「|」|~", text)
        for sub_text in splitted_text:
            if sub_text:
                self._tokens.append(sub_text)

    def cur(self):
        """返回当前位置的词元。"""
        if self._index >= len(self._tokens):
            return None
        else:
            token = self._tokens[self._index]
            return token

    def get_next(self):
        """返回当前位置词元并将游标后移。"""
        token = self.cur()
        if token:
            self._index = self._index + 1
        return token

    def peek(self):
        """返回下一位置词元但不移动游标。"""
        index = self._index + 1
        if index >= len(self._tokens):
            return None
        else:
            return self._tokens[index]

    @property
    def tokens(self):
        """返回当前词元列表。"""
        return self._tokens
