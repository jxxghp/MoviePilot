"""定时任务的编排层执行网关。"""

from typing import Any, Callable, Dict, Optional

from app.application.database import get_database_governance
from app.application.messaging.message import MessageHelper
from app.application.orchestration import ChainBase


class SchedulerChain:
    """
    定时任务执行网关，持有消息与模块分发设施：
    - 提供数据表清理
    - 广播 scheduler_job/clear_cache 给实现该接口的模块与插件
    - 转发系统提示消息
    """
    # 保留旧常量，插件和维护脚本如有引用无需跟随内部职责迁移。
    DEFAULT_BATCH_SIZE = 500

    def __init__(self):
        """初始化消息与模块分发设施实例。"""
        self._chain = ChainBase()

    @property
    def messagehelper(self) -> MessageHelper:
        """消息中心，用于记录需要在前端展示的系统提示消息"""
        return self._chain.messagehelper

    def cleanup(
            self,
            batch_size: Optional[int] = None,
            progress_callback: Optional[Callable[..., None]] = None,
    ) -> Dict[str, Any]:
        """
        按配置保留期执行分批清理。
        """
        return get_database_governance().cleanup(
            batch_size=batch_size,
            progress_callback=progress_callback,
        )

    def scheduler_job(self) -> None:
        """
        广播公共定时任务，由实现该接口的模块与插件自行处理
        """
        self._chain.scheduler_job()

    def clear_cache(self) -> None:
        """
        广播缓存清理，由实现该接口的模块与插件自行处理
        """
        self._chain.clear_cache()

    def post_message(self, *args, **kwargs) -> None:
        """
        发送系统通知消息，参数透传给消息分发设施
        """
        return self._chain.post_message(*args, **kwargs)
