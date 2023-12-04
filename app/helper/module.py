# -*- coding: utf-8 -*-
import importlib
import pkgutil
from pathlib import Path


class ModuleHelper:
    """
    模块动态加载
    """

    @classmethod
    def load(cls, package_path, filter_func=lambda name, obj: True):
        """
        导入子模块
        :param package_path: 父包名
        :param filter_func: 子模块过滤函数，入参为模块名和模块对象，返回True则导入，否则不导入
        :return:
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
                print(f'加载模块 {package_name} 失败：{err}')

        return submodules

    @staticmethod
    def dynamic_import_all_modules(base_path: Path, package_name: str):
        """
        动态导入所有模块到全局对象
        """
        modules = []
        # 遍历文件夹，找到所有模块文件
        for file in base_path.glob("*.py"):
            file_name = file.stem
            if file_name != "__init__":
                modules.append(file_name)
        # 保存已有的全局对象
        existing_globals = set(globals().keys())
        # 动态导入并添加到全局命名空间
        for module in modules:
            full_module_name = f"{package_name}.{module}"
            import_module = importlib.import_module(full_module_name)
            module_globals = import_module.__dict__
            # 仅导入全局对象中不存在的部分
            new_objects = {name: value for name, value in module_globals.items() if name not in existing_globals}
            # 更新全局命名空间
            globals().update(new_objects)
