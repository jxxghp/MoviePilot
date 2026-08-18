from typing import Any

from app.application.orchestration.ports import ModuleCapabilityDispatch, ParsingPorts
from app.runtime.events import EventManager
from app.schemas.types import EventType


class WebhookChain:
    """
    Webhook处理链，只组合报文解析域的能力端口
    """

    def __init__(self) -> None:
        """
        组合报文解析端口与事件管理器
        """
        self.parsing = ParsingPorts(ModuleCapabilityDispatch())
        self.eventmanager = EventManager()

    def message(self, body: Any, form: Any, args: Any) -> None:
        """
        处理Webhook报文并发送事件
        """
        # 获取主体内容
        event_info = self.parsing.webhook_parser(body=body, form=form, args=args)
        if not event_info:
            return
        # 广播事件
        self.eventmanager.send_event(EventType.WebhookMessage, event_info)
