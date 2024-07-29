import copy
import importlib
import threading
import traceback
from threading import Thread
from typing import Any, Union, Dict

from app.chain import ChainBase
from app.chain.download import DownloadChain
from app.chain.site import SiteChain
from app.chain.subscribe import SubscribeChain
from app.chain.system import SystemChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.event import Event as ManagerEvent, eventmanager, EventManager
from app.core.plugin import PluginManager
from app.helper.message import MessageHelper
from app.helper.thread import ThreadHelper
from app.log import logger
from app.scheduler import Scheduler
from app.schemas import Notification
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
    _event = threading.Event()

    def __init__(self):
        # 事件管理器
        self.eventmanager = EventManager()
        # 插件管理器
        self.pluginmanager = PluginManager()
        # 处理链
        self.chain = CommandChian()
        # 定时服务管理
        self.scheduler = Scheduler()
        # 消息管理器
        self.messagehelper = MessageHelper()
        # 线程管理器
        self.threader = ThreadHelper()
        # 内置命令
        self._commands = {
            "/cookiecloud": {
                "id": "cookiecloud",
                "type": "scheduler",
                "description": "同步站点",
                "category": "站点"
            },
            "/sites": {
                "func": SiteChain().remote_list,
                "description": "查询站点",
                "category": "站点",
                "data": {}
            },
            "/site_cookie": {
                "func": SiteChain().remote_cookie,
                "description": "更新站点Cookie",
                "data": {}
            },
            "/site_enable": {
                "func": SiteChain().remote_enable,
                "description": "启用站点",
                "data": {}
            },
            "/site_disable": {
                "func": SiteChain().remote_disable,
                "description": "禁用站点",
                "data": {}
            },
            "/mediaserver_sync": {
                "id": "mediaserver_sync",
                "type": "scheduler",
                "description": "同步媒体服务器",
                "category": "管理"
            },
            "/subscribes": {
                "func": SubscribeChain().remote_list,
                "description": "查询订阅",
                "category": "订阅",
                "data": {}
            },
            "/subscribe_refresh": {
                "id": "subscribe_refresh",
                "type": "scheduler",
                "description": "刷新订阅",
                "category": "订阅"
            },
            "/subscribe_search": {
                "id": "subscribe_search",
                "type": "scheduler",
                "description": "搜索订阅",
                "category": "订阅"
            },
            "/subscribe_delete": {
                "func": SubscribeChain().remote_delete,
                "description": "删除订阅",
                "data": {}
            },
            "/subscribe_tmdb": {
                "id": "subscribe_tmdb",
                "type": "scheduler",
                "description": "订阅元数据更新"
            },
            "/downloading": {
                "func": DownloadChain().remote_downloading,
                "description": "正在下载",
                "category": "管理",
                "data": {}
            },
            "/transfer": {
                "id": "transfer",
                "type": "scheduler",
                "description": "下载文件整理",
                "category": "管理"
            },
            "/redo": {
                "func": TransferChain().remote_transfer,
                "description": "手动整理",
                "data": {}
            },
            "/clear_cache": {
                "func": SystemChain().remote_clear_cache,
                "description": "清理缓存",
                "category": "管理",
                "data": {}
            },
            "/restart": {
                "func": SystemChain().restart,
                "description": "重启系统",
                "category": "管理",
                "data": {}
            },
            "/version": {
                "func": SystemChain().version,
                "description": "当前版本",
                "category": "管理",
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
                category=command.get('category'),
                data={
                    'etype': command.get('event'),
                    'data': command.get('data')
                }
            )
        # 广播注册命令菜单
        if not settings.DEV:
            self.chain.register_commands(commands=self.get_commands())
        # 消息处理线程
        self._thread = Thread(target=self.__run)
        # 启动事件处理线程
        self._thread.start()
        # 重启msg
        SystemChain().restart_finish()

    def __run(self):
        """
        事件处理线程
        """
        while not self._event.is_set():
            event, handlers = self.eventmanager.get_event()
            if event:
                logger.info(f"处理事件：{event.event_type} - {handlers}")
                for handler in handlers:
                    names = handler.__qualname__.split(".")
                    [class_name, method_name] = names
                    try:
                        if class_name in self.pluginmanager.get_plugin_ids():
                            # 插件事件
                            self.threader.submit(
                                self.pluginmanager.run_plugin_method,
                                class_name, method_name, copy.deepcopy(event)
                            )

                        else:
                            # 检查全局变量中是否存在
                            if class_name not in globals():
                                # 导入模块，除了插件和Command本身，只有chain能响应事件
                                try:
                                    module = importlib.import_module(
                                        f"app.chain.{class_name[:-5].lower()}"
                                    )
                                    class_obj = getattr(module, class_name)()
                                except Exception as e:
                                    logger.error(f"事件处理出错：{str(e)} - {traceback.format_exc()}")
                                    continue

                            else:
                                # 通过类名创建类实例
                                class_obj = globals()[class_name]()
                            # 检查类是否存在并调用方法
                            if hasattr(class_obj, method_name):
                                self.threader.submit(
                                    getattr(class_obj, method_name),
                                    copy.deepcopy(event)
                                )
                    except Exception as e:
                        logger.error(f"事件处理出错：{str(e)} - {traceback.format_exc()}")
                        self.messagehelper.put(title=f"{event.event_type} 事件处理出错",
                                               message=f"{class_name}.{method_name}：{str(e)}",
                                               role="system")
                        self.eventmanager.send_event(
                            EventType.SystemError,
                            {
                                "type": "event",
                                "event_type": event.event_type,
                                "event_handle": f"{class_name}.{method_name}",
                                "error": str(e),
                                "traceback": traceback.format_exc()
                            }
                        )

    def __run_command(self, command: Dict[str, any],
                      data_str: str = "",
                      channel: MessageChannel = None, userid: Union[str, int] = None):
        """
        运行定时服务
        """
        if command.get("type") == "scheduler":
            # 定时服务
            if userid:
                self.chain.post_message(
                    Notification(
                        channel=channel,
                        title=f"开始执行 {command.get('description')} ...",
                        userid=userid
                    )
                )

            # 执行定时任务
            self.scheduler.start(job_id=command.get("id"))

            if userid:
                self.chain.post_message(
                    Notification(
                        channel=channel,
                        title=f"{command.get('description')} 执行完成",
                        userid=userid
                    )
                )
        else:
            # 命令
            cmd_data = command['data'] if command.get('data') else {}
            args_num = ObjectUtils.arguments(command['func'])
            if args_num > 0:
                if cmd_data:
                    # 有内置参数直接使用内置参数
                    data = cmd_data.get("data") or {}
                    data['channel'] = channel
                    data['user'] = userid
                    if data_str:
                        data['args'] = data_str
                    cmd_data['data'] = data
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

    def stop(self):
        """
        停止事件处理线程
        """
        logger.info("正在停止事件处理...")
        self._event.set()
        try:
            self._thread.join()
            logger.info("事件处理停止完成")
        except Exception as e:
            logger.error(f"停止事件处理线程出错：{str(e)} - {traceback.format_exc()}")

    def get_commands(self):
        """
        获取命令列表
        """
        return self._commands

    def register(self, cmd: str, func: Any, data: dict = None,
                 desc: str = None, category: str = None) -> None:
        """
        注册命令
        """
        self._commands[cmd] = {
            "func": func,
            "description": desc,
            "category": category,
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
                if userid:
                    logger.info(f"用户 {userid} 开始执行：{command.get('description')} ...")
                else:
                    logger.info(f"开始执行：{command.get('description')} ...")

                # 执行命令
                self.__run_command(command, data_str=data_str,
                                   channel=channel, userid=userid)

                if userid:
                    logger.info(f"用户 {userid} {command.get('description')} 执行完成")
                else:
                    logger.info(f"{command.get('description')} 执行完成")
            except Exception as err:
                logger.error(f"执行命令 {cmd} 出错：{str(err)} - {traceback.format_exc()}")
                self.messagehelper.put(title=f"执行命令 {cmd} 出错",
                                       message=str(err),
                                       role="system")

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
