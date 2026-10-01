"""独立 state.db 模式与 FTS5 投影，采用 external-content 布局。"""

import sqlite3
import sys
from pathlib import Path

BUNDLED_CJK_EXTENSION = Path(sys.prefix) / "lib/moviepilot/libfts5_cjk.so"
SCHEMA_VERSION = 1
TOOL_PREFIX_CHARS = 8192
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS state_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '', started_at REAL NOT NULL, last_active REAL NOT NULL,
    parent_session_id TEXT NOT NULL DEFAULT '', end_reason TEXT NOT NULL DEFAULT '',
    message_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_recent ON sessions(last_active DESC);
CREATE INDEX IF NOT EXISTS sessions_title ON sessions(title COLLATE NOCASE, started_at DESC);
CREATE TABLE IF NOT EXISTS deleted_sessions (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_uid TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
    tool_call_id TEXT NOT NULL DEFAULT '', tool_name TEXT NOT NULL DEFAULT '',
    tool_calls TEXT NOT NULL DEFAULT '', tool_status TEXT NOT NULL DEFAULT '',
    timestamp REAL NOT NULL, provenance TEXT NOT NULL DEFAULT 'observed',
    active INTEGER NOT NULL DEFAULT 1, compacted INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id, message_uid)
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
CREATE TRIGGER IF NOT EXISTS messages_count_insert AFTER INSERT ON messages BEGIN
    UPDATE sessions SET message_count=message_count+1, last_active=MAX(last_active,new.timestamp)
    WHERE id=new.session_id;
END;
CREATE TRIGGER IF NOT EXISTS messages_count_delete AFTER DELETE ON messages BEGIN
    UPDATE sessions SET message_count=message_count-1 WHERE id=old.session_id;
END;
"""
INDEXES = {"messages_fts": "unicode61", "messages_fts_trigram": "trigram", "messages_fts_cjk": "cjk_unicode61"}


def load_cjk(connection: sqlite3.Connection, extension: Path) -> bool:
    """优先使用运行环境自带扩展，兼容旧配置目录；每次加载后立即关闭权限。"""
    if not hasattr(connection, "enable_load_extension"):
        return False
    for candidate in (BUNDLED_CJK_EXTENSION, extension):
        if not candidate.is_file():
            continue
        try:
            connection.enable_load_extension(True)
            connection.load_extension(str(candidate))
            return True
        except (sqlite3.Error, OSError):
            continue
        finally:
            connection.enable_load_extension(False)
    return False


def index_sql(table: str) -> str:
    """视图、触发器与重建共享同一投影，避免裁剪边界漂移损坏 FTS。"""
    tokenizer = INDEXES[table]
    base = table == "messages_fts"
    columns = "content,tool_name,tool_calls" if base else "content,tool_name"
    content = f"CASE WHEN role='tool' THEN substr(content,1,{TOOL_PREFIX_CHARS}) ELSE content END" if base else "content"
    predicate = "1" if base else "role<>'tool' AND session_id IN (SELECT id FROM sessions WHERE source NOT IN ('cron','subagent'))"
    projection = f"{content} AS content,tool_name" + (",tool_calls" if base else "")
    # 通过视图按 id 取相同值；删除触发器须在原文仍存在时运行。
    return f"""
    CREATE VIEW IF NOT EXISTS {table}_src AS SELECT id,{projection} FROM messages WHERE {predicate};
    CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5(
        {columns},content='{table}_src',content_rowid='id',tokenize='{tokenizer}');
    CREATE TRIGGER IF NOT EXISTS {table}_insert AFTER INSERT ON messages
    WHEN new.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta WHERE key='{table}:high'),-1)
      OR new.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta WHERE key='{table}:progress'),-1)
    BEGIN
        INSERT INTO {table}(rowid,{columns}) SELECT id,{columns} FROM {table}_src WHERE id=new.id;
    END;
    CREATE TRIGGER IF NOT EXISTS {table}_delete BEFORE DELETE ON messages
    WHEN old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta WHERE key='{table}:high'),-1)
      OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta WHERE key='{table}:progress'),-1)
    BEGIN
        INSERT INTO {table}({table},rowid,{columns}) SELECT 'delete',id,{columns} FROM {table}_src WHERE id=old.id;
    END;
    """


def ensure_schema(connection: sqlite3.Connection, *, cjk: bool) -> tuple[str, ...]:
    """新库建表，已有原文的新索引分批回填；不在请求内执行全库 rebuild。"""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError("Agent 消息库版本高于当前程序，拒绝降级写入")
    connection.executescript(SCHEMA_SQL)
    available = []
    for table in INDEXES:
        if table == "messages_fts_cjk" and not cjk:
            detach_index(connection, table)
            continue
        existing = connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
        status = connection.execute("SELECT value FROM state_meta WHERE key=?", (table,)).fetchone()
        try:
            if not existing or status and status[0] == 'detached':
                begin_rebuild(connection, table)
            else:
                connection.executescript(index_sql(table))
        except sqlite3.OperationalError as error:
            if "no such tokenizer" not in str(error) and "no such module: fts5" not in str(error):
                raise
            detach_index(connection, table)
            continue
        if not connection.execute("SELECT 1 FROM state_meta WHERE key=?", (table,)).fetchone():
            available.append(table)
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    connection.commit()
    return tuple(available)


def detach_index(connection: sqlite3.Connection, table: str) -> None:
    """不可用派生索引先拆写触发器，保护原文；不得把缺口索引当作完整召回。"""
    if table not in INDEXES:
        raise ValueError("未知历史索引")
    for suffix in ("insert", "delete"):
        connection.execute(f"DROP TRIGGER IF EXISTS {table}_{suffix}")
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
        connection.execute("INSERT OR REPLACE INTO state_meta VALUES (?, 'detached')", (table,))


def begin_rebuild(connection: sqlite3.Connection, table: str) -> None:
    """原子重建派生空表并设置高水位；新写即时索引，旧行按进度处理删除。"""
    if table not in INDEXES:
        raise ValueError("未知历史索引")
    script = f"""
    BEGIN IMMEDIATE;
    DROP TRIGGER IF EXISTS {table}_insert;
    DROP TRIGGER IF EXISTS {table}_delete;
    DROP TABLE IF EXISTS {table};
    INSERT OR REPLACE INTO state_meta VALUES ('{table}', 'building');
    INSERT OR REPLACE INTO state_meta VALUES ('{table}:high', (SELECT CAST(COALESCE(MAX(id),0) AS TEXT) FROM messages));
    INSERT OR REPLACE INTO state_meta VALUES ('{table}:progress','0');
    {index_sql(table)}
    COMMIT;
    """
    try:
        connection.executescript(script)
    except sqlite3.Error:
        connection.rollback()
        raise
    advance_index(connection, table, batch_size=0)


def advance_index(connection: sqlite3.Connection, table: str, *, batch_size: int = 500) -> None:
    """一个短写事务回填有限原始行，与新写/删除共享精确的已索引谓词。"""
    status = connection.execute("SELECT value FROM state_meta WHERE key=?", (table,)).fetchone()
    if not status or status[0] != 'building':
        return
    with connection:
        # 先占写锁再读取进度，多个宿主 worker 不会重复插入同一批 posting。
        connection.execute("UPDATE state_meta SET value=value WHERE key=?", (table,))
        high = int(connection.execute("SELECT value FROM state_meta WHERE key=?", (f'{table}:high',)).fetchone()[0])
        progress = int(connection.execute("SELECT value FROM state_meta WHERE key=?", (f'{table}:progress',)).fetchone()[0])
        ids = list(connection.execute('SELECT id FROM messages WHERE id>? AND id<=? ORDER BY id LIMIT ?',
                                      (progress, high, batch_size)))
        end = ids[-1][0] if ids else (high if batch_size else progress)
        if ids:
            columns = 'content,tool_name,tool_calls' if table == 'messages_fts' else 'content,tool_name'
            connection.execute(f'INSERT INTO {table}(rowid,{columns}) SELECT id,{columns} FROM {table}_src WHERE id>? AND id<=?',
                               (progress, end))
        connection.execute('UPDATE state_meta SET value=? WHERE key=?', (str(end), f'{table}:progress'))
        if end >= high:
            connection.executemany('DELETE FROM state_meta WHERE key=?', [(table,), (f'{table}:high',), (f'{table}:progress',)])
