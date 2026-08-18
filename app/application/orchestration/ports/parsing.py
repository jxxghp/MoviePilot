"""外部报文解析域的能力端口客户端。"""

from __future__ import annotations

from typing import Any, Optional

from app.application.orchestration.ports.dispatch import CapabilityPorts
from app.schemas.mediaserver import WebhookEventInfo
from app.schemas.message import IncomingMessage


class ParsingPorts(CapabilityPorts):
    """交互消息与 Webhook 报文解析的能力端口。"""

    def message_parser(
            self, source: str, body: Any, form: Any, args: Any
    ) -> Optional[IncomingMessage]:
        """
        解析消息内容，返回字典，注意以下约定值：
        userid: 用户ID
        username: 用户名
        text: 内容
        :param source: 消息来源（渠道配置名称）
        :param body: 请求体
        :param form: 表单
        :param args: 参数
        :return: 消息渠道、消息内容
        """
        return self._dispatch.unicast(
            "message_parser", source=source, body=body, form=form, args=args
        )

    def webhook_parser(
            self, body: Any, form: Any, args: Any
    ) -> Optional[WebhookEventInfo]:
        """
        解析Webhook报文体
        :param body:  请求体
        :param form:  请求表单
        :param args:  请求参数
        :return: 字典，解析为消息时需要包含：title、text、image
        """
        return self._dispatch.unicast(
            "webhook_parser", body=body, form=form, args=args
        )
