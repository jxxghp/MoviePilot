import time
from typing import Any

from app.chain import ChainBase
from app.utils.http import WebUtils
from app.schemas.types import EventType


class WebhookMessageChain(ChainBase):
    """
    响应Webhook事件
    """

    def process(self, body: Any, form: Any, args: Any) -> None:
        """
        处理Webhook报文并发送消息
        """
        # 获取主体内容
        event_info: dict = self.webhook_parser(body=body, form=form, args=args)
        if not event_info:
            return
        # 广播事件
        self.eventmanager.send_event(EventType.WebhookMessage, event_info)
        # 拼装消息内容
        _webhook_actions = {
            "library.new": "新入库",
            "system.webhooktest": "测试",
            "playback.start": "开始播放",
            "playback.stop": "停止播放",
            "user.authenticated": "登录成功",
            "user.authenticationfailed": "登录失败",
            "media.play": "开始播放",
            "media.stop": "停止播放",
            "PlaybackStart": "开始播放",
            "PlaybackStop": "停止播放",
            "item.rate": "标记了"
        }
        _webhook_images = {
            "emby": "https://emby.media/notificationicon.png",
            "plex": "https://www.plex.tv/wp-content/uploads/2022/04/new-logo-process-lines-gray.png",
            "jellyfin": "https://play-lh.googleusercontent.com/SCsUK3hCCRqkJbmLDctNYCfehLxsS4ggD1ZPHIFrrAN1Tn9yhjmGMPep2D9lMaaa9eQi"
        }

        if not _webhook_actions.get(event_info.get('event')):
            return

        # 消息标题
        if event_info.get('item_type') in ["TV", "SHOW"]:
            message_title = f"{_webhook_actions.get(event_info.get('event'))}剧集 {event_info.get('item_name')}"
        elif event_info.get('item_type') == "MOV":
            message_title = f"{_webhook_actions.get(event_info.get('event'))}电影 {event_info.get('item_name')}"
        elif event_info.get('item_type') == "AUD":
            message_title = f"{_webhook_actions.get(event_info.get('event'))}有声书 {event_info.get('item_name')}"
        else:
            message_title = f"{_webhook_actions.get(event_info.get('event'))}"

        # 消息内容
        message_texts = []
        if event_info.get('user_name'):
            message_texts.append(f"用户：{event_info.get('user_name')}")
        if event_info.get('device_name'):
            message_texts.append(f"设备：{event_info.get('client')} {event_info.get('device_name')}")
        if event_info.get('ip'):
            message_texts.append(f"IP地址：{event_info.get('ip')} {WebUtils.get_location(event_info.get('ip'))}")
        if event_info.get('percentage'):
            percentage = round(float(event_info.get('percentage')), 2)
            message_texts.append(f"进度：{percentage}%")
        if event_info.get('overview'):
            message_texts.append(f"剧情：{event_info.get('overview')}")
        message_texts.append(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))}")

        # 消息图片
        if not event_info.get("image_url"):
            image_url = _webhook_images.get(event_info.get("channel"))
        else:
            image_url = event_info.get("image_url")

        # 消息内容
        message_content = "\n".join(message_texts)

        # 发送消息
        self.post_message(title=message_title, text=message_content, image=image_url)
