import time
from typing import Any, List, Optional

from sqlalchemy import delete as sqlalchemy_delete
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.transferhistory import TransferHistory
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
        return TransferHistory.get(self._db, historyid)

    async def async_get(self, historyid: int) -> Optional[TransferHistory]:
        """
        异步获取转移历史。
        """
        return await TransferHistory.async_get(self._db, historyid)

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
        return await TransferHistory.async_list_by_title(
            self._db,
            title=title,
            page=page,
            count=count,
            status=status,
            wildcard=wildcard,
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
        return await TransferHistory.async_list_by_page(
            self._db, page=page, count=count, status=status
        )

    async def async_count(self, status: Optional[bool] = None) -> Optional[int]:
        """
        异步统计转移记录数量。
        """
        return await TransferHistory.async_count(self._db, status=status)

    async def async_count_by_title(
        self,
        title: str,
        status: Optional[bool] = None,
        wildcard: bool = False,
    ) -> Optional[int]:
        """
        异步按标题统计转移记录数量。
        """
        return await TransferHistory.async_count_by_title(
            self._db,
            title=title,
            status=status,
            wildcard=wildcard,
        )

    def get_by_title(self, title: str) -> List[TransferHistory]:
        """
        按标题查询转移记录
        :param title: 数据key
        """
        return TransferHistory.list_by_title(self._db, title)

    def get_by_src(
            self, src: str, storage: Optional[str] = None
    ) -> Optional[TransferHistory]:
        """
        按源查询转移记录
        :param src: 数据key
        :param storage: 存储类型
        :return: 命中的整理记录，未命中时返回 None
        """
        return TransferHistory.get_by_src(self._db, src, storage)

    def get_success_by_src(
            self, src: str, storage: Optional[str] = None
    ) -> Optional[TransferHistory]:
        """
        按源查询成功的转移记录，源路径原样精确匹配
        :param src: 数据key
        :param storage: 存储类型
        :return: 命中的成功整理记录，未命中时返回 None
        """
        return TransferHistory.get_success_by_src(self._db, src, storage)

    def get_by_dest(
            self, dest: str, storage: Optional[str] = None
    ) -> Optional[TransferHistory]:
        """
        按转移路径查询转移记录
        :param dest: 数据key
        :param storage: 存储类型
        """
        return TransferHistory.get_by_dest(self._db, dest, storage)

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
        return TransferHistory.list_success_by_src(
            self._db,
            src=src,
            storage=storage,
            recursive=recursive,
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
        return TransferHistory.list_success_move_by_dest(
            self._db,
            dest=dest,
            storage=storage,
            recursive=recursive,
        )

    def list_by_hash(self, download_hash: str) -> List[TransferHistory]:
        """
        按种子hash查询转移记录
        :param download_hash: 种子hash
        """
        return TransferHistory.list_by_hash(self._db, download_hash)

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
        return TransferHistory.statistic(self._db, days)

    async def async_statistic(self, days: int = 7) -> List[Any]:
        """异步统计最近若干天的整理历史数量。"""
        return await TransferHistory.async_statistic(self._db, days)

    def monthly_media_statistics(self) -> tuple[int, int, int, int]:
        """统计本月成功整理的电影、剧集、单集和音乐数量。"""
        return TransferHistory.monthly_media_statistics(self._db)

    def get_by(self, title: Optional[str] = None, year: Optional[str] = None, mtype: Optional[str] = None,
               season: Optional[str] = None, episode: Optional[str] = None,
               media_source: Optional[MediaSource] = None, media_id: Optional[str] = None,
               dest: Optional[str] = None) -> List[TransferHistory]:
        """
        按类型、标题、年份、季集查询转移记录
        """
        return TransferHistory.list_by(db=self._db,
                                       mtype=mtype,
                                       title=title,
                                       dest=dest,
                                       year=year,
                                       season=season,
                                       episode=episode,
                                       media_source=media_source,
                                       media_id=media_id)

    def get_by_media_identity(
            self, media_source: MediaSource, media_id: str,
            mtype: Optional[str] = None,
    ) -> Optional[TransferHistory]:
        """按规范媒体身份和类型查询整理记录。"""
        return TransferHistory.get_by_media_identity(
            db=self._db,
            media_source=media_source,
            media_id=media_id,
            mtype=mtype,
        )

    def delete(self, historyid):
        """
        删除转移记录
        """
        self._stage_delete(TransferHistory, historyid)

    def stage_delete(self, historyid: int) -> None:
        """暂存整理记录删除，不由模型装饰器提交事务。"""
        self._db.execute(
            sqlalchemy_delete(TransferHistory).where(
                TransferHistory.id == historyid
            )
        )

    def stage_truncate(self) -> None:
        """暂存全部整理记录删除，由请求级事务统一提交。"""
        self._db.execute(sqlalchemy_delete(TransferHistory))

    async def async_delete(self, historyid):
        """
        异步删除转移记录。
        """
        await self._stage_async_delete(TransferHistory, historyid)

    def truncate(self):
        """
        清空转移记录
        """
        self._stage_truncate(TransferHistory)

    def add_force(self, **kwargs) -> Optional[TransferHistory]:
        """
        新增转移历史，并以同源存储的记录为准替换旧记录。
        """
        # 文件项的默认存储是 local；归一化旧调用传入的 None，确保运行时语义与
        # (src, src_storage) 唯一索引一致。
        kwargs["src_storage"] = kwargs.get("src_storage") or "local"
        # 旧记录的清理交给 replace_by_src 按 (src, src_storage) 处理：
        # 仅按 src 删除会连带删掉其他存储下同路径的记录。
        kwargs.update({
            "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        })
        def stage(session: Session) -> Optional[TransferHistory]:
            """在同一事务替换记录并返回兼容查询投影。"""
            TransferHistory.replace_by_src(session, **kwargs)
            return TransferHistory.get_by_src(
                session,
                kwargs.get("src"),
                kwargs["src_storage"],
            )

        # 保持 add_force 的既有返回契约：返回可被调用方安全读取字段的查询结果，
        # 而非事务提交后可能已脱离会话的新建实例。
        return self._execute_sync_write(stage)

    def stage_replace_by_src(self, **kwargs) -> TransferHistory:
        """在调用方事务内按源路径替换整理历史并返回已分配 ID 的新记录。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("整理历史事务写入需要调用方提供同步 Session")
        kwargs["src_storage"] = kwargs.get("src_storage") or "local"
        kwargs["date"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        self._db.execute(
            sqlalchemy_delete(TransferHistory).where(
                TransferHistory.src == kwargs.get("src"),
                TransferHistory.src_storage == kwargs["src_storage"],
            )
        )
        self._db.flush()
        history = TransferHistory(**kwargs)
        self._db.add(history)
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

    def list_by_date(self, date: str) -> List[TransferHistory]:
        """
        查询某时间之后的转移历史
        :param date: 日期
        """
        return TransferHistory.list_by_date(self._db, date)
