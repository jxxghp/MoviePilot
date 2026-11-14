"""
外部媒体服务器回调的解析与业务事件分发。
"""

from typing import Any

from app.chain.base import ChainBase
from app.schemas.types import EventType


class WebhookChain(ChainBase):
    """
    Webhook处理链
    """

    def message(self, body: Any, form: Any, args: Any) -> None:
        """
        处理Webhook报文并发送事件
        """
        event_info = self.webhook_parser(body=body, form=form, args=args)
        if not event_info:
            return
        self.eventmanager.send_event(EventType.WebhookMessage, event_info)
