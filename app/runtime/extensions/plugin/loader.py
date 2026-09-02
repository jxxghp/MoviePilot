"""插件源码发现、导入和模块缓存清理。"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from app.foundation.environment import is_free_threaded_runtime
from app.runtime.extensions.plugin.version import resolve_plugin_version_dir
from app.runtime.settings import get_runtime_setting
from app.schemas.plugin import PluginInstance

PluginImportPreparer = Callable[..., None]
PluginImportScanner = Callable[..., None]
PluginValidator = Callable[[Any], bool]


class PluginLoader:
    """只负责从运行目录发现插件类，并维护对应模块缓存。"""

    _instance_import_lock = threading.RLock()

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
            source_dir = resolve_plugin_version_dir(plugin_dir)
            if not (source_dir / "__init__.py").exists():
                self._logger.debug(
                    f"跳过插件目录：{plugin_dir.name}（缺少__init__.py）"
                )
                continue
            if not self._is_runtime_compatible(source_dir):
                self._logger.warning(
                    f"跳过插件 {plugin_dir.name}：声明与当前运行时不兼容"
                )
                continue

            try:
                module_name = f"app.plugins.{plugin_dir.name}"
                self._logger.debug(f"正在导入插件模块：{module_name}")
                self._import_preparer(
                    plugin_id=plugin_dir.name,
                    plugin_dir=source_dir,
                )
                self._import_scanner(
                    plugin_id=plugin_dir.name,
                    plugin_dir=source_dir,
                )
                module = (
                    importlib.import_module(module_name)
                    if source_dir == plugin_dir
                    else self._import_versioned_module(module_name, source_dir)
                )
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

    @staticmethod
    def _import_versioned_module(module_name: str, source_dir: Path) -> Any:
        """按版本目录手动导入插件模块，绕开与目录名不一致的标准包解析。

        版本化布局下源码所在的版本目录名（如 v1_2_0）与保持不变的模块名
        （app.plugins.<插件ID>）不一致，标准 import 机制按模块名逐段定位文件会
        找不到源码，因此改为按已解析出的源码目录直接构造模块规格。

        :param module_name: 目标模块名
        :param source_dir: 已解析出的源码目录
        :return: 已执行完成的模块对象，模块名已在缓存中时直接返回缓存对象
        :raise ImportError: 无法为源码目录创建模块规格
        """
        cached = sys.modules.get(module_name)
        if cached is not None:
            return cached
        source_file = source_dir / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            module_name,
            source_file,
            submodule_search_locations=[str(source_dir)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"无法创建模块规格：{module_name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def load_instance(
        self,
        instance: PluginInstance,
        validator: PluginValidator,
    ) -> list[Any]:
        """在实例专属模块命名空间中重新执行源插件代码并返回适配类。"""
        plugin_dir = self._plugins_root / instance.source_plugin_id.lower()
        source_dir = resolve_plugin_version_dir(plugin_dir)
        source_file = source_dir / "__init__.py"
        if not source_file.exists():
            self._logger.warning(
                f"虚拟插件实例 {instance.instance_id} 的源码不存在：{source_dir}"
            )
            return []
        if not self._is_runtime_compatible(source_dir):
            self._logger.warning(
                f"跳过虚拟插件实例 {instance.instance_id}：声明与当前运行时不兼容"
            )
            return []

        module_name = f"app.plugins.{instance.instance_id.lower()}"
        self.clear_modules(instance.instance_id)
        try:
            self._import_preparer(
                plugin_id=instance.source_plugin_id.lower(),
                plugin_dir=source_dir,
            )
            self._import_scanner(
                plugin_id=instance.source_plugin_id.lower(),
                plugin_dir=source_dir,
            )
            spec = importlib.util.spec_from_file_location(
                module_name,
                source_file,
                submodule_search_locations=[str(source_dir)],
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"无法创建模块规格：{module_name}")
            module = importlib.util.module_from_spec(spec)
            self._execute_instance_module(
                module=module,
                module_name=module_name,
                source_module_name=(
                    f"app.plugins.{instance.source_plugin_id.lower()}"
                ),
                loader=spec.loader,
            )
            for name, candidate in module.__dict__.items():
                if name.startswith("_") or not isinstance(candidate, type):
                    continue
                if not validator(candidate):
                    continue
                self._adapt_instance_class(candidate, instance)
                self._logger.debug(
                    f"从 {instance.source_plugin_id} 加载虚拟插件实例：{instance.instance_id}"
                )
                return [candidate]
        except Exception as error:  # noqa: BLE001
            self.clear_modules(instance.instance_id)
            self._logger.error(
                f"加载虚拟插件实例 {instance.instance_id} 失败：{error} - "
                f"{traceback.format_exc()}"
            )
        return []

    @staticmethod
    def _is_runtime_compatible(plugin_dir: Path) -> bool:
        """按载荷自身 package 声明执行运行时兼容门禁，缺失声明时保持兼容。"""
        package_file = plugin_dir / "package.json"
        try:
            package = json.loads(package_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return True
        if not isinstance(package, dict):
            return True
        version_flag = get_runtime_setting("VERSION_FLAG")
        if version_flag and package.get(version_flag) is False:
            return False
        return not (is_free_threaded_runtime() and package.get("v3t") is False)

    def _execute_instance_module(
        self,
        *,
        module: Any,
        module_name: str,
        source_module_name: str,
        loader: Any,
    ) -> None:
        """执行实例模块，并把旧式自身绝对导入迁移到实例命名空间。"""
        source_prefix = f"{source_module_name}."
        parent_module = sys.modules.get("app.plugins")
        source_attribute = source_module_name.rsplit(".", 1)[-1]
        missing = object()
        with self._instance_import_lock:
            source_snapshot = {
                name: loaded_module
                for name, loaded_module in list(sys.modules.items())
                if name == source_module_name or name.startswith(source_prefix)
            }
            parent_snapshot = (
                getattr(parent_module, source_attribute, missing)
                if parent_module
                else missing
            )
            for name in source_snapshot:
                sys.modules.pop(name, None)
            sys.modules[module_name] = module
            # 兼容旧插件在包内仍写 app.plugins.<source> 的绝对导入。
            sys.modules[source_module_name] = module
            if parent_module:
                setattr(parent_module, source_attribute, module)
            captured: dict[str, Any] = {}
            try:
                loader.exec_module(module)
                captured = {
                    name: loaded_module
                    for name, loaded_module in list(sys.modules.items())
                    if name == source_module_name or name.startswith(source_prefix)
                }
            finally:
                for name in list(sys.modules):
                    if name == source_module_name or name.startswith(source_prefix):
                        sys.modules.pop(name, None)
                sys.modules.update(source_snapshot)
                if parent_module:
                    if parent_snapshot is missing:
                        try:
                            delattr(parent_module, source_attribute)
                        except AttributeError:
                            pass
                    else:
                        setattr(parent_module, source_attribute, parent_snapshot)

            for source_name, loaded_module in captured.items():
                suffix = source_name[len(source_module_name):]
                instance_name = f"{module_name}{suffix}"
                sys.modules[instance_name] = loaded_module
                self._retarget_module_identity(
                    loaded_module,
                    source_name,
                    instance_name,
                )

    @staticmethod
    def _retarget_module_identity(
        module: Any,
        source_name: str,
        instance_name: str,
    ) -> None:
        """修正被旧绝对路径加载对象的模块身份，避免事件与诊断键冲突。"""
        if getattr(module, "__name__", None) == source_name:
            module.__name__ = instance_name
        package_name = getattr(module, "__package__", None)
        if isinstance(package_name, str) and package_name.startswith(source_name):
            module.__package__ = instance_name + package_name[len(source_name):]
        spec = getattr(module, "__spec__", None)
        if spec and getattr(spec, "name", None) == source_name:
            spec.name = instance_name
        for value in vars(module).values():
            if getattr(value, "__module__", None) == source_name:
                try:
                    value.__module__ = instance_name
                except (AttributeError, TypeError):
                    continue

    @staticmethod
    def _adapt_instance_class(candidate: Any, instance: PluginInstance) -> None:
        """只改运行身份与展示元数据，不改源码、限定名和联邦产物。"""
        candidate.__name__ = instance.instance_id
        candidate.plugin_instance_id = instance.instance_id
        candidate.plugin_source_id = instance.source_plugin_id
        candidate.is_clone = True
        candidate.plugin_config_prefix = f"{instance.instance_id.lower()}_"
        if instance.plugin_name:
            candidate.plugin_name = instance.plugin_name
        if instance.plugin_desc:
            candidate.plugin_desc = instance.plugin_desc
        if instance.plugin_icon:
            candidate.plugin_icon = instance.plugin_icon

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
