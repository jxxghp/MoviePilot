from typing import Any, Dict

from app.chain import ChainBase
from app.schemas.types import MessageChannel, NotificationAction


class NotificationChain(ChainBase):
    """
    通知渠道管理链，仅按渠道名透明转发管理动作到模块

    不包含任何渠道特定逻辑：动作语义、表单参数解释、客户端实例解析与
    临时参数初始化全部封闭在实现 channel_manage 契约的模块内部
    """

    def manage_channel(
            self,
            channel: MessageChannel,
            action: NotificationAction,
            **params: Any,
    ) -> Dict[str, Any]:
        """
        对指定通知渠道执行管理动作

        :param channel: 渠道标识，用于模块路由
        :param action: 通用管理动作，具体语义由渠道模块解释
        :param params: 表单与动作参数，原样透传给模块
        :return: 统一结构 {"success": bool, "message": ..., ...}
        """
        result = self.run_module("channel_manage", channel=channel, action=action, **params)
        return result or {"success": False, "message": "该通知渠道未启用或不支持此管理动作"}
