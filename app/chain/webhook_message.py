from app.chain import _ChainBase


class WebhookMessageChain(_ChainBase):
    """
    响应Webhook事件
    """

    def process(self, message: dict) -> None:
        """
        处理Webhook报文并发送消息
        """
        # 获取主体内容
        info: dict = self.run_module('webhook_parser', message=message)
        if not info:
            return
        # 发送消息
        self.run_module("post_message", title=info.get("title"), text=info.get("text"), image=info.get("image"))
