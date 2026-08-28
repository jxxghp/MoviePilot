"""调度器兼容 Chain。"""

from typing import Any, Callable, Dict, Optional

from app.application.database import get_database_governance
from app.chain.base import ChainBase


class SchedulerChain(ChainBase):
    """保留插件使用的公共定时任务与数据治理入口。"""

    DEFAULT_BATCH_SIZE = 500

    def cleanup(
        self,
        batch_size: Optional[int] = None,
        progress_callback: Optional[Callable[..., None]] = None,
    ) -> Dict[str, Any]:
        """按配置保留期执行分批清理。"""
        return get_database_governance().cleanup(
            batch_size=batch_size,
            progress_callback=progress_callback,
        )


# 旧插件可能持久化或比较完整类路径，保持迁移前可观察身份。
SchedulerChain.__module__ = "app.scheduler"
