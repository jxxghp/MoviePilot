"""在独立消息库上按 Hermes 策略召回候选，禁止加载全库正文后筛选。"""

import sqlite3
from typing import Any

from app.agent.history.query import (
    CJK,
    cjk_eligible,
    cjk_query,
    like_predicate,
    normalize,
    quote_tokens,
    relaxed,
    terms,
    trigram_eligible,
)
from app.application.messaging.recall import RecallQuery

SCAN_LIMIT = 300
HIDDEN_SOURCES = ('kanban', 'subagent', 'tool')
VISIBLE = '(m.active=1 OR m.compacted=1)'


def filters(query: RecallQuery) -> tuple[list[str], list[Any]]:
    """所有召回路径共用来源、角色、时间和可见性条件，避免回退扩大范围。"""
    where = [VISIBLE, "s.source NOT IN ('kanban','subagent','tool')"]
    params: list[Any] = []
    if query.role_filter:
        where.append('m.role IN (' + ','.join('?' for _ in query.role_filter) + ')')
        params.extend(query.role_filter)
    for operator, bound in [('>=', query.after), ('<', query.before)]:
        if bound is not None:
            where.append(f's.started_at {operator} ?')
            params.append(bound)
    return where, params


def match(connection: sqlite3.Connection, table: str, text: str, query: RecallQuery) -> list[dict[str, Any]]:
    """默认 BM25 排序，仅回传命中片段及元数据；正文按最终命中再展开。"""
    if table not in {'messages_fts', 'messages_fts_cjk', 'messages_fts_trigram'}:
        raise ValueError('未知 FTS5 索引')
    where, params = filters(query)
    where.insert(0, f'{table} MATCH ?')
    params.insert(0, text)
    order = {'newest': 'm.timestamp DESC,rank', 'oldest': 'm.timestamp ASC,rank'}.get(query.sort, 'rank')
    sql = f"""SELECT m.id,m.session_id,m.role,m.active,m.compacted,s.source,
        s.started_at,s.title,s.model,s.parent_session_id,s.end_reason,
        snippet({table},-1,'>>>','<<<','...',40) AS snippet
        FROM {table} JOIN messages m ON m.id={table}.rowid JOIN sessions s ON s.id=m.session_id
        WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?"""
    return [dict(row) for row in connection.execute(sql, [*params, SCAN_LIMIT])]


def scan(connection: sqlite3.Connection, text: str, query: RecallQuery) -> list[dict[str, Any]]:
    """显式工具全文、短 CJK 或失效索引回退；外层 SQLite VM deadline 限制实际耗时。"""
    predicate, term_params, anchor = like_predicate(text)
    if not predicate or not anchor:
        return []
    where, params = filters(query)
    where.append(f'({predicate})')
    order = 'ASC' if query.sort == 'oldest' else 'DESC'
    sql = f"""SELECT m.id,m.session_id,m.role,m.active,m.compacted,s.source,
        s.started_at,s.title,s.model,s.parent_session_id,s.end_reason,
        substr(m.content,MAX(1,instr(lower(m.content),lower(?))-60),240) AS snippet
        FROM messages m JOIN sessions s ON s.id=m.session_id
        WHERE {' AND '.join(where)} ORDER BY m.timestamp {order} LIMIT ?"""
    return [dict(row) for row in connection.execute(sql, [anchor, *params, *term_params, SCAN_LIMIT])]


def candidates(connection: sqlite3.Connection, indexes: tuple[str, ...], query: RecallQuery) -> tuple[list[dict[str, Any]], str]:
    """对齐 Hermes 的普通词、CJK、trigram 和 OR 放宽路由，显式报告所走路径。"""
    text = normalize(query.query)
    if not text:
        return [], 'empty'
    if 'tool' in query.role_filter or not indexes:
        return scan(connection, text, query), 'like'
    is_cjk = bool(CJK.search(text))
    if is_cjk:
        raw = cjk_query(text)
        expression = quote_tokens(raw)
        if 'messages_fts_cjk' in indexes and cjk_eligible(raw):
            return match(connection, 'messages_fts_cjk', expression, query), 'cjk'
        cjk_terms = [unit for unit in terms(raw) if CJK.search(unit)]
        if 'messages_fts_trigram' in indexes and cjk_terms and all(sum(map(len, CJK.findall(unit))) >= 3 for unit in cjk_terms):
            return match(connection, 'messages_fts_trigram', expression, query), 'trigram'
        # Hermes 短 CJK 回退按非运算符词项 OR 匹配，不冒充普通 FTS 的严格 AND。
        fallback = ' OR '.join('"' + unit.replace('"', '') + '"' for unit in raw.split() if unit.upper() not in {'AND', 'OR', 'NOT'})
        return scan(connection, fallback, query), 'like'
    rows = match(connection, 'messages_fts', text, query) if 'messages_fts' in indexes else []
    if rows:
        return rows, 'fts5'
    for table in ('messages_fts_cjk', 'messages_fts_trigram'):
        if table in indexes and (table != 'messages_fts_trigram' or trigram_eligible(text)):
            rows = match(connection, table, quote_tokens(text), query)
            if rows:
                return rows, table.removeprefix('messages_fts_')
    relaxed_text = relaxed(text)
    if relaxed_text and 'messages_fts' in indexes:
        return match(connection, 'messages_fts', relaxed_text, query), 'fts5_or'
    return [], 'fts5'
