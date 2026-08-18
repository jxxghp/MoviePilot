from typing import Optional, Tuple, Union

from app.schemas.types import NotificationChannel


class InteractionChainMixin:
    """
    斜杠命令交互四件套委托：remote_list / parse_callback /
    handle_callback_interaction / handle_text_interaction。

    subscribe、site 等业务链的交互入口完全同构，唯一差异是各自的
    交互处理器构造参数。本 mixin 将四件套委托提取为公共实现，
    子类只需注入处理器类并实现 _interaction_handler 构造器。

    子类注入约定：
    - `_interaction_handler_type`：交互处理器类，提供静态 parse_callback；
    - `_interaction_handler()`：按各链业务动作构造处理器实例。
    """

    # 交互处理器类，子类注入（如 SubscribeInteractionHandler / SiteInteractionHandler）
    _interaction_handler_type: type = None

    def _interaction_handler(self):
        """
        构造交互处理器实例，由子类按各自业务动作注入实现。
        """
        raise NotImplementedError

    def remote_list(
            self,
            arg_str: str = "",
            channel: NotificationChannel = None,
            userid: Union[str, int] = None,
            source: Optional[str] = None,
    ):
        """
        斜杠命令统一入口，委托交互处理器。
        """
        return self._interaction_handler().remote_list(
            arg_str=arg_str, channel=channel, userid=userid, source=source
        )

    @classmethod
    def parse_callback(cls, callback_data: str) -> Optional[Tuple[str, str]]:
        """
        解析斜杠命令按钮回调。
        """
        return cls._interaction_handler_type.parse_callback(callback_data)

    def handle_callback_interaction(
            self,
            callback_data: str,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> bool:
        """委托交互处理器处理按钮回调。"""
        return self._interaction_handler().handle_callback_interaction(
            callback_data=callback_data,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

    def handle_text_interaction(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            text: str,
    ) -> bool:
        """委托交互处理器处理文本输入。"""
        return self._interaction_handler().handle_text_interaction(
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            text=text,
        )
