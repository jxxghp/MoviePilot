"""整理任务领域对象的共享类型收窄与字典投影。"""

from typing import Any, Protocol, cast

from app.domain.meta.metabase import MetaBase


class _DictionarySerializable(Protocol):
    """描述领域对象沿用的字典投影能力。"""

    def to_dict(self) -> dict[str, Any]:
        """返回领域对象的字典投影。"""


class _TransferTaskMetaSource(Protocol):
    """描述整理任务提供已解析领域元数据的最小形状。"""

    meta: MetaBase | None


def domain_to_dict(value: object) -> dict[str, Any]:
    """按领域对象既有 ``to_dict`` 合同生成字典投影。"""
    return cast(_DictionarySerializable, value).to_dict()


def transfer_task_meta(task: _TransferTaskMetaSource) -> MetaBase:
    """声明进入作业与规划边界的整理任务已完成元数据解析。"""
    return cast(MetaBase, task.meta)
