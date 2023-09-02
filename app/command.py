import traceback
from threading import Thread, Event
from typing import Any, Union

from app.chain import ChainBase
from app.chain.cookiecloud import CookieCloudChain
from app.chain.download import DownloadChain
from app.chain.mediaserver import MediaServerChain
from app.chain.site import SiteChain
from app.chain.subscribe import SubscribeChain
from app.chain.system import SystemChain
from app.chain.transfer import TransferChain
from app.core.event import Event as ManagerEvent
from app.core.event import eventmanager, EventManager
from app.core.plugin import PluginManager
from app.db import ScopedSession
from app.log import logger
from app.schemas.types import EventType, MessageChannel
from app.utils.object import ObjectUtils
from app.utils.singleton import Singleton


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
        # 数据库连接
        self._db = ScopedSession()
        # 事件管理器
        self.eventmanager = EventManager()
        # 插件管理器
        self.pluginmanager = PluginManager()
        # 处理链
        self.chain = CommandChian(self._db)
        # 内置命令
        self._commands = {
            "/cookiecloud": {
                "func": CookieCloudChain(self._db).remote_sync,
                "description": "同步站点",
                "data": {}
            },
            "/sites": {
                "func": SiteChain(self._db).remote_list,
                "description": "查询站点",
                "data": {}
            },
            "/site_cookie": {
                "func": SiteChain(self._db).remote_cookie,
                "description": "更新站点Cookie",
                "data": {}
            },
            "/site_enable": {
                "func": SiteChain(self._db).remote_enable,
                "description": "启用站点",
                "data": {}
            },
            "/site_disable": {
                "func": SiteChain(self._db).remote_disable,
                "description": "禁用站点",
                "data": {}
            },
            "/mediaserver_sync": {
                "func": MediaServerChain(self._db).remote_sync,
                "description": "同步媒体服务器",
                "data": {}
            },
            "/subscribes": {
                "func": SubscribeChain(self._db).remote_list,
                "description": "查询订阅",
                "data": {}
            },
            "/subscribe_refresh": {
                "func": SubscribeChain(self._db).remote_refresh,
                "description": "刷新订阅",
                "data": {}
            },
            "/subscribe_search": {
                "func": SubscribeChain(self._db).remote_search,
                "description": "搜索订阅",
                "data": {}
            },
            "/subscribe_delete": {
                "func": SubscribeChain(self._db).remote_delete,
                "description": "删除订阅",
                "data": {}
            },
            "/downloading": {
                "func": DownloadChain(self._db).remote_downloading,
                "description": "正在下载",
                "data": {}
            },
            "/transfer": {
                "func": TransferChain(self._db).process,
                "description": "下载文件整理",
                "data": {}
            },
            "/redo": {
                "func": TransferChain(self._db).remote_transfer,
                "description": "手动整理",
                "data": {}
            },
            "/clear_cache": {
                "func": SystemChain(self._db).remote_clear_cache,
                "description": "清理缓存",
                "data": {}
            }
        }
        # 汇总插件命令
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
        # 广播注册命令菜单
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

    def execute(self, cmd: str, data_str: str = "",
                channel: MessageChannel = None, userid: Union[str, int] = None) -> None:
        """
        执行命令
        """
        command = self.get(cmd)
        if command:
            try:
                logger.info(f"用户 {userid} 开始执行：{command.get('description')} ...")
                cmd_data = command['data'] if command.get('data') else {}
                args_num = ObjectUtils.arguments(command['func'])
                if args_num > 0:
                    if cmd_data:
                        # 有内置参数直接使用内置参数
                        command['func'](**cmd_data)
                    elif args_num == 2:
                        # 没有输入参数，只输入渠道和用户ID
                        command['func'](channel, userid)
                    elif args_num > 2:
                        # 多个输入参数：用户输入、用户ID
                        command['func'](data_str, channel, userid)
                else:
                    # 没有参数
                    command['func']()
                logger.info(f"用户 {userid} {command.get('description')} 执行完成")
            except Exception as err:
                logger.error(f"执行命令 {cmd} 出错：{str(err)}")
                traceback.print_exc()

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
        # 命令参数
        event_str = event.event_data.get('cmd')
        # 消息渠道
        event_channel = event.event_data.get('channel')
        # 消息用户
        event_user = event.event_data.get('user')
        if event_str:
            cmd = event_str.split()[0]
            args = " ".join(event_str.split()[1:])
            if self.get(cmd):
                self.execute(cmd, args, event_channel, event_user)

    def __del__(self):
        if self._db:
            self._db.close()
