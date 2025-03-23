import re
from typing import Tuple, Optional

import parse

from app.core.meta.metabase import MetaBase


class FormatParser(object):
    _key = ""
    _split_chars = r"\.|\s+|\(|\)|\[|]|-|\+|【|】|/|～|;|&|\||#|_|「|」|~"

    def __init__(self, eformat: str, details: Optional[str] =  None, part: Optional[str] =  None,
                 offset: Optional[str] =  None, key: Optional[str] =  "ep"):
        """
        :params eformat: 格式化字符串
        :params details: 格式化详情
        :params part: 分集
        :params offset: 偏移量 -10/EP*2
        :prams key: EP关键字
        """
        self._format = eformat
        self._start_ep = None
        self._end_ep = None
        if not offset:
            self.__offset = "EP"
        elif "EP" in offset:
            self.__offset = offset
        else:
            if offset.startswith("-") or offset.startswith("+"):
                self.__offset = f"EP{offset}"
            else:
                self.__offset = f"EP+{offset}"
        self._key = key
        self._part = None
        if part:
            self._part = part
        if details:
            if re.compile("\\d{1,4}-\\d{1,4}").match(details):
                self._start_ep = details
                self._end_ep = details
            else:
                tmp = details.split(",")
                if len(tmp) > 1:
                    self._start_ep = int(tmp[0])
                    self._end_ep = int(tmp[0]) if int(tmp[0]) > int(tmp[1]) else int(tmp[1])
                else:
                    self._start_ep = self._end_ep = int(tmp[0])

    @property
    def format(self):
        return self._format

    @property
    def start_ep(self):
        return self._start_ep

    @property
    def end_ep(self):
        return self._end_ep

    @property
    def part(self):
        return self._part

    @property
    def offset(self):
        return self.__offset

    def match(self, file: str) -> bool:
        if not self._format:
            return True
        s, e = self.__handle_single(file)
        if not s:
            return False
        if self._start_ep is None:
            return True
        if self._start_ep <= s <= self._end_ep:
            return True
        return False

    def split_episode(self, file_name: str, file_meta: MetaBase) -> Tuple[Optional[int], Optional[int], Optional[str]]:
        """
        拆分集数，返回开始集数，结束集数，Part信息
        """
        # 指定的具体集数，直接返回
        if self._start_ep is not None:
            if self._start_ep == self._end_ep:
                # `details` 格式为 `X-X` 或者 `X`
                if isinstance(self._start_ep, str):
                    # `details` 格式为 `X-X`
                    s, e = self._start_ep.split("-")
                    start_ep = self.__offset.replace("EP", s)
                    end_ep = self.__offset.replace("EP", e)
                    if int(s) == int(e):
                        return int(eval(start_ep)), None, self.part
                    return int(eval(start_ep)), int(eval(end_ep)), self.part
                else:
                    # `details` 格式为 `X`
                    start_ep = self.__offset.replace("EP", str(self._start_ep))
                    return int(eval(start_ep)), None, self.part
            elif not self._format:
                # `details` 格式为 `X,X`
                start_ep = self.__offset.replace("EP", str(self._start_ep))
                end_ep = self.__offset.replace("EP", str(self._end_ep))
                return int(eval(start_ep)), int(eval(end_ep)), self.part
        if not self._format:
            # 未填入`集数定位` 且没有`指定集数` 仅处理`集数偏移`
            start_ep = eval(self.__offset.replace("EP", str(file_meta.begin_episode))) if file_meta.begin_episode else None
            end_ep = eval(self.__offset.replace("EP", str(file_meta.end_episode))) if file_meta.end_episode else None
            return int(start_ep) if start_ep else None, int(end_ep) if end_ep else None, self.part
        else:
            # 有`集数定位`
            s, e = self.__handle_single(file_name)
            start_ep = self.__offset.replace("EP", str(s)) if s else None
            end_ep = self.__offset.replace("EP", str(e)) if e else None
            return int(eval(start_ep)) if start_ep else None, int(eval(end_ep)) if end_ep else None, self.part

    def __handle_single(self, file: str) -> Tuple[Optional[int], Optional[int]]:
        """
        处理单集，返回单集的开始和结束集数
        """
        if not self._format:
            return None, None
        ret = parse.parse(self._format, file)
        if not ret or not ret.__contains__(self._key):
            return None, None
        episodes = ret.__getitem__(self._key)
        if not re.compile(r"^(EP)?(\d{1,4})(-(EP)?(\d{1,4}))?$", re.IGNORECASE).match(episodes):
            return None, None
        episode_splits = list(filter(lambda x: re.compile(r'[a-zA-Z]*\d{1,4}', re.IGNORECASE).match(x),
                                     re.split(r'%s' % self._split_chars, episodes)))
        if len(episode_splits) == 1:
            return int(re.compile(r'[a-zA-Z]*', re.IGNORECASE).sub("", episode_splits[0])), None
        else:
            return int(re.compile(r'[a-zA-Z]*', re.IGNORECASE).sub("", episode_splits[0])), int(
                re.compile(r'[a-zA-Z]*', re.IGNORECASE).sub("", episode_splits[1]))
