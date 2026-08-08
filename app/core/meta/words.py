import ast
import operator
from functools import lru_cache
from typing import List, Optional, Tuple

import cn2an
import regex as re

from app.core.meta.config_source import get_meta_config
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.utils.singleton import Singleton


_COMBINED_WORD_RE = re.compile(r'^\s*(.*?)\s*=>\s*(.*?)\s*&&\s*(.*?)\s*<>\s*(.*?)\s*>>\s*(.*?)\s*$')
_LEADING_ZERO_RE = re.compile(r"^0+")
_EP_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])EP(?![A-Za-z0-9_])")
_IMPLICIT_EP_EXPRESSION_RE = re.compile(r"(?:\d|\))\s*EP|EP\s*(?:\d|\()")
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


def _calculate_episode_offset(offset: str, episode: int) -> int:
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
        appley_words = []
        # 读取自定义识别词
        words: List[str] = custom_words or get_meta_config(SystemConfigKey.CustomIdentifiers) or []
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
                        title, message, state = self.__episode_offset(title, pyq, pyh, offsets)
                elif word_type == "replace":
                    title, message, state = self.__replace_regex(title, params[0], params[1])
                elif word_type == "offset":
                    title, message, state = self.__episode_offset(title, params[0], params[1], params[2])
                else:  # block
                    title, message, state = self.__replace_regex(title, params[0], "")

                if state:
                    appley_words.append(word)

            except Exception as err:
                logger.warn(f"自定义识别词 {word} 预处理标题失败：{str(err)} - 标题：{title}")

        return title, appley_words

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
            logger.warn(f"自定义识别词正则替换失败：{str(err)} - 标题：{title}，被替换词：{replaced}，替换词：{replace}")
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
                episode_num_offset_int = _calculate_episode_offset(offset, episode_num_int)
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
            logger.warn(f"自定义识别词集数偏移失败：{str(err)} - 标题：{title}，前定位词：{front}，后定位词：{back}，偏移量：{offset}")
            return title, str(err), False
