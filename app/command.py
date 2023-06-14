import traceback
from threading import Thread, Event
from typing import Any

from app.chain import ChainBase
from app.chain.cookiecloud import CookieCloudChain
from app.chain.douban_sync import DoubanSyncChain
from app.chain.subscribe import SubscribeChain
from app.chain.transfer import TransferChain
from app.core.event import eventmanager, EventManager
from app.core.plugin import PluginManager
from app.core.event import Event as ManagerEvent
from app.log import logger
from app.utils.singleton import Singleton
from app.utils.types import EventType


class CommandChian(ChainBase):
    """
    插件处理链
    """

    def process(self, *args, **kwargs):
        pass


class Command(metaclass=Singleton):
    """
    全局命令管理，消费事件
    """
    # 内建命令
    _commands = {}

    # 退出事件
    _event = Event()

    def __init__(self):
        # 事件管理器
        self.eventmanager = EventManager()
        # 插件管理器
        self.pluginmanager = PluginManager()
        # 汇总插件命令
        self._commands = {
            "/cookiecloud": {
                "func": CookieCloudChain().process,
                "description": "同步站点Cookie",
                "data": {}
            },
            "/douban_sync": {
                "func": DoubanSyncChain().process,
                "description": "同步豆瓣想看",
                "data": {}
            },
            "/subscribe_refresh": {
                "func": SubscribeChain().refresh,
                "description": "刷新订阅",
                "data": {}
            },
            "/subscribe_search": {
                "func": SubscribeChain().search,
                "description": "搜索订阅",
                "data": {
                    'state': 'R',
                }
            },
            "/subscribes": {
                "func": SubscribeChain().list,
                "description": "查询订阅",
                "data": {}
            },
            "/subscribe_delete": {
                "func": SubscribeChain().delete,
                "description": "删除订阅",
                "data": {}
            },
            "/transfer": {
                "func": TransferChain().process,
                "description": "下载文件整理",
                "data": {}
            }
        }
        plugin_commands = self.pluginmanager.get_plugin_commands()
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
        # 处理链
        self.chain = CommandChian()
        # 广播注册命令
        self.chain.register_commands(commands=self.get_commands())
        # 消息处理线程
        self._thread = Thread(target=self.__run)
        # 启动事件处理线程
        self._thread.start()

    def __run(self):
        """
        事件处理线程
        """
        while not self._event.is_set():
            event, handlers = self.eventmanager.get_event()
            if event:
                logger.info(f"处理事件：{event.event_type} - {handlers}")
                for handler in handlers:
                    try:
                        names = handler.__qualname__.split(".")
                        if names[0] == "Command":
                            self.command_event(event)
                        else:
                            self.pluginmanager.run_plugin_method(names[0], names[1], event)
                    except Exception as e:
                        logger.error(f"事件处理出错：{str(e)} - {traceback.format_exc()}")

    def stop(self):
        """
        停止事件处理线程
        """
        self._event.set()
        self._thread.join()

    def get_commands(self):
        """
        获取命令列表
        """
        return self._commands

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

    def execute(self, cmd: str, data_str: str = "") -> None:
        """
        执行命令
        """
        command = self.get(cmd)
        if command:
            try:
                logger.info(f"开始执行：{command.get('description')} ...")
                cmd_data = command['data'] if command.get('data') else {}
                if cmd_data:
                    command['func'](**cmd_data)
                elif data_str:
                    command['func'](data_str)
                else:
                    command['func']()
            except Exception as err:
                logger.error(f"执行命令 {cmd} 出错：{str(err)}")

    @staticmethod
    def send_plugin_event(etype: EventType, data: dict) -> None:
        """
        发送插件命令
        """
        EventManager().send_event(etype, data)

    @eventmanager.register(EventType.CommandExcute)
    def command_event(self, event: ManagerEvent) -> None:
        """
        注册命令执行事件
        event_data: {
            "cmd": "/xxx args"
        }
        """
        event_str = event.event_data.get('cmd')
        if event_str:
            cmd = event_str.split()[0]
            args = " ".join(event_str.split()[1:])
            if self.get(cmd):
                self.execute(cmd, args)
