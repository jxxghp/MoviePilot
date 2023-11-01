from typing import Any

from app.chain import ChainBase
from app.schemas.types import EventType
from app.utils.singleton import Singleton


class WebhookChain(ChainBase, metaclass=Singleton):
    """
    Webhook处理链
    """

    def message(self, body: Any, form: Any, args: Any) -> None:
        """
        处理Webhook报文并发送事件
        """
        # 获取主体内容
        event_info = self.webhook_parser(body=body, form=form, args=args)
        if not event_info:
            return
        # 广播事件
        self.eventmanager.send_event(EventType.WebhookMessage, event_info)
