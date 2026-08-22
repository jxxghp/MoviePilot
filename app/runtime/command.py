import copy
import threading
import traceback
from concurrent.futures import Future
from typing import Any, Callable, List, Union, Dict, Optional

from app.runtime.events import Event as ManagerEvent, eventmanager, Event
from app.runtime.extensions.admission.command_arbitration import (
    BUILTIN_LAYER,
    OTHER_LAYER,
    PLUGIN_LAYER,
)
from app.runtime.extensions.projection.command import PluginCommandTable
from app.runtime.extensions.registry.command import CommandClaim
from app.runtime.extensions.contract.instance import split_instance_key
from app.runtime.thread import ThreadHelper
from app.runtime.log import logger
from app.schemas.command import CommandConflict, CommandLayer, CommandOrigin
from app.schemas.message import Message
from app.schemas.event import CommandRegisterEventData
from app.schemas.types import EventType, NotificationChannel, ChainEventType
from app.foundation.reflection import ObjectUtils
from app.foundation.singleton import Singleton
from app.foundation.collections import DictUtils


# 内建命令清单的来源，由组合根在导入期注册
_builtin_commands_provider: Optional[Callable[[], Dict[str, dict]]] = None
# 命令消息网关的来源，由组合根在导入期注册
_command_messenger_provider: Optional[Callable[[], Any]] = None


def register_builtin_commands(provider: Callable[[], Dict[str, dict]]) -> None:
    """
    注册内建命令清单的来源

    命令词与业务实现的绑定认识全部业务域，归组合根持有；命令中枢经本入口取用，
    因而不反向依赖组合根。

    :param provider: 交出内建命令表的可调用对象
    :return: 无返回值
    """
    global _builtin_commands_provider
    _builtin_commands_provider = provider


def _resolve_builtin_commands() -> Dict[str, dict]:
    """
    取出内建命令表

    :return: 命令词到命令表条目的映射
    :raises RuntimeError: 组合根尚未注册内建命令清单
    """
    if _builtin_commands_provider is None:
        raise RuntimeError(
            "内建命令清单未注册：请先导入 app.startup.command_initializer 完成组合根装配"
        )
    return _builtin_commands_provider()


def register_command_messenger(provider: Callable[[], Any]) -> None:
    """
    注册命令消息网关的来源

    命令分发要广播菜单命令注册、发送命令回复、收口渠道处理状态并在出错时留下系统提示，
    这四件事都经模块分发设施完成。网关实现归应用层，命令中枢经本入口取用。

    :param provider: 交出命令消息网关的可调用对象
    :return: 无返回值
    """
    global _command_messenger_provider
    _command_messenger_provider = provider


def _messenger() -> Any:
    """
    取出命令消息网关

    :return: 命令消息网关
    :raises RuntimeError: 组合根尚未注册命令消息网关
    """
    if _command_messenger_provider is None:
        raise RuntimeError(
            "命令消息网关未注册：请先导入 app.startup.command_initializer 完成组合根装配"
        )
    return _command_messenger_provider()


def _command_callable(command: Dict[str, Any]) -> Optional[Callable]:
    """
    取出命令表条目的实现

    插件命令与单独注册的命令在登记时就已绑定实现；内建命令交出的是解析器，
    业务链在此刻才物化。

    :param command: 命令表条目
    :return: 命令实现；条目既无实现也无解析器时为 None
    """
    func = command.get("func")
    if func is not None:
        return func
    provider = command.get("provider")
    return provider() if provider else None


def _command_layer(layer: str, entry: Dict[str, Any]) -> CommandLayer:
    """
    把一条命令表条目投影为来源分层条目

    :param layer: 来源层标识
    :param entry: 命令表条目
    :return: 来源分层条目，插件层附带实例键的反解结果
    """
    owner = entry.get("pid")
    extension_id, instance_id = split_instance_key(owner) if owner else (None, None)
    return CommandLayer(
        layer=layer,
        owner=owner,
        extension_id=extension_id,
        instance_id=instance_id,
        description=entry.get("description"),
        category=entry.get("category"),
    )


def _finish_command_processing_status(status: Optional[dict], user_id: Optional[str] = None) -> None:
    """
    命令执行完成后通过消息模块收口渠道处理状态。
    """
    if not status:
        return
    _messenger().finish_message_processing_status(
        status=status,
        userid=user_id,
    )


class Command(metaclass=Singleton):
    """
    全局命令管理，消费事件
    """

    def __init__(self):
        super().__init__()
        # 注册的命令集合
        self._registered_commands = {}
        # 所有命令集合
        self._commands = {}
        # 内建命令集合
        self._preset_commands = _resolve_builtin_commands()
        # 其他命令集合
        self._other_commands = {}
        # 插件命令表，随插件命令登记版本对齐
        self._plugin_table = PluginCommandTable(
            builtin_command_words=lambda: self._preset_commands,
            event_sender=self.send_plugin_event,
        )
        # 初始化锁
        self._rlock = threading.RLock()
        # 初始化命令
        self.init_commands()

    def init_commands(self, pid: Optional[str] = None) -> Future:
        """
        提交菜单命令重建任务，并返回可等待的完成信号。
        """
        return ThreadHelper().submit(self.__init_commands_background, pid)

    def __init_commands_background(self, pid: Optional[str] = None) -> None:
        """
        后台初始化菜单命令
        """
        try:
            with self._rlock:
                logger.debug("Acquired lock for initializing commands in background.")
                self._plugin_table.rebuild()
                self._commands = self.__merge_commands()

                # 强制触发注册
                force_register = False
                # 触发事件允许可以拦截和调整命令
                event, initial_commands = self.__trigger_register_commands_event()

                if event and event.event_data:
                    # 如果事件返回有效的 event_data，使用事件中调整后的命令
                    event_data: CommandRegisterEventData = event.event_data
                    # 如果事件被取消，跳过命令注册
                    if event_data.cancel:
                        logger.debug(
                            f"Command initialization canceled by event: {event_data.source}"
                        )
                        return
                    # 如果拦截源与插件标识一致时，这里认为需要强制触发注册
                    if pid is not None and pid == event_data.source:
                        force_register = True
                    initial_commands = event_data.commands or {}
                    logger.debug(
                        f"Registering command count from event: {len(initial_commands)}"
                    )
                else:
                    logger.debug(
                        f"Registering initial command count: {len(initial_commands)}"
                    )

                # initial_commands 必须是 self._commands 的子集
                filtered_initial_commands = DictUtils.filter_keys_to_subset(
                    initial_commands, self._commands
                )
                # 如果 filtered_initial_commands 为空，则跳过注册
                if not filtered_initial_commands and not force_register:
                    logger.debug("Filtered commands are empty, skipping registration.")
                    return

                # 对比调整后的命令与当前命令
                if (
                    filtered_initial_commands != self._registered_commands
                    or force_register
                ):
                    logger.debug(
                        "Command set has changed or force registration is enabled."
                    )
                    self._registered_commands = filtered_initial_commands
                    _messenger().register_commands(commands=filtered_initial_commands)
                else:
                    logger.debug(
                        "Command set unchanged, skipping broadcast registration."
                    )
        except Exception as e:
            logger.error(
                f"Error occurred during command initialization in background: {e}",
                exc_info=True,
            )

    def __merge_commands(self) -> Dict[str, dict]:
        """
        把内建、插件与单独注册三处来源合并成一张命令表

        合并次序即优先级，后一处压住前一处的同名命令词。

        :return: 命令词到命令表条目的映射
        """
        return {
            **self._preset_commands,
            **self._plugin_table.commands,
            **self._other_commands,
        }

    def __trigger_register_commands_event(self) -> tuple[Optional[Event], dict]:
        """
        触发事件，允许调整命令数据
        """

        def add_commands(source, command_type):
            """
            添加命令集合
            """
            for cmd, command in source.items():
                if not command.get("show", True):
                    continue

                command_data = {
                    "type": command_type,
                    "description": command.get("description"),
                    "category": command.get("category"),
                }
                # 如果有 pid，则添加到命令数据中
                plugin_id = command.get("pid")
                if plugin_id:
                    command_data["pid"] = plugin_id
                commands[cmd] = command_data

        # 初始化命令字典
        commands: Dict[str, dict] = {}
        add_commands(self._preset_commands, "preset")
        add_commands(self._plugin_table.commands, "plugin")
        add_commands(self._other_commands, "other")

        # 触发事件允许可以拦截和调整命令
        event_data = CommandRegisterEventData(
            commands=commands, origin="CommandChain", service=None
        )
        event = eventmanager.send_event(ChainEventType.CommandRegister, event_data)
        return event, commands

    def command_origins(self) -> List[CommandOrigin]:
        """
        列出全部命令词的来源分层

        命令表是内建 < 插件 < 其它三处来源合并出的一张平表，合并完就看不出一条命令归谁；
        插件声明因跨插件同名或撞上内建而失效时，此前只在服务端日志里留过一次告警，用户
        敲了没反应也无从得知原因。本方法把合并前的分层与两类失效原样交出。

        :return: 按命令词排序的来源条目
        """
        self._refresh_plugin_commands()
        with self._rlock:
            preset = dict(self._preset_commands)
            plugin = dict(self._plugin_table.commands)
            declined = dict(self._plugin_table.declined)
            other = dict(self._other_commands)
        claims = {claim.cmd: claim for claim in self._plugin_table.claims()}
        words = set(preset) | set(plugin) | set(declined) | set(other) | set(claims)
        return [
            self.__command_origin(cmd, preset, plugin, declined, other, claims.get(cmd))
            for cmd in sorted(words)
        ]

    @staticmethod
    def __command_origin(
        cmd: str,
        preset: Dict[str, dict],
        plugin: Dict[str, dict],
        declined: Dict[str, dict],
        other: Dict[str, dict],
        claim: Optional[CommandClaim],
    ) -> CommandOrigin:
        """
        判定一个命令词的生效来源、被压住的下层与失效的插件声明

        :param cmd: 命令词
        :param preset: 内建命令表
        :param plugin: 通过裁决的插件命令表
        :param declined: 撞上内建且未声明接管意图而作废的插件命令表
        :param other: 单独注册的命令表
        :param claim: 插件对该命令词的声明及跨插件裁决结果，无插件声明时为 None
        :return: 该命令词的来源条目
        """
        layers: List[CommandLayer] = []
        if cmd in preset:
            layers.append(_command_layer(BUILTIN_LAYER, preset[cmd]))
        if cmd in plugin:
            layers.append(_command_layer(PLUGIN_LAYER, plugin[cmd]))
        if cmd in other:
            layers.append(_command_layer(OTHER_LAYER, other[cmd]))
        conflict = (
            CommandConflict(plugins=list(claim.plugins), owners=list(claim.owners))
            if claim is not None and not claim.effective
            else None
        )
        rejected = (
            [_command_layer(PLUGIN_LAYER, declined[cmd])] if cmd in declined else []
        )
        if not layers:
            return CommandOrigin(
                cmd=cmd, effective=False, declined=rejected, conflict=conflict
            )
        return CommandOrigin(
            cmd=cmd,
            effective=True,
            source=layers[-1],
            shadowed=layers[:-1],
            declined=rejected,
            conflict=conflict,
        )

    def __run_command(
        self,
        command: Dict[str, any],
        data_str: Optional[str] = "",
        channel: NotificationChannel = None,
        source: Optional[str] = None,
        userid: Union[str, int] = None,
    ):
        """
        运行定时服务
        """
        if command.get("type") == "scheduler":
            # 定时服务
            if userid:
                _messenger().post_message(
                    Message(
                        channel=channel,
                        source=source,
                        title=f"开始执行 {command.get('description')} ...",
                        userid=userid,
                    )
                )

            # 执行定时任务
            _command_callable(command)()

            if userid:
                _messenger().post_message(
                    Message(
                        channel=channel,
                        source=source,
                        title=f"{command.get('description')} 执行完成",
                        userid=userid,
                    )
                )
        else:
            # 命令
            func = _command_callable(command)
            cmd_data = copy.deepcopy(command["data"]) if command.get("data") else {}
            args_num = ObjectUtils.arguments(func)
            if args_num > 0:
                if cmd_data:
                    # 有内置参数直接使用内置参数
                    data = cmd_data.get("data") or {}
                    data["channel"] = channel
                    data["source"] = source
                    data["user"] = userid
                    if data_str:
                        data["arg_str"] = data_str
                    cmd_data["data"] = data
                    func(**cmd_data)
                elif args_num == 3:
                    # 没有输入参数，只输入渠道来源、用户ID和消息来源
                    func(channel, userid, source)
                elif args_num > 3:
                    # 多个输入参数：用户输入、用户ID
                    func(data_str, channel, userid, source)
            else:
                # 没有参数
                func()

    def _refresh_plugin_commands(self) -> None:
        """
        插件命令登记发生变化时重新组装命令表

        插件停用或卸载后其命令必须立刻从命令表里消失，而广播注册是异步的、也未必每次
        都被触发。插件命令表是注册表某一版本的快照，版本号变了即代表快照过期，在查表前
        对齐一次，用户敲已卸载插件的命令就得到「命令不存在」而不是调用到已卸载的代码。
        """
        if not self._plugin_table.refresh():
            return
        with self._rlock:
            self._commands = self.__merge_commands()

    def get_commands(self):
        """
        获取命令列表
        """
        self._refresh_plugin_commands()
        if not self._commands:
            with self._rlock:
                if not self._commands:
                    self._commands = self.__merge_commands()
        return self._commands

    def get(self, cmd: str) -> Any:
        """
        获取命令
        """
        self._refresh_plugin_commands()
        return self._commands.get(cmd, {})

    def register(
        self,
        cmd: str,
        func: Any,
        data: Optional[dict] = None,
        desc: Optional[str] = None,
        category: Optional[str] = None,
        show: bool = True,
    ) -> None:
        """
        注册单个命令
        """
        # 单独调用的，统一注册到其他
        self._other_commands[cmd] = {
            "func": func,
            "description": desc,
            "category": category,
            "data": data or {},
            "show": show,
        }

    def execute(
        self,
        cmd: str,
        data_str: Optional[str] = "",
        channel: NotificationChannel = None,
        source: Optional[str] = None,
        userid: Union[str, int] = None,
    ) -> None:
        """
        执行命令
        """
        command = self.get(cmd)
        if command:
            try:
                if userid:
                    logger.info(
                        f"用户 {userid} 开始执行：{command.get('description')} ..."
                    )
                else:
                    logger.info(f"开始执行：{command.get('description')} ...")

                # 执行命令
                self.__run_command(
                    command,
                    data_str=data_str,
                    channel=channel,
                    source=source,
                    userid=userid,
                )

                if userid:
                    logger.info(f"用户 {userid} {command.get('description')} 执行完成")
                else:
                    logger.info(f"{command.get('description')} 执行完成")
            except Exception as err:
                logger.error(
                    f"执行命令 {cmd} 出错：{str(err)} - {traceback.format_exc()}"
                )
                _messenger().put_system_message(
                    title=f"执行命令 {cmd} 出错", message=str(err)
                )

    @staticmethod
    def send_plugin_event(etype: EventType, data: dict) -> None:
        """
        发送插件命令
        """
        eventmanager.send_event(etype, data)

    @eventmanager.register(EventType.CommandExcute)
    def command_event(self, event: ManagerEvent) -> None:
        """
        注册命令执行事件
        event_data: {
            "cmd": "/xxx args"
        }
        """
        # 命令参数
        event_str = event.event_data.get("cmd")
        # 消息渠道
        event_channel = event.event_data.get("channel")
        # 消息来源
        event_source = event.event_data.get("source")
        # 消息用户
        event_user = event.event_data.get("user")
        try:
            if event_str:
                cmd = event_str.split()[0]
                args = " ".join(event_str.split()[1:])
                if self.get(cmd):
                    self.execute(
                        cmd=cmd,
                        data_str=args,
                        channel=event_channel,
                        source=event_source,
                        userid=event_user,
                    )
        finally:
            _finish_command_processing_status(
                event.event_data.get("processing_status"),
                user_id=event_user,
            )

    @eventmanager.register(EventType.ModuleReload)
    def module_reload_event(self, _: ManagerEvent) -> None:
        """
        注册模块重载事件
        """
        # 发生模块重载时，重新注册命令
        self.init_commands()
