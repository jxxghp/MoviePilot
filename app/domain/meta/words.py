import ast
import logging
import operator
from functools import lru_cache
from typing import Callable, List, Optional, Tuple

import cn2an
import regex as re

from app.foundation.singleton import Singleton


_custom_words_provider: Callable[[], object] = lambda: ()
logger = logging.getLogger(__name__)


def configure_custom_words_provider(provider: Callable[[], object]) -> None:
    """注入用户识别词来源，领域层只负责解析和应用规则。"""
    global _custom_words_provider
    _custom_words_provider = provider


def get_custom_words() -> object:
    """返回当前自定义识别词原始配置。"""
    return _custom_words_provider()


_COMBINED_WORD_RE = re.compile(r'^\s*(.*?)\s*=>\s*(.*?)\s*&&\s*(.*?)\s*<>\s*(.*?)\s*>>\s*(.*?)\s*$')
_LEADING_ZERO_RE = re.compile(r"^0+")
_EP_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])EP(?![A-Za-z0-9_])")
_IMPLICIT_EP_EXPRESSION_RE = re.compile(r"(?:\d|\))\s*EP|EP\s*(?:\d|\()")
_SUBTITLE_EPISODE_RANGE_RE = re.compile(
    r"(?<!\d)\[?\s*(?P<begin>\d{1,4})\s*-\s*(?P<end>\d{1,4})\s*"
    r"(?:(?:Fin|End)(?![a-z0-9])|完结(?![\u4e00-\u9fff]))"
    r"(?:\s*\](?!\d)|(?!\s*(?:\]\d|\d))\s*)",
    re.IGNORECASE,
)
_SUBTITLE_EPISODE_RE = re.compile(
    r"(?<![全共])(?P<episode>[0-9一二三四五六七八九十百零]+)\s*[集话話期幕](?!\s*[全共])",
    re.IGNORECASE,
)
_SUBTITLE_EPISODE_TITLE_RE = re.compile(
    r"(?<![A-Za-z0-9_])Episode\s+(?P<episode>\d{1,4})(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_SUBTITLE_EPISODE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:EP|E)(?P<episode>\d{1,4})(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_EPISODE_OFFSET_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_EPISODE_OFFSET_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


@lru_cache(maxsize=1024)
def _compile_custom_word_regex(pattern: str):
    """
    编译自定义识别词正则，缓存重复识别链路中反复使用的同一规则。
    """
    return re.compile(pattern)


def calculate_episode_offset(offset: str, episode: int) -> int:
    """
    按白名单算术语法计算集数偏移，避免执行任意表达式。
    """
    if _IMPLICIT_EP_EXPRESSION_RE.search(offset):
        raise ValueError("EP 表达式不支持省略运算符")
    expression, replace_count = _EP_TOKEN_RE.subn(str(episode), offset)
    if "EP" in offset and replace_count == 0:
        raise ValueError("EP 占位符格式不正确")
    tree = ast.parse(expression, mode="eval")
    return int(_evaluate_episode_offset_node(tree.body))


def _evaluate_episode_offset_node(node: ast.AST):
    """
    递归计算集数偏移 AST 节点，仅允许数字和基础算术运算。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _EPISODE_OFFSET_OPS:
        left = _evaluate_episode_offset_node(node.left)
        right = _evaluate_episode_offset_node(node.right)
        return _EPISODE_OFFSET_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _EPISODE_OFFSET_UNARY_OPS:
        operand = _evaluate_episode_offset_node(node.operand)
        return _EPISODE_OFFSET_UNARY_OPS[type(node.op)](operand)
    raise ValueError("集数偏移表达式仅支持数字、EP、括号和基础算术运算符")


def _format_episode_offset(episode_num_str: str, episode_num_offset_int: int) -> str:
    """
    按原集数字符串格式返回偏移后的集数字符串。
    """
    if not episode_num_str.isdigit():
        return cn2an.an2cn(episode_num_offset_int, "low")
    width = len(episode_num_str) if _LEADING_ZERO_RE.search(episode_num_str) else 0
    if episode_num_offset_int < 0:
        return f"-{str(abs(episode_num_offset_int)).zfill(width)}"
    return str(episode_num_offset_int).zfill(width)


class WordsMatcher(metaclass=Singleton):
    """
    自定义识别词匹配器。
    """

    def prepare(self, title: str, custom_words: List[str] = None) -> Tuple[str, List[str]]:
        """
        预处理标题，支持三种格式
        1：屏蔽词
        2：被替换词 => 替换词
        3：前定位词 <> 后定位词 >> 偏移量（EP）
        """
        title, _, appley_words = self.__prepare(title, None, custom_words)
        return title, appley_words

    def prepare_with_subtitle(
        self,
        title: str,
        subtitle: Optional[str] = None,
        custom_words: Optional[List[str]] = None,
    ) -> Tuple[str, Optional[str], List[str]]:
        """
        预处理标题和副标题，支持跨字段应用集数偏移规则。

        :param title: 主标题、种子名或文件名
        :param subtitle: 副标题或站点描述
        :param custom_words: 临时自定义识别词列表
        :return: 处理后的标题、副标题和已应用的识别词
        """
        return self.__prepare(title, subtitle, custom_words)

    def __prepare(
        self,
        title: str,
        subtitle: Optional[str],
        custom_words: Optional[List[str]] = None,
    ) -> Tuple[str, Optional[str], List[str]]:
        """按识别词顺序处理标题，并在需要时更新副标题中的集数。"""
        appley_words = []
        # 读取自定义识别词
        words: List[str] = custom_words or get_custom_words() or []
        for word in words:
            if not word or word.startswith("#"):
                continue
            try:
                word_info = self.__parse_word(word)
                if not word_info:
                    continue
                word_type, params = word_info
                if word_type == "replace_and_offset":
                    thc, bthc, pyq, pyh, offsets = params
                    # 替换词
                    title, message, state = self.__replace_regex(title, thc, bthc)
                    if state:
                        # 替换词成功再进行集偏移
                        title, subtitle, message, state = self.__episode_offset_with_subtitle(
                            title, subtitle, pyq, pyh, offsets
                        )
                elif word_type == "replace":
                    title, message, state = self.__replace_regex(title, params[0], params[1])
                elif word_type == "offset":
                    title, subtitle, message, state = self.__episode_offset_with_subtitle(
                        title, subtitle, params[0], params[1], params[2]
                    )
                else:  # block
                    title, message, state = self.__replace_regex(title, params[0], "")

                if state:
                    appley_words.append(word)

            except Exception as err:
                logger.warning(f"自定义识别词 {word} 预处理标题失败：{str(err)} - 标题：{title}")

        return title, subtitle, appley_words

    @staticmethod
    def __parse_word(word: str) -> Optional[Tuple[str, Tuple[str, ...]]]:
        """
        解析识别词格式。复杂识别词保留原来的字段含义，只把多次正则提取合并为一次。
        """
        if word.count(" => ") and word.count(" && ") and word.count(" >> ") and word.count(" <> "):
            word_match = _COMBINED_WORD_RE.match(word)
            if not word_match:
                raise ValueError("复杂识别词格式不正确")
            return "replace_and_offset", tuple(item.strip() for item in word_match.groups())
        if word.count(" => "):
            strings = word.split(" => ")
            return "replace", (strings[0], strings[1])
        if word.count(" >> ") and word.count(" <> "):
            strings = word.split(" <> ")
            offsets = strings[1].split(" >> ")
            strings[1] = offsets[0]
            return "offset", (strings[0], strings[1], offsets[1])
        if not word.strip():
            return None
        return "block", (word,)

    @staticmethod
    def __replace_regex(title: str, replaced: str, replace: str) -> Tuple[str, str, bool]:
        """
        正则替换
        """
        try:
            replaced_re = _compile_custom_word_regex(r'%s' % replaced)
            title, count = replaced_re.subn(r'%s' % replace, title)
            return title, "", count > 0
        except Exception as err:
            logger.warning(f"自定义识别词正则替换失败：{str(err)} - 标题：{title}，被替换词：{replaced}，替换词：{replace}")
            return title, str(err), False

    @staticmethod
    def __episode_offset(title: str, front: str, back: str, offset: str) -> Tuple[str, str, bool]:
        """
        集数偏移
        """
        try:
            if back and not _compile_custom_word_regex(r'%s' % back).search(title):
                return title, "", False
            if front and not _compile_custom_word_regex(r'%s' % front).search(title):
                return title, "", False
            offset_word_info_re = _compile_custom_word_regex(
                r'(?<=%s.*?)[0-9一二三四五六七八九十]+(?=.*?%s)' % (front, back)
            )
            episode_nums_str = offset_word_info_re.findall(title)
            if not episode_nums_str:
                return title, "", False
            episode_nums_offset_str = []
            offset_order_flag = False
            for episode_num_str in episode_nums_str:
                episode_num_int = int(cn2an.cn2an(episode_num_str, "smart"))
                episode_num_offset_int = calculate_episode_offset(offset, episode_num_int)
                # 向前偏移
                if episode_num_int > episode_num_offset_int:
                    offset_order_flag = True
                # 向后偏移
                elif episode_num_int < episode_num_offset_int:
                    offset_order_flag = False
                episode_num_offset_str = _format_episode_offset(
                    episode_num_str, episode_num_offset_int
                )
                episode_nums_offset_str.append(episode_num_offset_str)
            episode_nums_dict = dict(zip(episode_nums_str, episode_nums_offset_str))
            # 集数向前偏移，集数按升序处理
            if offset_order_flag:
                episode_nums_list = sorted(episode_nums_dict.items(), key=lambda x: x[1])
            # 集数向后偏移，集数按降序处理
            else:
                episode_nums_list = sorted(episode_nums_dict.items(), key=lambda x: x[1], reverse=True)
            for episode_num in episode_nums_list:
                episode_offset_re = _compile_custom_word_regex(
                    r'(?<=%s.*?)%s(?=.*?%s)' % (front, episode_num[0], back)
                )
                title = episode_offset_re.sub(r'%s' % episode_num[1], title)
            return title, "", True
        except Exception as err:
            logger.warning(f"自定义识别词集数偏移失败：{str(err)} - 标题：{title}，前定位词：{front}，后定位词：{back}，偏移量：{offset}")
            return title, str(err), False

    def __episode_offset_with_subtitle(
        self,
        title: str,
        subtitle: Optional[str],
        front: str,
        back: str,
        offset: str,
    ) -> Tuple[str, Optional[str], str, bool]:
        """
        在标题或副标题中应用集数偏移，必要时跨字段定位副标题集数。
        """
        title, message, state = self.__episode_offset(title, front, back, offset)
        if state:
            return title, subtitle, message, True
        if not subtitle:
            return title, subtitle, message, False

        parsed_subtitle, message, state = self.__episode_offset(subtitle, front, back, offset)
        if state:
            return title, parsed_subtitle, message, True
        if not self.__locators_match(f"{title} {subtitle}", front, back):
            return title, subtitle, message, False

        parsed_subtitle, message, state = self.__episode_offset_subtitle(subtitle, offset)
        return title, parsed_subtitle if state else subtitle, message, state

    @staticmethod
    def __locators_match(text: str, front: str, back: str) -> bool:
        """判断前后定位词是否在标题和副标题组成的上下文中同时出现。"""
        try:
            if front and not _compile_custom_word_regex(front).search(text):
                return False
            if back and not _compile_custom_word_regex(back).search(text):
                return False
            return True
        except Exception:
            return False

    @staticmethod
    def __subtitle_episode_spans(subtitle: str) -> List[Tuple[int, int]]:
        """提取副标题中会被 MetaBase 识别为集数的数字范围。"""
        spans: List[Tuple[int, int]] = []
        patterns = (
            (_SUBTITLE_EPISODE_RANGE_RE, ("begin", "end")),
            (_SUBTITLE_EPISODE_RE, ("episode",)),
            (_SUBTITLE_EPISODE_TITLE_RE, ("episode",)),
            (_SUBTITLE_EPISODE_TOKEN_RE, ("episode",)),
        )
        for pattern, group_names in patterns:
            for match in pattern.finditer(subtitle):
                for group_name in group_names:
                    start, end = match.span(group_name)
                    if start < 0 or end < 0:
                        continue
                    if not any(start < old_end and end > old_start for old_start, old_end in spans):
                        spans.append((start, end))
        return sorted(spans)

    @staticmethod
    def __episode_offset_subtitle(subtitle: str, offset: str) -> Tuple[str, str, bool]:
        """只偏移副标题中的集数表达式，避免修改副标题里的季数或年份。"""
        spans = WordsMatcher.__subtitle_episode_spans(subtitle)
        if not spans:
            return subtitle, "", False
        try:
            replacements = []
            for start, end in spans:
                episode_num_str = subtitle[start:end]
                episode_num_int = int(cn2an.cn2an(episode_num_str, "smart"))
                episode_num_offset_int = calculate_episode_offset(offset, episode_num_int)
                replacements.append(
                    (start, end, _format_episode_offset(episode_num_str, episode_num_offset_int))
                )
            for start, end, replacement in reversed(replacements):
                subtitle = f"{subtitle[:start]}{replacement}{subtitle[end:]}"
            return subtitle, "", True
        except Exception as err:
            logger.warning(f"自定义识别词副标题集数偏移失败：{str(err)} - 副标题：{subtitle}，偏移量：{offset}")
            return subtitle, str(err), False
