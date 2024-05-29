# -*- coding: utf-8 -*-
import importlib
import pkgutil
import traceback
from pathlib import Path

from app.log import logger


class ModuleHelper:
    """
    模块动态加载
    """

    @classmethod
    def load(cls, package_path: str, filter_func=lambda name, obj: True):
        """
        导入模块
        :param package_path: 父包名
        :param filter_func: 子模块过滤函数，入参为模块名和模块对象，返回True则导入，否则不导入
        :return: 导入的模块对象列表
        """

        submodules: list = []
        packages = importlib.import_module(package_path)
        for importer, package_name, _ in pkgutil.iter_modules(packages.__path__):
            try:
                if package_name.startswith('_'):
                    continue
                full_package_name = f'{package_path}.{package_name}'
                module = importlib.import_module(full_package_name)
                importlib.reload(module)
                for name, obj in module.__dict__.items():
                    if name.startswith('_'):
                        continue
                    if isinstance(obj, type) and filter_func(name, obj):
                        submodules.append(obj)
            except Exception as err:
                logger.debug(f'加载模块 {package_name} 失败：{str(err)} - {traceback.format_exc()}')

        return submodules

    @classmethod
    def load_with_pre_filter(cls, package_path: str, filter_func=lambda name, obj: True):
        """
        导入子模块
        :param package_path: 父包名
        :param filter_func: 子模块过滤函数，入参为模块名和模块对象，返回True则导入，否则不导入
        :return: 导入的模块对象列表
        """

        submodules: list = []
        packages = importlib.import_module(package_path)

        def reload_module_objects(target_module):
            """加载模块并返回对象"""
            importlib.reload(target_module)
            # reload后，重新过滤已经重新加载后的模块中的对象
            return [
                obj for name, obj in target_module.__dict__.items()
                if not name.startswith('_') and isinstance(obj, type) and filter_func(name, obj)
            ]

        def reload_sub_modules(parent_module, parent_module_name):
            """重新加载一级子模块"""
            for sub_importer, sub_module_name, sub_is_pkg in pkgutil.walk_packages(parent_module.__path__):
                full_sub_module_name = f'{parent_module_name}.{sub_module_name}'
                try:
                    full_sub_module = importlib.import_module(full_sub_module_name)
                    importlib.reload(full_sub_module)
                except Exception as sub_err:
                    logger.debug(f'加载子模块 {full_sub_module_name} 失败：{str(sub_err)} - {traceback.format_exc()}')

        # 遍历包中的所有子模块
        for importer, package_name, is_pkg in pkgutil.iter_modules(packages.__path__):
            if package_name.startswith('_'):
                continue
            full_package_name = f'{package_path}.{package_name}'
            try:
                module = importlib.import_module(full_package_name)
                # 预检查模块中的对象
                candidates = [(name, obj) for name, obj in module.__dict__.items() if
                              not name.startswith('_') and isinstance(obj, type)]
                # 确定是否需要重新加载
                if any(filter_func(name, obj) for name, obj in candidates):
                    # 如果子模块是包，重新加载其子模块
                    if is_pkg:
                        reload_sub_modules(module, full_package_name)
                    submodules.extend(reload_module_objects(module))
            except Exception as err:
                logger.debug(f'加载模块 {package_name} 失败：{str(err)} - {traceback.format_exc()}')

        return submodules

    @staticmethod
    def dynamic_import_all_modules(base_path: Path, package_name: str):
        """
        动态导入目录下所有模块
        """
        modules = []
        # 遍历文件夹，找到所有模块文件
        for file in base_path.glob("*.py"):
            file_name = file.stem
            if file_name != "__init__":
                modules.append(file_name)
                full_module_name = f"{package_name}.{file_name}"
                importlib.import_module(full_module_name)
