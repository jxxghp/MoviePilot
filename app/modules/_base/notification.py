"""消息渠道模块业务样板基类。

沉淀各消息渠道模块逐字复制的样板：管理员判断、连接测试、
斜杠命令注册。渠道差异（客户端类型、菜单 API、前置条件）通过
类属性与钩子方法保留在各模块。
"""
import copy
from typing import Dict, List, Optional, Tuple, Union

from app.application.messaging.agent import (
    matches_channel_admin,
    resolve_config_principal_ids,
)
from app.foundation.collections import DictUtils
from app.modules import _MessageBase, _ModuleBase, TService
from app.runtime.events import eventmanager
from app.runtime.log import logger
from app.schemas.event import CommandRegisterEventData
from app.schemas.types import ChainEventType


class _MessageChannelModuleBase(_ModuleBase, _MessageBase[TService]):
    """
    消息渠道模块业务样板基类。
    """

    # 管理员配置键，子类覆写（如 "TELEGRAM_ADMINS"）
    _admin_config_key: str = ""
    # 命令注册事件源标识，默认取模块名，子类可覆写
    _command_origin: Optional[str] = None

    @classmethod
    def _get_admins(cls, config: Optional[dict]) -> List[str]:
        """
        解析渠道管理员配置，兼容逗号分隔和首尾空白。
        """
        return sorted(resolve_config_principal_ids(config, cls._admin_config_key))

    def _should_reject_admin_command(
            self,
            config: Optional[dict],
            *user_ids: Optional[Union[str, int]],
    ) -> bool:
        """
        判断命令或命令型按钮回调是否应因非管理员身份被拒绝。
        """
        if not self._get_admins(config):
            return False
        # 模块实例未初始化时 self._channel 为空，退回静态子类型声明
        channel = self._channel or self.get_subtype()
        return not matches_channel_admin(
            channel,
            config,
            *user_ids,
        )

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        测试模块连接性
        """
        if not self.get_instances():
            return None
        for name, client in self.get_instances().items():
            state, message = self._test_connection(client)
            if not state:
                suffix = f"：{message}" if message else ""
                return False, f"{self.get_name()} {name} 未就绪{suffix}"
        return True, ""

    def _test_connection(self, client) -> Tuple[bool, str]:
        """
        连接测试钩子，返回 (是否就绪, 失败信息)，子类可覆写。
        """
        return bool(client.get_state()), ""

    def register_commands(self, commands: Dict[str, dict]) -> None:
        """
        注册命令，实现这个函数接收系统可用的命令菜单

        :param commands: 命令字典
        """
        for client_config in self.get_configs().values():
            if not self._commands_enabled(client_config.config):
                continue

            client = self.get_instance(client_config.name)
            if not client:
                continue

            # 触发事件，允许调整命令数据，这里需要进行深复制，避免实例共享
            scoped_commands = copy.deepcopy(commands)
            event = eventmanager.send_event(
                ChainEventType.CommandRegister,
                CommandRegisterEventData(
                    commands=scoped_commands,
                    origin=self._command_origin or self.get_name(),
                    service=client_config.name,
                ),
            )

            # 如果事件返回有效的 event_data，使用事件中调整后的命令
            if event and event.event_data:
                event_data: CommandRegisterEventData = event.event_data
                # 如果事件被取消，跳过命令注册，并清理菜单
                if event_data.cancel:
                    self._delete_commands(client)
                    logger.debug(
                        f"Command registration for {client_config.name} canceled by event: {event_data.source}"
                    )
                    continue
                scoped_commands = event_data.commands or {}
                if not scoped_commands:
                    logger.debug("Filtered commands are empty, skipping registration.")
                    self._delete_commands(client)

            # scoped_commands 必须是 commands 的子集
            filtered_scoped_commands = DictUtils.filter_keys_to_subset(
                scoped_commands,
                commands,
            )
            # 如果 filtered_scoped_commands 为空，则跳过注册
            if not filtered_scoped_commands:
                logger.debug("Filtered commands are empty, skipping registration.")
                self._delete_commands(client)
                continue
            # 对比调整后的命令与当前命令
            if filtered_scoped_commands != commands:
                logger.debug(
                    f"Command set has changed, Updating new commands: {filtered_scoped_commands}"
                )
            self._apply_commands(client, filtered_scoped_commands)

    def _commands_enabled(self, config: Optional[dict]) -> bool:
        """
        命令注册前置条件钩子，返回 False 时跳过该实例，子类可覆写。
        """
        return True

    def _delete_commands(self, client) -> None:
        """
        清理已注册命令的钩子，子类可覆写（如改用菜单 API）。
        """
        client.delete_commands()

    def _apply_commands(self, client, commands: Dict[str, dict]) -> None:
        """
        应用命令集合的钩子，子类可覆写（如改用菜单 API）。
        """
        client.register_commands(commands)
