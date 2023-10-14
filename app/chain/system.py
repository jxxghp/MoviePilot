from typing import Union

from app.chain import ChainBase
from app.schemas import Notification, MessageChannel
from app.utils.system import SystemUtils


class SystemChain(ChainBase):
    """
    系统级处理链
    """

    def remote_clear_cache(self, channel: MessageChannel, userid: Union[int, str]):
        """
        清理系统缓存
        """
        self.clear_cache()
        self.post_message(Notification(channel=channel,
                                       title=f"缓存清理完成！", userid=userid))

    def restart(self, channel: MessageChannel, userid: Union[int, str]):
        """
        重启系统
        """
        self.post_message(Notification(channel=channel,
                                       title=f"系统正在重启，请耐心等候！", userid=userid))
        SystemUtils.restart()
