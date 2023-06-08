import traceback
from abc import abstractmethod
from typing import Optional, Any

from app.core import Context, ModuleManager, EventManager
from app.log import logger
from app.utils.singleton import AbstractSingleton, Singleton


class ChainBase(AbstractSingleton, metaclass=Singleton):
    """
    处理链基类
    """

    def __init__(self):
        """
        公共初始化
        """
        self.modulemanager = ModuleManager()
        self.eventmanager = EventManager()

    @abstractmethod
    def process(self, *args, **kwargs) -> Optional[Context]:
        """
        处理链，返回上下文
        """
        pass

    def run_module(self, method: str, *args, **kwargs) -> Any:
        """
        运行包含该方法的所有模块，然后返回结果
        """

        def is_result_empty(ret):
            """
            判断结果是否为空
            """
            if isinstance(ret, tuple):
                return all(value is None for value in ret)
            else:
                return result is None

        logger.debug(f"请求模块执行：{method} ...")
        result = None
        modules = self.modulemanager.get_modules(method)
        for module in modules:
            try:
                if is_result_empty(result):
                    result = getattr(module, method)(*args, **kwargs)
                else:
                    if isinstance(result, tuple):
                        temp = getattr(module, method)(*result)
                    else:
                        temp = getattr(module, method)(result)
                    if temp:
                        result = temp
            except Exception as err:
                logger.error(f"运行模块 {method} 出错：{module.__class__.__name__} - {err}\n{traceback.print_exc()}")
        return result
