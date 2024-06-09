from abc import abstractmethod, ABCMeta
from typing import Tuple, Union

from app.db.systemconfig_oper import SystemConfigOper
from app.schemas import Notification
from app.schemas.types import SystemConfigKey, MessageChannel


class _ModuleBase(metaclass=ABCMeta):
    """
    模块基类，实现对应方法，在有需要时会被自动调用，返回None代表不启用该模块，将继续执行下一模块
    输入参数与输出参数一致的，或没有输出的，可以被多个模块重复实现
    """

    @abstractmethod
    def init_module(self) -> None:
        """
        模块初始化
        """
        pass

    @abstractmethod
    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """
        模块开关设置，返回开关名和开关值，开关值为True时代表有值即打开，不实现该方法或返回None代表不使用开关
        部分模块支持同时开启多个，此时设置项以,分隔，开关值使用in判断
        """
        pass

    @staticmethod
    @abstractmethod
    def get_name() -> str:
        """
        获取模块名称
        """
        pass

    @abstractmethod
    def stop(self) -> None:
        """
        如果关闭时模块有服务需要停止，需要实现此方法
        :return: None，该方法可被多个模块同时处理
        """
        pass

    @abstractmethod
    def test(self) -> Tuple[bool, str]:
        """
        模块测试, 返回测试结果和错误信息
        """
        pass


def checkMessage(channel_type: MessageChannel):
    """
    检查消息渠道及消息类型，如不符合则不处理
    """

    def decorator(func):
        def wrapper(self, message: Notification, *args, **kwargs):
            # 检查消息渠道
            if message.channel and message.channel != channel_type:
                return None
            else:
                # 检查消息类型开关
                if message.mtype:
                    switchs = SystemConfigOper().get(SystemConfigKey.NotificationChannels) or []
                    for switch in switchs:
                        if switch.get("mtype") == message.mtype.value:
                            if channel_type == MessageChannel.Wechat and not switch.get("wechat"):
                                return None
                            if channel_type == MessageChannel.Telegram and not switch.get("telegram"):
                                return None
                            if channel_type == MessageChannel.Slack and not switch.get("slack"):
                                return None
                            if channel_type == MessageChannel.SynologyChat and not switch.get("synologychat"):
                                return None
                            if channel_type == MessageChannel.VoceChat and not switch.get("vocechat"):
                                return None
                            if channel_type == MessageChannel.WebPush and not switch.get("webpush"):
                                return None
                return func(self, message, *args, **kwargs)

        return wrapper

    return decorator
