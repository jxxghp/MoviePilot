"""插件实例生命周期应用能力。"""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any, Optional, ParamSpec, TypeVar, cast

from app.runtime.extensions.plugin.database import PluginDatabase
from app.runtime.observability import record_metric
from app.schemas.plugin import PluginRuntimeStatus

P = ParamSpec("P")
R = TypeVar("R")


def observe_plugin_lifecycle(operation: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """为插件生命周期入口记录不含插件标识的低基数耗时。"""

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        """包装单个同步生命周期方法，并保留原始调用签名。"""

        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            """执行生命周期方法并把失败状态归一为 error。"""
            started_at = time.perf_counter()
            outcome = "success"
            try:
                result = func(*args, **kwargs)
                statuses = result.values() if isinstance(result, dict) else (result,)
                if result is False or PluginRuntimeStatus.LOAD_FAILED in statuses:
                    outcome = "error"
                return result
            except BaseException:
                outcome = "error"
                raise
            finally:
                record_metric(
                    "plugin.lifecycle.duration",
                    time.perf_counter() - started_at,
                    operation=operation,
                    outcome=outcome,
                )

        return cast(Callable[P, R], wrapper)

    return decorator


class PluginLifecycle:
    """管理插件发现、初始化、启停和热重载，不持有市场或 HTTP 路由职责。"""

    _EVENT_HANDLERS_QUIESCED = "__event_handlers__"

    def __init__(
        self,
        *,
        classes: dict[str, Any],
        running: dict[str, Any],
        load_plugins: Callable[[Optional[str], list[str], Callable[[Any], bool]], list[Any]],
        installed_plugins: Callable[[], list[str]],
        plugin_config: Callable[[str], dict],
        auth_checker: Callable[[Any], bool],
        clear_modules: Callable[[Optional[str]], Any],
        clear_tools: Callable[[], None],
        enable_events: Callable[[Any], None],
        disable_events: Callable[[Any], None],
        runtime_status_writer: Callable[[str, PluginRuntimeStatus], None],
        database: Callable[[], PluginDatabase],
        log: Any,
        event_sender: Callable[..., Any],
        refresh_classification: Callable[[str, Any], None] | None = None,
        remove_classification: Callable[[str], None] | None = None,
    ) -> None:
        """保存注册表、加载器、数据库和事件端口。"""
        self._classes = classes
        self._running = running
        self._load_plugins = load_plugins
        self._installed_plugins = installed_plugins
        self._plugin_config = plugin_config
        self._auth_checker = auth_checker
        self._clear_modules = clear_modules
        self._clear_tools = clear_tools
        self._enable_events = enable_events
        self._disable_events = disable_events
        self._runtime_status_writer = runtime_status_writer
        self._database = database
        self._logger = log
        self._event_sender = event_sender
        self._refresh_classification = refresh_classification or (
            lambda _plugin_id, _instance: None
        )
        self._remove_classification = remove_classification or (
            lambda _plugin_id: None
        )
        self._lifecycle_lock = threading.RLock()
        self._quiesced_hooks: dict[str, set[str]] = {}

    @observe_plugin_lifecycle("start")
    def start(
        self,
        plugin_id: Optional[str] = None,
    ) -> dict[str, PluginRuntimeStatus]:
        """加载并初始化插件，返回每个目标的明确运行结果。"""
        installed_plugins = self._installed_plugins()
        results: dict[str, PluginRuntimeStatus] = {}
        if plugin_id:
            self._runtime_status_writer(plugin_id, PluginRuntimeStatus.READY)

        def check_module(module: Any) -> bool:
            """判断模块是否具备宿主插件最小生命周期钩子。"""
            return hasattr(module, "init_plugin") and hasattr(module, "plugin_name")

        plugins = self._load_plugins(plugin_id, installed_plugins, check_module)
        plugins.sort(key=lambda item: getattr(item, "plugin_order", 0))
        for plugin in plugins:
            current_id = plugin.__name__
            if plugin_id and current_id.casefold() != plugin_id.casefold():
                continue
            try:
                if not self._auth_checker(plugin):
                    self._remove_classification(current_id)
                    if current_id in self._classes:
                        self._classes[current_id] = plugin
                    status = PluginRuntimeStatus.BLOCKED_BY_POLICY
                    self._runtime_status_writer(plugin_id or current_id, status)
                    results[plugin_id or current_id] = status
                    continue
                self._remove_classification(current_id)
                self._classes[current_id] = plugin
                instance = plugin()
                instance.init_plugin(self._plugin_config(current_id))
                self._ensure_database(current_id, instance)
                enabled = bool(instance.get_state())
                if enabled:
                    self._refresh_classification_safely(current_id, instance)
                else:
                    self._remove_classification(current_id)
                self._quiesced_hooks.pop(current_id, None)
                self._running[current_id] = instance
                self._logger.info(
                    f"加载插件：{current_id} 版本：{instance.plugin_version}"
                )
                if enabled:
                    self._enable_events(plugin)
                else:
                    self._disable_events(plugin)
                status = PluginRuntimeStatus.ACTIVE
                self._runtime_status_writer(plugin_id or current_id, status)
                results[plugin_id or current_id] = status
            except Exception as error:  # noqa: BLE001
                self._remove_classification(current_id)
                status = PluginRuntimeStatus.LOAD_FAILED
                self._runtime_status_writer(plugin_id or current_id, status)
                results[plugin_id or current_id] = status
                # 建库发生在进入运行态之前：失败的插件不会出现在 _running 里，卸载路径
                # 因此够不到它，句柄只能在这里释放
                self._release_databases((current_id,))
                self._logger.error(
                    f"加载插件 {current_id} 出错：{error} - {traceback.format_exc()}"
                )
        if plugin_id and not any(
            result_id.casefold() == plugin_id.casefold()
            for result_id in results
        ):
            self._remove_classification(plugin_id)
            status = PluginRuntimeStatus.LOAD_FAILED
            self._runtime_status_writer(plugin_id, status)
            results[plugin_id] = status
        self._clear_tools()
        return results

    @staticmethod
    def _declaration(instance: Any, hook_name: str) -> Any:
        """读取插件的数据库声明钩子，未实现该钩子时视为未声明。"""
        hook = getattr(instance, hook_name, None)
        return hook() if callable(hook) else None

    def _ensure_database(self, plugin_id: str, instance: Any) -> None:
        """按插件声明建立其自有数据库，两项声明都缺失时不建库。"""
        migrations = self._declaration(instance, "get_database_migrations")
        self._database().ensure(
            plugin_id,
            tuple(self._declaration(instance, "get_database_models") or ()),
            Path(migrations) if migrations else None,
        )

    def _release_databases(self, plugin_ids: tuple[str, ...]) -> None:
        """释放已卸载插件的自有数据库连接，单个失败不得阻断其余释放。"""
        database = self._database()
        for plugin_id in plugin_ids:
            try:
                database.release(plugin_id)
            except Exception as error:  # noqa: BLE001  释放故障不得阻断卸载
                self._logger.warning(
                    f"释放插件 {plugin_id} 的数据库连接时发生错误: {error}"
                )

    @observe_plugin_lifecycle("initialize")
    def initialize(self, plugin_id: str, config: dict) -> None:
        """重新应用指定插件配置并刷新事件注册状态。"""
        plugin = self._running.get(plugin_id)
        if not plugin:
            return
        self._remove_classification(plugin_id)
        try:
            plugin.init_plugin(config)
            enabled = bool(plugin.get_state())
            if enabled:
                self._refresh_classification_safely(plugin_id, plugin)
                self._enable_events(type(plugin))
            else:
                self._remove_classification(plugin_id)
                self._disable_events(type(plugin))
        except Exception:
            self._remove_classification(plugin_id)
            raise
        self._clear_tools()

    def _refresh_classification_safely(
        self,
        plugin_id: str,
        instance: Any,
    ) -> None:
        """隔离可选分类声明故障，并确保无效声明不会保留旧注册。"""
        try:
            self._refresh_classification(plugin_id, instance)
        except Exception as error:  # noqa: BLE001  可选声明不得阻断插件主功能
            self._remove_classification(plugin_id)
            self._logger.warning(
                f"插件 {plugin_id} 的分类扩展声明无效，已忽略：{error}"
            )

    @observe_plugin_lifecycle("stop")
    def stop(self, plugin_id: Optional[str] = None) -> None:
        """按旧单阶段 ABI 先解绑 handler，再停止并强制卸载插件。"""
        with self._lifecycle_lock:
            plugins = self._select_running_plugins(plugin_id)
            self._quiesce_selected(plugins)
            self._finalize(
                plugin_id,
                require_quiesced=False,
                disable_events=not self._handlers_quiesced(plugins),
            )

    @observe_plugin_lifecycle("quiesce")
    def quiesce(self, plugin_id: Optional[str] = None) -> bool:
        """先解绑事件 handler，再按旧 hook 顺序停止生产者并保留实例。"""
        with self._lifecycle_lock:
            plugins = self._select_running_plugins(plugin_id)
            return self._quiesce_selected(plugins)

    @observe_plugin_lifecycle("quiesce_handlers")
    def quiesce_handlers(self, plugin_id: Optional[str] = None) -> bool:
        """禁止目标插件接收新事件，保留实例供在途 handler 和后续 hook 使用。"""
        with self._lifecycle_lock:
            plugins = self._select_running_plugins(plugin_id)
            return self._disable_selected_handlers(plugins)

    @observe_plugin_lifecycle("quiesce_services")
    def quiesce_services(self, plugin_id: Optional[str] = None) -> bool:
        """在事件结算屏障后执行旧 close、stop_service hook。"""
        with self._lifecycle_lock:
            plugins = self._select_running_plugins(plugin_id)
            if not self._handlers_quiesced(plugins):
                self._logger.warning("插件事件 handler 尚未全部停用，拒绝关闭插件资源")
                return False
            return self._quiesce_hooks(plugins)

    def _quiesce_selected(self, plugins: dict[str, Any]) -> bool:
        """兼容单阶段调用：先停用全部 handler，再执行稳定快照的旧 hooks。"""
        if not self._disable_selected_handlers(plugins):
            return False
        return self._quiesce_hooks(plugins)

    def _disable_selected_handlers(self, plugins: dict[str, Any]) -> bool:
        """先停用稳定快照的全部事件入口，任一失败时不执行破坏性 hook。"""
        all_converged = True
        for current_id, plugin in plugins.items():
            completed = self._quiesced_hooks.setdefault(current_id, set())
            if self._EVENT_HANDLERS_QUIESCED in completed:
                continue
            try:
                self._disable_events(type(plugin))
            except Exception as error:  # noqa: BLE001  插件边界必须隔离
                all_converged = False
                self._logger.warning(
                    f"停用插件 {current_id} 的事件 handler 时发生错误: {error}"
                )
                continue
            completed.add(self._EVENT_HANDLERS_QUIESCED)
        return all_converged

    def _quiesce_hooks(self, plugins: dict[str, Any]) -> bool:
        """执行旧 ABI hooks，并只重试尚未成功的步骤。"""
        all_converged = True
        for current_id, plugin in plugins.items():
            completed = self._quiesced_hooks.setdefault(current_id, set())
            for hook_name in ("close", "stop_service"):
                if hook_name in completed:
                    continue
                hook = getattr(plugin, hook_name, None)
                if not callable(hook):
                    completed.add(hook_name)
                    continue
                try:
                    result = hook()
                except Exception as error:  # noqa: BLE001  插件边界必须隔离
                    all_converged = False
                    self._logger.warning(
                        f"停止插件 {current_id} 的 {hook_name} 时发生错误: {error}"
                    )
                    continue
                if result is False:
                    all_converged = False
                    self._logger.warning(
                        f"停止插件 {current_id} 的 {hook_name} 未收敛"
                    )
                    continue
                completed.add(hook_name)
        return all_converged

    @observe_plugin_lifecycle("finalize")
    def finalize(self, plugin_id: Optional[str] = None) -> bool:
        """在 handler、旧 hook 和事件屏障均收敛后卸载插件实例。"""
        return self._finalize(
            plugin_id,
            require_quiesced=True,
            disable_events=False,
        )

    def _finalize(
        self,
        plugin_id: Optional[str],
        *,
        require_quiesced: bool,
        disable_events: bool = True,
    ) -> bool:
        """按严格或兼容策略卸载插件，并在清理失败时保留实例所有权。"""
        with self._lifecycle_lock:
            plugins = self._select_running_plugins(plugin_id)
            if require_quiesced and any(
                not self._is_quiesced(current_id, plugin)
                for current_id, plugin in plugins.items()
            ):
                self._logger.warning("插件后台服务尚未全部收敛，拒绝卸载运行实例")
                return False

            try:
                if disable_events:
                    for plugin in plugins.values():
                        self._disable_events(type(plugin))
                self._clear_modules(plugin_id)
                self._clear_tools()
                for current_id in plugins:
                    self._remove_classification(current_id)
            except Exception as error:  # noqa: BLE001  保留实例所有权供后续重试
                self._logger.warning(f"卸载插件运行实例时发生错误: {error}")
                return False

            if plugin_id:
                runtime_id = self._resolve_runtime_id(plugin_id)
                self._classes.pop(runtime_id, None)
                self._running.pop(runtime_id, None)
                self._quiesced_hooks.pop(runtime_id, None)
                self._release_databases((runtime_id,))
            else:
                # 启动中途失败的插件只登记在 _classes 里，漏掉它就漏掉它的连接池
                released_ids = tuple(dict.fromkeys((*self._running, *self._classes)))
                self._classes.clear()
                self._running.clear()
                self._quiesced_hooks.clear()
                self._release_databases(released_ids)
            self._logger.info("插件停止完成")
            return True

    def _select_running_plugins(self, plugin_id: Optional[str]) -> dict[str, Any]:
        """返回本阶段处理的稳定实例快照，并保持旧停机日志语义。"""
        if plugin_id:
            self._logger.info(f"正在停止插件 {plugin_id}...")
            runtime_id = self._resolve_runtime_id(plugin_id)
            plugin = self._running.get(runtime_id)
            plugins = {runtime_id: plugin} if plugin else {}
            if not plugin:
                self._logger.debug(f"插件 {plugin_id} 不存在或未加载")
            return plugins
        self._logger.info("正在停止所有插件...")
        return dict(self._running)

    def _resolve_runtime_id(self, plugin_id: str) -> str:
        """按不区分大小写的插件 ID 找到运行时注册表键。"""
        for runtime_id in (*self._running, *self._classes):
            if runtime_id.casefold() == plugin_id.casefold():
                return runtime_id
        return plugin_id

    def _is_quiesced(self, plugin_id: str, plugin: Any) -> bool:
        """判断 handler 及当前实例声明的旧 ABI hooks 是否均已成功收敛。"""
        required = {self._EVENT_HANDLERS_QUIESCED} | {
            hook_name
            for hook_name in ("close", "stop_service")
            if callable(getattr(plugin, hook_name, None))
        }
        return required.issubset(self._quiesced_hooks.get(plugin_id, set()))

    def _handlers_quiesced(self, plugins: dict[str, Any]) -> bool:
        """判断稳定快照中的全部插件是否已经停用事件入口。"""
        return all(
            self._EVENT_HANDLERS_QUIESCED
            in self._quiesced_hooks.get(plugin_id, set())
            for plugin_id in plugins
        )

    @observe_plugin_lifecycle("reload")
    def reload(
        self,
        plugin_id: str,
        reload_event: Any,
    ) -> PluginRuntimeStatus:
        """重启指定插件并返回本次加载结果。"""
        self._runtime_status_writer(plugin_id, PluginRuntimeStatus.READY)
        self.stop(plugin_id)
        results = self.start(plugin_id)
        status = next(
            status
            for result_id, status in results.items()
            if result_id.casefold() == plugin_id.casefold()
        )
        self._event_sender(reload_event, data={"plugin_id": plugin_id})
        return status
