"""Agent 专属 SQLite 消息库：用户独立文件、短连接、WAL 和有界查询。"""

import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.agent.history.schema import INDEXES, SCHEMA_VERSION, advance_index, detach_index, ensure_schema, load_cjk
from app.agent.history.search import SCAN_LIMIT, candidates
from app.agent.history.view import bookends, hydrate, lineage, metadata, read, shape, title_hit, visible_history, window
from app.agent.policy.sanitizer import sanitize_archived_text
from app.application.messaging.recall import RecallLegacyRepository, RecallMessage, RecallQuery, RecallSession
from app.foundation.identity import build_user_memory_key


class SqliteRecallRepository:
    """每个用户一个 state.db，搜索排名和文件路径均不包含其他用户数据。"""

    def __init__(self, runtime_dir: Path, *, query_timeout: float = 3.0, legacy: RecallLegacyRepository | None = None, retention_cutoff: Callable[[], float | None] | None = None) -> None:
        """只保存路径；所有连接都在宿主 worker 内打开并在操作结束时关闭。"""
        self._root: Path = runtime_dir / 'history'
        self._extension = runtime_dir / 'lib' / 'libfts5_cjk.so'
        self._query_timeout = query_timeout
        self._legacy = legacy
        self._retention_cutoff = retention_cutoff

    def database_path(self, user_id: str) -> Path:
        """仅接受宿主真实用户身份，使用不可逆目录键防止路径注入。"""
        key = build_user_memory_key(user_id)
        if not key:
            raise ValueError('历史消息库需要真实用户身份')
        return self._root / 'users' / key / 'state.db'

    @contextmanager
    def _connect(self, user_id: str, *, write: bool = False) -> Iterator[tuple[sqlite3.Connection, tuple[str, ...]]]:
        """短生命周期连接；读操作通过 SQLite VM deadline 终止昂贵回退。"""
        path = self.database_path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(path, timeout=self._query_timeout)
        connection.row_factory = sqlite3.Row
        try:
            path.chmod(0o600)
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA journal_size_limit=67108864')
            connection.execute('PRAGMA checkpoint_fullfsync=ON')
            cjk = load_cjk(connection, self._extension)
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            needs_cjk = cjk and not connection.execute("SELECT 1 FROM sqlite_master WHERE name='messages_fts_cjk'").fetchone()
            detached = version == SCHEMA_VERSION and connection.execute(
                "SELECT 1 FROM state_meta WHERE value='detached' AND (key<>'messages_fts_cjk' OR ?)", (cjk,)).fetchone()
            if version != SCHEMA_VERSION or needs_cjk or detached:
                indexes = ensure_schema(connection, cjk=cjk)
            else:
                if not cjk:
                    detach_index(connection, 'messages_fts_cjk')
                    connection.commit()
                indexes = tuple(name for name in INDEXES
                                if connection.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone()
                                and not connection.execute('SELECT 1 FROM state_meta WHERE key=?', (name,)).fetchone())
            if not write:
                connection.execute('BEGIN')
                deadline = time.monotonic() + self._query_timeout
                connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            with connection:
                yield connection, indexes
        finally:
            connection.close()

    def maintain(self, user_id: str, *, batch_size: int = 500) -> dict[str, Any]:
        """显式维护入口按批回填索引，返回进度供宿主续跑，不由模型调用。"""
        if not 1 <= batch_size <= 2000:
            raise ValueError('索引维护批次无效')
        migration = self._import_legacy(user_id)
        expired = self._expire(user_id, batch_size)
        with self._connect(user_id, write=True) as (connection, _):
            for table in INDEXES:
                advance_index(connection, table, batch_size=batch_size)
            progress = dict(connection.execute("SELECT key,value FROM state_meta WHERE value='building' OR key LIKE '%:progress' OR key LIKE '%:high'"))
            return dict(pending=expired == batch_size or not migration.get('complete') or any(value == 'building' for value in progress.values()),
                        indexes=progress, legacy_import=migration)

    def _expire(self, user_id: str, batch_size: int) -> int:
        """独立库按共享 Agent 保留期清理，不依赖主库行是否存在。"""
        cutoff = self._retention_cutoff() if self._retention_cutoff else None
        if cutoff is None:
            return 0
        with self._connect(user_id, write=True) as (connection, _):
            sessions = list(connection.execute('SELECT id FROM sessions WHERE last_active<? ORDER BY last_active LIMIT ?',
                                               (cutoff, batch_size)))
            for row in sessions:
                connection.execute('INSERT OR IGNORE INTO deleted_sessions VALUES (?)', (row['id'],))
                connection.execute('DELETE FROM messages WHERE session_id=?', (row['id'],))
                connection.execute('DELETE FROM sessions WHERE id=?', (row['id'],))
            return len(sessions)

    def append(self, user_id: str, session: RecallSession, messages: tuple[RecallMessage, ...]) -> None:
        """派生索引损坏时只失效索引并重试一次，原始消息库仍是事实来源。"""
        try:
            self._append_batch(user_id, session, messages)
        except sqlite3.DatabaseError as error:
            if not self._recover_indexes(user_id, error):
                raise
            self._append_batch(user_id, session, messages)

    def _recover_indexes(self, user_id: str, error: sqlite3.DatabaseError) -> bool:
        """仅处理可确认的 FTS 虚表损坏，绝不删除或重建原始消息数据库。"""
        if getattr(error, 'sqlite_errorcode', None) != sqlite3.SQLITE_CORRUPT_VTAB and 'fts5' not in str(error).lower():
            return False
        connection = sqlite3.connect(self.database_path(user_id), timeout=self._query_timeout)
        try:
            load_cjk(connection, self._extension)
            with connection:
                for table in INDEXES:
                    detach_index(connection, table)
        finally:
            connection.close()
        return True

    def _append_batch(self, user_id: str, session: RecallSession, messages: tuple[RecallMessage, ...]) -> None:
        """会话、原始消息和 FTS 在一个事务内提交，工具原文不按索引前缀截断。"""
        if not session.session_id or len(session.session_id) > 255 or len(messages) > 100:
            raise ValueError('历史会话或消息批次无效')
        if any(not message.message_id or message.role not in {'user', 'assistant', 'tool'} for message in messages):
            raise ValueError('历史消息缺少稳定 ID 或不是原始消息')
        now = time.time()
        with self._connect(user_id, write=True) as (connection, _):
            if connection.execute('SELECT 1 FROM deleted_sessions WHERE id=?', (session.session_id,)).fetchone():
                return
            connection.execute('''INSERT INTO sessions(id,source,title,model,started_at,last_active,parent_session_id,end_reason)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE sessions.title END,
                model=CASE WHEN excluded.model<>'' THEN excluded.model ELSE sessions.model END''',
                (session.session_id,session.source,session.title,session.model,session.started_at or now,session.last_active or now,
                 session.parent_session_id,session.end_reason))
            connection.executemany('''INSERT INTO messages(session_id,message_uid,role,content,tool_call_id,
                tool_name,tool_calls,tool_status,timestamp,provenance) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id,message_uid) DO NOTHING''',
                [(session.session_id, item.message_id, item.role, sanitize_archived_text(item.content), item.tool_call_id,
                  item.tool_name,sanitize_archived_text(item.tool_calls),item.tool_status,item.timestamp or (session.last_active if item.provenance == 'legacy_snapshot' and session.last_active else now),item.provenance) for item in messages])

    def compact(self, user_id: str, session_id: str, message_ids: tuple[str, ...]) -> None:
        """只改活动状态而不更新索引内容，压缩后仍能召回同会话的原始证据。"""
        with self._connect(user_id, write=True) as (connection, _):
            connection.executemany('UPDATE messages SET active=0,compacted=1 WHERE session_id=? AND message_uid=?',
                                   [(session_id, message_id) for message_id in message_ids])

    def delete(self, user_id: str, session_id: str) -> None:
        """先删除消息同步索引，再移除会话；删除墓碑阻止在途采集复活历史。"""
        with self._connect(user_id, write=True) as (connection, _):
            connection.execute('INSERT OR IGNORE INTO deleted_sessions VALUES (?)', (session_id,))
            connection.execute('DELETE FROM messages WHERE session_id=?', (session_id,))
            connection.execute('DELETE FROM sessions WHERE id=?', (session_id,))

    def search(self, user_id: str, query: RecallQuery) -> dict[str, Any]:
        """索引失效后只重试一次，索引回填由后台维护续跑。"""
        try:
            return self._query(user_id, query)
        except sqlite3.DatabaseError as error:
            if self._recover_indexes(user_id, error):
                return self._query(user_id, query)
            return dict(success=False, error='storage_unavailable', complete=False)

    def _query(self, user_id: str, query: RecallQuery) -> dict[str, Any]:
        """四种形态与 Hermes 同义；索引缺失或查询超时不能伪报检索完整。"""
        if not 1 <= query.limit <= 10 or not 0 <= query.window <= 20 or len(query.exclude_session_ids) > 20:
            raise ValueError('历史检索预算无效')
        try:
            with self._connect(user_id) as (connection, indexes):
                if query.session_id:
                    result = self._scroll(connection, query) if query.around_message_id else read(connection, query.session_id)
                elif not query.query.strip():
                    result = self._browse(connection, query)
                else:
                    result = self._discover(connection, indexes, query)
                done = self._legacy is None or bool(connection.execute("SELECT 1 FROM state_meta WHERE key='legacy_done'").fetchone())
                result['legacy_import'] = dict(complete=done, note='Older surviving snapshots are imported in background; misses are inconclusive while incomplete.')
                result['index_status'] = {name: name in indexes for name in INDEXES}
                return result
        except sqlite3.OperationalError as error:
            reason = 'query_timeout' if 'interrupted' in str(error) or 'locked' in str(error) else 'query_unavailable'
            return dict(success=False, error=reason, complete=False,
                        hint='Narrow the query or time range; an unavailable search does not mean history is absent.')

    def _import_legacy(self, user_id: str) -> dict[str, Any]:
        """首次查询分批搬迁已有快照，完成后不再读取主库。"""
        if self._legacy is None:
            return dict(complete=True)
        from app.agent.history.importer import import_page

        with self._connect(user_id, write=True) as (connection, _):
            return import_page(connection, self._legacy, user_id, self.append)

    @staticmethod
    def _scroll(connection: sqlite3.Connection, query: RecallQuery) -> dict[str, Any]:
        """只展开可见历史；父会话/子锚点仅在同一谱系内允许重绑定。"""
        if not metadata(connection, query.session_id):
            return dict(success=False, error='session_not_found')
        row = connection.execute('SELECT m.*,s.end_reason FROM messages m JOIN sessions s ON s.id=m.session_id WHERE m.id=?',
                                 (query.around_message_id,)).fetchone()
        if not row or not visible_history(connection, dict(row), query.current_session_id):
            return dict(success=False, error='anchor_not_in_history')
        owner = row['session_id']
        if owner != query.session_id and lineage(connection, owner) != lineage(connection, query.session_id):
            return dict(success=False, error='anchor_not_in_session')
        view = window(connection, owner, query.around_message_id, query.window)
        return dict(success=True, mode='scroll', session_id=owner, around_message_id=query.around_message_id,
                    messages=[shape(item, cap=4000, anchor=query.around_message_id) for item in view.get('window', [])],
                    messages_before=view.get('messages_before', 0), messages_after=view.get('messages_after', 0),
                    hint='Scroll forward using the LAST message id; backward using the FIRST. The anchor repeats for orientation.')

    @staticmethod
    def _browse(connection: sqlite3.Connection, query: RecallQuery) -> dict[str, Any]:
        """最近会话先走活动时间索引限量取候选，不扫描全库会话链。"""
        rows = connection.execute("SELECT * FROM sessions WHERE source NOT IN ('kanban','subagent','tool') ORDER BY last_active DESC LIMIT ?",
                                  (query.limit + 15,))
        results: list[dict[str, Any]] = []
        for row in rows:
            if row['id'] == query.current_session_id:
                continue
            first, _ = bookends(connection, row['id'], 1, 0)
            results.append(dict(session_id=row['id'], title=row['title'], source=row['source'],
                                started_at=row['started_at'], last_active=row['last_active'],
                                message_count=row['message_count'], preview=shape(first[0], cap=80)['content'] if first else ''))
            if len(results) == query.limit:
                break
        return dict(success=True, mode='browse', results=results, count=len(results))

    @staticmethod
    def _discover(connection: sqlite3.Connection, indexes: tuple[str, ...], query: RecallQuery) -> dict[str, Any]:
        """300 候选先降低 cron 优先级再谱系去重，首个命中自适应展开。"""
        hits, route = candidates(connection, indexes, query)
        excluded = {lineage(connection, item) for item in query.exclude_session_ids}
        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        title = title_hit(connection, query.query)
        if title and (query.after is None or title['started_at'] >= query.after) and (query.before is None or title['started_at'] < query.before):
            hits = [title, *hits]
        for hit in sorted(hits, key=lambda row: row['source'] == 'cron'):
            root = lineage(connection, hit['session_id'])
            if root in seen or root in excluded or not visible_history(connection, hit, query.current_session_id):
                continue
            entry = hydrate(connection, hit, full=query.detail == 'full' or not results)
            if entry is not None:
                seen.add(root)
                results.append(entry)
            if len(results) >= query.limit:
                break
        return dict(success=True, mode='discover', query=query.query, detail=query.detail,
                    results=results, count=len(results), search_path=route,
                    candidate_limit=SCAN_LIMIT, candidate_limit_reached=len(hits) == SCAN_LIMIT,
                    hint='Search ANDs terms by default. Broaden with OR; quote phrases or use prefix*. Scroll exact anchors for evidence.')
