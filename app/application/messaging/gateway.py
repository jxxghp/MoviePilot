"""命令分发使用的消息网关。

命令分发既要把菜单命令注册广播给实现该接口的模块与插件，也要发送命令回复、编辑已发
出的回复并收口渠道侧的输入/处理状态。这四件事都经模块分发设施完成，本模块把它们收成
一个满足 `MessageGateway` 协议的对象，交互处理器与命令中枢共用同一份实现。
"""

from typing import Dict

from app.application.orchestration import ChainBase


class CommandChain:
    """
    命令分发消息网关，持有消息与模块分发设施：
    - 收口渠道消息处理状态
    - 广播命令注册表给实现该接口的模块与插件
    - 转发/编辑命令回复消息
    """

    def __init__(self):
        """初始化消息与模块分发设施实例。"""
        self._chain = ChainBase()

    def finish_message_processing_status(self, *args, **kwargs) -> None:
        """
        结束渠道侧消息输入/处理状态，参数透传给消息分发设施
        """
        return self._chain.finish_message_processing_status(*args, **kwargs)

    def register_commands(self, commands: Dict[str, dict]) -> None:
        """
        广播菜单命令注册，由实现该接口的模块与插件自行处理
        """
        self._chain.register_commands(commands=commands)

    def post_message(self, *args, **kwargs) -> None:
        """
        发送命令回复消息，参数透传给消息分发设施
        """
        return self._chain.post_message(*args, **kwargs)

    def edit_message(self, **kwargs) -> bool:
        """
        编辑已发送的命令回复消息，参数透传给消息分发设施
        """
        return self._chain.edit_message(**kwargs)

    def put_system_message(self, title: str, message: str) -> None:
        """
        记录一条系统提示，命令执行出错时供前端消息中心展示

        :param title: 提示标题
        :param message: 提示正文
        :return: 无返回值
        """
        self._chain.messagehelper.put(title=title, message=message, role="system")
