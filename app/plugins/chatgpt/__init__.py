from typing import Any, List, Dict, Tuple

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.plugins.chatgpt.openai import OpenAi
from app.schemas.types import EventType


class ChatGPT(_PluginBase):
    # 插件名称
    plugin_name = "ChatGPT"
    # 插件描述
    plugin_desc = "消息交互支持与ChatGPT对话。"
    # 插件图标
    plugin_icon = "chatgpt.png"
    # 主题色
    plugin_color = "#74AA9C"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "chatgpt_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    openai = None
    _enabled = False
    _proxy = False
    _recognize = False
    _openai_url = None
    _openai_key = None

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._proxy = config.get("proxy")
            self._recognize = config.get("recognize")
            self._openai_url = config.get("openai_url")
            self._openai_key = config.get("openai_key")
            self.openai = OpenAi(api_key=self._openai_key, api_url=self._openai_url,
                                 proxy=settings.PROXY if self._proxy else None)

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
                                    'md': 4
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
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'proxy',
                                            'label': '使用代理',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'recognize',
                                            'label': '辅助识别',
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'openai_url',
                                            'label': 'OpenAI API Url',
                                            'placeholder': 'https://api.openai.com',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'openai_key',
                                            'label': 'sk-xxx'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "proxy": False,
            "recognize": False,
            "openai_url": "https://api.openai.com",
            "openai_key": ""
        }

    def get_page(self) -> List[dict]:
        pass

    @eventmanager.register(EventType.UserMessage)
    def talk(self, event: Event):
        """
        监听用户消息，获取ChatGPT回复
        """
        if not self._enabled:
            return
        if not self.openai:
            return
        text = event.event_data.get("text")
        userid = event.event_data.get("userid")
        channel = event.event_data.get("channel")
        if not text:
            return
        response = self.openai.get_response(text=text, userid=userid)
        if response:
            self.post_message(channel=channel, title=response, userid=userid)

    @eventmanager.register(EventType.NameRecognize)
    def recognize(self, event: Event):
        """
        监听识别事件，使用ChatGPT辅助识别名称
        """
        if not event.event_data:
            return
        title = event.event_data.get("title")
        if not title:
            return
        # 收到事件后需要立码返回，避免主程序等待
        if not self._enabled \
                or not self.openai \
                or not self._recognize:
            eventmanager.send_event(
                EventType.NameRecognizeResult,
                {
                    'title': title
                }
            )
            return
        # 调用ChatGPT
        response = self.openai.get_media_name(filename=title)
        logger.info(f"ChatGPT辅助识别结果：{response}")
        if response:
            eventmanager.send_event(
                EventType.NameRecognizeResult,
                {
                    'title': title,
                    'name': response.get("title"),
                    'year': response.get("year"),
                    'season': response.get("season"),
                    'episode': response.get("episode")
                }
            )

    def stop_service(self):
        """
        退出插件
        """
        pass
