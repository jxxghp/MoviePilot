"""
插件公开能力投影。
"""

import hashlib
import inspect
import posixpath
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import urlencode

from app.runtime.extensions.plugin.contracts import (
    PluginDashboardError,
    PluginNotFoundError,
    supports_plugin_hook,
)
from app.runtime.log import logger as default_logger
from app.runtime.log import wrap_for_plugin_instance
from app.schemas.plugin import PluginDashboard

# Agent 助手形象 key 规则：插件内唯一，前端以 "<plugin_id>:<key>" 组合成全局选择值
_AGENT_PET_KEY_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")
_AGENT_PET_MODES = frozenset({"stage", "renderer"})
_AGENT_PET_BUBBLES = frozenset({"host", "self"})
# 预览图与头像允许原样透传的绝对地址前缀，其余一律视为相对联邦产物目录的路径
_AGENT_PET_ABSOLUTE_PREVIEW_PREFIXES = ("http://", "https://", "data:")
# 桌面小组件标识与形态枚举白名单
_WIDGET_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_WIDGET_KINDS = frozenset({"metrics", "list", "poster", "nowPlaying", "custom"})


class _InvalidAgentPet(ValueError):
    """
    Agent 助手形象声明字段不符合宿主契约。
    """


class PluginProjection:
    """
    把运行态插件投影为宿主命令、API、服务、模块和动作清单。
    """

    def __init__(
        self,
        running_plugins: Mapping[str, Any],
        log: Any = default_logger,
        remote_entry_factory: Optional[Callable[[str, str], str]] = None,
        plugins_root: Optional[Path] = None,
    ) -> None:
        """
        保存运行态插件映射和错误日志端口。
        """
        self._running_plugins = running_plugins
        self._logger = log
        self._remote_entry_factory = remote_entry_factory
        self._plugins_root = plugins_root.resolve() if plugins_root is not None else None

    def _items(self, pid: Optional[str]) -> list[tuple[str, Any]]:
        """
        返回指定插件或运行态插件的稳定快照。
        """
        snapshot = dict(self._running_plugins)
        if pid:
            plugin = snapshot.get(pid)
            return [(pid, plugin)] if plugin is not None else []
        return list(snapshot.items())

    def commands(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        聚合插件命令并补充插件 ID。
        """
        commands: list[dict[str, Any]] = []
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
        """
        聚合插件 API 并补充宿主路径和默认认证方式，端点绑定发起实例的日志上下文。
        """
        apis: list[dict[str, Any]] = []
        for plugin_id, plugin in self._items(pid):
            if not supports_plugin_hook(plugin, "get_api"):
                continue
            try:
                for source_api in plugin.get_api() or []:
                    api = dict(source_api)
                    api["path"] = f"/{plugin_id}{api['path']}"
                    if not api.get("auth"):
                        api["auth"] = "apikey"
                    endpoint = api.get("endpoint")
                    if callable(endpoint):
                        api["endpoint"] = wrap_for_plugin_instance(endpoint, plugin_id)
                    apis.append(api)
            except Exception as error:
                self._logger.error(f"获取插件 {plugin_id} API出错：{str(error)}")
        return apis

    def services(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        聚合启用插件的定时服务。
        """
        services: list[dict[str, Any]] = []
        for plugin_id, plugin in self._items(pid):
            if not supports_plugin_hook(plugin, "get_service"):
                continue
            try:
                if plugin.get_state():
                    services.extend(plugin.get_service() or [])
            except Exception as error:
                self._logger.error(f"获取插件 {plugin_id} 服务出错：{str(error)}")
        return services

    def modules(self, pid: Optional[str] = None) -> Dict[tuple[str, str], Dict[str, Any]]:
        """
        聚合启用插件的模块方法清单。
        """
        modules: dict[tuple[str, str], dict[str, Any]] = {}
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
                    modules[(plugin_id, plugin.get_name())] = dict(declared)
            except Exception as error:
                self._logger.error(f"获取插件 {plugin_id} 模块出错：{str(error)}")
        return modules

    def media_sources(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        聚合启用插件声明的媒体数据源。
        """
        sources: list[dict[str, Any]] = []
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
        """
        把旧字典或新 SDK 模型转换为隔离的 JSON 字典。
        """
        if isinstance(source, Mapping):
            return dict(source)
        model_dump = getattr(source, "model_dump", None)
        if not callable(model_dump):
            return None
        payload = model_dump(mode="json")
        return dict(payload) if isinstance(payload, Mapping) else None

    def actions(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        聚合启用插件的工作流动作。
        """
        actions: list[dict[str, Any]] = []
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
        """
        投影插件联邦远程入口，并保持旧渲染模式筛选语义。
        """
        remotes = []
        for plugin_id, plugin in self._items(pid):
            if not supports_plugin_hook(plugin, "get_render_mode"):
                continue
            render_mode, dist_path = plugin.get_render_mode()
            if render_mode != "vue":
                continue
            remotes.append(self._remote_descriptor(plugin_id, plugin, dist_path))
        return remotes

    def _remote_descriptor(
        self,
        plugin_id: str,
        plugin: Any,
        dist_path: str,
    ) -> Dict[str, Any]:
        """
        构造联邦远程入口描述，分身额外带出其源插件 ID。

        分身与本体共享同一份前端产物，只有源插件名下才有产物目录；前端联邦加载器
        拿不到源插件 ID 就只能按分身 ID 去取，必然落空。入口 URL 同时带上插件版本，
        让插件更新后得到新的浏览器与 Service Worker 缓存键。
        """
        if not self._remote_entry_factory:
            raise RuntimeError("插件联邦入口生成器尚未配置")
        remote_url = self._versioned_url(
            self._remote_entry_factory(plugin_id, dist_path), plugin
        )
        remote: Dict[str, Any] = {
            "id": plugin_id,
            "url": remote_url,
            "name": plugin.plugin_name,
        }
        source_plugin_id = getattr(plugin, "plugin_source_id", None)
        if source_plugin_id:
            remote["source_plugin_id"] = source_plugin_id
        plugin_version = getattr(plugin, "plugin_version", None)
        if plugin_version:
            remote["version"] = str(plugin_version)
        bundle_hash = self._remote_bundle_hash(
            source_plugin_id or plugin_id,
            dist_path,
        )
        if bundle_hash:
            remote["bundle_hash"] = bundle_hash
        return remote

    def _remote_bundle_hash(self, source_plugin_id: str, dist_path: str) -> Optional[str]:
        """
        计算插件联邦入口 remoteEntry.js 的短内容摘要，供移动端无协商磁盘缓存失效使用。
        """
        if self._plugins_root is None or not isinstance(dist_path, str) or not dist_path.strip():
            return None
        relative_dist = Path(dist_path.strip("/"))
        if relative_dist.is_absolute() or ".." in relative_dist.parts or "\\" in dist_path:
            return None
        try:
            plugin_dir = (self._plugins_root / source_plugin_id.lower()).resolve()
            if not plugin_dir.is_relative_to(self._plugins_root):
                return None
            entry_file = (plugin_dir / relative_dist / "remoteEntry.js").resolve()
            if not entry_file.is_relative_to(plugin_dir) or not entry_file.is_file():
                return None
            return hashlib.sha256(entry_file.read_bytes()).hexdigest()[:12]
        except OSError:
            return None

    @staticmethod
    def _versioned_url(url: str, plugin: Any) -> str:
        """
        为插件静态资源 URL 附加插件版本，插件更新后得到新的缓存键。
        """
        plugin_version = getattr(plugin, "plugin_version", None)
        if not plugin_version:
            return url
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{urlencode({'v': str(plugin_version)})}"

    def auth_providers(self) -> List[Dict[str, Any]]:
        """
        投影启用插件声明的登录认证提供方。
        """
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
                    remote = self._remote_descriptor(plugin_id, plugin, dist_path)
                    provider.setdefault("component", "AuthPage")
                    provider["remote"] = remote
                providers.append(provider)
        return providers

    def sidebar(self) -> List[Dict[str, Any]]:
        """
        投影启用 Vue 插件的侧栏导航，并规整权限、分区和顺序。
        """
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

    def agent_pets(self) -> List[Dict[str, Any]]:
        """
        投影启用 Vue 插件声明的 Agent 助手形象。

        只收已启用、渲染模式为 vue 且实现 ``get_agent_pets`` 的插件；任一字段非法的
        声明整项丢弃并记录警告，同一插件内重复 key 只保留首项。分身沿用实例
        ``plugin_id``，``source_plugin_id`` 指向共享前端产物的源插件。
        """
        items: List[Dict[str, Any]] = []
        for plugin_id, plugin in sorted(self._items(None), key=lambda pair: pair[0]):
            if not plugin.get_state() or not supports_plugin_hook(
                    plugin, "get_agent_pets"
            ):
                continue
            if not supports_plugin_hook(plugin, "get_render_mode"):
                continue
            render_mode, dist_path = plugin.get_render_mode()
            if render_mode != "vue":
                continue
            try:
                declared = plugin.get_agent_pets() or []
            except Exception as error:
                self._logger.error(
                    f"获取插件[{plugin_id}]助手形象出错：{str(error)}"
                )
                continue
            items.extend(
                self._agent_pet_items(plugin_id, plugin, dist_path or "", declared)
            )
        return items

    def _agent_pet_items(
        self,
        plugin_id: str,
        plugin: Any,
        dist_path: str,
        declared: Any,
    ) -> List[Dict[str, Any]]:
        """
        规整单个插件的助手形象声明，丢弃非法项与重复 key。
        """
        if not isinstance(declared, list):
            self._logger.warning(
                f"插件[{plugin_id}]get_agent_pets() 必须返回列表，已忽略"
            )
            return []
        # 带出联邦入口与插件版本：前端据此直接注册远程入口、省去一次 remotes 往返，
        # 并按版本判断插件是否升级，以决定是否重试本会话加载失败的形象。
        plugin_version = getattr(plugin, "plugin_version", None)
        base = {
            "plugin_id": plugin_id,
            "source_plugin_id": getattr(plugin, "plugin_source_id", None) or plugin_id,
            "plugin_name": plugin.plugin_name,
            "plugin_version": str(plugin_version) if plugin_version else None,
            "remote_url": self._remote_descriptor(plugin_id, plugin, dist_path)["url"],
        }
        seen: set[str] = set()
        items: List[Dict[str, Any]] = []
        for raw in declared:
            try:
                item = self._agent_pet_fields(raw)
            except _InvalidAgentPet as error:
                self._logger.warning(f"插件[{plugin_id}]助手形象声明无效，已跳过：{error}")
                continue
            if item["key"] in seen:
                self._logger.warning(
                    f"插件[{plugin_id}]助手形象 key 重复，已跳过：{item['key']!r}"
                )
                continue
            seen.add(item["key"])
            for field in ("preview", "avatar"):
                image = item.pop(field)
                item[f"{field}_url"] = (
                    self._agent_pet_image_url(plugin_id, plugin, dist_path, image)
                    if image else None
                )
            items.append({**base, **item})
        return items

    @staticmethod
    def _agent_pet_fields(raw: Any) -> Dict[str, Any]:
        """
        校验并补齐单项助手形象声明的默认值，非法时抛出 ``_InvalidAgentPet``。
        """
        if not isinstance(raw, Mapping):
            raise _InvalidAgentPet(f"声明必须是字典：{raw!r}")
        key = raw.get("key")
        if not isinstance(key, str) or not _AGENT_PET_KEY_PATTERN.match(key):
            raise _InvalidAgentPet(f"key 不符合 [a-z0-9_-]{{1,32}}：{key!r}")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise _InvalidAgentPet(f"{key} 缺少 name")
        description = raw.get("description")
        if description is not None and not isinstance(description, str):
            raise _InvalidAgentPet(f"{key} 的 description 必须是字符串")
        mode = raw.get("mode") or "renderer"
        if not isinstance(mode, str) or mode not in _AGENT_PET_MODES:
            raise _InvalidAgentPet(f"{key} 的 mode 未知：{mode!r}")
        component = raw.get("component") or "AgentPet"
        if not isinstance(component, str) or not component.strip().removeprefix("./"):
            raise _InvalidAgentPet(f"{key} 的 component 无效：{component!r}")
        api_version = raw.get("api_version", 1)
        if isinstance(api_version, bool) or not isinstance(api_version, int) or api_version < 1:
            raise _InvalidAgentPet(f"{key} 的 api_version 必须是正整数：{api_version!r}")
        preview = PluginProjection._agent_pet_image(key, "preview", raw.get("preview"))
        avatar = PluginProjection._agent_pet_image(key, "avatar", raw.get("avatar"))
        bubbles = None
        random_actions = None
        if mode == "stage":
            bubbles = raw.get("bubbles") or "host"
            if not isinstance(bubbles, str) or bubbles not in _AGENT_PET_BUBBLES:
                raise _InvalidAgentPet(f"{key} 的 bubbles 未知：{bubbles!r}")
        else:
            random_actions = PluginProjection._agent_pet_random_actions(
                key, raw.get("random_actions")
            )
        return {
            "key": key,
            "name": name.strip(),
            "description": description.strip() if description else None,
            "mode": mode,
            "component": component.strip().removeprefix("./"),
            "api_version": api_version,
            "preview": preview,
            "avatar": avatar,
            "bubbles": bubbles,
            "random_actions": random_actions,
        }

    @staticmethod
    def _agent_pet_random_actions(key: str, value: Any) -> Optional[List[str]]:
        """
        校验 renderer 模式的随机动作候选，保持声明顺序去重；缺省表示宿主全集。
        """
        if value is None:
            return None
        if not isinstance(value, list) or not all(
                isinstance(action, str) and action for action in value
        ):
            raise _InvalidAgentPet(f"{key} 的 random_actions 必须是非空字符串列表")
        return list(dict.fromkeys(value))

    @staticmethod
    def _agent_pet_image(key: str, field: str, value: Any) -> Optional[str]:
        """
        校验预览图或头像声明：绝对地址仅允许 http(s) 与 data，相对路径不得越出联邦产物目录。
        """
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise _InvalidAgentPet(f"{key} 的 {field} 必须是字符串")
        preview = value.strip()
        if preview.lower().startswith(_AGENT_PET_ABSOLUTE_PREVIEW_PREFIXES):
            return preview
        relative = preview.removeprefix("./")
        if (
                ":" in relative
                or "\\" in relative
                or any(segment in ("", ".", "..") for segment in relative.split("/"))
        ):
            raise _InvalidAgentPet(f"{key} 的 {field} 路径无效：{value!r}")
        return relative

    def _agent_pet_image_url(
        self,
        plugin_id: str,
        plugin: Any,
        dist_path: str,
        preview: str,
    ) -> str:
        """
        把已校验的预览图或头像解析为可访问 URL。

        绝对地址原样透传；相对路径解析到 remoteEntry 所在目录，与 remoteEntry 共用
        插件静态文件接口、实例 ID 与版本缓存键，分身由静态文件接口映射回源插件目录。
        """
        if preview.lower().startswith(_AGENT_PET_ABSOLUTE_PREVIEW_PREFIXES):
            return preview
        if not self._remote_entry_factory:
            raise RuntimeError("插件联邦入口生成器尚未配置")
        entry_dir = posixpath.dirname(self._remote_entry_factory(plugin_id, dist_path))
        return self._versioned_url(f"{entry_dir}/{preview}", plugin)

    def dashboard_metadata(self) -> List[Dict[str, str]]:
        """
        投影启用插件的单仪表板或多仪表板元信息。
        """
        metadata: list[dict[str, str]] = []
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
        """
        调用插件仪表板钩子并返回稳定投影，不依赖 HTTP 异常。
        """
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
        # 第三方仪表盘钩子失败只影响当前面板，不能中断其他插件展示。
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

    def widget_metadata(self) -> List[Dict[str, Any]]:
        """
        投影启用插件声明的桌面小组件目录，并为仅实现仪表板的插件提供自动降级入口。

        :return: 已启用插件的小组件元信息列表
        """
        items: list[dict[str, Any]] = []
        declared_pairs: set[tuple[str, str]] = set()
        for plugin_id, plugin in sorted(self._items(None), key=lambda pair: pair[0]):
            if not plugin.get_state():
                continue
            has_page = bool(supports_plugin_hook(plugin, "get_page"))
            plugin_name = getattr(plugin, "plugin_name", None) or plugin_id
            if supports_plugin_hook(plugin, "get_widgets"):
                try:
                    raw_list = plugin.get_widgets() or []
                except Exception as error:
                    self._logger.error(f"获取插件[{plugin_id}]桌面小组件元信息出错：{error}")
                    raw_list = []
                if isinstance(raw_list, list):
                    for raw in raw_list:
                        if not isinstance(raw, Mapping):
                            continue
                        raw_key = str(raw.get("key") or "").strip()
                        if not _WIDGET_KEY_PATTERN.match(raw_key):
                            self._logger.warning(
                                f"插件[{plugin_id}]桌面小组件 key 无效，已跳过：{raw_key!r}"
                            )
                            continue
                        if (plugin_id, raw_key) in declared_pairs:
                            continue
                        name = str(raw.get("name") or "").strip() or plugin_name
                        kind = str(raw.get("kind") or "metrics").strip()
                        if kind not in _WIDGET_KINDS:
                            kind = "metrics"
                        description = raw.get("description")
                        desc_str = description.strip() if isinstance(description, str) and description.strip() else None
                        declared_pairs.add((plugin_id, raw_key))
                        items.append({
                            "plugin_id": plugin_id,
                            "plugin_name": plugin_name,
                            "key": raw_key,
                            "name": name,
                            "description": desc_str,
                            "kind": kind,
                            "source": "widget",
                            "has_page": has_page,
                        })
            if supports_plugin_hook(plugin, "get_dashboard"):
                try:
                    dash_metas = (
                        plugin.get_dashboard_meta()
                        if supports_plugin_hook(plugin, "get_dashboard_meta")
                        else None
                    )
                except Exception as error:
                    self._logger.error(f"获取插件[{plugin_id}]仪表盘兜底小组件元信息出错：{error}")
                    continue
                entries = dash_metas if dash_metas else [{"key": "", "name": plugin_name}]
                for entry in entries:
                    if not isinstance(entry, Mapping):
                        continue
                    dash_key = str(entry.get("key") or "").strip()
                    if dash_key and not _WIDGET_KEY_PATTERN.match(dash_key):
                        continue
                    if (plugin_id, dash_key) in declared_pairs:
                        continue
                    declared_pairs.add((plugin_id, dash_key))
                    items.append({
                        "plugin_id": plugin_id,
                        "plugin_name": plugin_name,
                        "key": dash_key,
                        "name": str(entry.get("name") or "").strip() or plugin_name,
                        "description": None,
                        "kind": "metrics",
                        "source": "dashboard",
                        "has_page": has_page,
                    })
        return items

    def widget_data(self, plugin_id: str, key: str = "") -> Optional[Dict[str, Any]]:
        """
        读取插件专属桌面小组件快照；未实现专属方法时自动投影该插件的仪表板结构。

        :param plugin_id: 运行态插件标识
        :param key: 小组件或仪表板子键
        :return: 原始小组件字典；插件当前无小组件内容时返回 None
        :raises PluginNotFoundError: 插件未加载或未启用
        :raises PluginDashboardError: 插件方法执行失败或返回非法结构
        """
        plugin = self._running_plugins.get(plugin_id)
        if not plugin or not plugin.get_state():
            raise PluginNotFoundError(f"插件 {plugin_id} 不存在或未启用")
        plugin_name = getattr(plugin, "plugin_name", None) or plugin_id
        if supports_plugin_hook(plugin, "get_widget_data"):
            try:
                method = plugin.get_widget_data
                param_count = len(inspect.signature(method).parameters)
                raw = method(key) if param_count > 0 else method()
            except Exception as error:  # noqa: BLE001
                self._logger.error(f"插件 {plugin_id} 调用方法 get_widget_data 出错: {error}")
                raise PluginDashboardError(
                    f"插件 {plugin_id} 调用方法 get_widget_data 出错"
                ) from error
            if raw is None:
                return None
            if not isinstance(raw, Mapping):
                raise PluginDashboardError(f"插件 {plugin_id} 返回的小组件数据格式错误")
            payload = dict(raw)
            payload.setdefault("plugin_id", plugin_id)
            payload.setdefault("plugin_name", plugin_name)
            payload.setdefault("widget_key", key)
            payload.setdefault("title", plugin_name)
            return payload
        if supports_plugin_hook(plugin, "get_dashboard"):
            dash = self.dashboard(plugin_id, key)
            if dash is None:
                return None
            return self._dashboard_to_widget_payload(plugin_id, plugin_name, key, dash)
        return None

    @classmethod
    def _dashboard_to_widget_payload(
        cls,
        plugin_id: str,
        plugin_name: str,
        key: str,
        dash: PluginDashboard,
    ) -> Dict[str, Any]:
        """
        把存量插件的 Vuetify 仪表板元素树提取为桌面小组件可消费的指标与摘要列表。
        """
        attrs = dash.attrs if isinstance(dash.attrs, Mapping) else {}
        title = str(attrs.get("title") or dash.name or plugin_name).strip() or plugin_name
        subtitle = str(attrs.get("subtitle") or "").strip() or None
        if dash.render_mode == "vue":
            return {
                "plugin_id": plugin_id,
                "plugin_name": plugin_name,
                "widget_key": key,
                "kind": "poster",
                "title": title,
                "subtitle": subtitle or "交互式联邦仪表盘 · 点击在 App 中打开",
                "metrics": [],
                "items": [],
            }
        metrics: list[dict[str, Any]] = []
        lines: list[str] = []
        cls._extract_vuetify_snippets(dash.elements, metrics, lines, depth=0)
        dedup_lines = list(dict.fromkeys(line for line in lines if line))[:6]
        if metrics:
            return {
                "plugin_id": plugin_id,
                "plugin_name": plugin_name,
                "widget_key": key,
                "kind": "metrics",
                "title": title,
                "subtitle": subtitle,
                "metrics": metrics[:8],
                "items": [
                    {"id": f"item-{idx}", "title": text}
                    for idx, text in enumerate(dedup_lines[:4])
                ],
            }
        return {
            "plugin_id": plugin_id,
            "plugin_name": plugin_name,
            "widget_key": key,
            "kind": "list",
            "title": title,
            "subtitle": subtitle,
            "metrics": [],
            "items": [
                {"id": f"item-{idx}", "title": text}
                for idx, text in enumerate(dedup_lines[:6])
            ],
        }

    @classmethod
    def _extract_vuetify_snippets(
        cls,
        node: Any,
        metrics: list[dict[str, Any]],
        lines: list[str],
        depth: int,
    ) -> None:
        """
        有界递归扫描仪表板 Vuetify JSON 树，识别成对的「指标名/数值」与可读文本行。
        """
        if depth > 14 or (len(metrics) >= 8 and len(lines) >= 12):
            return
        if isinstance(node, str):
            cleaned = " ".join(node.split()).strip()
            if (
                cleaned
                and cleaned != "null"
                and not cleaned.startswith(("mdi-", "ri-", "#", "{", "."))
                and len(cleaned) <= 80
            ):
                lines.append(cleaned)
            return
        if isinstance(node, list):
            for child in node:
                cls._extract_vuetify_snippets(child, metrics, lines, depth + 1)
            return
        if not isinstance(node, Mapping):
            return
        comp = str(node.get("component") or "")
        if comp.lower() in {"style", "script", "vicon"}:
            return
        props = node.get("props")
        if isinstance(props, Mapping):
            prop_title = str(props.get("title") or props.get("label") or "").strip()
            prop_value = str(props.get("value") or props.get("subtitle") or "").strip()
            if (
                prop_title
                and prop_value
                and len(prop_title) <= 24
                and len(prop_value) <= 32
                and not prop_title.startswith("mdi-")
                and not prop_value.startswith("mdi-")
                and len(metrics) < 8
            ):
                metrics.append({
                    "id": f"m-{len(metrics)}",
                    "label": prop_title,
                    "value": prop_value,
                })
            else:
                for field in ("title", "subtitle", "label", "text", "value"):
                    val = props.get(field)
                    if isinstance(val, str):
                        cls._extract_vuetify_snippets(val, metrics, lines, depth + 1)
        direct_text = node.get("text")
        if isinstance(direct_text, str):
            cls._extract_vuetify_snippets(direct_text, metrics, lines, depth + 1)
        content = node.get("content")
        if isinstance(content, list):
            local_lines: list[str] = []
            local_metrics: list[dict[str, Any]] = []
            for child in content:
                cls._extract_vuetify_snippets(child, local_metrics, local_lines, depth + 1)
            for item in local_metrics:
                if len(metrics) < 8:
                    metrics.append(item)
            if (
                len(local_lines) == 2
                and len(local_lines[0]) <= 20
                and len(local_lines[1]) <= 28
                and len(metrics) < 8
            ):
                metrics.append({
                    "id": f"m-{len(metrics)}",
                    "label": local_lines[0],
                    "value": local_lines[1],
                })
            else:
                lines.extend(local_lines)
