"""保留旧 schemas 整理工作项的宽松构造契约。"""

from typing import Any, Optional

from app.application.transfer import (
    TransferQueue as CanonicalTransferQueue,
    TransferTask as CanonicalTransferTask,
)


def _serialize_legacy_value(value: Any) -> Any:
    """同时支持旧 Pydantic 对象与新的领域对象序列化接口。"""
    if value is None:
        return None
    if callable(getattr(value, "to_dict", None)):
        return value.to_dict()
    if callable(getattr(value, "model_dump", None)):
        return value.model_dump()
    return value


class TransferTask(CanonicalTransferTask):
    """兼容旧任务允许插件传入自定义 meta/mediainfo 对象的行为。"""

    meta: Optional[Any] = None
    mediainfo: Optional[Any] = None

    def to_dict(self):
        """返回兼容领域对象和旧 Pydantic 对象的任务字典。"""
        values = vars(self).copy()
        values["fileitem"] = _serialize_legacy_value(self.fileitem)
        values["meta"] = _serialize_legacy_value(self.meta)
        values["mediainfo"] = _serialize_legacy_value(self.mediainfo)
        values["target_directory"] = _serialize_legacy_value(self.target_directory)
        return values


class TransferQueue(CanonicalTransferQueue):
    """让旧宽松任务可以继续进入新的整理队列。"""

    task: Optional[TransferTask] = None


__all__ = ["TransferQueue", "TransferTask"]
