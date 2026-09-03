"""插件公开能力投影。"""

import inspect
from typing import Any, Callable, Dict, List, Mapping, Optional

from app.runtime.extensions.plugin.contracts import (
    PluginDashboardError,
    PluginNotFoundError,
    supports_plugin_hook,
)
from app.runtime.log import logger as default_logger
from app.schemas.plugin import PluginDashboard


class PluginProjection:
    """把运行态插件投影为宿主命令、API、服务、模块和动作清单。"""

    def __init__(
        self,
        running_plugins: Mapping[str, Any],
        log: Any = default_logger,
        remote_entry_factory: Optional[Callable[[str, str], str]] = None,
    ) -> None:
        """保存运行态插件映射和错误日志端口。"""
        self._running_plugins = running_plugins
        self._logger = log
        self._remote_entry_factory = remote_entry_factory

    def _items(self, pid: Optional[str]) -> list[tuple[str, Any]]:
        """返回指定插件或运行态插件的稳定快照。"""
        snapshot = dict(self._running_plugins)
        if pid:
            plugin = snapshot.get(pid)
            return [(pid, plugin)] if plugin is not None else []
        return list(snapshot.items())

    def commands(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """聚合插件命令并补充插件 ID。"""
        commands: list[dict] = []
        for plugin_id, plugin in self._items(pid):
            if not supports_plugin_hook(plugin, "get_command"):
                continue
            try:
                if not plugin.get_state():
                    continue
                for command in plugin.get_command() or []:
                    command["pid"] = plugin_id
                    commands.append(command)
            except Exception as error:
                self._logger.error(f"获取插件命令出错：{str(error)}")
        return commands

    def apis(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """聚合插件 API 并补充宿主路径和默认认证方式。"""
        apis: list[dict] = []
        for plugin_id, plugin in self._items(pid):
            if not supports_plugin_hook(plugin, "get_api"):
                continue
            try:
                for source_api in plugin.get_api() or []:
                    api = dict(source_api)
                    api["path"] = f"/{plugin_id}{api['path']}"
                    if not api.get("auth"):
                        api["auth"] = "apikey"
                    apis.append(api)
            except Exception as error:
                self._logger.error(f"获取插件 {plugin_id} API出错：{str(error)}")
        return apis

    def services(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """聚合启用插件的定时服务。"""
        services: list[dict] = []
        for plugin_id, plugin in self._items(pid):
            if not supports_plugin_hook(plugin, "get_service"):
                continue
            try:
                if plugin.get_state():
                    services.extend(plugin.get_service() or [])
            except Exception as error:
                self._logger.error(f"获取插件 {plugin_id} 服务出错：{str(error)}")
        return services

    def modules(self, pid: Optional[str] = None) -> Dict[tuple, Dict[str, Any]]:
        """聚合启用插件的模块方法清单。"""
        modules: dict[tuple, dict] = {}
        for plugin_id, plugin in self._items(pid):
            if not supports_plugin_hook(plugin, "get_module"):
                continue
            try:
                if plugin.get_state():
                    declared = plugin.get_module()
                    # 基类默认实现返回 None；只接受映射，防止把 list 当成方法表传入调度器
                    if declared is None:
                        continue
                    if not isinstance(declared, Mapping):
                        self._logger.error(
                            f"插件 {plugin_id} 的 get_module() 返回值必须是字典，实际是 {type(declared).__name__}"
                        )
                        continue
                    modules[(plugin_id, plugin.get_name())] = declared
            except Exception as error:
                self._logger.error(f"获取插件 {plugin_id} 模块出错：{str(error)}")
        return modules

    def media_sources(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """聚合启用插件声明的媒体数据源。"""
        sources: list[dict] = []
        for plugin_id, plugin in self._items(pid):
            if not supports_plugin_hook(plugin, "get_media_source"):
                continue
            try:
                if not plugin.get_state():
                    continue
                for source in plugin.get_media_source() or []:
                    item = self._media_source_mapping(source)
                    if item is None:
                        continue
                    item.setdefault("plugin_id", plugin_id)
                    sources.append(item)
            except Exception as error:
                self._logger.error(f"获取插件 {plugin_id} 媒体数据源出错：{str(error)}")
        return sources

    @staticmethod
    def _media_source_mapping(source: Any) -> dict[str, Any] | None:
        """把旧字典或新 SDK 模型转换为隔离的 JSON 字典。"""
        if isinstance(source, Mapping):
            return dict(source)
        model_dump = getattr(source, "model_dump", None)
        if not callable(model_dump):
            return None
        payload = model_dump(mode="json")
        return dict(payload) if isinstance(payload, Mapping) else None

    def actions(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """聚合启用插件的工作流动作。"""
        actions: list[dict] = []
        for plugin_id, plugin in self._items(pid):
            if not supports_plugin_hook(plugin, "get_actions"):
                continue
            try:
                if not plugin.get_state():
                    continue
                plugin_actions = plugin.get_actions()
                if plugin_actions:
                    actions.append({
                        "plugin_id": plugin_id,
                        "plugin_name": plugin.plugin_name,
                        "actions": plugin_actions,
                    })
            except Exception as error:
                self._logger.error(f"获取插件 {plugin_id} 动作出错：{str(error)}")
        return actions

    def remotes(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """投影插件联邦远程入口，并保持旧渲染模式筛选语义。"""
        remotes = []
        for plugin_id, plugin in self._items(pid):
            if not supports_plugin_hook(plugin, "get_render_mode"):
                continue
            render_mode, dist_path = plugin.get_render_mode()
            if render_mode != "vue":
                continue
            if not self._remote_entry_factory:
                raise RuntimeError("插件联邦入口生成器尚未配置")
            remote = {
                "id": plugin_id,
                "url": self._remote_entry_factory(plugin_id, dist_path),
                "name": plugin.plugin_name,
            }
            source_plugin_id = getattr(plugin, "plugin_source_id", None)
            if source_plugin_id:
                remote["source_plugin_id"] = source_plugin_id
            remotes.append(remote)
        return remotes

    def auth_providers(self) -> List[Dict[str, Any]]:
        """投影启用插件声明的登录认证提供方。"""
        providers = []
        for plugin_id, plugin in self._items(None):
            if not plugin.get_state() or not supports_plugin_hook(
                    plugin, "get_auth_providers"
            ):
                continue
            try:
                plugin_providers = plugin.get_auth_providers() or []
            except Exception as error:
                self._logger.error(
                    f"获取插件 {plugin_id} 登录认证提供方出错：{str(error)}"
                )
                continue
            render_mode = None
            dist_path = None
            if supports_plugin_hook(plugin, "get_render_mode"):
                render_mode, dist_path = plugin.get_render_mode()
            for raw_provider in plugin_providers:
                if not raw_provider or not isinstance(raw_provider, dict):
                    continue
                provider = raw_provider.copy()
                provider["type"] = "plugin"
                provider["plugin_id"] = plugin_id
                provider.setdefault("id", f"plugin:{plugin_id}")
                provider.setdefault("name", plugin.plugin_name)
                provider.setdefault("enabled", True)
                if render_mode == "vue" and dist_path:
                    if not self._remote_entry_factory:
                        raise RuntimeError("插件联邦入口生成器尚未配置")
                    provider.setdefault("component", "AuthPage")
                    remote = {
                        "id": plugin_id,
                        "url": self._remote_entry_factory(plugin_id, dist_path),
                        "name": plugin.plugin_name,
                    }
                    source_plugin_id = getattr(plugin, "plugin_source_id", None)
                    if source_plugin_id:
                        remote["source_plugin_id"] = source_plugin_id
                    provider["remote"] = remote
                providers.append(provider)
        return providers

    def sidebar(self) -> List[Dict[str, Any]]:
        """投影启用 Vue 插件的侧栏导航，并规整权限、分区和顺序。"""
        valid_sections = {"start", "discovery", "subscribe", "organize", "system"}
        valid_permissions = {"subscribe", "discovery", "search", "manage", "admin"}
        items = []
        for plugin_id, plugin in self._items(None):
            if not plugin.get_state() or not supports_plugin_hook(
                    plugin, "get_sidebar_nav"
            ):
                continue
            if not supports_plugin_hook(plugin, "get_render_mode"):
                continue
            render_mode, _ = plugin.get_render_mode()
            if render_mode != "vue":
                continue
            try:
                nav_list = plugin.get_sidebar_nav()
                if not nav_list:
                    continue
                for raw in nav_list:
                    if not raw or not isinstance(raw, dict):
                        continue
                    nav_key = str(
                        raw.get("nav_key") or raw.get("key") or "main"
                    ).strip()
                    if not nav_key or any(
                            character in nav_key for character in ["/", "?", "#", " "]
                    ):
                        self._logger.warning(
                            f"插件[{plugin_id}]侧栏项 nav_key 无效，已跳过: "
                            f"{nav_key!r}"
                        )
                        continue
                    section = str(raw.get("section") or "system").lower()
                    if section not in valid_sections:
                        section = "system"
                    permission = raw.get("permission")
                    if permission is not None and str(permission) not in valid_permissions:
                        permission = None
                    elif permission is not None:
                        permission = str(permission)
                    try:
                        order = int(raw.get("order", 0))
                    except (TypeError, ValueError):
                        order = 0
                    items.append({
                        "plugin_id": plugin_id,
                        "nav_key": nav_key,
                        "title": raw.get("title") or plugin.plugin_name,
                        "icon": raw.get("icon") or "mdi-puzzle",
                        "section": section,
                        "permission": permission,
                        "order": order,
                    })
            except Exception as error:
                self._logger.error(
                    f"获取插件[{plugin_id}]侧栏导航出错：{str(error)}"
                )
        items.sort(
            key=lambda item: (
                item["section"],
                item["order"],
                item["plugin_id"],
                item["nav_key"],
            )
        )
        return items

    def dashboard_metadata(self) -> List[Dict[str, str]]:
        """投影启用插件的单仪表板或多仪表板元信息。"""
        metadata = []
        for plugin_id, plugin in self._items(None):
            if not supports_plugin_hook(plugin, "get_dashboard"):
                continue
            try:
                if not plugin.get_state():
                    continue
                if supports_plugin_hook(plugin, "get_dashboard_meta"):
                    plugin_metadata = plugin.get_dashboard_meta()
                    if plugin_metadata:
                        metadata.extend({
                            "id": plugin_id,
                            "name": item.get("name"),
                            "key": item.get("key"),
                        } for item in plugin_metadata if item)
                else:
                    metadata.append({
                        "id": plugin_id,
                        "name": plugin.plugin_name,
                        "key": "",
                    })
            except Exception as error:
                self._logger.error(
                    f"获取插件[{plugin_id}]仪表盘元数据出错：{str(error)}"
                )
        return metadata

    def dashboard(
        self,
        plugin_id: str,
        key: str,
        user_agent: Optional[str] = None,
    ) -> Optional[PluginDashboard]:
        """调用插件仪表板钩子并返回稳定投影，不依赖 HTTP 异常。"""
        plugin = self._running_plugins.get(plugin_id)
        if not plugin:
            raise PluginNotFoundError(f"插件 {plugin_id} 不存在或未加载")
        try:
            render_mode, _ = plugin.get_render_mode()
            method = plugin.get_dashboard
            count = len(inspect.signature(method).parameters)
            if count > 1:
                dashboard = method(key=key, user_agent=user_agent)
            elif count > 0:
                dashboard = method(user_agent=user_agent)
            else:
                dashboard = method()
        except Exception as error:  # noqa: BLE001
            self._logger.error(f"插件 {plugin_id} 调用方法 get_dashboard 出错: {error}")
            raise PluginDashboardError(
                f"插件 {plugin_id} 调用方法 get_dashboard 出错: {error}"
            ) from error
        if dashboard is None:
            return None
        if not isinstance(dashboard, (tuple, list)) or len(dashboard) != 3:
            self._logger.error(f"插件 {plugin_id} 返回的仪表盘数据格式错误")
            raise PluginDashboardError(
                f"插件 {plugin_id} 返回的仪表盘数据格式错误"
            )
        cols, attrs, elements = dashboard
        return PluginDashboard(
            id=plugin_id,
            name=plugin.plugin_name,
            key=key,
            render_mode=render_mode,
            cols=cols or {},
            attrs=attrs or {},
            elements=elements,
            source_plugin_id=getattr(plugin, "plugin_source_id", None),
            is_instance=bool(getattr(plugin, "plugin_source_id", None)),
            instance_mode=(
                "virtual" if getattr(plugin, "plugin_source_id", None) else None
            ),
        )
