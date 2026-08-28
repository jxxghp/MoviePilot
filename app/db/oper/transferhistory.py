import time
from typing import Any, List, Optional, cast

from sqlalchemy import delete as sqlalchemy_delete
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.transferhistory import TransferHistory
from app.db.oper.query import (
    descending,
    enum_values,
    execute_page,
    literal_contains,
    media_identity_conditions,
    music_type_condition,
    required_media_identity_conditions,
)
from app.schemas.query import (
    QueryPageRequest,
    QuerySortField,
    TransferHistoryFilter,
)
from app.schemas.types import MediaSource


class TransferHistoryOper(DbOper):
    """
    转移历史管理
    """

    def get(self, historyid: int) -> Optional[TransferHistory]:
        """
        获取转移历史
        :param historyid: 转移历史id
        """
        return self.get_by_id(historyid)

    def get_by_id(self, record_id: int) -> Optional[TransferHistory]:
        """按稳定记录 ID 读取单条整理历史。"""
        return cast(
            Optional[TransferHistory],
            self._execute_sync_query(
                lambda session: session.execute(
                    select(TransferHistory).where(TransferHistory.id == record_id)
                ).scalars().first()
            ),
        )

    def query(
        self,
        filters: TransferHistoryFilter,
        page: QueryPageRequest,
    ) -> tuple[list[TransferHistory], int]:
        """按稳定筛选和分页合同读取整理历史记录及总数。"""
        def execute(session: Session) -> tuple[list[TransferHistory], int]:
            """在同一会话中构造并执行整理历史 count/page 查询。"""
            conditions = media_identity_conditions(TransferHistory, filters)
            ids = enum_values(filters.ids)
            media_types = enum_values(filters.media_types)
            media_sources = enum_values(filters.media_sources)
            if ids:
                conditions.append(TransferHistory.id.in_(ids))
            if media_types:
                conditions.append(TransferHistory.type.in_(media_types))
            if media_sources:
                conditions.append(TransferHistory.media_source.in_(media_sources))
            if filters.require_media_identity:
                conditions.extend(required_media_identity_conditions(TransferHistory))
            if filters.title:
                conditions.append(TransferHistory.title == filters.title)
            if filters.text:
                conditions.append(
                    literal_contains(TransferHistory.title, filters.text)
                    | literal_contains(TransferHistory.src, filters.text)
                    | literal_contains(TransferHistory.dest, filters.text)
                )
            for column, value in (
                (TransferHistory.year, filters.year),
                (TransferHistory.seasons, filters.seasons),
                (TransferHistory.episodes, filters.episodes),
                (TransferHistory.src, filters.src),
                (TransferHistory.dest, filters.dest),
                (TransferHistory.download_hash, filters.download_hash),
                (TransferHistory.episode_group, filters.episode_group),
            ):
                if value is not None and value != "":
                    conditions.append(column == value)
            if filters.status is not None:
                if filters.status:
                    conditions.append(TransferHistory.status.is_(True))
                else:
                    conditions.append(
                        or_(
                            TransferHistory.status.is_(False),
                            TransferHistory.status.is_(None),
                        )
                    )
            music_condition = music_type_condition(
                TransferHistory.music_type,
                filters.music_type,
            )
            if music_condition is not None:
                conditions.append(music_condition)

            count_statement = select(func.count(TransferHistory.id))
            page_statement = select(TransferHistory)
            if conditions:
                count_statement = count_statement.where(*conditions)
                page_statement = page_statement.where(*conditions)
            descending_order = descending(page)
            if page.sort.field == QuerySortField.ID:
                primary = (
                    TransferHistory.id.desc()
                    if descending_order
                    else TransferHistory.id.asc()
                )
                secondary = (
                    TransferHistory.date.desc()
                    if descending_order
                    else TransferHistory.date.asc()
                )
            else:
                primary = (
                    TransferHistory.date.desc().nullslast()
                    if descending_order
                    else TransferHistory.date.asc().nullsfirst()
                )
                secondary = (
                    TransferHistory.id.desc()
                    if descending_order
                    else TransferHistory.id.asc()
                )
            page_statement = page_statement.order_by(primary, secondary)
            return cast(
                tuple[list[TransferHistory], int],
                execute_page(session, count_statement, page_statement, page),
            )

        return self._execute_sync_query(execute)

    async def async_get(self, historyid: int) -> Optional[TransferHistory]:
        """
        异步获取转移历史。
        """
        return await self._execute_async_query(
            lambda session: TransferHistory.async_get(session, historyid)
        )

    async def async_list_by_title(
        self,
        title: str,
        page: int = 1,
        count: int = 30,
        status: Optional[bool] = None,
        wildcard: bool = False,
    ) -> List[TransferHistory]:
        """
        异步按标题分页查询转移记录。
        """
        return await self._execute_async_query(
            lambda session: TransferHistory.async_list_by_title(
                session,
                title=title,
                page=page,
                count=count,
                status=status,
                wildcard=wildcard,
            )
        )

    async def async_list_by_page(
        self,
        page: int = 1,
        count: int = 30,
        status: Optional[bool] = None,
    ) -> List[TransferHistory]:
        """
        异步分页查询转移记录。
        """
        return await self._execute_async_query(
            lambda session: TransferHistory.async_list_by_page(
                session, page=page, count=count, status=status
            )
        )

    async def async_count(self, status: Optional[bool] = None) -> Optional[int]:
        """
        异步统计转移记录数量。
        """
        return await self._execute_async_query(
            lambda session: TransferHistory.async_count(session, status=status)
        )

    async def async_count_by_title(
        self,
        title: str,
        status: Optional[bool] = None,
        wildcard: bool = False,
    ) -> Optional[int]:
        """
        异步按标题统计转移记录数量。
        """
        return await self._execute_async_query(
            lambda session: TransferHistory.async_count_by_title(
                session,
                title=title,
                status=status,
                wildcard=wildcard,
            )
        )

    def get_by_title(self, title: str) -> List[TransferHistory]:
        """
        按标题查询转移记录
        :param title: 数据key
        """
        return self._execute_sync_query(
            lambda session: TransferHistory.list_by_title(session, title)
        )

    def get_by_src(
            self, src: str, storage: Optional[str] = None
    ) -> Optional[TransferHistory]:
        """
        按源查询转移记录
        :param src: 数据key
        :param storage: 存储类型
        :return: 命中的整理记录，未命中时返回 None
        """
        return self._execute_sync_query(
            lambda session: TransferHistory.get_by_src(session, src, storage)
        )

    def get_by_transfer_task_id(
            self,
            *,
            task_id: str,
    ) -> Optional[TransferHistory]:
        """按稳定整理任务标识读取终态历史。"""
        return self._execute_sync_query(
            lambda session: TransferHistory.get_by_transfer_task_id(
                session,
                task_id=task_id,
            )
        )

    def get_success_by_src(
            self, src: str, storage: Optional[str] = None
    ) -> Optional[TransferHistory]:
        """
        按源查询成功的转移记录，源路径原样精确匹配
        :param src: 数据key
        :param storage: 存储类型
        :return: 命中的成功整理记录，未命中时返回 None
        """
        return self._execute_sync_query(
            lambda session: TransferHistory.get_success_by_src(
                session, src, storage
            )
        )

    def get_by_dest(
            self, dest: str, storage: Optional[str] = None
    ) -> Optional[TransferHistory]:
        """
        按转移路径查询转移记录
        :param dest: 数据key
        :param storage: 存储类型
        """
        return self._execute_sync_query(
            lambda session: TransferHistory.get_by_dest(session, dest, storage)
        )

    def list_success_by_src(
            self,
            src: str,
            storage: Optional[str] = None,
            recursive: bool = False,
    ) -> List[TransferHistory]:
        """
        按源路径查询成功整理记录。

        :param src: 源路径
        :param storage: 源存储类型
        :param recursive: 是否递归匹配目录子项
        :return: 命中的成功整理记录
        """
        return self._execute_sync_query(
            lambda session: TransferHistory.list_success_by_src(
                session,
                src=src,
                storage=storage,
                recursive=recursive,
            )
        )

    def list_success_move_by_dest(
            self,
            dest: str,
            storage: Optional[str] = None,
            recursive: bool = False,
    ) -> List[TransferHistory]:
        """
        按目标路径查询成功移动记录。

        :param dest: 目标路径
        :param storage: 目标存储类型
        :param recursive: 是否递归匹配目录子项
        :return: 命中的成功移动记录
        """
        return self._execute_sync_query(
            lambda session: TransferHistory.list_success_move_by_dest(
                session,
                dest=dest,
                storage=storage,
                recursive=recursive,
            )
        )

    def list_by_hash(self, download_hash: str) -> List[TransferHistory]:
        """
        按种子hash查询转移记录
        :param download_hash: 种子hash
        """
        return self._execute_sync_query(
            lambda session: TransferHistory.list_by_hash(session, download_hash)
        )

    def add(self, **kwargs):
        """
        新增转移历史
        """
        kwargs.update({
            "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        })
        self._stage_create(TransferHistory(**kwargs))

    def statistic(self, days: int = 7) -> List[Any]:
        """
        统计最近days天的下载历史数量
        """
        return self._execute_sync_query(
            lambda session: TransferHistory.statistic(session, days)
        )

    async def async_statistic(self, days: int = 7) -> List[Any]:
        """异步统计最近若干天的整理历史数量。"""
        return await self._execute_async_query(
            lambda session: TransferHistory.async_statistic(session, days)
        )

    def monthly_media_statistics(self) -> tuple[int, int, int, int]:
        """统计本月成功整理的电影、剧集、单集和音乐数量。"""
        return self._execute_sync_query(
            TransferHistory.monthly_media_statistics
        )

    def get_by(self, title: Optional[str] = None, year: Optional[str] = None, mtype: Optional[str] = None,
               season: Optional[str] = None, episode: Optional[str] = None,
               media_source: Optional[MediaSource] = None, media_id: Optional[str] = None,
               dest: Optional[str] = None) -> List[TransferHistory]:
        """
        按类型、标题、年份、季集查询转移记录
        """
        return self._execute_sync_query(
            lambda session: TransferHistory.list_by(
                db=session,
                mtype=mtype,
                title=title,
                dest=dest,
                year=year,
                season=season,
                episode=episode,
                media_source=media_source,
                media_id=media_id,
            )
        )

    def get_by_media_identity(
            self, media_source: MediaSource, media_id: str,
            mtype: Optional[str] = None,
    ) -> Optional[TransferHistory]:
        """按规范媒体身份和类型查询整理记录。"""
        return self._execute_sync_query(
            lambda session: TransferHistory.get_by_media_identity(
                db=session,
                media_source=media_source,
                media_id=media_id,
                mtype=mtype,
            )
        )

    def delete(self, historyid):
        """
        删除旧转移记录，失败任务历史由状态机独占。
        """
        self._execute_sync_write(
            lambda session: session.execute(
                sqlalchemy_delete(TransferHistory).where(
                    TransferHistory.id == historyid,
                    TransferHistory.transfer_task_id.is_(None),
                )
            )
        )

    def stage_delete(self, historyid: int) -> None:
        """暂存整理记录删除，事务由调用方统一提交。"""
        self._db.execute(
            sqlalchemy_delete(TransferHistory).where(
                TransferHistory.id == historyid,
                TransferHistory.transfer_task_id.is_(None),
            )
        )

    async def async_stage_delete(self, historyid: int) -> None:
        """在调用方异步事务内暂存旧整理记录删除。"""
        if not isinstance(self._db, AsyncSession):
            raise RuntimeError("整理历史异步删除需要调用方提供异步 Session")
        await self._db.execute(
            sqlalchemy_delete(TransferHistory).where(
                TransferHistory.id == historyid,
                TransferHistory.transfer_task_id.is_(None),
            )
        )

    def stage_truncate(self) -> None:
        """暂存旧整理记录删除，只保留当前失败任务历史。"""
        self._db.execute(
            sqlalchemy_delete(TransferHistory).where(
                TransferHistory.transfer_task_id.is_(None)
            )
        )

    async def async_delete(self, historyid):
        """
        异步删除旧转移记录，失败任务历史由状态机独占。
        """
        async def stage(session: AsyncSession) -> None:
            """在异步事务内只删除没有任务回执的历史。"""
            await session.execute(
                sqlalchemy_delete(TransferHistory).where(
                    TransferHistory.id == historyid,
                    TransferHistory.transfer_task_id.is_(None),
                )
            )

        await self._execute_async_write(stage)

    def truncate(self):
        """
        清空旧转移记录，只保留当前失败任务历史。
        """
        self._execute_sync_write(
            lambda session: session.execute(
                sqlalchemy_delete(TransferHistory).where(
                    TransferHistory.transfer_task_id.is_(None)
                )
            )
        )

    def stage_replace_by_src(self, **kwargs) -> TransferHistory:
        """在调用方事务内按源路径替换整理历史并返回已分配 ID 的新记录。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("整理历史事务写入需要调用方提供同步 Session")
        kwargs["src_storage"] = kwargs.get("src_storage") or "local"
        kwargs["date"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        return TransferHistory.replace_by_src(self._db, **kwargs)

    def stage_upsert_by_transfer_task_id(
            self,
            *,
            task_id: str,
            settlement_revision: int,
            retain_task_mapping: bool,
            payload: dict[str, Any],
    ) -> TransferHistory:
        """在调用方事务内按任务标识幂等暂存终态历史。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("整理历史任务结算需要调用方提供同步 Session")
        payload = dict(payload)
        payload["src_storage"] = payload.get("src_storage") or "local"
        payload["date"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        return TransferHistory.upsert_by_transfer_task_id(
            self._db,
            task_id=task_id,
            settlement_revision=settlement_revision,
            retain_task_mapping=retain_task_mapping,
            payload=payload,
        )

    def stage_bind_settlement(
            self,
            *,
            task_id: str,
            settlement_revision: int,
            src: str,
            storage: Optional[str] = None,
    ) -> Optional[TransferHistory]:
        """复用已有成功历史且清除失败任务映射，不改写业务字段。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("整理历史任务回执绑定需要调用方提供同步 Session")
        history = TransferHistory.get_success_by_src(self._db, src, storage)
        if history is None:
            return None
        history.transfer_task_id = None
        history.transfer_settlement_revision = None
        self._db.flush()
        return history

    def update_download_hash(self, historyid, download_hash):
        """
        补充转移记录download_hash
        """
        self._execute_sync_write(
            lambda session: TransferHistory.update_download_hash(
                session,
                historyid,
                download_hash,
            )
        )

    def stage_update_download_hash(
        self,
        historyid: int,
        download_hash: str,
    ) -> None:
        """在调用方事务内暂存整理历史下载任务 Hash 更新。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("整理历史同步更新需要调用方提供同步 Session")
        TransferHistory.update_download_hash(
            self._db,
            historyid,
            download_hash,
        )

    def list_by_date(self, date: str) -> List[TransferHistory]:
        """
        查询某时间之后的转移历史
        :param date: 日期
        """
        return self._execute_sync_query(
            lambda session: TransferHistory.list_by_date(session, date)
        )
