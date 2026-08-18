"""插件源码发现、导入和模块缓存清理。"""

from __future__ import annotations

import importlib
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional


PluginImportPreparer = Callable[..., None]
PluginImportScanner = Callable[..., None]
PluginValidator = Callable[[Any], bool]


class PluginLoader:
    """只负责从运行目录发现插件类，并维护对应模块缓存。"""

    def __init__(
        self,
        *,
        plugins_root: Path,
        import_preparer: PluginImportPreparer,
        import_scanner: PluginImportScanner,
        log: Any,
    ) -> None:
        """保存插件目录、导入前置能力和日志端口。"""
        self._plugins_root = plugins_root
        self._import_preparer = import_preparer
        self._import_scanner = import_scanner
        self._logger = log

    def load(
        self,
        plugin_id: Optional[str],
        installed_plugins: list[str],
        validator: PluginValidator,
    ) -> list[Any]:
        """只导入指定插件或已安装插件，并返回通过契约检查的插件类。"""
        if not self._plugins_root.exists():
            self._logger.warning(f"插件目录不存在：{self._plugins_root}")
            return []

        targets = (
            [plugin_id.lower()]
            if plugin_id
            else [item.lower() for item in installed_plugins]
        )
        if not targets:
            self._logger.debug("没有需要加载的插件")
            return []

        plugins = []
        loaded_classes = set()
        for plugin_dir in self._plugins_root.iterdir():
            if not plugin_dir.is_dir() or plugin_dir.name.startswith("_"):
                continue
            if plugin_dir.name not in targets:
                self._logger.debug(
                    f"跳过插件目录：{plugin_dir.name}（不在加载列表中）"
                )
                continue
            if not (plugin_dir / "__init__.py").exists():
                self._logger.debug(
                    f"跳过插件目录：{plugin_dir.name}（缺少__init__.py）"
                )
                continue

            try:
                module_name = f"app.plugins.{plugin_dir.name}"
                self._logger.debug(f"正在导入插件模块：{module_name}")
                self._import_preparer(
                    plugin_id=plugin_dir.name,
                    plugin_dir=plugin_dir,
                )
                self._import_scanner(
                    plugin_id=plugin_dir.name,
                    plugin_dir=plugin_dir,
                )
                module = importlib.import_module(module_name)
                for name, candidate in module.__dict__.items():
                    if name.startswith("_") or not isinstance(candidate, type):
                        continue
                    if name in loaded_classes or not validator(candidate):
                        continue
                    loaded_classes.add(name)
                    plugins.append(candidate)
                    self._logger.debug(f"找到符合条件的插件类：{name}")
                    break
            except Exception as err:
                self._logger.error(
                    f"加载插件 {plugin_dir.name} 失败：{str(err)} - "
                    f"{traceback.format_exc()}"
                )
        return plugins

    def clear_modules(self, plugin_id: Optional[str] = None) -> list[str]:
        """清除指定插件或全部插件的 Python 模块缓存。"""
        prefix = (
            f"app.plugins.{plugin_id.lower()}"
            if plugin_id
            else "app.plugins"
        )
        removed = [
            module_name
            for module_name in list(sys.modules)
            if module_name == prefix or module_name.startswith(f"{prefix}.")
        ]
        for module_name in removed:
            sys.modules.pop(module_name, None)
            self._logger.debug(f"已清除插件模块缓存：{module_name}")
        importlib.invalidate_caches()
        self._logger.debug("已清除查找器的缓存")
        if plugin_id:
            if removed:
                self._logger.info(
                    f"插件 {plugin_id} 共清除 {len(removed)} 个模块缓存：{removed}"
                )
            else:
                self._logger.debug(f"插件 {plugin_id} 没有找到需要清除的模块缓存")
        return removed
