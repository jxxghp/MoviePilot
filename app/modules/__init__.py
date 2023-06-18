from abc import abstractmethod, ABCMeta
from typing import Tuple, Union


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
        """
        pass

    @abstractmethod
    def stop(self) -> None:
        """
        如果关闭时模块有服务需要停止，需要实现此方法
        :return: None，该方法可被多个模块同时处理
        """
        pass
