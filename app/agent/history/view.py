"""会话阅读、锚点窗口与首末书挡；只展开最终选择的消息。"""

import sqlite3
from typing import Any

from app.agent.history.search import HIDDEN_SOURCES, VISIBLE
from app.agent.policy.sanitizer import sanitize_for_host


def shape(row: sqlite3.Row | dict[str, Any], *, cap: int, anchor: int = 0) -> dict[str, Any]:
    """分别限制模型可见正文与工具参数，保留定位和截断证据。"""
    item = dict(row)
    content = str(sanitize_for_host(item['content'][:cap]))
    result = {key: item[key] for key in ('id', 'role', 'timestamp', 'provenance')}
    result['content'] = content
    if len(item['content']) > cap:
        result.update(content_truncated=True, original_content_chars=len(item['content']))
    for key in ('tool_name', 'tool_call_id', 'tool_calls', 'tool_status'):
        if item.get(key):
            result[key] = sanitize_for_host(item[key])
    if item['id'] == anchor:
        result['anchor'] = True
    return result


def metadata(connection: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    """不存在或隐藏来源都不返回可供猜测的会话元数据。"""
    row = connection.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
    return dict(row) if row and row['source'] not in HIDDEN_SOURCES else {}


def lineage(connection: sqlite3.Connection, session_id: str) -> str:
    """沿宿主记录的父链去重；环或异常长链按当前已知节点收口。"""
    visited: set[str] = set()
    current = session_id
    while current and current not in visited and len(visited) < 100:
        visited.add(current)
        parent = metadata(connection, current).get('parent_session_id')
        if not parent:
            break
        current = parent
    return current


def visible_history(connection: sqlite3.Connection, hit: dict[str, Any], current: str) -> bool:
    """排除仍在活动上下文的本会话和分支，压缩及 reset 历史可重新发现。"""
    if not current or lineage(connection, hit['session_id']) != lineage(connection, current):
        return True
    if hit.get('compacted'):
        return True
    return hit.get('end_reason') in {'compression', 'new_session', 'session_reset', 'session_switch', 'idle', 'daily', 'suspended', 'resume_pending_expired'}


def window(connection: sqlite3.Connection, session_id: str, anchor: int, radius: int) -> dict[str, Any]:
    """锚点归属精确匹配后按消息序列展开，跨会话主键间隙不改变窗口大小。"""
    target = connection.execute(f'SELECT m.* FROM messages m WHERE session_id=? AND id=? AND {VISIBLE}',
                                (session_id, anchor)).fetchone()
    if not target:
        return {}
    before = list(connection.execute(f'SELECT m.* FROM messages m WHERE session_id=? AND id<? AND {VISIBLE} ORDER BY id DESC LIMIT ?',
                                     (session_id, anchor, radius)))
    after = list(connection.execute(f'SELECT m.* FROM messages m WHERE session_id=? AND id>? AND {VISIBLE} ORDER BY id LIMIT ?',
                                    (session_id, anchor, radius)))
    counts = connection.execute(f'SELECT SUM(id<?),SUM(id>?) FROM messages m WHERE session_id=? AND {VISIBLE}',
                                (anchor, anchor, session_id)).fetchone()
    return dict(window=[*reversed(before), target, *after], messages_before=counts[0] or 0, messages_after=counts[1] or 0)


def bookends(connection: sqlite3.Connection, session_id: str, head: int = 3, tail: int = 3) -> tuple[list[Any], list[Any]]:
    """借助 session/id 索引只读首尾，不为截断返回值先加载全部会话。"""
    first = list(connection.execute(f'SELECT m.* FROM messages m WHERE session_id=? AND {VISIBLE} ORDER BY id LIMIT ?', (session_id, head)))
    last = list(connection.execute(f'SELECT m.* FROM messages m WHERE session_id=? AND {VISIBLE} ORDER BY id DESC LIMIT ?', (session_id, tail)))
    return first, list(reversed(last))


def hydrate(connection: sqlite3.Connection, hit: dict[str, Any], *, full: bool) -> dict[str, Any] | None:
    """首结果默认展开 ±5 和首尾各3，其余只给锚点，保留继续阅读方向。"""
    view = window(connection, hit['session_id'], hit['id'], 5 if full else 0)
    if not view:
        return None
    root = lineage(connection, hit['session_id'])
    meta = metadata(connection, root) or metadata(connection, hit['session_id'])
    first, last = bookends(connection, hit['session_id']) if full else ([], [])
    result = dict(session_id=hit['session_id'], title=meta.get('title'), when=meta.get('started_at'),
                  source=meta.get('source'), model=meta.get('model'), matched_role=hit['role'],
                  match_message_id=hit['id'], snippet=sanitize_for_host(hit.get('snippet', '')),
                  messages=[shape(row, cap=4000, anchor=hit['id']) for row in view['window']],
                  bookend_start=[shape(row, cap=1200) for row in first],
                  bookend_end=[shape(row, cap=1200) for row in last],
                  messages_before=view['messages_before'], messages_after=view['messages_after'],
                  detail='full' if full else 'compact')
    if root != hit['session_id']:
        result['parent_session_id'] = root
    return result


def read(connection: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    """阅读首20/尾10条消息，每条2000字符；不把消息数限制误当字节限制。"""
    meta = metadata(connection, session_id)
    if not meta:
        return dict(success=False, error='session_not_found')
    first, last = bookends(connection, session_id, 20, 10)
    rows = {row['id']: row for row in [*first, *last]}
    total = connection.execute(f'SELECT COUNT(*) FROM messages m WHERE session_id=? AND {VISIBLE}', (session_id,)).fetchone()[0]
    return dict(success=True, mode='read', session_id=session_id, session_meta=meta,
                message_count=total, truncated=total > len(rows),
                messages=[shape(rows[key], cap=2000) for key in sorted(rows)],
                hint='Use session_id + around_message_id to scroll through the middle.')


def title_hit(connection: sqlite3.Connection, title: str) -> dict[str, Any] | None:
    """先尝试精确标题及最新 #N 续篇，不把标题命中冒充正文命中。"""
    title = title.strip().strip("`'\"")
    escaped = title.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    row = connection.execute(
        "SELECT * FROM sessions WHERE title LIKE ? ESCAPE '\\' ORDER BY started_at DESC LIMIT 1",
        (escaped + ' #%',),
    ).fetchone() or connection.execute('SELECT * FROM sessions WHERE title=? ORDER BY started_at DESC LIMIT 1', (title,)).fetchone()
    if not row or row['source'] in HIDDEN_SOURCES:
        return None
    message = connection.execute(f'SELECT m.id,m.active,m.compacted FROM messages m WHERE session_id=? AND {VISIBLE} ORDER BY id LIMIT 1',
                                 (row['id'],)).fetchone()
    if not message:
        return None
    return dict(row) | dict(message) | dict(session_id=row['id'], role='session_title', snippet=f"Session title matched: {row['title']}")
