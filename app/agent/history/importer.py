"""旧恢复快照的可续跑搬迁；每次只处理有界会话批次。"""

import sqlite3
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from langchain_core.messages import messages_from_dict

from app.agent.history.message import evidence, legacy_identity
from app.application.messaging.recall import RecallLegacyRepository, RecallMessage, RecallSession


def import_page(connection: sqlite3.Connection, legacy: RecallLegacyRepository, user_id: str,
                append: Callable[[str, RecallSession, tuple[RecallMessage, ...]], None]) -> dict[str, Any]:
    """搬迁成功后才前进游标，失败可重试；结果始终携带尚未完成状态。"""
    state = dict(connection.execute("SELECT key,value FROM state_meta WHERE key IN ('legacy_cursor','legacy_done')"))
    if state.get('legacy_done'):
        return dict(complete=True)
    cursor = int(state.get('legacy_cursor', '0'))
    records = legacy.page(user_id, cursor, 20)
    for record in records:
        messages = messages_from_dict(list(record.messages))
        batch = []
        for position, message in enumerate(messages):
            if not message.id:
                message.id = legacy_identity(record.session.session_id, position, message)
            item = evidence(message)
            if item is not None:
                batch.append(replace(item, provenance='legacy_snapshot'))
        for start in range(0, len(batch), 100):
            append(user_id, record.session, tuple(batch[start:start + 100]))
        cursor = record.id
        with connection:
            connection.execute("INSERT OR REPLACE INTO state_meta VALUES ('legacy_cursor',?)", (str(cursor),))
    if len(records) < 20:
        with connection:
            connection.execute("INSERT OR REPLACE INTO state_meta VALUES ('legacy_done','1')")
    return dict(complete=len(records) < 20, last_source_id=cursor,
                note='Only surviving legacy snapshots can be imported. Earlier discarded messages are unavailable.')
