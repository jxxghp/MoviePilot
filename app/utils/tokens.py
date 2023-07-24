import re


class Tokens:
    _text: str = ""
    _index: int = 0
    _tokens: list = []

    def __init__(self, text):
        self._text = text
        self._tokens = []
        self.load_text(text)

    def load_text(self, text):
        splitted_text = re.split(r"\.|\s+|\(|\)|\[|]|-|\+|【|】|/|～|;|&|\||#|_|「|」|~", text)
        for sub_text in splitted_text:
            if sub_text:
                self._tokens.append(sub_text)

    def cur(self):
        if self._index >= len(self._tokens):
            return None
        else:
            token = self._tokens[self._index]
            return token

    def get_next(self):
        token = self.cur()
        if token:
            self._index = self._index + 1
        return token

    def peek(self):
        index = self._index + 1
        if index >= len(self._tokens):
            return None
        else:
            return self._tokens[index]
