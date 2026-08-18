"""过滤规则领域：内置规则定义与规则表达式解析器。"""

import threading
from typing import Dict, Optional, Protocol, Union

from pyparsing import (
    Combine,
    Forward,
    Literal,
    ParseResults,
    Word,
    alphanums,
    alphas,
    infix_notation,
    nums,
    opAssoc,
)


class FilterRuleAccelerator(Protocol):
    """规则表达式解析可选使用的加速器契约，具体实现由启动层注入。"""

    def parse_filter_rule(self, expression: str) -> Optional[list]:
        """解析表达式，无法处理时返回空值。"""


_filter_rule_accelerator: Optional[FilterRuleAccelerator] = None


def configure_filter_rule_runtime(*, accelerator: Optional[FilterRuleAccelerator]) -> None:
    """注入可选的规则表达式解析加速器，保持领域解析器与平台实现解耦。"""
    global _filter_rule_accelerator
    _filter_rule_accelerator = accelerator


def get_filter_rule_accelerator() -> Optional[FilterRuleAccelerator]:
    """返回启动层注入的可选规则表达式解析加速器。"""
    return _filter_rule_accelerator


# 内置规则只在这里维护一份，便于过滤模块和 Agent 工具共享同一套事实来源。
BUILTIN_RULE_SET: Dict[str, dict] = {
    # 蓝光原盘
    "BLU": {
        "include": [
            r"(?i)(\bBlu-?Ray\b.*\b(?:VC-?1|AVC|MPEG-?2)\b|\b(?:UHD|4K|2160p)\b(?:.*Blu-?Ray)?.*\b(?:HEVC|H\.?265)\b|\bBlu-?Ray\b.*\b(?:UHD|4K|2160p)\b.*\b(?:HEVC|H\.?265)\b|\b(?:COMPLETE|FULL)\b.*\b(?:(?:UHD|4K|2160p)\b.*)?Blu-?Ray\b|\b(BD25|BD50|BD66|BD100|BDMV|MiniBD)\b)"
        ],
        "exclude": [
            r"(?i)(\b[XH]\.?264\b|\b[XH]\.?265\b|\bWEB-?DL\b|\bWEB-?RIP\b|\bHDTV(?:RIP)?\b|\bREMUX\b|\bBDRip\b|\bBRRip\b|\bHDRip\b|\bENCODE\b|\b(?<!WEB-|HDTV)RIP\b)"
        ],
    },
    # 4K
    "4K": {
        "include": [r"4k|2160p|x2160"],
        "exclude": [],
    },
    # 1080P
    "1080P": {
        "include": [r"1080[pi]|x1080"],
        "exclude": [],
    },
    # 720P
    "720P": {
        "include": [r"720[pi]|x720"],
        "exclude": [],
    },
    # 中字
    "CNSUB": {
        "include": [
            r"[中国國繁简](/|\s|\\|\|)?[繁简英粤]|[英简繁](/|\s|\\|\|)?[中繁简]"
            r"|繁體|简体|[中国國][字配]|国语|國語|中文|中字|简日|繁日|简繁|繁体"
            r"|([\s,.-\[])(chs|cht)(|[\s,.-\]])"
            r"|(?<![a-z0-9])(?<!\d\s)(gb|big5)(?![a-z0-9])"
        ],
        "exclude": [],
        "tmdb": {
            "original_language": "zh,cn",
        },
    },
    # 官种
    "GZ": {
        "include": [r"官方", r"官种", r"官组"],
        "match": ["labels"],
    },
    # 特效字幕
    "SPECSUB": {
        "include": [r"特效"],
        "exclude": [],
    },
    # BluRay
    "BLURAY": {
        "include": [r"Blu-?Ray"],
        "exclude": [],
    },
    # UHD
    "UHD": {
        "include": [r"UHD|UltraHD"],
        "exclude": [],
    },
    # H265
    "H265": {
        "include": [r"[Hx].?265|HEVC"],
        "exclude": [],
    },
    # H264
    "H264": {
        "include": [r"[Hx].?264|AVC"],
        "exclude": [],
    },
    # 杜比视界
    "DOLBY": {
        "include": [r"Dolby[\s.]+Vision|DOVI|[\s.]+DV[\s.]+|杜比视界"],
        "exclude": [],
    },
    # 杜比全景声
    "ATMOS": {
        "include": [r"Dolby[\s.+]+Atmos|Atmos|杜比全景[声聲]"],
        "exclude": [],
    },
    # HDR
    "HDR": {
        "include": [r"[\s.]+HDR[\s.]+|HDR10|HDR10\+|HDRVivid"],
        "exclude": [],
    },
    # SDR
    "SDR": {
        "include": [r"[\s.]+SDR[\s.]+"],
        "exclude": [],
    },
    # 重编码
    "REMUX": {
        "include": [r"REMUX"],
        "exclude": [],
    },
    # WEB-DL
    "WEBDL": {
        "include": [r"WEB-?DL|WEB-?RIP"],
        "exclude": [],
    },
    # 免费
    "FREE": {
        "downloadvolumefactor": 0,
    },
    # 国语配音
    "CNVOI": {
        "include": [r"[国國][语語]配音|[国國]配|[国國][语語]"],
        "exclude": [],
        "tmdb": {
            "original_language": "zh",
        },
    },
    # 粤语配音
    "HKVOI": {
        "include": [r"粤语配音|粤语"],
        "exclude": [],
    },
    # 60FPS
    "60FPS": {
        "include": [r"60fps|60帧"],
        "exclude": [],
    },
    # 3D
    "3D": {
        "include": [r"3D"],
        "exclude": [],
    },
    # Hi-Res 无损音频
    "HIRES": {
        "include": [r"(?i)\b(?:Hi[ ._-]?Res(?:olution)?|DSD(?:64|128|256|512)?)\b|高解析|(?:24|32)\s*(?:-?bit|位)"],
        "exclude": [],
    },
    # 无损音频
    "LOSSLESS": {
        "include": [r"(?i)\b(?:Lossless|FLAC|ALAC|APE|WAV|WAVE|AIFF?|PCM|DSD|DSF|DFF)\b|无损"],
        "exclude": [],
    },
    "FLAC": {"include": [r"(?i)(?<![A-Z0-9])FLAC(?![A-Z0-9])"], "exclude": []},
    "ALAC": {"include": [r"(?i)(?<![A-Z0-9])ALAC(?![A-Z0-9])"], "exclude": []},
    "APE": {"include": [r"(?i)(?<![A-Z0-9])APE(?![A-Z0-9])"], "exclude": []},
    "WAV": {"include": [r"(?i)(?<![A-Z0-9])WAV(?:E)?(?![A-Z0-9])"], "exclude": []},
    "DSD": {"include": [r"(?i)(?<![A-Z0-9])(?:DSD(?:64|128|256|512)?|DSF|DFF)(?![A-Z0-9])"], "exclude": []},
    "MP3": {"include": [r"(?i)(?<![A-Z0-9])MP3(?![A-Z0-9])"], "exclude": []},
    "AAC": {"include": [r"(?i)(?<![A-Z0-9])(?:AAC|M4A)(?![A-Z0-9])"], "exclude": []},
    "OPUS": {"include": [r"(?i)(?<![A-Z0-9])OPUS(?![A-Z0-9])"], "exclude": []},
    "BITRATE320": {"include": [r"(?i)(?<!\d)320\s*k(?:bps?|b(?:it)?/?s?)?(?![a-z])"], "exclude": []},
    "BITRATE256": {"include": [r"(?i)(?<!\d)256\s*k(?:bps?|b(?:it)?/?s?)?(?![a-z])"], "exclude": []},
    "BITRATE192": {"include": [r"(?i)(?<!\d)192\s*k(?:bps?|b(?:it)?/?s?)?(?![a-z])"], "exclude": []},
}


def get_builtin_rule_set() -> Dict[str, dict]:
    """
    返回内置过滤规则定义。

    返回:
    内置规则名称到规则定义的映射
    """
    return BUILTIN_RULE_SET


class RuleParser:

    _lock = threading.Lock()
    _thread_local = threading.local()

    def __init__(self):
        """
        定义语法规则
        """
        with self._lock:
            if not hasattr(self._thread_local, 'initialized'):
                # 表达式
                expr: Forward = Forward()
                # 原子
                atom: Combine = Combine(Word(alphas, alphanums) | (Word(nums) + Word(alphas, alphanums)))
                # 逻辑非操作符
                operator_not: Literal = Literal('!').set_parse_action(lambda t: 'not')
                # 逻辑或操作符
                operator_or: Literal = Literal('|').set_parse_action(lambda t: 'or')
                # 逻辑与操作符
                operator_and: Literal = Literal('&').set_parse_action(lambda t: 'and')
                # 定义表达式的语法规则
                expr <<= (operator_not + expr) | atom | ('(' + expr + ')')

                # 运算符优先级
                self.expr = infix_notation(expr,
                                          [(operator_not, 1, opAssoc.RIGHT),
                                           (operator_and, 2, opAssoc.LEFT),
                                           (operator_or, 2, opAssoc.LEFT)])

                self._thread_local.expr = self.expr
                self._thread_local.initialized = True
            else:
                self.expr = self._thread_local.expr

    def parse(self, expression: str) -> ParseResults:
        """
        解析给定的表达式。

        参数:
        expression -- 要解析的表达式

        返回:
        解析结果
        """
        accelerator = get_filter_rule_accelerator()
        rust_result = accelerator.parse_filter_rule(expression) if accelerator else None
        if rust_result is not None:
            return _RustParseResults(rust_result)
        return self.expr.parse_string(expression)


class _RustParseResults(list):
    """
    包装 Rust 解析结果，提供本模块调用方使用的 as_list/asList 接口。
    """

    def as_list(self) -> list:
        """
        返回兼容 pyparsing.ParseResults.as_list 的列表结构。
        """
        return list(self)

    def asList(self) -> list:  # noqa: N802
        """
        返回兼容 pyparsing.ParseResults.asList 的列表结构。
        """
        return self.as_list()


def parse_rule_group(rule_group: str) -> Union[list, str]:
    """
    解析单个优先级层级表达式。

    参数:
    rule_group -- 单层规则表达式

    返回:
    布尔组合结构（列表）或单条规则名称（字符串）
    """
    return RuleParser().parse(rule_group).as_list()[0]


if __name__ == '__main__':
    # 测试代码
    expression_str = """
     SPECSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & 60FPS & !DOLBY & !SDR & !3D > CNSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & 60FPS & !DOLBY & !SDR & !3D > SPECSUB & 4K & !BLU & !REMUX & !WEBDL & 60FPS & !DOLBY & !SDR & !3D > CNSUB & 4K & !BLU & !REMUX & !WEBDL & 60FPS & !DOLBY & !SDR & !3D > SPECSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > CNSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > CNSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > SPECSUB & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > CNSUB & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > CNSUB & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > SPECSUB & CNVOI & 4K & WEBDL & 60FPS & !DOLBY & !SDR & !3D > CNSUB & CNVOI & 4K & WEBDL & 60FPS & !DOLBY & !SDR & !3D > SPECSUB & 4K & WEBDL & 60FPS & !DOLBY & !SDR & !3D > CNSUB & 4K & WEBDL & 60FPS & !DOLBY & !SDR & !3D > SPECSUB & CNVOI & 4K & WEBDL & !DOLBY & HDR & !3D > CNSUB & CNVOI & 4K & WEBDL & !DOLBY & HDR & !3D > SPECSUB & CNVOI & 4K & WEBDL & !DOLBY & !3D > CNSUB & CNVOI & 4K & WEBDL & !DOLBY & !3D > SPECSUB & 4K & WEBDL & !DOLBY & HDR & !3D > CNSUB & 4K & WEBDL & !DOLBY & HDR & !3D > SPECSUB & 4K & WEBDL & !DOLBY & !3D > CNSUB & 4K & WEBDL & !DOLBY & !3D > SPECSUB & CNVOI & 4K & !BLU & !WEBDL & !DOLBY & HDR & !3D > CNSUB & CNVOI & 4K & !BLU & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & CNVOI & 4K & !BLU & !WEBDL & !DOLBY & !3D > CNSUB & CNVOI & 4K & !BLU & !WEBDL & !DOLBY & !3D > SPECSUB & 4K & !BLU & !WEBDL & !DOLBY & HDR & !3D > CNSUB & 4K & !BLU & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & 4K & !BLU & !WEBDL & !DOLBY & !SDR & !3D > CNSUB & 4K & !BLU & !WEBDL & !DOLBY & !SDR & !3D > 4K & !BLU & !REMUX & !DOLBY & HDR & !3D > 4K & !BLURAY & !REMUX & !DOLBY & !3D > SPECSUB & 1080P & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > CNSUB & 1080P & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & 1080P & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > CNSUB & 1080P & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > SPECSUB & 1080P & !BLU & !WEBDL & !DOLBY & HDR & !3D > CNSUB & 1080P & !BLU & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & 1080P & !BLU & !WEBDL & !DOLBY & !3D > CNSUB & 1080P & !BLU & !WEBDL & !DOLBY & !3D > SPECSUB & 1080P & WEBDL & !DOLBY & HDR & !3D > CNSUB & 1080P & WEBDL & !DOLBY & HDR & !3D > SPECSUB & 1080P & WEBDL & !DOLBY & !3D > CNSUB & 1080P & WEBDL & !DOLBY & !3D > 1080P & !BLU & !REMUX & !DOLBY & HDR & !3D > 1080P & !BLU & !REMUX & !DOLBY & !3D
    """
    for exp in expression_str.split('>'):
        parsed_expr = RuleParser().parse(exp.strip())
        print(parsed_expr.asList())
