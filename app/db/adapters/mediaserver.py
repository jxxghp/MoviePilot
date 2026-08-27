"""媒体服务器本地缓存的显式短会话与事务适配器。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Optional, TypeVar, Union

from sqlalchemy.orm import Session

from app.application.mediaserver import MediaServerSyncItem
from app.db.oper.mediaserver import MediaServerOper
from app.db.uow import SqlAlchemyUnitOfWork
from app.schemas.types import MediaSource

T = TypeVar("T")


class TransactionalMediaServerRepository:
    """让每次媒体库缓存查询或变更各自拥有一个短生命周期 Session。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """保存由启动组合根提供的同步会话工厂。"""
        self._session_factory = session_factory

    def _read(self, operation: Callable[[MediaServerOper], T]) -> T:
        """在独立只读会话中执行媒体库缓存查询。"""
        with self._session_factory() as session:
            return operation(MediaServerOper(db=session))

    def _write(self, operation: Callable[[MediaServerOper], T]) -> T:
        """在独立 UoW 中执行并提交一项媒体库缓存变更。"""
        with self._session_factory() as session:
            unit_of_work = SqlAlchemyUnitOfWork(session)
            try:
                result = operation(MediaServerOper(db=session))
                unit_of_work.commit()
                return result
            except Exception:
                unit_of_work.rollback()
                raise

    def get_item_id(
        self,
        *,
        title: Optional[str] = None,
        year: Optional[Union[str, int]] = None,
        mtype: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        season: Optional[int] = None,
    ) -> Optional[str]:
        """在短会话中返回匹配条目的服务器 item_id。"""
        item_id: Optional[str] = self._read(
            lambda repository: repository.get_item_id(
                title=title,
                year=year,
                mtype=mtype,
                media_source=media_source,
                media_id=media_id,
                season=season,
            )
        )
        return item_id

    def upsert(self, item: MediaServerSyncItem) -> bool:
        """在单个短事务中新增或更新一个媒体库同步条目。"""
        payload = {
            "server": item.server,
            "library": item.library,
            "item_id": item.item_id,
            "item_type": item.item_type,
            "title": item.title,
            "original_title": item.original_title,
            "year": item.year,
            "media_source": item.media_source,
            "media_id": item.media_id,
            "path": item.path,
            "seasoninfo": {
                season: list(episodes)
                for season, episodes in item.seasoninfo
            },
            "note": (
                json.loads(item.note_json)
                if item.note_json is not None
                else None
            ),
            "lst_mod_date": item.lst_mod_date,
        }
        created: bool = self._write(
            lambda repository: repository.upsert(**payload)
        )
        return created

    def delete_stale(self, *, server: str, sync_time: str) -> int:
        """在短事务中删除指定服务器本轮未更新的条目。"""
        deleted: int = self._write(
            lambda repository: repository.delete_stale(
                server=server,
                sync_time=sync_time,
            ),
        )
        return deleted

    def delete_excluded_servers(self, servers: list[str]) -> int:
        """在短事务中删除已停用或已移除服务器的条目。"""
        deleted: int = self._write(
            lambda repository: repository.delete_excluded_servers(servers)
        )
        return deleted
