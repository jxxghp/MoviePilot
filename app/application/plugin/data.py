"""插件持久化数据写用例。"""

from typing import Protocol


class PluginDataMutationRepository(Protocol):
    """插件数据删除命令所需的无提交仓储端口。"""

    def stage_delete(self, plugin_id: str) -> None:
        """暂存目标插件的全部持久化数据删除。"""
        ...


class UnitOfWork(Protocol):
    """插件数据同步写用例所需的事务端口。"""

    def commit(self) -> None:
        """提交当前逻辑操作。"""
        ...

    def rollback(self) -> None:
        """回滚当前逻辑操作。"""
        ...


class DeletePluginDataCommand:
    """在一个显式事务中删除目标插件的全部持久化数据。"""

    def __init__(
        self,
        repository: PluginDataMutationRepository,
        unit_of_work: UnitOfWork,
    ) -> None:
        """保存无提交仓储和事务所有者。"""
        self._repository = repository
        self._unit_of_work = unit_of_work

    def execute(self, plugin_id: str) -> None:
        """暂存并提交删除；任一步失败时回滚并传播原异常。"""
        try:
            self._repository.stage_delete(plugin_id)
            self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise
