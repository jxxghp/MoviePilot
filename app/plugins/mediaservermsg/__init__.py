import time
from typing import Any, List, Dict, Tuple

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import WebhookEventInfo
from app.schemas.types import EventType, MediaType, MediaImageType, NotificationType
from app.utils.web import WebUtils


class MediaServerMsg(_PluginBase):
    # 插件名称
    plugin_name = "媒体库服务器通知"
    # 插件描述
    plugin_desc = "发送Emby/Jellyfin/Plex服务器的播放、入库等通知消息。"
    # 插件图标
    plugin_icon = "mediaplay.png"
    # 主题色
    plugin_color = "#42A3DB"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "mediaservermsg_"
    # 加载顺序
    plugin_order = 14
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _types = []

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

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._types = config.get("types") or []

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        types_options = [
            {"title": "新入库", "value": "library.new"},
            {"title": "开始播放", "value": "playback.start|media.play|PlaybackStart"},
            {"title": "停止播放", "value": "playback.stop|media.stop|PlaybackStop"},
            {"title": "用户标记", "value": "item.rate"},
            {"title": "测试", "value": "system.webhooktest"},
        ]
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'types',
                                            'label': '消息类型',
                                            'items': types_options
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '需要设置媒体服务器Webhook，回调相对路径为 /api/v1/webhook?token=moviepilot（3001端口），其中 moviepilot 为设置的 API_TOKEN。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "types": []
        }

    def get_page(self) -> List[dict]:
        pass

    @eventmanager.register(EventType.WebhookMessage)
    def send(self, event: Event):
        """
        发送通知消息
        """
        if not self._enabled:
            return

        event_info: WebhookEventInfo = event.event_data
        if not event_info:
            return

        # 不在支持范围不处理
        if not self._webhook_actions.get(event_info.event):
            return

        # 不在选中范围不处理
        msgflag = False
        for _type in self._types:
            if event_info.event in _type.split("|"):
                msgflag = True
                break
        if not msgflag:
            logger.info(f"未开启 {event_info.event} 类型的消息通知")
            return

        # 消息标题
        if event_info.item_type in ["TV", "SHOW"]:
            message_title = f"{self._webhook_actions.get(event_info.event)}剧集 {event_info.item_name}"
        elif event_info.item_type == "MOV":
            message_title = f"{self._webhook_actions.get(event_info.event)}电影 {event_info.item_name}"
        elif event_info.item_type == "AUD":
            message_title = f"{self._webhook_actions.get(event_info.event)}有声书 {event_info.item_name}"
        else:
            message_title = f"{self._webhook_actions.get(event_info.event)}"

        # 消息内容
        message_texts = []
        if event_info.user_name:
            message_texts.append(f"用户：{event_info.user_name}")
        if event_info.device_name:
            message_texts.append(f"设备：{event_info.client} {event_info.device_name}")
        if event_info.ip:
            message_texts.append(f"IP地址：{event_info.ip} {WebUtils.get_location(event_info.ip)}")
        if event_info.percentage:
            percentage = round(float(event_info.percentage), 2)
            message_texts.append(f"进度：{percentage}%")
        if event_info.overview:
            message_texts.append(f"剧情：{event_info.overview}")
        message_texts.append(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))}")

        # 消息内容
        message_content = "\n".join(message_texts)

        # 消息图片
        image_url = event_info.image_url
        # 查询剧集图片
        if (event_info.tmdb_id
                and event_info.season_id
                and event_info.episode_id):
            specific_image = self.chain.obtain_specific_image(
                mediaid=event_info.tmdb_id,
                mtype=MediaType.TV,
                image_type=MediaImageType.Backdrop,
                season=event_info.season_id,
                episode=event_info.episode_id
            )
            if specific_image:
                image_url = specific_image
        # 使用默认图片
        if not image_url:
            image_url = self._webhook_images.get(event_info.channel)

        # 发送消息
        self.post_message(mtype=NotificationType.MediaServer,
                          title=message_title, text=message_content, image=image_url)

    def stop_service(self):
        """
        退出插件
        """
        pass
