from datetime import datetime
from typing import List, Optional, Tuple

from app.db.base import DbOper
from app.db.models.transferpending import TransferPending


class TransferPendingOper(DbOper):
    """
    待整理文件登记管理。

    只保存「存储 + 源文件路径」这一最小事实，用于在进程重启后把没走完整理链的
    文件重新送回去，避免挂载故障重启后永久漏件。
    """

    def register(self, storage: str, src_path: str) -> Optional[TransferPending]:
        """
        登记一个待整理文件。
        :param storage: 存储
        :param src_path: 源文件路径
        :return: 登记记录
        """
        return TransferPending.register(
            self._db,
            storage=storage,
            src_path=src_path,
            now_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    def discard(self, storage: str, src_path: str) -> int:
        """
        注销一个待整理文件登记。
        :param storage: 存储
        :param src_path: 源文件路径
        :return: 删除的记录数
        """
        return TransferPending.discard(self._db, storage=storage, src_path=src_path)

    def list_all(self, limit: Optional[int] = 5000) -> List[Tuple[str, str]]:
        """
        列出全部待整理登记，供启动回放使用。

        返回纯元组而不是 ORM 实例：回放发生在会话之外，ORM 实例脱离 session
        后访问属性会触发 DetachedInstanceError。
        :param limit: 单次回放上限
        :return: (存储, 源文件路径) 列表
        """
        return [
            (item.storage, item.src_path)
            for item in TransferPending.list_all(self._db, limit=limit) or []
            if item and item.storage and item.src_path
        ]

    def clear(self) -> int:
        """
        清空全部待整理登记。
        :return: 删除的记录数
        """
        return TransferPending.clear(self._db)
