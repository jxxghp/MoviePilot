"""插件公开能力投影。"""

from typing import Any, Callable, Dict, List, Mapping, Optional

from app.runtime.extensions.contract import (
    ExtensionDistribution,
    ExtensionFaultScope,
    ExtensionProvider,
    supports_extension_hook,
)
from app.runtime.log import logger as default_logger
from app.schemas.notification import ChannelCapabilities, channel_identity


class PluginExtension:
    """把运行态插件实例投影为扩展视图。"""

    distribution = ExtensionDistribution.MARKET
    fault_scope = ExtensionFaultScope.PLUGIN

    def __init__(self, instance: Any, extension_id: Optional[str] = None) -> None:
        """保存被投影的插件实例及其注册标识。

        :param instance: 运行态插件实例
        :param extension_id: 插件注册标识，缺省时取插件类名
        """
        self.instance = instance
        self._extension_id = extension_id or instance.__class__.__name__

    @property
    def extension_id(self) -> str:
        """返回插件在宿主内的稳定标识。"""
        return self._extension_id

    @property
    def display_name(self) -> str:
        """返回插件展示名。"""
        return self.instance.get_name()

    @property
    def priority(self) -> int:
        """返回插件声明的加载顺序。"""
        return getattr(self.instance, "plugin_order", 0) or 0

    def is_enabled(self) -> bool:
        """返回插件自身声明的启用状态。"""
        return bool(self.instance.get_state())

    def initialize(self, config: Optional[dict] = None) -> None:
        """按给定配置生效插件设置并建立插件自有资源。

        :param config: 插件配置字典
        :return: 无返回值
        """
        self.instance.init_plugin(config)

    def terminate(self) -> None:
        """释放插件持有的数据库连接与后台服务。"""
        if hasattr(self.instance, "close"):
            self.instance.close()
        if hasattr(self.instance, "stop_service"):
            self.instance.stop_service()

    def self_test(self) -> Optional[tuple]:
        """执行插件声明的连通性自检。

        :return: `(是否可连通, 失败原因)`；插件未声明自检钩子或自检返回值不合契约时
            为 ``None``；自检抛出异常时返回 `(False, 异常信息)`
        """
        if not self.supports_hook("test"):
            return None
        try:
            result = self.instance.test()
        except Exception as error:
            default_logger.error(
                f"插件[{self._extension_id}]自检出错：{str(error)}"
            )
            return False, str(error)
        if result is None:
            return None
        if (
            isinstance(result, tuple)
            and len(result) == 2
            and isinstance(result[0], bool)
            and isinstance(result[1], str)
        ):
            return result
        default_logger.warning(
            f"插件[{self._extension_id}]自检返回值不合契约，已忽略：{result!r}"
        )
        return None

    def supports_hook(self, name: str) -> bool:
        """判断插件是否实现了指定扩展点。

        :param name: 扩展点名称
        :return: 该扩展点已实现时为 True
        """
        return supports_extension_hook(self.instance, name)

    def capability_table(self) -> Dict[str, Callable[..., Any]]:
        """返回插件声明的可分发方法表。

        :return: 方法名到插件方法的映射；插件未启用或未声明时为空字典
        """
        if not self.supports_hook("get_module") or not self.is_enabled():
            return {}
        table = self.instance.get_module()
        return table if isinstance(table, Mapping) else {}

    def capability_names(self) -> tuple[str, ...]:
        """列出插件可被分发触达的方法名。

        :return: 插件声明的方法名元组
        """
        return tuple(self.capability_table())

    def capability(self, name: str) -> Optional[Callable[..., Any]]:
        """取用插件声明的指定可分发方法。

        :param name: 方法名称
        :return: 插件方法；未声明时为 ``None``
        """
        return self.capability_table().get(name)


class PluginProviderSource:
    """把插件声明的方法表投影为分发提供者。"""

    distribution = ExtensionDistribution.MARKET

    def __init__(self, catalog: Any) -> None:
        """保存插件模块目录端口。

        :param catalog: 提供插件方法表快照的目录
        """
        self._catalog = catalog

    @staticmethod
    def announce_phase(method: str) -> None:
        """插件按提供者逐个记录请求日志，阶段开始不额外记录。

        :param method: 模块方法名称
        :return: 无返回值
        """

    def _providers(self, method: str):
        """遍历插件注入的同名方法。

        :param method: 模块方法名称
        :return: 提供者迭代器
        """
        plugin_modules = self._catalog.get_plugin_modules()
        for (plugin_id, plugin_name), module_dict in plugin_modules.items():
            func = module_dict.get(method)
            if not func:
                continue
            yield ExtensionProvider(
                extension_id=plugin_id,
                display_name=plugin_name,
                distribution=ExtensionDistribution.MARKET,
                fault_scope=ExtensionFaultScope.PLUGIN,
                invoke=func,
                announces_invocation=True,
            )

    def notify_providers(self, method: str):
        """返回应被通知的插件提供者。

        :param method: 模块方法名称
        :return: 提供者迭代器
        """
        return self._providers(method)

    def answer_providers(self, method: str):
        """返回参与仲裁的插件提供者。

        :param method: 模块方法名称
        :return: 提供者迭代器
        """
        return self._providers(method)


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

    def _extensions(self, pid: Optional[str]) -> list[PluginExtension]:
        """返回指定插件或全部运行态插件的扩展视图快照。

        :param pid: 插件 ID，为空时返回全部运行态插件
        :return: 插件扩展视图列表
        """
        snapshot = dict(self._running_plugins)
        if pid:
            plugin = snapshot.get(pid)
            return [PluginExtension(plugin, pid)] if plugin is not None else []
        return [
            PluginExtension(plugin, plugin_id)
            for plugin_id, plugin in snapshot.items()
        ]

    def commands(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """聚合插件命令并补充插件 ID。"""
        commands: list[dict] = []
        for extension in self._extensions(pid):
            plugin_id, plugin = extension.extension_id, extension.instance
            if not extension.supports_hook("get_command"):
                continue
            try:
                if not extension.is_enabled():
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
        for extension in self._extensions(pid):
            plugin_id, plugin = extension.extension_id, extension.instance
            if not extension.supports_hook("get_api"):
                continue
            try:
                for api in plugin.get_api() or []:
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
        for extension in self._extensions(pid):
            plugin_id, plugin = extension.extension_id, extension.instance
            if not extension.supports_hook("get_service"):
                continue
            try:
                if extension.is_enabled():
                    services.extend(plugin.get_service() or [])
            except Exception as error:
                self._logger.error(f"获取插件 {plugin_id} 服务出错：{str(error)}")
        return services

    def modules(self, pid: Optional[str] = None) -> Dict[tuple, Dict[str, Any]]:
        """聚合启用插件的模块方法清单。"""
        modules: dict[tuple, dict] = {}
        for extension in self._extensions(pid):
            plugin_id, plugin = extension.extension_id, extension.instance
            if not extension.supports_hook("get_module"):
                continue
            try:
                if extension.is_enabled():
                    modules[(plugin_id, extension.display_name)] = (
                        plugin.get_module() or []
                    )
            except Exception as error:
                self._logger.error(f"获取插件 {plugin_id} 模块出错：{str(error)}")
        return modules

    def actions(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """聚合启用插件的工作流动作。"""
        actions: list[dict] = []
        for extension in self._extensions(pid):
            plugin_id, plugin = extension.extension_id, extension.instance
            if not extension.supports_hook("get_actions"):
                continue
            try:
                if not extension.is_enabled():
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
        for extension in self._extensions(pid):
            plugin_id, plugin = extension.extension_id, extension.instance
            if not extension.supports_hook("get_render_mode"):
                continue
            render_mode, dist_path = plugin.get_render_mode()
            if render_mode != "vue":
                continue
            if not self._remote_entry_factory:
                raise RuntimeError("插件联邦入口生成器尚未配置")
            remotes.append({
                "id": plugin_id,
                "url": self._remote_entry_factory(plugin_id, dist_path),
                "name": plugin.plugin_name,
            })
        return remotes

    def auth_providers(self) -> List[Dict[str, Any]]:
        """投影启用插件声明的登录认证提供方。"""
        providers = []
        for extension in self._extensions(None):
            plugin_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled() or not extension.supports_hook(
                    "get_auth_providers"
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
            if extension.supports_hook("get_render_mode"):
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
                    provider["remote"] = {
                        "id": plugin_id,
                        "url": self._remote_entry_factory(plugin_id, dist_path),
                        "name": plugin.plugin_name,
                    }
                providers.append(provider)
        return providers

    def sidebar(self) -> List[Dict[str, Any]]:
        """投影启用 Vue 插件的侧栏导航，并规整权限、分区和顺序。"""
        valid_sections = {"start", "discovery", "subscribe", "organize", "system"}
        valid_permissions = {"subscribe", "discovery", "search", "manage", "admin"}
        items = []
        for extension in self._extensions(None):
            plugin_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled() or not extension.supports_hook(
                    "get_sidebar_nav"
            ):
                continue
            if not extension.supports_hook("get_render_mode"):
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

    def channel_capabilities(
        self, pid: Optional[str] = None
    ) -> Dict[str, List[ChannelCapabilities]]:
        """投影启用插件声明的消息渠道能力。

        :param pid: 插件 ID，为空时返回全部运行态插件
        :return: 插件 ID 到其声明的 `ChannelCapabilities` 列表的映射
        """
        result: Dict[str, List[ChannelCapabilities]] = {}
        for extension in self._extensions(pid):
            plugin_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled() or not extension.supports_hook(
                    "get_channel_capabilities"
            ):
                continue
            try:
                declared = plugin.get_channel_capabilities() or []
            except Exception as error:
                self._logger.error(
                    f"获取插件 {plugin_id} 渠道能力出错：{str(error)}"
                )
                continue
            accepted: List[ChannelCapabilities] = []
            for item in declared:
                if not isinstance(item, ChannelCapabilities):
                    self._logger.warning(
                        f"插件[{plugin_id}]声明的渠道能力类型无效，已跳过：{item!r}"
                    )
                    continue
                if not channel_identity(item.channel):
                    continue
                accepted.append(item)
            result[plugin_id] = accepted
        return result

    def dashboard_metadata(self) -> List[Dict[str, str]]:
        """投影启用插件的单仪表板或多仪表板元信息。"""
        metadata = []
        for extension in self._extensions(None):
            plugin_id, plugin = extension.extension_id, extension.instance
            if not extension.supports_hook("get_dashboard"):
                continue
            try:
                if not extension.is_enabled():
                    continue
                if extension.supports_hook("get_dashboard_meta"):
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
