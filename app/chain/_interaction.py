"""
业务处理链到消息交互处理器的统一委托。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Optional, Tuple, Union

from app.chain._contracts import InteractionMixinHost
from app.schemas.types import NotificationChannel

if TYPE_CHECKING:
    from app.application.messaging.site import SiteInteractionHandler
    from app.application.messaging.subscribe import SubscribeInteractionHandler


class InteractionChainMixin:
    """
    统一委托斜杠命令的列表、回调解析、按钮处理和文本交互：remote_list / parse_callback /
    handle_callback_interaction / handle_text_interaction。

    subscribe、site 等业务链的交互入口完全同构，唯一差异是各自的
    交互处理器构造参数。本混入类将这些委托提取为公共实现，
    子类只需注入处理器类并实现 _interaction_handler 构造器。

    子类注入约定：
    - `_interaction_handler_type`：交互处理器类，提供静态 parse_callback；
    - `_interaction_handler()`：按各链业务动作构造处理器实例。
    """

    __mixin_host_protocol__: ClassVar[Any] = InteractionMixinHost

    # 交互处理器类，子类注入（如 SubscribeInteractionHandler / SiteInteractionHandler）
    _interaction_handler_type: Optional[type[SiteInteractionHandler | SubscribeInteractionHandler]] = None

    def _interaction_handler(self) -> SiteInteractionHandler | SubscribeInteractionHandler:
        """
        构造交互处理器实例，由子类按各自业务动作注入实现。
        """
        raise NotImplementedError

    def remote_list(
            self,
            arg_str: str = "",
            channel: Optional[NotificationChannel] = None,
            userid: Optional[Union[str, int]] = None,
            source: Optional[str] = None,
    ) -> object:
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
        handler_type = cls._interaction_handler_type
        if handler_type is None:
            raise AttributeError("'NoneType' object has no attribute 'parse_callback'")
        return handler_type.parse_callback(callback_data)

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
        """
        委托交互处理器处理按钮回调。
        """
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
        """
        委托交互处理器处理文本输入。
        """
        return self._interaction_handler().handle_text_interaction(
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            text=text,
        )
