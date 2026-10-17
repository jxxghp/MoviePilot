"""
媒体服务器条目缓存的数据访问入口，适配调用方会话及兼容事务执行器。
"""

from typing import TYPE_CHECKING, Any, Optional, Union, cast

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.mediaserver import MediaServerItem

if TYPE_CHECKING:
    from app.schemas.types import MediaSource


class MediaServerOper(DbOper):
    """
    媒体服务器数据管理
    """

    def __init__(
        self,
        db: Optional[Union[Session, AsyncSession]] = None,
    ) -> None:
        """
        保存调用方提供的同步或异步查询会话。
        """
        super().__init__(db)

    @staticmethod
    def __prepare_payload(kwargs: dict[str, Any]) -> dict[str, Any]:
        """
        过滤数据库模型不存在或不应由远端覆盖的字段
        """
        return {
            k: v for k, v in kwargs.items()
            if hasattr(MediaServerItem, k) and k != "id"
        }

    @staticmethod
    def _contains_season(item: MediaServerItem, season: int) -> bool:
        """
        兼容 JSON 往返后变为字符串的 seasoninfo 键。
        """
        seasoninfo = item.seasoninfo or {}
        return season in seasoninfo or str(season) in seasoninfo

    @classmethod
    def _select_cross_source_item(
        cls,
        items: list[MediaServerItem],
        season: Optional[int] = None,
    ) -> Optional[MediaServerItem]:
        """
        从跨来源标题候选中选出唯一作品。

        跨来源无法比对 ID，标题和年份只有在候选指向同一身份时才可信；
        同名同年的不同作品无法区分，宁可判为未入库也不能借用他人的季集。
        """
        identities = {
            (item.media_source, item.media_id)
            for item in items
            if item.media_source and item.media_id
        }
        if len(identities) > 1:
            return None
        if season is not None:
            # 同一作品分布在多个媒体库时，优先选择包含目标季的条目。
            return next(
                (item for item in items if cls._contains_season(item, season)),
                None,
            )
        return items[0] if items else None

    def add(self, **kwargs: Any) -> bool:
        """
        新增媒体服务器数据
        """
        kwargs = self.__prepare_payload(kwargs)
        server = kwargs.get("server")
        item_id = kwargs.get("item_id")
        if not server or not item_id:
            return False
        item = MediaServerItem(**kwargs)
        existing = self._execute_sync_query(
            lambda session: MediaServerItem.get_by_server_itemid(
                session, server, item_id
            )
        )
        if not existing:
            self._stage_create(item)
            return True
        return False

    def upsert(self, **kwargs: Any) -> bool:
        """
        按媒体服务器和条目ID新增或更新数据
        """
        kwargs = self.__prepare_payload(kwargs)
        server = kwargs.get("server")
        item_id = kwargs.get("item_id")
        if not server or not item_id:
            return False

        item = self._execute_sync_query(
            lambda session: MediaServerItem.get_by_server_itemid(
                session, server, item_id
            )
        )
        if item:
            self._stage_update(item, kwargs)
            return False

        self._stage_create(MediaServerItem(**kwargs))
        return True

    def empty(self, server: Optional[str] = None) -> None:
        """
        清空媒体服务器数据
        """
        self._execute_sync_write(
            lambda session: MediaServerItem.empty(session, server)
        )

    def delete_stale(self, server: str, sync_time: str) -> int:
        """
        删除本轮同步未更新的旧数据
        """
        return self._execute_sync_write(
            lambda session: MediaServerItem.delete_stale(
                session,
                server,
                sync_time,
            )
        )

    def delete_excluded_servers(self, servers: list[str]) -> int:
        """
        删除未启用或已移除媒体服务器的数据
        """
        return self._execute_sync_write(
            lambda session: MediaServerItem.delete_excluded_servers(
                session,
                servers,
            )
        )

    def exists(self, **kwargs: Any) -> Optional[MediaServerItem]:
        """
        判断媒体服务器数据是否存在，来源身份未命中时按标题信息跨来源回退。
        """
        if kwargs.get("media_source") and kwargs.get("media_id"):
            item = self._execute_sync_query(
                lambda session: MediaServerItem.exist_by_media_identity(
                    session,
                    media_source=cast("MediaSource | str", kwargs.get("media_source")),
                    media_id=cast(str, kwargs.get("media_id")),
                    mtype=cast(Optional[str], kwargs.get("mtype")),
                    server=kwargs.get("server"),
                )
            )
            if not item and kwargs.get("title") and kwargs.get("year"):
                # 媒体库只保存一个主来源，非主来源卡片需要标题和年份兜底。
                item = self._select_cross_source_item(
                    self._execute_sync_query(
                        lambda session: MediaServerItem.list_cross_source_by_title(
                            session,
                            title=cast(str, kwargs.get("title")),
                            mtype=cast(Optional[str], kwargs.get("mtype")),
                            year=cast(str | int, kwargs.get("year")),
                            media_source=cast("MediaSource | str", kwargs["media_source"]),
                            server=kwargs.get("server"),
                        )
                    ),
                    kwargs.get("season"),
                )
        elif kwargs.get("title"):
            # 按标题、类型、年份查
            item = self._execute_sync_query(
                lambda session: MediaServerItem.exists_by_title(
                    session,
                    title=cast(str, kwargs.get("title")),
                    mtype=cast(Optional[str], kwargs.get("mtype")),
                    year=cast(Optional[str | int], kwargs.get("year")),
                    server=kwargs.get("server"),
                )
            )
        else:
            return None
        if not item:
            return None

        if kwargs.get("season") is not None:
            # 判断季是否存在
            if not self._contains_season(item, kwargs["season"]):
                return None
        return item

    async def async_exists(self, **kwargs: Any) -> Optional[MediaServerItem]:
        """
        异步判断媒体服务器数据是否存在，来源身份未命中时按标题信息跨来源回退。
        """
        if kwargs.get("media_source") and kwargs.get("media_id"):
            item = await self._execute_async_query(
                lambda session: MediaServerItem.async_exist_by_media_identity(
                    session,
                    media_source=cast("MediaSource | str", kwargs.get("media_source")),
                    media_id=cast(str, kwargs.get("media_id")),
                    mtype=cast(Optional[str], kwargs.get("mtype")),
                    server=kwargs.get("server"),
                )
            )
            if not item and kwargs.get("title") and kwargs.get("year"):
                # 与同步入口保持一致：跨来源卡片按标题和年份回退匹配媒体库条目。
                item = self._select_cross_source_item(
                    await self._execute_async_query(
                        lambda session: MediaServerItem.async_list_cross_source_by_title(
                            session,
                            title=cast(str, kwargs.get("title")),
                            mtype=cast(Optional[str], kwargs.get("mtype")),
                            year=cast(str | int, kwargs.get("year")),
                            media_source=cast("MediaSource | str", kwargs["media_source"]),
                            server=kwargs.get("server"),
                        )
                    ),
                    kwargs.get("season"),
                )
        elif kwargs.get("title"):
            # 按标题、类型、年份查
            item = await self._execute_async_query(
                lambda session: MediaServerItem.async_exists_by_title(
                    session,
                    title=cast(str, kwargs.get("title")),
                    mtype=cast(Optional[str], kwargs.get("mtype")),
                    year=cast(Optional[str | int], kwargs.get("year")),
                    server=kwargs.get("server"),
                )
            )
        else:
            return None
        if not item:
            return None

        if kwargs.get("season") is not None:
            # 判断季是否存在
            if not self._contains_season(item, kwargs["season"]):
                return None
        return item

    def get_item_id(self, **kwargs: Any) -> Optional[str]:
        """
        获取媒体服务器数据ID
        """
        item = self.exists(**kwargs)
        if not item:
            return None
        return str(item.item_id)

    async def async_get_item_id(self, **kwargs: Any) -> Optional[str]:
        """
        异步获取媒体服务器数据ID
        """
        item = await self.async_exists(**kwargs)
        if not item:
            return None
        return str(item.item_id)
