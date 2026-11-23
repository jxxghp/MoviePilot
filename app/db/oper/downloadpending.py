"""
待确认下载记录的数据访问入口，适配调用方会话及兼容事务执行器。
"""

from typing import List, Optional

from app.db.base import DbOper
from app.db.models.downloadpending import DownloadPending


class DownloadPendingOper(DbOper):
    """
    待确认下载记录管理。
    """

    def list_all(self) -> List[DownloadPending]:
        """
        查询当前全部待确认下载记录。

        :return: 待确认记录列表
        """
        return self._execute_sync_query(DownloadPending.list_all)

    def list_by_target_keys(self, target_keys: List[str]) -> List[DownloadPending]:
        """
        按媒体级目标键批量查询待确认下载记录。

        :param target_keys: 待查询的目标键列表
        :return: 匹配的待确认记录列表
        """
        return self._execute_sync_query(
            lambda session: DownloadPending.list_by_target_keys(session, target_keys)
        )

    def list_by_subscription_id(self, subscription_id: int) -> List[DownloadPending]:
        """
        按订阅主键查询待确认下载记录。

        :param subscription_id: 订阅主键
        :return: 匹配的待确认记录列表
        """
        return self._execute_sync_query(
            lambda session: DownloadPending.list_by_subscription_id(session, subscription_id)
        )

    def get_by_key(self, pending_key: str) -> Optional[DownloadPending]:
        """
        按唯一键查询单条待确认记录。

        :param pending_key: 待确认记录唯一键
        :return: 匹配记录；不存在时返回 None
        """
        return self._execute_sync_query(
            lambda session: DownloadPending.get_by_key(session, pending_key)
        )

    def upsert(self, pending_key: str, **kwargs: object) -> DownloadPending:
        """
        新增或更新单条待确认记录。

        :param pending_key: 待确认记录唯一键
        :param kwargs: 需写入的模型列值
        :return: 写入后的 ORM 实例
        """
        return self._execute_sync_write(
            lambda session: DownloadPending.upsert(session, pending_key=pending_key, **kwargs)
        )

    def touch_checked(self, pending_key: str, checked_at: str) -> Optional[DownloadPending]:
        """
        更新单条待确认记录的最近核对时间。

        :param pending_key: 待确认记录唯一键
        :param checked_at: 本次核对时间文本
        :return: 更新后的 ORM 实例；不存在时返回 None
        """
        return self._execute_sync_write(
            lambda session: DownloadPending.touch_checked(
                session, pending_key=pending_key, checked_at=checked_at
            )
        )

    def delete_by_key(self, pending_key: str) -> int:
        """
        按唯一键删除待确认记录。

        :param pending_key: 待确认记录唯一键
        :return: 删除的行数
        """
        return self._execute_sync_write(
            lambda session: DownloadPending.delete_by_key(session, pending_key)
        )

    def delete_by_target_key(self, target_key: str) -> int:
        """
        按媒体级目标键删除关联的待确认记录。

        :param target_key: 媒体级目标键
        :return: 删除的行数
        """
        return self._execute_sync_write(
            lambda session: DownloadPending.delete_by_target_key(session, target_key)
        )
