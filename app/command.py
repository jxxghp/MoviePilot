from typing import Any

from app.chain.cookiecloud import CookieCloudChain
from app.chain.douban_sync import DoubanSyncChain
from app.chain.subscribe import SubscribeChain
from app.core import eventmanager, PluginManager, EventManager
from app.core.event_manager import Event
from app.log import logger
from app.utils.singleton import Singleton
from app.utils.types import EventType


class Command(metaclass=Singleton):
    """
    全局命令管理
    """
    # 内建命令
    _commands = {
        "/cookiecloud": {
            "func": CookieCloudChain().process,
            "description": "同步CookieCloud的Cookie",
            "data": {}
        },
        "/doubansync": {
            "func": DoubanSyncChain().process,
            "description": "同步豆瓣想看",
            "data": {}
        },
        "/subscribe": {
            "func": SubscribeChain().search,
            "description": "刷新所有订阅",
            "data": {
                'state': 'R',
            }
        }
    }

    def __init__(self):
        # 注册插件命令
        plugin_commands = PluginManager().get_plugin_commands()
        for command in plugin_commands:
            self.register(
                cmd=command.get('cmd'),
                func=Command.send_plugin_event,
                desc=command.get('desc'),
                data={
                    'etype': command.get('event'),
                    'data': command.get('data')
                }
            )

    def register(self, cmd: str, func: Any, data: dict = None, desc: str = None) -> None:
        """
        注册命令
        """
        self._commands[cmd] = {
            "func": func,
            "description": desc,
            "data": data or {}
        }

    def get(self, cmd: str) -> Any:
        """
        获取命令
        """
        return self._commands.get(cmd, {})

    def execute(self, cmd: str) -> None:
        """
        执行命令
        """
        command = self.get(cmd)
        if command:
            logger.info(f"开始执行：{command.get('description')} ...")
            data = command['data'] if command.get('data') else {}
            command['func'](**data)

    @staticmethod
    def send_plugin_event(etype: EventType, data: dict) -> None:
        """
        发送插件命令
        """
        EventManager().send_event(etype, data)

    @eventmanager.register(EventType.CommandExcute)
    def command_event(self, event: Event) -> None:
        """
        注册命令执行事件
        event_data: {
            "cmd": "/xxx"
        }
        """
        cmd = event.event_data.get('cmd')
        if self.get(cmd):
            self.execute(cmd)
