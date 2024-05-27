import traceback
from typing import Generator, Optional, Tuple, Any

from app.core.config import settings
from app.helper.module import ModuleHelper
from app.log import logger
from app.utils.object import ObjectUtils
from app.utils.singleton import Singleton


class ModuleManager(metaclass=Singleton):
    """
    模块管理器
    """

    # 模块列表
    _modules: dict = {}
    # 运行态模块列表
    _running_modules: dict = {}

    def __init__(self):
        self.load_modules()

    def load_modules(self):
        """
        加载所有模块
        """
        # 扫描模块目录
        modules = ModuleHelper.load(
            "app.modules",
            filter_func=lambda _, obj: hasattr(obj, 'init_module') and hasattr(obj, 'init_setting')
        )
        self._running_modules = {}
        self._modules = {}
        for module in modules:
            module_id = module.__name__
            self._modules[module_id] = module
            try:
                # 生成实例
                _module = module()
                # 初始化模块
                if self.check_setting(_module.init_setting()):
                    # 通过模板开关控制加载
                    _module.init_module()
                    self._running_modules[module_id] = _module
                    logger.info(f"Moudle Loaded：{module_id}")
            except Exception as err:
                logger.error(f"Load Moudle Error：{module_id}，{str(err)} - {traceback.format_exc()}", exc_info=True)

    def stop(self):
        """
        停止所有模块
        """
        logger.info("正在停止所有模块...")
        for module_id, module in self._running_modules.items():
            if hasattr(module, "stop"):
                try:
                    module.stop()
                    logger.info(f"Moudle Stoped：{module_id}")
                except Exception as err:
                    logger.error(f"Stop Moudle Error：{module_id}，{str(err)} - {traceback.format_exc()}", exc_info=True)
        logger.info("模块停止完成")

    def reload(self):
        """
        重新加载所有模块
        """
        self.stop()
        self.load_modules()

    def test(self, modleid: str) -> Tuple[bool, str]:
        """
        测试模块
        """
        if modleid not in self._running_modules:
            return False, "模块未加载，请检查参数设置"
        module = self._running_modules[modleid]
        if hasattr(module, "test") \
                and ObjectUtils.check_method(getattr(module, "test")):
            return module.test()
        return True, "模块不支持测试"

    @staticmethod
    def check_setting(setting: Optional[tuple]) -> bool:
        """
        检查开关是否己打开，开关使用,分隔多个值，符合其中即代表开启
        """
        if not setting:
            return True
        switch, value = setting
        option = getattr(settings, switch)
        if not option:
            return False
        if option and value is True:
            return True
        if value in option:
            return True
        return False

    def get_running_module(self, module_id: str) -> Any:
        """
        根据模块id获取模块运行实例
        """
        if not module_id:
            return None
        if not self._running_modules:
            return None
        return self._running_modules.get(module_id)

    def get_running_modules(self, method: str) -> Generator:
        """
        获取实现了同一方法的模块列表
        """
        if not self._running_modules:
            return []
        for _, module in self._running_modules.items():
            if hasattr(module, method) \
                    and ObjectUtils.check_method(getattr(module, method)):
                yield module

    def get_module(self, module_id: str) -> Any:
        """
        根据模块id获取模块
        """
        if not module_id:
            return None
        if not self._modules:
            return None
        return self._modules.get(module_id)

    def get_modules(self) -> dict:
        """
        获取模块列表
        """
        return self._modules
