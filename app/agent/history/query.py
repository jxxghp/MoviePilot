"""Hermes FTS5 查询语义：短语、布尔、前缀、CJK 与无结果放宽。"""

# Query normalization/routing adapted from NousResearch/hermes-agent
# f42f579cf8bac4918ac9599bece71618afadd846, hermes_state_search.py (MIT).
# Copyright (c) 2025 Nous Research. See native/fts5_cjk/LICENSE.hermes.
import re

CJK = re.compile(r"[\u1100-\u11ff\u3040-\u30ff\u3130-\u318f\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\ua960-\ua97f\uac00-\ud7ff\uf900-\ufaff\U00020000-\U0002fa1f]+")
TOKENS = re.compile(r'"[^"]*"|\S+')


def normalize(query: str) -> str:
    """保留成对引号与运算符，对带点、连字符的对象名使用短语语义。"""
    quoted: list[str] = []

    def hold(match: re.Match[str]) -> str:
        """临时保护用户明确给出的短语，避免二次加引号。"""
        quoted.append(match.group(0))
        return f"\x00Q{len(quoted) - 1}\x00"

    value = re.sub(r'"[^"]*"', hold, query[:1024]).replace('"', ' ')
    special = '+{}():"^@/#&|~[]<>,;!?$=\\\''
    value = re.sub('[' + re.escape(special) + ']', ' ', value)
    if not CJK.search(value):
        value = value.replace('%', ' ')
    value = re.sub(r'\*+', '*', value)
    value = re.sub(r'(^|\s)\*', r'\1', value)
    value = re.sub(r'(?i)^(AND|OR|NOT)\b\s*', '', value.strip())
    value = re.sub(r'(?i)\s+(AND|OR|NOT)\s*$', '', value.strip())
    value = re.sub(r'\b(\w+(?:[._-]\w+)+)\b', r'"\1"', value)
    for index, phrase in enumerate(quoted):
        value = value.replace(f'\x00Q{index}\x00', phrase)
    return value.strip()


def relaxed(query: str) -> str:
    """仅对无显式 OR/NOT 的多词查询放宽，短语始终作为整体。"""
    tokens = TOKENS.findall(query)
    if any(token.upper() in {'OR', 'NOT'} for token in tokens):
        return ''
    units = [token for token in tokens if token.upper() != 'AND']
    return ' OR '.join(units) if len(units) >= 2 else ''


def terms(query: str) -> list[str]:
    """取路由所需非运算符单元，不用总字数误判多个中文短词。"""
    return [token.strip('"').strip('*') for token in TOKENS.findall(query)
            if token.upper() not in {'AND', 'OR', 'NOT', 'NEAR'}]


def cjk_eligible(query: str) -> bool:
    """孤立单字不能在长 CJK 词中命中 bigram，保持 Hermes 的 LIKE 回退。"""
    runs = CJK.findall(query)
    return bool(runs) and all(len(run) >= 2 for run in runs)


def trigram_eligible(query: str) -> bool:
    """每个查询单元都需至少三字，避免短词令整个 AND 查询静默归零。"""
    units = terms(query)
    return bool(units) and all(len(unit) >= 3 for unit in units)


def like_predicate(query: str) -> tuple[str, list[str], str]:
    """把 Hermes 支持的布尔子集变为绑定参数的字面量 LIKE。"""
    groups: list[list[str]] = [[]]
    params: list[str] = []
    negate = False
    anchor = ''
    for token in TOKENS.findall(query):
        operator = token.upper()
        if operator == 'OR':
            if groups[-1]:
                groups.append([])
            negate = False
        elif operator == 'NOT':
            negate = True
        elif operator not in {'AND', 'NEAR'}:
            term = token.strip('"').strip('*').strip()
            if term:
                pattern = '%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
                clause = "(m.content LIKE ? ESCAPE '\\' OR m.tool_name LIKE ? ESCAPE '\\' OR m.tool_calls LIKE ? ESCAPE '\\')"
                groups[-1].append(('NOT ' if negate else '') + clause)
                params.extend([pattern] * 3)
                if not negate and not anchor:
                    anchor = term
                negate = False
    return ' OR '.join('(' + ' AND '.join(group) + ')' for group in groups if group), params, anchor


def quote_tokens(query: str) -> str:
    """沿用 Hermes 子串路由逐单元加引号，保留布尔操作符。"""
    return ' '.join(token if token.upper() in {'AND', 'OR', 'NOT'} else '"' + token.replace('"', '""') + '"'
                    for token in query.strip('"').strip().split())


def cjk_query(query: str) -> str:
    """只移除 CJK token 末尾前缀星号，不把用户字面的百分号当通配符。"""
    return ' '.join(token.rstrip('*') or token for token in query.split()).strip('"').strip()
