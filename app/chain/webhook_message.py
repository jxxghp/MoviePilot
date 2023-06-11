from typing import Any

from app.chain import ChainBase


class WebhookMessageChain(ChainBase):
    """
    响应Webhook事件
    """

    def process(self, body: Any, form: Any, args: Any) -> None:
        """
        处理Webhook报文并发送消息
        """
        # 获取主体内容
        info: dict = self.webhook_parser(body=body, form=form, args=args)
        if not info:
            return
        # 发送消息
        self.post_message(title=info.get("title"), text=info.get("text"), image=info.get("image"))
