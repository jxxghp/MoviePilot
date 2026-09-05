"""钉钉自定义机器人通知模块。"""

from typing import List, Optional, Tuple, Union

from app.domain.context import Context, MediaInfo
from app.modules._base.notification import _MessageChannelModuleBase
from app.modules.dingtalk.dingtalk import DingTalk
from app.schemas.message import Message
from app.schemas.types import ModuleType, NotificationChannel


class DingTalkModule(_MessageChannelModuleBase[DingTalk]):
    """把 MoviePilot 通知转换为钉钉自定义机器人群消息。"""

    def init_module(self) -> None:
        """从已启用的 dingtalk 通知配置创建客户端实例。"""
        super().init_service(service_name=DingTalk.__name__.lower(), service_type=DingTalk)
        self._channel = NotificationChannel.DingTalk

    @staticmethod
    def get_name() -> str:
        """返回模块展示名称。"""
        return "DingTalk"

    @staticmethod
    def get_type() -> ModuleType:
        """声明该模块属于通知渠道。"""
        return ModuleType.Notification

    @staticmethod
    def get_subtype() -> NotificationChannel:
        """返回钉钉通知渠道枚举。"""
        return NotificationChannel.DingTalk

    @staticmethod
    def get_priority() -> int:
        """返回通知模块调度优先级。"""
        return 11

    def stop(self) -> None:
        """同步 Webhook 客户端没有需要释放的长连接资源。"""

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """钉钉启用状态由通知渠道配置统一管理。"""
        return None

    def _commands_enabled(self, config: Optional[dict[str, object]]) -> bool:
        """
        钉钉自定义机器人没有命令注册或删除 API，跳过基类命令处理。
        """
        return False

    def post_message(self, message: Message, **kwargs) -> None:
        """向所有匹配消息范围的钉钉配置发送普通通知。"""
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client = self.get_instance(conf.name)
            if client:
                client.send_msg(
                    title=message.title,
                    text=message.text,
                    image=message.image,
                    userid=str(message.userid) if message.userid else None,
                    link=message.link,
                )

    def post_medias_message(self, message: Message, medias: List[MediaInfo]) -> None:
        """把媒体候选列表降级为可点击的 Markdown 列表后发送。"""
        if not medias:
            return
        lines = []
        for index, media in enumerate(medias, start=1):
            label = media.title_year
            if media.detail_link:
                label = f"[{label}]({media.detail_link})"
            lines.append(f"{index}. {label}")
        self._post_list_message(message, "\n\n".join(lines))

    def post_torrents_message(self, message: Message, torrents: List[Context]) -> None:
        """把资源候选列表降级为可点击的 Markdown 列表后发送。"""
        if not torrents:
            return
        lines = []
        for index, context in enumerate(torrents, start=1):
            torrent = context.torrent_info
            label = torrent.title
            if torrent.page_url:
                label = f"[{label}]({torrent.page_url})"
            lines.append(f"{index}. {label}")
        self._post_list_message(message, "\n\n".join(lines))

    def _post_list_message(self, message: Message, text: str) -> None:
        """复用渠道筛选规则发送列表型 Markdown 消息。"""
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client = self.get_instance(conf.name)
            if client:
                client.send_msg(
                    title=message.title,
                    text=text,
                    userid=str(message.userid) if message.userid else None,
                    link=message.link,
                )
