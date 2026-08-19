"""插件公开能力投影。"""

from typing import Any, Callable, Dict, List, Mapping, Optional

from app.runtime.extensions.contract import (
    ExtensionDistribution,
    ExtensionFaultScope,
    ExtensionProvider,
    supports_extension_hook,
)
from app.runtime.extensions.instance import (
    extension_id_of,
    matches_extension,
    split_instance_key,
)
from app.runtime.log import logger as default_logger
from app.runtime.log import wrap_for_plugin_instance
from app.schemas.notification import ChannelCapabilities, channel_identity

# 数据源前缀分发名归一为契约名前，插件若挂载下列旧名，从此不会被任何分发调用触达。
# 键为废弃名，值为应改用的新契约名；async_ 前缀的旧名对应 async_ 前缀的新契约名。
DEPRECATED_PLUGIN_METHOD_NAMES: dict[str, str] = {
    "anilist_credits": "media_credits",
    "anilist_discover": "discover",
    "anilist_info": "media_detail",
    "anilist_person_credits": "person_credits",
    "anilist_person_detail": "person_detail",
    "anilist_popular_this_season": "discover_board",
    "anilist_recommendations": "media_recommend",
    "anilist_trending": "discover_board",
    "async_anilist_credits": "async_media_credits",
    "async_anilist_discover": "async_discover",
    "async_anilist_info": "async_media_detail",
    "async_anilist_person_credits": "async_person_credits",
    "async_anilist_person_detail": "async_person_detail",
    "async_anilist_popular_this_season": "async_discover_board",
    "async_anilist_recommendations": "async_media_recommend",
    "async_anilist_trending": "async_discover_board",
    "async_bangumi_calendar": "async_discover_board",
    "async_bangumi_credits": "async_media_credits",
    "async_bangumi_discover": "async_discover",
    "async_bangumi_info": "async_media_detail",
    "async_bangumi_person_credits": "async_person_credits",
    "async_bangumi_person_detail": "async_person_detail",
    "async_bangumi_recommend": "async_media_recommend",
    "async_douban_discover": "async_discover",
    "async_douban_info": "async_media_detail",
    "async_douban_movie_credits": "async_media_credits",
    "async_douban_movie_recommend": "async_media_recommend",
    "async_douban_person_credits": "async_person_credits",
    "async_douban_person_detail": "async_person_detail",
    "async_douban_tv_credits": "async_media_credits",
    "async_douban_tv_recommend": "async_media_recommend",
    "async_match_doubaninfo": "async_match_media",
    "async_match_tmdbinfo": "async_match_media",
    "async_movie_hot": "async_discover_board",
    "async_movie_showing": "async_discover_board",
    "async_movie_top250": "async_discover_board",
    "async_tmdb_discover": "async_discover",
    "async_tmdb_info": "async_media_detail",
    "async_tmdb_movie_credits": "async_media_credits",
    "async_tmdb_movie_recommend": "async_media_recommend",
    "async_tmdb_movie_similar": "async_media_similar",
    "async_tmdb_person_credits": "async_person_credits",
    "async_tmdb_person_detail": "async_person_detail",
    "async_tmdb_trending": "async_discover_board",
    "async_tmdb_tv_credits": "async_media_credits",
    "async_tmdb_tv_recommend": "async_media_recommend",
    "async_tmdb_tv_similar": "async_media_similar",
    "async_tv_animation": "async_discover_board",
    "async_tv_hot": "async_discover_board",
    "async_tv_weekly_chinese": "async_discover_board",
    "async_tv_weekly_global": "async_discover_board",
    "bangumi_calendar": "discover_board",
    "bangumi_credits": "media_credits",
    "bangumi_discover": "discover",
    "bangumi_info": "media_detail",
    "bangumi_person_credits": "person_credits",
    "bangumi_person_detail": "person_detail",
    "bangumi_recommend": "media_recommend",
    "douban_discover": "discover",
    "douban_info": "media_detail",
    "douban_movie_credits": "media_credits",
    "douban_movie_recommend": "media_recommend",
    "douban_person_credits": "person_credits",
    "douban_person_detail": "person_detail",
    "douban_tv_credits": "media_credits",
    "douban_tv_recommend": "media_recommend",
    "match_doubaninfo": "match_media",
    "match_tmdbinfo": "match_media",
    "movie_hot": "discover_board",
    "movie_showing": "discover_board",
    "movie_top250": "discover_board",
    "tmdb_discover": "discover",
    "tmdb_info": "media_detail",
    "tmdb_movie_credits": "media_credits",
    "tmdb_movie_recommend": "media_recommend",
    "tmdb_movie_similar": "media_similar",
    "tmdb_person_credits": "person_credits",
    "tmdb_person_detail": "person_detail",
    "tmdb_trending": "discover_board",
    "tmdb_tv_credits": "media_credits",
    "tmdb_tv_recommend": "media_recommend",
    "tmdb_tv_similar": "media_similar",
    "tv_animation": "discover_board",
    "tv_hot": "discover_board",
    "tv_weekly_chinese": "discover_board",
    "tv_weekly_global": "discover_board",
    "tvdb_info": "media_detail",
}

# 本轮归一新引入、取代废弃名的多来源契约名，衍生自上表的取值集合而非单独维护。
# 挂载这些方法名的插件实现覆盖全部数据源，须按 source 参数自认领，非本来源
# 必须返回 None 而非空列表，否则会拦截该契约下的全部来源。
_NEW_MULTI_SOURCE_CONTRACT_NAMES = frozenset(DEPRECATED_PLUGIN_METHOD_NAMES.values())

# 已就废弃分发名/新多来源契约名告警过的 (插件ID, 方法名) 组合，避免 modules() 被
# 高频调用（每次分发都会取用插件模块表）时同一提示反复刷屏。跨 PluginProjection
# 实例共享：该类每次调用都会重新构造，去重状态不能挂在实例上。
_deprecated_method_warnings_seen: set[tuple[str, str]] = set()
_new_contract_hints_seen: set[tuple[str, str]] = set()

# 已就「同一插件多个实例挂载同一契约名」告警过的 (插件ID, 方法名) 组合，去重理由同上。
_sibling_contract_warnings_seen: set[tuple[str, str]] = set()


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
        for (extension_id, plugin_name), module_dict in plugin_modules.items():
            func = module_dict.get(method)
            if not func:
                continue
            yield ExtensionProvider(
                extension_id=extension_id,
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
        remote_entry_factory: Optional[Callable[[str, str, Optional[str]], str]] = None,
    ) -> None:
        """保存运行态插件映射和错误日志端口。"""
        self._running_plugins = running_plugins
        self._logger = log
        self._remote_entry_factory = remote_entry_factory

    def _extensions(self, pid: Optional[str]) -> list[PluginExtension]:
        """返回指定筛选条件命中的运行态插件实例的扩展视图快照。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 插件扩展视图列表
        """
        return [
            PluginExtension(plugin, key)
            for key, plugin in dict(self._running_plugins).items()
            if matches_extension(key, pid)
        ]

    def commands(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """聚合插件命令并补充插件 ID。"""
        commands: list[dict] = []
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.supports_hook("get_command"):
                continue
            try:
                if not extension.is_enabled():
                    continue
                for command in plugin.get_command() or []:
                    command["pid"] = extension_id
                    commands.append(command)
            except Exception as error:
                self._logger.error(f"获取插件命令出错：{str(error)}")
        return commands

    def apis(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """聚合插件 API 并补充宿主路径、默认认证方式，并把路由处理函数绑定到声明它的实例。

        HTTP 路由的 endpoint 由 FastAPI 在注册时捕获、请求到达时才被调用，宿主既不
        介入这次调用也无法在调用现场获知它归属哪个实例；本方法是路由注册前的唯一
        必经点，在这里按实例包一层，使请求执行期间的插件日志归入声明该路由的实例，
        而不是退化成按栈回溯只能定位到插件、定位不到具体实例，落入插件兜底目录。
        """
        apis: list[dict] = []
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.supports_hook("get_api"):
                continue
            try:
                plugin_id, instance_id = split_instance_key(extension_id)
                for api in plugin.get_api() or []:
                    api["path"] = f"/{extension_id}{api['path']}"
                    if not api.get("auth"):
                        api["auth"] = "apikey"
                    endpoint = api.get("endpoint")
                    if callable(endpoint):
                        api["endpoint"] = wrap_for_plugin_instance(
                            endpoint, plugin_id, instance_id
                        )
                    apis.append(api)
            except Exception as error:
                self._logger.error(f"获取插件 {extension_id} API出错：{str(error)}")
        return apis

    def services(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """聚合启用插件的定时服务并标注归属实例键。

        同一插件的多个实例可能声明相同的服务 id，调用方须按 `pid` 字段区分
        归属实例才能构造不冲突的定时任务标识。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 服务声明列表，每项的 `pid` 字段被改写为声明来源的实例键
        """
        services: list[dict] = []
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.supports_hook("get_service"):
                continue
            try:
                if extension.is_enabled():
                    for service in plugin.get_service() or []:
                        service["pid"] = extension_id
                        services.append(service)
            except Exception as error:
                self._logger.error(f"获取插件 {extension_id} 服务出错：{str(error)}")
        return services

    def modules(self, pid: Optional[str] = None) -> Dict[tuple, Dict[str, Any]]:
        """聚合启用插件的模块方法清单。

        键取 `(实例键, 展示名)`：同一插件的多个实例展示名相同，只有实例键能把它们
        区分开，否则后登记的实例会覆盖先登记的，被覆盖的那一份实现从此不再参与分发。
        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: `(实例键, 展示名)` 到该实例方法表的映射
        """
        modules: dict[tuple, dict] = {}
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.supports_hook("get_module"):
                continue
            try:
                if extension.is_enabled():
                    table = plugin.get_module() or []
                    modules[(extension_id, extension.display_name)] = table
                    self._warn_dispatch_migration(extension_id, table)
            except Exception as error:
                self._logger.error(f"获取插件 {extension_id} 模块出错：{str(error)}")
        self._warn_sibling_contract_overlap(modules)
        return modules

    def _warn_sibling_contract_overlap(self, modules: Mapping[tuple, Any]) -> None:
        """就同一插件的多个实例挂载同一契约名各打一次提示，不改写方法表。

        多实例同时挂同一契约名是合法配置：广播逐个触达、多播收齐每个实例的答案，
        但单播只取首个非空答案，其余实例的实现不会被调用。

        :param modules: `(实例键, 展示名)` 到方法表的映射
        :return: 无返回值
        """
        claimants: dict[tuple[str, str], list[str]] = {}
        for (extension_id, _display_name), table in modules.items():
            if not isinstance(table, Mapping):
                continue
            for method in table:
                claimants.setdefault(
                    (extension_id_of(extension_id), method), []
                ).append(extension_id)
        for (plugin_id, method), extension_ids in claimants.items():
            if len(extension_ids) < 2:
                continue
            key = (plugin_id, method)
            if key in _sibling_contract_warnings_seen:
                continue
            _sibling_contract_warnings_seen.add(key)
            self._logger.warning(
                f"插件[{plugin_id}]有 {len(extension_ids)} 个实例挂载模块方法 "
                f"{method!r}：{extension_ids}；广播与多播会逐个触达，单播按实例登记"
                f"顺序取首个非空答案，其余实例不会被调用。若只应由一个实例应答，"
                f"请停用其余实例，或让它们对不负责的请求返回 None 让出"
            )

    def _warn_dispatch_migration(self, extension_id: str, table: Any) -> None:
        """就插件挂载的废弃分发名和新多来源契约名各打一次提示，不改写方法表。

        :param extension_id: 插件 ID
        :param table: 插件 `get_module()` 声明的方法表
        :return: 无返回值
        """
        if not isinstance(table, Mapping):
            return
        for method in table:
            replacement = DEPRECATED_PLUGIN_METHOD_NAMES.get(method)
            if replacement:
                key = (extension_id, method)
                if key not in _deprecated_method_warnings_seen:
                    _deprecated_method_warnings_seen.add(key)
                    self._logger.warning(
                        f"插件[{extension_id}]挂载的模块方法名 {method!r} 已随分发面归一废弃，"
                        f"不会再被任何分发调用触达；请改用新契约名 {replacement!r}"
                    )
                continue
            if method in _NEW_MULTI_SOURCE_CONTRACT_NAMES:
                key = (extension_id, method)
                if key not in _new_contract_hints_seen:
                    _new_contract_hints_seen.add(key)
                    self._logger.info(
                        f"插件[{extension_id}]挂载的模块方法名 {method!r} 是多来源契约，"
                        f"由多个数据源共用同一分发名；实现须按 source 参数自认领，"
                        f"非本插件负责的来源须返回 None 让出，否则会拦截该契约下的全部来源"
                    )

    def actions(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """聚合启用插件的工作流动作。"""
        actions: list[dict] = []
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.supports_hook("get_actions"):
                continue
            try:
                if not extension.is_enabled():
                    continue
                plugin_actions = plugin.get_actions()
                if plugin_actions:
                    actions.append({
                        "plugin_id": extension_id,
                        "plugin_name": plugin.plugin_name,
                        "actions": plugin_actions,
                    })
            except Exception as error:
                self._logger.error(f"获取插件 {extension_id} 动作出错：{str(error)}")
        return actions

    def _remote_descriptor(
        self, extension_id: str, plugin: Any, dist_path: str
    ) -> Dict[str, Any]:
        """构造联邦远程入口描述，附带按版本区分的标识，避免同插件不同版本撞名。

        Module Federation 的远程名是浏览器端的全局单一键空间，同一插件的两个版本
        若共用同一标识会互相覆盖或复用对方注册的入口。``remote_key`` 在实例键后
        拼接插件版本号，使不同版本天然得到不同标识；插件未声明 ``plugin_version``
        时没有版本信息可拼，回落为与 ``id`` 相同的取值。``id``/``url``/``name`` 三个
        既有字段保持原语义与格式不变，供未接入版本标识的前端继续按旧约定使用。
        :param extension_id: 插件实例键
        :param plugin: 运行态插件实例
        :param dist_path: 插件声明的联邦构建产物相对路径
        :return: 含 id、url、name、version、remote_key 的远程入口描述
        :raises RuntimeError: 联邦入口生成器尚未配置
        """
        if not self._remote_entry_factory:
            raise RuntimeError("插件联邦入口生成器尚未配置")
        version = getattr(plugin, "plugin_version", None) or None
        return {
            "id": extension_id,
            "url": self._remote_entry_factory(extension_id, dist_path, version),
            "name": plugin.plugin_name,
            "version": version,
            "remote_key": f"{extension_id}#{version}" if version else extension_id,
        }

    def remotes(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """投影插件联邦远程入口，并保持旧渲染模式筛选语义。"""
        remotes = []
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.supports_hook("get_render_mode"):
                continue
            render_mode, dist_path = plugin.get_render_mode()
            if render_mode != "vue":
                continue
            remotes.append(self._remote_descriptor(extension_id, plugin, dist_path))
        return remotes

    def auth_providers(self) -> List[Dict[str, Any]]:
        """投影启用插件声明的登录认证提供方。"""
        providers = []
        for extension in self._extensions(None):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled() or not extension.supports_hook(
                    "get_auth_providers"
            ):
                continue
            try:
                plugin_providers = plugin.get_auth_providers() or []
            except Exception as error:
                self._logger.error(
                    f"获取插件 {extension_id} 登录认证提供方出错：{str(error)}"
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
                provider["plugin_id"] = extension_id
                provider.setdefault("id", f"plugin:{extension_id}")
                provider.setdefault("name", plugin.plugin_name)
                provider.setdefault("enabled", True)
                # plugin_id 沿用既有语义继续填实例键；这两个字段显式拆出实例标识
                # 与实例键，供需要区分同一插件多个实例的调用方使用。
                provider["instance_id"] = split_instance_key(extension_id)[1]
                provider["instance_key"] = extension_id
                if render_mode == "vue" and dist_path:
                    provider.setdefault("component", "AuthPage")
                    provider["remote"] = self._remote_descriptor(
                        extension_id, plugin, dist_path
                    )
                providers.append(provider)
        return providers

    def sidebar(self) -> List[Dict[str, Any]]:
        """投影启用 Vue 插件的侧栏导航，并规整权限、分区和顺序。"""
        valid_sections = {"start", "discovery", "subscribe", "organize", "system"}
        valid_permissions = {"subscribe", "discovery", "search", "manage", "admin"}
        items = []
        for extension in self._extensions(None):
            extension_id, plugin = extension.extension_id, extension.instance
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
                            f"插件[{extension_id}]侧栏项 nav_key 无效，已跳过: "
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
                        "plugin_id": extension_id,
                        "nav_key": nav_key,
                        "title": raw.get("title") or plugin.plugin_name,
                        "icon": raw.get("icon") or "mdi-puzzle",
                        "section": section,
                        "permission": permission,
                        "order": order,
                        # plugin_id 沿用既有语义继续填实例键；这两个字段显式拆出
                        # 实例标识与实例键，供需要区分同一插件多个实例的调用方使用。
                        "instance_id": split_instance_key(extension_id)[1],
                        "instance_key": extension_id,
                    })
            except Exception as error:
                self._logger.error(
                    f"获取插件[{extension_id}]侧栏导航出错：{str(error)}"
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

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 实例键到其声明的 `ChannelCapabilities` 列表的映射
        """
        result: Dict[str, List[ChannelCapabilities]] = {}
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled() or not extension.supports_hook(
                    "get_channel_capabilities"
            ):
                continue
            try:
                declared = plugin.get_channel_capabilities() or []
            except Exception as error:
                self._logger.error(
                    f"获取插件 {extension_id} 渠道能力出错：{str(error)}"
                )
                continue
            accepted: List[ChannelCapabilities] = []
            for item in declared:
                if not isinstance(item, ChannelCapabilities):
                    self._logger.warning(
                        f"插件[{extension_id}]声明的渠道能力类型无效，已跳过：{item!r}"
                    )
                    continue
                if not channel_identity(item.channel):
                    continue
                accepted.append(item)
            result[extension_id] = accepted
        return result

    def dashboard_metadata(self) -> List[Dict[str, str]]:
        """投影启用插件的单仪表板或多仪表板元信息。"""
        metadata = []
        for extension in self._extensions(None):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.supports_hook("get_dashboard"):
                continue
            try:
                if not extension.is_enabled():
                    continue
                if extension.supports_hook("get_dashboard_meta"):
                    plugin_metadata = plugin.get_dashboard_meta()
                    if plugin_metadata:
                        # id 沿用既有语义继续填实例键；这两个字段显式拆出实例标识
                        # 与实例键，供需要区分同一插件多个实例的调用方使用。
                        metadata.extend({
                            "id": extension_id,
                            "name": item.get("name"),
                            "key": item.get("key"),
                            "instance_id": split_instance_key(extension_id)[1],
                            "instance_key": extension_id,
                        } for item in plugin_metadata if item)
                else:
                    metadata.append({
                        "id": extension_id,
                        "name": plugin.plugin_name,
                        "key": "",
                        "instance_id": split_instance_key(extension_id)[1],
                        "instance_key": extension_id,
                    })
            except Exception as error:
                self._logger.error(
                    f"获取插件[{extension_id}]仪表盘元数据出错：{str(error)}"
                )
        return metadata
