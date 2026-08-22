"""插件公开能力投影。"""

from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from app.runtime.extensions.contract.extension import (
    ExtensionDistribution,
    ExtensionFaultScope,
    ExtensionProvider,
    supports_extension_hook,
)
from app.runtime.extensions.contract.instance import (
    extension_id_of,
    matches_extension,
    split_instance_key,
)
from app.runtime.extensions.contract.declaration import (
    declaration_action_identity,
    declaration_action_impl,
    declaration_action_kwargs,
    declaration_command_data,
    declaration_command_identity,
    declaration_command_override,
    declaration_command_presentation,
    declaration_command_show,
    declaration_config_component,
    declaration_config_schema,
    declaration_dashboard_identity,
    declaration_filter_rule_conditions,
    declaration_filter_rule_group_identity,
    declaration_filter_rule_group_scope,
    declaration_filter_rule_identity,
    declaration_impl,
    declaration_media_source_identity,
    declaration_media_source_methods,
    declaration_media_types,
    declaration_methods,
    declaration_schedule_identity,
    declaration_schedule_kwargs,
    declaration_schedule_trigger,
    declaration_service_instance_identity,
    declaration_service_instance_requirement,
)
from app.runtime.extensions.projection.auth_entries import list_auth_entries
from app.runtime.extensions.projection.media_source_faces import media_source_capabilities
from app.runtime.extensions.admission.action import action_declaration_violation
from app.runtime.extensions.admission.agent_tool import (
    agent_tool_declaration_name,
    agent_tool_declaration_violation,
)
from app.runtime.extensions.admission.channel import (
    channel_capability_declaration_violation,
)
from app.runtime.extensions.admission.command import (
    command_declaration_violation,
)
from app.runtime.extensions.admission.dashboard import dashboard_declaration_violation
from app.runtime.extensions.admission.extension_scoped import elect_extension_scoped
from app.runtime.extensions.admission.filter_rule import (
    filter_rule_declaration_violation,
    filter_rule_group_declaration_violation,
)
from app.runtime.extensions.admission.media_source import (
    media_source_declaration_violation,
)
from app.runtime.extensions.projection.media_source_routing import (
    media_source_method_table,
    routes_by_source,
)
from app.runtime.extensions.admission.meta_parser import (
    meta_parser_declaration_violation,
)
from app.runtime.extensions.admission.module import module_declaration_violation
from app.runtime.extensions.admission.schedule import (
    schedule_declaration_violation,
    schedule_trigger_args,
)
from app.runtime.extensions.admission.service_instance import (
    SERVICE_INSTANCE_SCHEMA_DEPRECATION,
    service_instance_declaration_violation,
)
from app.runtime.extensions.admission.service_instance_requirement import (
    projected_service_instance_requirement,
)
from app.runtime.deprecation.policy import is_active as deprecation_is_active
from app.runtime.deprecation.policy import warn as deprecation_warn
from app.runtime.log import logger as default_logger
from app.runtime.log import wrap_for_plugin_instance
from app.schemas.media import normalize_media_source
from app.schemas.notification import ChannelCapabilities, channel_identity
from app.schemas.rule import CustomRule, FilterRuleGroup

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

# 已就「同一实例的两条模块来源挂载同一方法名」告警过的
# (实例键, 低优先来源, 高优先来源, 重叠方法名元组) 组合，去重理由同上。
_module_source_overlap_warnings_seen: set[tuple[str, str, str, tuple[str, ...]]] = set()

# 已就「挂了多来源契约方法却没声明任何媒体数据源」提示过的 (实例键, 方法名) 组合，
# 去重理由同上。
_undeclared_media_source_hints_seen: set[tuple[str, str]] = set()

# 已就「同一实例的定时任务标识被新旧两条来源同时挂载」提示过的 (实例键, 任务标识)
# 组合，去重理由同上。
_schedule_source_overlap_hints_seen: set[tuple[str, str]] = set()

# 已就「同一实例的两条命令来源声明同一命令词」告警过的 (实例键, 命令词) 组合，
# 去重理由同上。
_command_source_overlap_warned: set[tuple[str, str]] = set()


def _service_instance_identity(declaration: Any) -> Optional[tuple]:
    """取服务实例声明在扩展级裁决中的标识。

    :param declaration: 已通过契约校验的服务实例声明
    :return: (能力标签, 类型标识)；任一为空时为 None
    """
    capability, service_type, _name = declaration_service_instance_identity(declaration)
    if not capability or not service_type:
        return None
    return capability, service_type


def _media_source_identity(declaration: Any) -> Optional[tuple]:
    """取媒体数据源声明在扩展级裁决中的标识。

    标识按 `MediaSource` 归一后再比对，取用端也按归一值去重，两处口径一致才不会
    出现「裁决认为是两个数据源、取用端认为是同一个」的分歧。
    :param declaration: 已通过契约校验的媒体数据源声明
    :return: (归一后的数据源标识,)；标识为空时为 None
    """
    media_source, _name = declaration_media_source_identity(declaration)
    if not media_source:
        return None
    normalized = normalize_media_source(media_source)
    return (normalized.value if normalized is not None else media_source,)


def _agent_tool_identity(declaration: Any) -> Optional[tuple]:
    """取智能体工具声明在扩展级裁决中的标识。

    工具在智能体侧按工具名寻址，重名工具会让目录判定为身份歧义并拒绝解析，
    因此标识取最终生效的工具名而非实现类。
    :param declaration: 已通过契约校验的智能体工具声明
    :return: (工具名,)；工具名为空时为 None
    """
    name = agent_tool_declaration_name(declaration)
    return (name,) if name else None


def _command_identity(declaration: Any) -> Optional[tuple]:
    """取命令声明在扩展级裁决中的标识。

    :param declaration: 已通过契约校验的命令声明
    :return: (命令词,)；命令词为空时为 None
    """
    cmd, _name = declaration_command_identity(declaration)
    return (cmd,) if cmd else None


def _filter_rule_identity(declaration: Any) -> Optional[tuple]:
    """取筛选规则声明在扩展级裁决中的标识。

    :param declaration: 已通过契约校验的筛选规则声明
    :return: (规则标识,)；标识为空时为 None
    """
    rule_id, _name = declaration_filter_rule_identity(declaration)
    return (rule_id,) if rule_id else None


def _filter_rule_group_identity(declaration: Any) -> Optional[tuple]:
    """取筛选规则组声明在扩展级裁决中的标识。

    :param declaration: 已通过契约校验的筛选规则组声明
    :return: (规则组名,)；组名为空时为 None
    """
    name, _rule_string = declaration_filter_rule_group_identity(declaration)
    return (name,) if name else None


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

        目录快照未必都经过 `PluginProjection.modules()` 的映射校验（例如调度器
        直接接一份自定义目录），坏插件把方法表声明成非映射类型时在此处二次防御：
        产出一个调用即报错的占位提供者，交给分发器既有的异常捕获与故障上报通道
        隔离，不让它击穿同批次其它插件。
        :param method: 模块方法名称
        :return: 提供者迭代器
        """
        plugin_modules = self._catalog.get_plugin_modules()
        for (extension_id, plugin_name), module_dict in plugin_modules.items():
            if not isinstance(module_dict, Mapping):
                yield ExtensionProvider(
                    extension_id=extension_id,
                    display_name=plugin_name,
                    distribution=ExtensionDistribution.MARKET,
                    fault_scope=ExtensionFaultScope.PLUGIN,
                    invoke=self._malformed_declaration_invoke(extension_id, module_dict),
                    announces_invocation=False,
                )
                continue
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

    @staticmethod
    def _malformed_declaration_invoke(
        extension_id: str, module_dict: Any
    ) -> Callable[..., Any]:
        """构造非法方法表声明的占位调用。

        :param extension_id: 插件标识
        :param module_dict: 插件声明的非映射方法表
        :return: 调用即抛出 ``TypeError`` 的函数，供分发器按插件故障统一上报
        """
        def _raise(*_args: Any, **_kwargs: Any) -> None:
            raise TypeError(
                f"插件 {extension_id} 的模块声明必须是映射，"
                f"实际是 {type(module_dict).__name__}"
            )

        return _raise

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

    @staticmethod
    def _extension_scope(pid: Optional[str]) -> Optional[str]:
        """把一次查询的筛选条件放宽到整个插件族。

        扩展级声明的去重要在同一插件的全部实例之间裁决，只看被查询的那个实例
        取不到兄弟实例的声明，认哪一次就会随查询条件变化。
        :param pid: 插件 ID 或实例键，为空时命中全部
        :return: 收集声明时使用的筛选条件
        """
        return extension_id_of(pid) if pid else None

    @staticmethod
    def _narrow_to_query(
        declared: Dict[str, List[Any]], pid: Optional[str]
    ) -> Dict[str, List[Any]]:
        """把插件族范围的裁决结果收窄回本次查询的筛选条件。

        :param declared: 实例键到声明列表的映射
        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 只含命中实例的映射
        """
        return {
            key: items for key, items in declared.items() if matches_extension(key, pid)
        }

    def provided_commands(self, pid: Optional[str] = None) -> Dict[str, List[Any]]:
        """投影启用插件声明且通过登记契约校验的远程命令。

        命令词是扩展级事实：它是用户在聊天窗口里手打的全局标识，进的是按命令词建键的
        全局命令表，也是外部渠道菜单里的命令名，用户敲它时不带任何实例限定符，宿主无从
        把同一个词分派给「第二个分身」。因此同插件多实例声明同一命令词只登记一次；各
        实例声明不同命令词互不影响。

        单条声明不合契约只跳过该条，既不影响同一实例的其余命令，也不影响其它实例；
        单个实例取声明时抛异常同理只跳过该实例。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 实例键到其命令声明列表的映射，仅含通过契约校验且在同插件多实例裁决中
            胜出的条目
        """
        return self._collect_extension_scoped(
            pid,
            hook="provides_commands",
            violation_of=command_declaration_violation,
            identity_of=_command_identity,
            subject="命令",
            unique_within_instance=True,
        )

    @staticmethod
    def declared_command(item: Any) -> Dict[str, Any]:
        """把单条已通过契约校验的命令声明投影为命令描述字典。

        描述字典的 `cmd`、`desc`、`category`、`data` 与 `get_command()` 返回项同名同义，
        命令中枢与查询插件能力的调用方按同一份形状消费；`impl`、`args_description` 与
        `overrides_builtin` 是声明式专有的字段，不带 `event` 表示本条命令由宿主直接调用
        实现而不转发事件。`get_command()` 报不出接管内建命令的意图，其条目因此恒为不接管。

        :param item: 已通过契约校验的命令声明
        :return: 命令描述字典
        """
        cmd, name = declaration_command_identity(item)
        category, args_description = declaration_command_presentation(item)
        show = declaration_command_show(item)
        overrides_builtin = declaration_command_override(item)
        data = declaration_command_data(item)
        return {
            "cmd": cmd,
            "desc": name,
            "category": category,
            "args_description": args_description,
            "show": True if show is None else bool(show),
            "overrides_builtin": bool(overrides_builtin),
            "data": dict(data) if data else {},
            "impl": declaration_impl(item),
        }

    def _legacy_commands(
        self, extension: PluginExtension, extension_id: str, plugin: Any
    ) -> List[Dict[str, Any]]:
        """取用插件 `get_command()` 声明的命令，并按既有规则记录废弃告警。

        :param extension: 插件扩展视图
        :param extension_id: 插件实例键
        :param plugin: 插件运行实例
        :return: 命令描述字典列表；未声明该钩子、废弃阶段已默认关闭或返回空值时为空列表
        """
        if not extension.supports_hook("get_command"):
            return []
        # 废弃阶段推进到默认关闭后，该钩子整体不再生效，按未声明处理；标识列入
        # DEPRECATION_ENABLED 可临时恢复，用于观察真实依赖方
        if not deprecation_is_active("plugin.get_command"):
            return []
        try:
            declared = plugin.get_command()
        except Exception as error:
            self._logger.error(f"获取插件 {extension_id} 命令出错：{str(error)}")
            return []
        if not declared:
            return []
        deprecation_warn("plugin.get_command", context=extension_id)
        return [dict(item) for item in declared if isinstance(item, Mapping)]

    def commands(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """聚合启用插件的远程命令并标注归属实例键。

        聚合两条来源：`provides_commands()` 声明式登记（经契约校验与同插件多实例裁决）
        与 `get_command()` 裸列表（已进入废弃期，触达即告警一次）。同一实例两条来源
        声明同一命令词时声明式生效，与 `provides_modules()` 对 `get_module()` 的口径一致。

        跨插件同命令词的处置不在这里：本方法回答「谁声明了什么」，哪一条最终生效由命令
        注册表按登记内容裁决。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 命令描述列表，每项的 `pid` 字段为声明来源的实例键
        """
        commands: list[dict] = []
        declared_by_instance = self.provided_commands(pid)
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled():
                continue
            declared = [
                self.declared_command(item)
                for item in declared_by_instance.get(extension_id, [])
            ]
            taken = {item["cmd"] for item in declared}
            for command in declared:
                commands.append({**command, "pid": extension_id})
            for command in self._legacy_commands(extension, extension_id, plugin):
                word = command.get("cmd")
                if word in taken:
                    self._warn_command_source_overlap(extension_id, word)
                    continue
                commands.append({**command, "pid": extension_id})
        return commands

    def _warn_command_source_overlap(self, extension_id: str, cmd: Any) -> None:
        """就同一实例两条来源声明同一命令词打一次提示。

        :param extension_id: 插件实例键
        :param cmd: 被两条来源同时声明的命令词
        :return: 无返回值
        """
        seen = (extension_id, str(cmd))
        if seen in _command_source_overlap_warned:
            return
        _command_source_overlap_warned.add(seen)
        self._logger.warning(
            f"插件[{extension_id}]的命令 {cmd} 同时由 provides_commands() 与 "
            f"get_command() 声明，声明式登记生效，get_command() 的同名条目已忽略"
        )

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
                for source_api in plugin.get_api() or []:
                    # 拷贝而非原地改写：`get_api()` 常返回模块级常量或 `self._apis`，
                    # 同一插件的多个分身各投影一次时会共享同一个底层 dict/list——原地
                    # 改写会让第二个分身在第一个分身已经改过的 path 上再叠一层前缀
                    # （如 `/p@b/p@a/x`），且后写入的 endpoint 包装会覆盖先写入的，
                    # 两个分身的路由与日志都会串到错的实例上。
                    api = dict(source_api)
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

    def provided_schedules(self, pid: Optional[str] = None) -> Dict[str, List[Any]]:
        """投影启用插件声明且通过登记契约校验的定时任务。

        单条声明不合契约只跳过该条，既不影响同一实例的其余任务，也不影响其它
        实例；单个实例取声明时抛异常同理只跳过该实例。

        任务标识在声明它的实例内唯一：同一实例把同一个标识声明两次时保留先声明
        的那一条、拒绝后一条。这与「绝不取第一个」不冲突——那条规则管的是在多个
        各自成立的候选里替用户挑一个，这里两条声明指的是同一个任务，后一条是重复
        表达而不是另一个候选。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 实例键到其定时任务声明列表的映射，仅含通过契约校验的条目
        """
        result: Dict[str, List[Any]] = {}
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled() or not extension.supports_hook(
                    "provides_schedules"
            ):
                continue
            try:
                declared = plugin.provides_schedules() or []
            except Exception as error:
                self._logger.error(
                    f"获取插件 {extension_id} 定时任务声明出错：{str(error)}"
                )
                continue
            accepted: List[Any] = []
            claimed: set[str] = set()
            for item in declared:
                violation = schedule_declaration_violation(item)
                if violation is None:
                    job_id, _name = declaration_schedule_identity(item)
                    if job_id in claimed:
                        violation = f"任务标识 {job_id!r} 在本实例内重复声明"
                    else:
                        claimed.add(job_id)
                if violation:
                    self._logger.error(
                        f"插件[{extension_id}]声明的定时任务 {item!r} 不合登记契约，"
                        f"已跳过：{violation}"
                    )
                    continue
                accepted.append(item)
            result[extension_id] = accepted
        return result

    @staticmethod
    def _declared_schedule(extension_id: str, item: Any) -> Dict[str, Any]:
        """把单条已通过契约校验的定时任务声明投影为与 `get_service()` 一致的描述字典。

        `trigger` 与 `kwargs` 交出的是调度类型与调度参数这两样纯数据，触发器由调度器
        自行按其时区建；在此处先建好会让触发器带上本进程的本地时区，覆盖掉宿主为整个
        调度器配置的那一个。

        :param extension_id: 插件实例键
        :param item: 已通过契约校验的定时任务声明
        :return: 含 id、name、trigger、kwargs、func、func_kwargs、pid 的任务描述字典
        """
        job_id, name = declaration_schedule_identity(item)
        trigger, trigger_args = declaration_schedule_trigger(item)
        kwargs = declaration_schedule_kwargs(item)
        return {
            "id": job_id,
            "name": name,
            "trigger": trigger,
            "kwargs": schedule_trigger_args(trigger, trigger_args),
            "func": declaration_impl(item),
            "func_kwargs": dict(kwargs) if kwargs else {},
            "pid": extension_id,
        }

    def _legacy_services(
        self, extension: PluginExtension, extension_id: str, plugin: Any
    ) -> List[Dict[str, Any]]:
        """取用插件 `get_service()` 声明的定时服务，并按既有规则记录废弃告警。

        :param extension: 插件扩展视图
        :param extension_id: 插件实例键
        :param plugin: 插件运行实例
        :return: 服务描述列表，每项补上归属实例键；未声明该钩子、废弃阶段已默认
            关闭或返回空值时为空列表
        """
        if not extension.supports_hook("get_service"):
            return []
        # 废弃阶段推进到默认关闭后，该钩子整体不再生效，按未声明处理；标识列入
        # DEPRECATION_ENABLED 可临时恢复，用于观察真实依赖方
        if not deprecation_is_active("plugin.get_service"):
            return []
        try:
            plugin_services = plugin.get_service()
        except Exception as error:
            self._logger.error(f"获取插件 {extension_id} 服务出错：{str(error)}")
            return []
        if not plugin_services:
            return []
        deprecation_warn("plugin.get_service", context=extension_id)
        services: List[Dict[str, Any]] = []
        for service in plugin_services:
            service["pid"] = extension_id
            services.append(service)
        return services

    def services(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """聚合启用插件的定时任务并标注归属实例键。

        聚合两条来源：`provides_schedules()` 声明式登记（经契约校验）与
        `get_service()` 裸列表（后者已进入废弃期，触达即告警一次）；同一实例的
        同一任务标识被两条来源同时挂载时声明式生效，与 `provides_modules()` 对
        `get_module()` 的口径一致。

        同一插件的多个实例可能声明相同的任务标识，调用方须按 `pid` 字段区分归属
        实例才能构造不冲突的定时任务标识。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 任务描述列表，每项的 `pid` 字段为声明来源的实例键
        """
        services: list[dict] = []
        declared_by_instance = self.provided_schedules(pid)
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled():
                continue
            declared = [
                self._declared_schedule(extension_id, item)
                for item in declared_by_instance.get(extension_id, [])
            ]
            declared_ids = {service["id"] for service in declared}
            legacy = [
                service
                for service in self._legacy_services(extension, extension_id, plugin)
                if not self._shadowed_by_declaration(extension_id, service, declared_ids)
            ]
            services.extend(declared + legacy)
        return services

    def _shadowed_by_declaration(
        self, extension_id: str, service: Dict[str, Any], declared_ids: set[str]
    ) -> bool:
        """判断旧钩子交出的任务是否已被同实例的声明式登记接管，并就重叠提示一次。

        :param extension_id: 插件实例键
        :param service: `get_service()` 交出的任务描述
        :param declared_ids: 该实例经声明式登记的任务标识集合
        :return: 已被接管为 True
        """
        job_id = service.get("id")
        if job_id not in declared_ids:
            return False
        key = (extension_id, str(job_id))
        if key not in _schedule_source_overlap_hints_seen:
            _schedule_source_overlap_hints_seen.add(key)
            self._logger.info(
                f"插件[{extension_id}]的任务 {job_id!r} 同时由 get_service() 与 "
                f"provides_schedules() 挂载，以声明式登记为准"
            )
        return True

    def provided_modules(self, pid: Optional[str] = None) -> Dict[str, List[Any]]:
        """投影启用插件声明且通过登记契约校验的模块方法表。

        单条声明不合契约只跳过该条，既不影响同一实例的其余声明，也不影响其它
        实例；单个实例取声明时抛异常同理只跳过该实例。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 实例键到其模块声明列表的映射，仅含通过契约校验的条目
        """
        result: Dict[str, List[Any]] = {}
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled() or not extension.supports_hook(
                    "provides_modules"
            ):
                continue
            try:
                declared = plugin.provides_modules() or []
            except Exception as error:
                self._logger.error(
                    f"获取插件 {extension_id} 模块声明出错：{str(error)}"
                )
                continue
            accepted: List[Any] = []
            for item in declared:
                violation = module_declaration_violation(item)
                if violation:
                    self._logger.error(
                        f"插件[{extension_id}]声明的模块 {item!r} 不合登记契约，"
                        f"已跳过：{violation}"
                    )
                    continue
                accepted.append(item)
            result[extension_id] = accepted
        return result

    def modules(self, pid: Optional[str] = None) -> Dict[tuple, Dict[str, Any]]:
        """聚合启用插件的模块方法清单。

        聚合三条来源，优先级从低到高为：`get_module()` 裸方法表（已进入废弃期，触达
        即告警一次）、`provides_modules()` 声明式方法表、`provides_media_sources()`
        随数据源声明交出的实现。同一实例的两条来源同时挂载同一方法名时高优先级的
        生效，并就重叠方法名各打一次提示。

        数据源声明交出的多来源契约方法带 source 路由，非本来源的调用不会触达实现。

        键取 `(实例键, 展示名)`：同一插件的多个实例展示名相同，只有实例键能把它们
        区分开，否则后登记的实例会覆盖先登记的，被覆盖的那一份实现从此不再参与分发。
        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: `(实例键, 展示名)` 到该实例方法表的映射
        """
        modules: dict[tuple, dict] = {}
        declared_by_instance = self.provided_modules(pid)
        media_sources_by_instance = self.provided_media_sources(pid)
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled():
                continue
            declared_table = self._merge_declared_module_methods(
                declared_by_instance.get(extension_id, [])
            )
            source_declarations = media_sources_by_instance.get(extension_id, [])
            source_table = media_source_method_table(source_declarations)
            legacy_table, legacy_present = self._legacy_module_table(
                extension, extension_id, plugin
            )
            if not declared_table and not source_table and not legacy_present:
                continue
            self._hint_undeclared_media_source(
                extension_id, declared_table, source_declarations
            )
            modules[(extension_id, extension.display_name)] = self._merge_module_sources(
                extension_id,
                (
                    ("get_module()", legacy_table),
                    ("provides_modules()", declared_table),
                    ("provides_media_sources()", source_table),
                ),
            )
        self._warn_sibling_contract_overlap(modules)
        return modules

    @staticmethod
    def _merge_declared_module_methods(declarations: List[Any]) -> Dict[str, Any]:
        """把同一实例的多条模块声明合并为单张方法表。

        同名方法被多条声明挂载时后一条覆盖前一条，与 `dict.update` 语义一致。

        :param declarations: 已通过契约校验的模块声明列表
        :return: 合并后的方法名到可调用对象的映射
        """
        merged: dict[str, Any] = {}
        for declaration in declarations:
            methods = declaration_methods(declaration)
            if methods:
                merged.update(methods)
        return merged

    def _legacy_module_table(
        self, extension: PluginExtension, extension_id: str, plugin: Any
    ) -> tuple[Dict[str, Any], bool]:
        """取用插件 `get_module()` 声明的方法表，并按既有规则记录废弃告警。

        :param extension: 插件扩展视图
        :param extension_id: 插件实例键
        :param plugin: 插件运行实例
        :return: (方法表, 是否取到有效声明)；未声明该钩子、返回 None 或格式非法时
            方法表为空字典且第二项为 False
        """
        if not extension.supports_hook("get_module"):
            return {}, False
        # 废弃阶段推进到默认关闭后，该钩子整体不再生效，按未声明处理；标识列入
        # DEPRECATION_ENABLED 可临时恢复，用于观察真实依赖方
        if not deprecation_is_active("plugin.get_module"):
            return {}, False
        try:
            table = plugin.get_module()
        except Exception as error:
            self._logger.error(f"获取插件 {extension_id} 模块出错：{str(error)}")
            return {}, False
        # 基类默认实现返回 None；只接受映射，防止把 list 当成方法表传入调度器
        if table is None:
            return {}, False
        if not isinstance(table, Mapping):
            self._logger.error(
                f"插件 {extension_id} 的 get_module() 返回值必须是字典，"
                f"实际是 {type(table).__name__}"
            )
            return {}, False
        # 注入式模块声明整体处于废弃期，按实例留一次痕迹；表内旧分发名的
        # 迁移提示是另一件事，两者互不替代
        deprecation_warn("plugin.get_module", context=extension_id)
        self._warn_dispatch_migration(extension_id, table)
        return table, True

    def _merge_module_sources(
        self, extension_id: str, lanes: tuple[tuple[str, Dict[str, Any]], ...]
    ) -> Dict[str, Any]:
        """按优先级合并多条来源的方法表，同名方法高优先级来源生效。

        只有一条来源提供方法时原样返回该来源的表对象，不额外拷贝：告警是只读
        动作，插件如果依赖投影结果与自己交出的表同一身份，该身份不因告警而改变。

        :param extension_id: 插件实例键
        :param lanes: (来源钩子名, 方法表) 序列，按优先级从低到高排列
        :return: 合并后的方法表，高优先级条目覆盖低优先级的同名条目
        """
        present = [(hook, table) for hook, table in lanes if table]
        if not present:
            return {}
        if len(present) == 1:
            return present[0][1]
        merged: Dict[str, Any] = {}
        for index, (hook, table) in enumerate(present):
            for higher_hook, higher_table in present[index + 1:]:
                self._warn_module_source_overlap(
                    extension_id, hook, table, higher_hook, higher_table
                )
            merged.update(table)
        return merged

    def _warn_module_source_overlap(
        self,
        extension_id: str,
        hook: str,
        table: Dict[str, Any],
        higher_hook: str,
        higher_table: Dict[str, Any],
    ) -> None:
        """就两条来源挂载的同名方法打一次提示，不改写方法表。

        :param extension_id: 插件实例键
        :param hook: 低优先级来源的钩子名
        :param table: 低优先级来源的方法表
        :param higher_hook: 高优先级来源的钩子名
        :param higher_table: 高优先级来源的方法表
        :return: 无返回值
        """
        overlap = tuple(sorted(set(table) & set(higher_table)))
        if not overlap:
            return
        key = (extension_id, hook, higher_hook, overlap)
        if key in _module_source_overlap_warnings_seen:
            return
        _module_source_overlap_warnings_seen.add(key)
        self._logger.warning(
            f"插件[{extension_id}]的 {hook} 与 {higher_hook} 同时挂载方法名 "
            f"{list(overlap)}：{higher_hook} 的声明优先生效，{hook} 中的同名实现"
            f"不会被调用，请从 {hook} 中移除"
        )

    def _hint_undeclared_media_source(
        self, extension_id: str, declared_table: Dict[str, Any], source_declarations: List[Any]
    ) -> None:
        """就没有数据源声明却挂载多来源契约方法的实例各打一次提示，不改写方法表。

        这种写法有两种都成立的意图：接管一个已存在的来源，或者提供一个新来源却漏写
        了数据源声明。宿主分不清是哪一种——实现服务哪个来源要到调用时才知道——因此
        只提示不拒绝，拒绝会把前一种合法用法一并挡掉。

        :param extension_id: 插件实例键
        :param declared_table: `provides_modules()` 已通过契约校验的合并方法表
        :param source_declarations: 该实例已通过契约校验的媒体数据源声明列表
        :return: 无返回值
        """
        if source_declarations:
            return
        for method in declared_table:
            if not routes_by_source(method):
                continue
            key = (extension_id, method)
            if key in _undeclared_media_source_hints_seen:
                continue
            _undeclared_media_source_hints_seen.add(key)
            self._logger.info(
                f"插件[{extension_id}]在 provides_modules() 里挂载了多来源契约方法 "
                f"{method!r}，却没有声明任何媒体数据源：若这是一个新数据源，请改用 "
                f"provides_media_sources() 把展示信息与实现写在同一条声明里，否则它"
                f"不会出现在来源列表中，用户在界面上选不到；若只是接管已有来源，"
                f"实现须按 source 自认领，非本来源返回 None 让出"
            )

    def provided_media_sources(self, pid: Optional[str] = None) -> Dict[str, List[Any]]:
        """投影启用插件声明且通过登记契约校验的媒体数据源。

        单条声明不合契约只跳过该条，既不影响同一实例的其余声明，也不影响其它
        实例；单个实例取声明时抛异常同理只跳过该实例。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 实例键到其媒体数据源声明列表的映射，仅含通过契约校验且在同插件
            多实例裁决中胜出的条目
        """
        result: Dict[str, List[Any]] = {}
        for extension in self._extensions(self._extension_scope(pid)):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled() or not extension.supports_hook(
                    "provides_media_sources"
            ):
                continue
            try:
                declared = plugin.provides_media_sources() or []
            except Exception as error:
                self._logger.error(
                    f"获取插件 {extension_id} 媒体数据源声明出错：{str(error)}"
                )
                continue
            accepted: List[Any] = []
            for item in declared:
                violation = media_source_declaration_violation(item)
                if violation:
                    self._logger.error(
                        f"插件[{extension_id}]声明的媒体数据源 {item!r} 不合登记契约，"
                        f"已跳过：{violation}"
                    )
                    continue
                accepted.append(item)
            result[extension_id] = accepted
        return self._narrow_to_query(
            elect_extension_scoped(
                result,
                _media_source_identity,
                subject="媒体数据源标识",
                hook="provides_media_sources",
                log=self._logger,
            ),
            pid,
        )

    @staticmethod
    def _declared_media_source(extension_id: str, item: Any) -> Dict[str, Any]:
        """把单条已通过契约校验的媒体数据源声明投影为描述字典。

        能力面按声明交出的方法名推导，不取声明方另填的字段：宿主已经知道这条声明
        挂了哪些方法，让作者再写一遍只会多出一处可写漏的地方。

        :param extension_id: 插件实例键
        :param item: 已通过契约校验的媒体数据源声明
        :return: 含 name、media_source、plugin_id、capabilities 的描述字典，声明了
            media_types 时另含该字段
        """
        media_source, name = declaration_media_source_identity(item)
        media_types = declaration_media_types(item)
        methods = declaration_media_source_methods(item) or {}
        entry: Dict[str, Any] = {
            "name": name,
            "media_source": media_source,
            "plugin_id": extension_id,
            "capabilities": [
                capability.value for capability in media_source_capabilities(methods)
            ],
        }
        if media_types is not None:
            entry["media_types"] = list(media_types)
        return entry

    def _legacy_media_sources(
        self, extension: PluginExtension, extension_id: str, plugin: Any
    ) -> List[Dict[str, Any]]:
        """取用插件 `get_media_source()` 声明的数据源，并按既有规则记录废弃告警。

        :param extension: 插件扩展视图
        :param extension_id: 插件实例键
        :param plugin: 插件运行实例
        :return: 数据源描述字典列表；未声明该钩子或废弃阶段已默认关闭时为空列表
        """
        if not extension.supports_hook("get_media_source"):
            return []
        # 废弃阶段推进到默认关闭后，该钩子整体不再生效，按未声明处理；标识列入
        # DEPRECATION_ENABLED 可临时恢复，用于观察真实依赖方
        if not deprecation_is_active("plugin.get_media_source"):
            return []
        try:
            raw_sources = plugin.get_media_source()
        except Exception as error:
            self._logger.error(f"获取插件 {extension_id} 媒体数据源出错：{str(error)}")
            return []
        if raw_sources is None:
            return []
        deprecation_warn("plugin.get_media_source", context=extension_id)
        sources: List[Dict[str, Any]] = []
        for source in raw_sources:
            if isinstance(source, dict):
                item = source.copy()
                item.setdefault("plugin_id", extension_id)
                sources.append(item)
        return sources

    def media_sources(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """聚合启用插件声明的媒体数据源。

        聚合两条来源：`provides_media_sources()` 声明式登记（经契约校验）与
        `get_media_source()` 裸描述字典列表（后者已进入废弃期，触达即告警一次）。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 数据源描述列表，每项带上声明它的实例键
        """
        sources: list[dict] = []
        declared_by_instance = self.provided_media_sources(pid)
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled():
                continue
            for item in declared_by_instance.get(extension_id, []):
                sources.append(self._declared_media_source(extension_id, item))
            sources.extend(self._legacy_media_sources(extension, extension_id, plugin))
        return sources

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

    def provided_actions(self, pid: Optional[str] = None) -> Dict[str, List[Any]]:
        """投影启用插件声明且通过登记契约校验的工作流动作。

        单条声明不合契约只跳过该条，既不影响同一实例的其余声明，也不影响其它
        实例；单个实例取声明时抛异常同理只跳过该实例。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 实例键到其动作声明列表的映射，仅含通过契约校验的条目
        """
        result: Dict[str, List[Any]] = {}
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled() or not extension.supports_hook(
                    "provides_actions"
            ):
                continue
            try:
                declared = plugin.provides_actions() or []
            except Exception as error:
                self._logger.error(
                    f"获取插件 {extension_id} 动作声明出错：{str(error)}"
                )
                continue
            accepted: List[Any] = []
            for item in declared:
                violation = action_declaration_violation(item)
                if violation:
                    self._logger.error(
                        f"插件[{extension_id}]声明的动作 {item!r} 不合登记契约，"
                        f"已跳过：{violation}"
                    )
                    continue
                accepted.append(item)
            result[extension_id] = accepted
        return result

    @staticmethod
    def _declared_action(item: Any) -> Dict[str, Any]:
        """把单条已通过契约校验的动作声明投影为与 `get_actions()` 一致的描述字典。

        声明了服务实例作用对象时附带该坐标，供工作流编辑器渲染实例选择器；未声明时
        整个键都不出现，描述字典与该字段存在之前逐键相同。

        :param item: 已通过契约校验的动作声明
        :return: 含 action_id、name、func、kwargs 的动作描述字典，声明了作用对象时
            另含 requires_service_instance
        """
        action_id, name = declaration_action_identity(item)
        kwargs = declaration_action_kwargs(item)
        descriptor = {
            "action_id": action_id,
            "name": name,
            "func": declaration_action_impl(item),
            "kwargs": dict(kwargs) if kwargs else {},
        }
        requirement = projected_service_instance_requirement(
            declaration_service_instance_requirement(item)
        )
        if requirement is not None:
            descriptor["requires_service_instance"] = requirement
        return descriptor

    def _legacy_actions(
        self, extension: PluginExtension, extension_id: str, plugin: Any
    ) -> List[Dict[str, Any]]:
        """取用插件 `get_actions()` 声明的动作，并按既有规则记录废弃告警。

        :param extension: 插件扩展视图
        :param extension_id: 插件实例键
        :param plugin: 插件运行实例
        :return: 插件声明的动作列表；未声明该钩子、废弃阶段已默认关闭或返回
            空值时为空列表
        """
        if not extension.supports_hook("get_actions"):
            return []
        # 废弃阶段推进到默认关闭后，该钩子整体不再生效，按未声明处理；标识列入
        # DEPRECATION_ENABLED 可临时恢复，用于观察真实依赖方
        if not deprecation_is_active("plugin.get_actions"):
            return []
        try:
            plugin_actions = plugin.get_actions()
        except Exception as error:
            self._logger.error(f"获取插件 {extension_id} 动作出错：{str(error)}")
            return []
        if not plugin_actions:
            return []
        deprecation_warn("plugin.get_actions", context=extension_id)
        return plugin_actions

    def actions(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """聚合启用插件的工作流动作。

        聚合两条来源：`provides_actions()` 声明式登记（经契约校验）与
        `get_actions()` 裸列表（后者已进入废弃期，触达即告警一次）；同一实例
        两条来源皆有声明时动作列表合并，声明式登记排在前面。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 按插件实例分组的动作列表
        """
        actions: list[dict] = []
        declared_by_instance = self.provided_actions(pid)
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled():
                continue
            declared_actions = [
                self._declared_action(item)
                for item in declared_by_instance.get(extension_id, [])
            ]
            legacy_actions = self._legacy_actions(extension, extension_id, plugin)
            merged = declared_actions + legacy_actions
            if merged:
                actions.append({
                    "plugin_id": extension_id,
                    "plugin_name": plugin.plugin_name,
                    "actions": merged,
                })
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

    def _build_auth_provider(
        self,
        extension_id: str,
        plugin: Any,
        fields: Mapping[str, Any],
        render_mode: Optional[str],
        dist_path: Optional[str],
    ) -> Dict[str, Any]:
        """按同一套字段语义组装单条认证提供方描述。

        配置扇出与旧钩子两条来源共用本方法，因此登录页看到的字段形状与来源无关。

        :param extension_id: 插件实例键
        :param plugin: 运行态插件实例
        :param fields: 展示字段（id/name/icon/enabled，配置扇出的入口另带 service_type）
        :param render_mode: 声明方扩展当前的渲染模式
        :param dist_path: vue 模式下的联邦构建产物相对路径
        :return: 含 id/type/plugin_id/name/enabled/instance_id/instance_key 的字典，
            vue 模式下另含登录入口渲染用的 component 与 remote
        """
        provider = dict(fields)
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
            provider["remote"] = self._remote_descriptor(extension_id, plugin, dist_path)
        return provider

    def _legacy_auth_providers(
        self,
        extension: PluginExtension,
        extension_id: str,
        plugin: Any,
        render_mode: Optional[str],
        dist_path: Optional[str],
    ) -> List[Dict[str, Any]]:
        """取用插件 `get_auth_providers()` 声明的登录入口，并按既有规则记录废弃告警。

        :param extension: 插件扩展视图
        :param extension_id: 插件实例键
        :param plugin: 插件运行实例
        :param render_mode: 声明方扩展当前的渲染模式
        :param dist_path: vue 模式下的联邦构建产物相对路径
        :return: 认证提供方描述列表；未声明该钩子或废弃阶段已默认关闭时为空列表
        """
        if not extension.supports_hook("get_auth_providers"):
            return []
        # 废弃阶段推进到默认关闭后，该钩子整体不再生效，按未声明处理；标识列入
        # DEPRECATION_ENABLED 可临时恢复，用于观察真实依赖方
        if not deprecation_is_active("plugin.get_auth_providers"):
            return []
        try:
            plugin_providers = plugin.get_auth_providers() or []
        except Exception as error:
            self._logger.error(
                f"获取插件 {extension_id} 登录认证提供方出错：{str(error)}"
            )
            return []
        providers: List[Dict[str, Any]] = []
        for raw_provider in plugin_providers:
            if not raw_provider or not isinstance(raw_provider, dict):
                continue
            providers.append(
                self._build_auth_provider(
                    extension_id, plugin, raw_provider, render_mode, dist_path
                )
            )
        if providers:
            deprecation_warn("plugin.get_auth_providers", context=extension_id)
        return providers

    def _configured_auth_providers(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """投影用户已配置的登录入口。

        入口由登录认证族的实例配置扇出，配置与类型登记的连接、单实例裁决与身份标识
        去歧义都收在 `app.runtime.extensions.projection.auth_entries`；本方法只补上登记表里没有
        的那一样——声明该类型的插件实例当下的渲染模式与联邦远程入口，vue 模式下登录页
        据此加载入口组件。

        入口的展示名取实例名而不是类型名：用户接了两台媒体服务器时，登录页上要能分辨
        点的是哪一台。

        单个入口组装失败只跳过它自己：登录页是所有登录方式的唯一入口，一条坏配置或一个
        实现有问题的插件不能让整份列表消失。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 认证提供方描述列表
        """
        extensions = {
            extension.extension_id: extension
            for extension in self._extensions(pid)
            if extension.is_enabled()
        }
        providers: List[Dict[str, Any]] = []
        for entry in list_auth_entries():
            extension = extensions.get(entry.owner)
            if extension is None:
                continue
            try:
                render_mode, dist_path = self._extension_render_mode(extension)
                providers.append(
                    self._build_auth_provider(
                        entry.owner,
                        extension.instance,
                        {
                            "id": entry.identity,
                            "name": entry.name,
                            "icon": entry.icon,
                            "enabled": True,
                            "service_type": entry.service_type,
                        },
                        render_mode,
                        dist_path,
                    )
                )
            except Exception as error:
                self._logger.error(
                    f"组装插件实例 {entry.owner} 的登录入口 {entry.identity} 出错，"
                    f"已跳过该入口：{str(error)}"
                )
        return providers

    @staticmethod
    def _extension_render_mode(
        extension: PluginExtension,
    ) -> Tuple[Optional[str], Optional[str]]:
        """读取扩展当前的渲染模式与联邦构建产物路径。

        :param extension: 插件扩展视图
        :return: (渲染模式, 构建产物相对路径) 二元组；未声明该钩子时两位均为 None
        """
        if not extension.supports_hook("get_render_mode"):
            return None, None
        return extension.instance.get_render_mode()

    def auth_providers(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        """投影登录页可展示的插件登录入口。

        聚合两条来源：登录认证族的实例配置扇出（用户配几条就有几个入口），与
        `get_auth_providers()` 裸字典列表（分身级旧写法，已进入废弃期，触达即告警
        一次）。两条来源按同一套字段语义组装：vue 渲染模式下登录入口组件缺省为
        `AuthPage`，附带联邦远程入口描述。

        描述里不带任何配置载荷：本列表由未登录状态下的登录页取用，而登录入口的配置里
        装着客户端密钥一类的东西。

        单个插件实例出错只跳过它自己，其余入口照常出现在登录页上。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 认证提供方描述列表
        """
        providers = self._configured_auth_providers(pid)
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled():
                continue
            try:
                render_mode, dist_path = self._extension_render_mode(extension)
                providers.extend(
                    self._legacy_auth_providers(
                        extension, extension_id, plugin, render_mode, dist_path
                    )
                )
            except Exception as error:
                self._logger.error(
                    f"组装插件实例 {extension_id} 的登录入口出错，已跳过该实例："
                    f"{str(error)}"
                )
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

    def provided_channel_capabilities(
        self, pid: Optional[str] = None
    ) -> Dict[str, List[ChannelCapabilities]]:
        """投影启用插件声明且通过登记契约校验的消息渠道能力。

        单条声明不合契约只跳过该条，既不影响同一实例的其余声明，也不影响其它
        实例；单个实例取声明时抛异常同理只跳过该实例。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 实例键到其渠道能力声明列表的映射，仅含通过契约校验的条目
        """
        result: Dict[str, List[ChannelCapabilities]] = {}
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled() or not extension.supports_hook(
                    "provides_channel_capabilities"
            ):
                continue
            try:
                declared = plugin.provides_channel_capabilities() or []
            except Exception as error:
                self._logger.error(
                    f"获取插件 {extension_id} 渠道能力声明出错：{str(error)}"
                )
                continue
            accepted: List[ChannelCapabilities] = []
            for item in declared:
                violation = channel_capability_declaration_violation(item)
                if violation:
                    self._logger.error(
                        f"插件[{extension_id}]声明的渠道能力 {item!r} 不合登记契约，"
                        f"已跳过：{violation}"
                    )
                    continue
                accepted.append(item)
            result[extension_id] = accepted
        return result

    def _legacy_channel_capabilities(
        self, extension: PluginExtension, extension_id: str, plugin: Any
    ) -> List[ChannelCapabilities]:
        """取用插件 `get_channel_capabilities()` 声明的渠道能力，并按既有规则记录废弃告警。

        :param extension: 插件扩展视图
        :param extension_id: 插件实例键
        :param plugin: 插件运行实例
        :return: 通过基础形状校验的渠道能力列表；未声明该钩子或废弃阶段已默认
            关闭时为空列表
        """
        if not extension.supports_hook("get_channel_capabilities"):
            return []
        # 废弃阶段推进到默认关闭后，该钩子整体不再生效，按未声明处理；标识列入
        # DEPRECATION_ENABLED 可临时恢复，用于观察真实依赖方
        if not deprecation_is_active("plugin.get_channel_capabilities"):
            return []
        try:
            declared = plugin.get_channel_capabilities() or []
        except Exception as error:
            self._logger.error(f"获取插件 {extension_id} 渠道能力出错：{str(error)}")
            return []
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
        if accepted:
            deprecation_warn("plugin.get_channel_capabilities", context=extension_id)
        return accepted

    @staticmethod
    def _merge_channel_capability_sources(
        declared: List[ChannelCapabilities], legacy: List[ChannelCapabilities]
    ) -> List[ChannelCapabilities]:
        """合并声明式与旧式两条来源的渠道能力列表，同一渠道标识以声明式登记为准。

        渠道能力管理器按渠道标识做字典登记，同一标识后登记的条目覆盖先登记的，
        因此声明式条目排在旧式条目之后，才能在标识重合时保持声明式登记优先生效。

        :param declared: `provides_channel_capabilities()` 已通过契约校验的声明列表
        :param legacy: `get_channel_capabilities()` 声明的渠道能力列表
        :return: 合并后的渠道能力列表，旧式条目在前、声明式条目在后
        """
        if not declared:
            return legacy
        if not legacy:
            return declared
        return [*legacy, *declared]

    def channel_capabilities(
        self, pid: Optional[str] = None
    ) -> Dict[str, List[ChannelCapabilities]]:
        """投影启用插件声明的消息渠道能力。

        聚合两条来源：`provides_channel_capabilities()` 声明式登记（经契约校验）
        与 `get_channel_capabilities()` 裸列表（后者已进入废弃期，触达即告警
        一次）。同一渠道标识被两条来源同时声明时，声明式登记优先生效。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 实例键到其声明的 `ChannelCapabilities` 列表的映射
        """
        result: Dict[str, List[ChannelCapabilities]] = {}
        declared_by_instance = self.provided_channel_capabilities(pid)
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled():
                continue
            declared = declared_by_instance.get(extension_id, [])
            legacy = self._legacy_channel_capabilities(extension, extension_id, plugin)
            if not declared and not legacy:
                continue
            result[extension_id] = self._merge_channel_capability_sources(declared, legacy)
        return result

    def provided_service_instances(self, pid: Optional[str] = None) -> Dict[str, List[Any]]:
        """投影启用插件声明且通过登记契约校验的可配置服务实例类型。

        单条声明不合契约只跳过该条，既不影响同一实例的其余声明，也不影响其它
        实例；单个实例取声明时抛异常同理只跳过该实例。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 实例键到其服务实例声明列表的映射，仅含通过契约校验且在同插件
            多实例裁决中胜出的条目
        """
        result: Dict[str, List[Any]] = {}
        for extension in self._extensions(self._extension_scope(pid)):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled() or not extension.supports_hook(
                    "provides_service_instances"
            ):
                continue
            try:
                declared = plugin.provides_service_instances() or []
            except Exception as error:
                self._logger.error(
                    f"获取插件 {extension_id} 服务实例声明出错：{str(error)}"
                )
                continue
            # config_component 只在扩展渲染模式为 vue 时合法，默认 vuetify 与
            # get_render_mode() 基类实现的缺省值一致
            render_mode = "vuetify"
            if extension.supports_hook("get_render_mode"):
                render_mode, _ = plugin.get_render_mode()
            accepted: List[Any] = []
            for item in declared:
                violation = service_instance_declaration_violation(
                    item, render_mode=render_mode
                )
                if violation:
                    self._logger.error(
                        f"插件[{extension_id}]声明的服务实例 {item!r} 不合登记契约，"
                        f"已跳过：{violation}"
                    )
                    continue
                if declaration_config_schema(item) is None:
                    deprecation_warn(
                        SERVICE_INSTANCE_SCHEMA_DEPRECATION, context=extension_id
                    )
                accepted.append(item)
            result[extension_id] = accepted
        return self._narrow_to_query(
            elect_extension_scoped(
                result,
                _service_instance_identity,
                subject="服务实例类型",
                hook="provides_service_instances",
                log=self._logger,
            ),
            pid,
        )

    def service_instance_component_descriptor(
        self, extension_id: str, plugin: Any, component: str
    ) -> Dict[str, Any]:
        """构造服务实例 vue 模式配置界面的组件描述：组件名加所在联邦远程入口。

        调用方需自行保证 ``component`` 已通过登记契约校验、其声明方渲染模式
        确为 vue；本方法只负责组装，不重复校验。

        :param extension_id: 插件实例键
        :param plugin: 运行态插件实例
        :param component: 服务实例声明携带的组件名
        :return: 含 component 与 remote（联邦远程入口描述）的字典
        :raises RuntimeError: 联邦入口生成器尚未配置
        """
        _, dist_path = plugin.get_render_mode()
        return {
            "component": component,
            "remote": self._remote_descriptor(extension_id, plugin, dist_path),
        }

    def provided_meta_parsers(self, pid: Optional[str] = None) -> Dict[str, List[Any]]:
        """投影启用插件声明且通过登记契约校验的名称解析器。

        解析环绑在声明它的实例上：同一插件的两个分身各配一份模型、各声明一次，
        即两个各自成立的解析环，因此不按扩展标识去重。

        单条声明不合契约只跳过该条，既不影响同一实例的其余声明，也不影响其它
        实例；单个实例取声明时抛异常同理只跳过该实例。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 实例键到其名称解析器声明列表的映射，仅含通过契约校验的条目
        """
        result: Dict[str, List[Any]] = {}
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled() or not extension.supports_hook(
                    "provides_meta_parsers"
            ):
                continue
            try:
                declared = plugin.provides_meta_parsers() or []
            except Exception as error:
                self._logger.error(
                    f"获取插件 {extension_id} 名称解析器声明出错：{str(error)}"
                )
                continue
            accepted: List[Any] = []
            for item in declared:
                violation = meta_parser_declaration_violation(item)
                if violation:
                    self._logger.error(
                        f"插件[{extension_id}]声明的名称解析器 {item!r} 不合登记契约，"
                        f"已跳过：{violation}"
                    )
                    continue
                accepted.append(item)
            result[extension_id] = accepted
        return result

    def provided_agent_tools(self, pid: Optional[str] = None) -> Dict[str, List[Any]]:
        """投影启用插件声明且通过登记契约校验的智能体工具。

        单条声明不合契约只跳过该条，既不影响同一实例的其余声明，也不影响其它
        实例；单个实例取声明时抛异常同理只跳过该实例。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 实例键到其智能体工具声明列表的映射，仅含通过契约校验且在同插件
            多实例裁决中胜出的条目
        """
        result: Dict[str, List[Any]] = {}
        for extension in self._extensions(self._extension_scope(pid)):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled() or not extension.supports_hook(
                    "provides_agent_tools"
            ):
                continue
            try:
                declared = plugin.provides_agent_tools() or []
            except Exception as error:
                self._logger.error(
                    f"获取插件 {extension_id} 智能体工具声明出错：{str(error)}"
                )
                continue
            accepted: List[Any] = []
            for item in declared:
                violation = agent_tool_declaration_violation(item)
                if violation:
                    self._logger.error(
                        f"插件[{extension_id}]声明的智能体工具 {item!r} 不合登记契约，"
                        f"已跳过：{violation}"
                    )
                    continue
                accepted.append(item)
            result[extension_id] = accepted
        return self._narrow_to_query(
            elect_extension_scoped(
                result,
                _agent_tool_identity,
                subject="智能体工具名",
                hook="provides_agent_tools",
                log=self._logger,
            ),
            pid,
        )

    def provided_filter_rules(self, pid: Optional[str] = None) -> Dict[str, List[Any]]:
        """投影启用插件声明且通过登记契约校验的筛选规则。

        单条声明不合契约只跳过该条，既不影响同一实例的其余声明，也不影响其它
        实例；单个实例取声明时抛异常同理只跳过该实例。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 实例键到其筛选规则声明列表的映射，仅含通过契约校验且在同插件
            多实例裁决中胜出的条目
        """
        return self._collect_extension_scoped(
            pid,
            hook="provides_filter_rules",
            violation_of=filter_rule_declaration_violation,
            identity_of=_filter_rule_identity,
            subject="筛选规则",
        )

    def provided_filter_rule_groups(self, pid: Optional[str] = None) -> Dict[str, List[Any]]:
        """投影启用插件声明且通过登记契约校验的筛选规则组。

        单条声明不合契约只跳过该条，既不影响同一实例的其余声明，也不影响其它
        实例；单个实例取声明时抛异常同理只跳过该实例。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 实例键到其筛选规则组声明列表的映射，仅含通过契约校验且在同插件
            多实例裁决中胜出的条目
        """
        return self._collect_extension_scoped(
            pid,
            hook="provides_filter_rule_groups",
            violation_of=filter_rule_group_declaration_violation,
            identity_of=_filter_rule_group_identity,
            subject="筛选规则组",
        )

    def _collect_extension_scoped(
        self,
        pid: Optional[str],
        *,
        hook: str,
        violation_of: Callable[[Any], Optional[str]],
        identity_of: Callable[[Any], Optional[tuple]],
        subject: str,
        unique_within_instance: bool = False,
    ) -> Dict[str, List[Any]]:
        """收集一族扩展级声明：取用、契约校验、同插件多实例裁决。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :param hook: 声明钩子名
        :param violation_of: 单条声明的契约校验函数，合规时返回 None
        :param identity_of: 从单条声明推导扩展级标识的函数，推导不出时返回 None
        :param subject: 标识在日志文案里的称呼
        :param unique_within_instance: 为真时同一实例内重复声明同一标识只保留第一条，
            其余跳过并报错；标识在实例内本就是键的族须开启，否则后一条会静默盖掉前一条
        :return: 实例键到其声明列表的映射，仅含通过契约校验且在裁决中胜出的条目
        """
        result: Dict[str, List[Any]] = {}
        for extension in self._extensions(self._extension_scope(pid)):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled() or not extension.supports_hook(hook):
                continue
            try:
                declared = getattr(plugin, hook)() or []
            except Exception as error:
                self._logger.error(
                    f"获取插件 {extension_id} {subject}声明出错：{str(error)}"
                )
                continue
            accepted: List[Any] = []
            claimed: set = set()
            for item in declared:
                violation = violation_of(item)
                if violation:
                    self._logger.error(
                        f"插件[{extension_id}]声明的{subject} {item!r} 不合登记契约，"
                        f"已跳过：{violation}"
                    )
                    continue
                identity = identity_of(item) if unique_within_instance else None
                if identity is not None and identity in claimed:
                    self._logger.error(
                        f"插件[{extension_id}]在同一实例内重复声明了{subject} "
                        f"{'/'.join(str(part) for part in identity)}，"
                        f"该标识在实例内唯一，后一条声明已跳过"
                    )
                    continue
                if identity is not None:
                    claimed.add(identity)
                accepted.append(item)
            result[extension_id] = accepted
        return self._narrow_to_query(
            elect_extension_scoped(
                result, identity_of, subject=subject, hook=hook, log=self._logger
            ),
            pid,
        )

    @staticmethod
    def declared_filter_rule(item: Any) -> tuple:
        """把单条已通过契约校验的筛选规则声明投影为规则集条目。

        投影结果经 `CustomRule` 归一，形状与用户自定义规则完全一致：规则引擎
        （含 Rust 快路）按同一份数据形状消费，分辨不出规则来自插件还是用户。

        :param item: 已通过契约校验的筛选规则声明
        :return: (规则标识, 规则定义字典) 二元组
        """
        rule_id, name = declaration_filter_rule_identity(item)
        conditions = declaration_filter_rule_conditions(item)
        return rule_id, CustomRule(id=rule_id, name=name, **conditions).model_dump()

    @staticmethod
    def declared_filter_rule_group(item: Any) -> tuple:
        """把单条已通过契约校验的筛选规则组声明投影为规则组条目。

        投影结果经 `FilterRuleGroup` 归一，形状与用户配置的规则组完全一致。

        :param item: 已通过契约校验的筛选规则组声明
        :return: (规则组名, 规则组定义字典) 二元组
        """
        name, rule_string = declaration_filter_rule_group_identity(item)
        media_type, category = declaration_filter_rule_group_scope(item)
        return name, FilterRuleGroup(
            name=name,
            rule_string=rule_string,
            media_type=media_type,
            category=category,
        ).model_dump()

    def provided_dashboards(self, pid: Optional[str] = None) -> Dict[str, List[Any]]:
        """投影启用插件声明且通过登记契约校验的仪表盘。

        单条声明不合契约只跳过该条，既不影响同一实例的其余声明，也不影响其它
        实例；单个实例取声明时抛异常同理只跳过该实例。

        :param pid: 插件 ID 命中该插件全部实例，实例键只命中该实例，为空时命中全部
        :return: 实例键到其仪表盘声明列表的映射，仅含通过契约校验的条目
        """
        result: Dict[str, List[Any]] = {}
        for extension in self._extensions(pid):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.is_enabled() or not extension.supports_hook(
                    "provides_dashboards"
            ):
                continue
            try:
                declared = plugin.provides_dashboards() or []
            except Exception as error:
                self._logger.error(
                    f"获取插件 {extension_id} 仪表盘声明出错：{str(error)}"
                )
                continue
            # config_component 只在扩展渲染模式为 vue 时合法，默认 vuetify 与
            # get_render_mode() 基类实现的缺省值一致
            render_mode = "vuetify"
            if extension.supports_hook("get_render_mode"):
                render_mode, _ = plugin.get_render_mode()
            accepted: List[Any] = []
            for item in declared:
                violation = dashboard_declaration_violation(item, render_mode=render_mode)
                if violation:
                    self._logger.error(
                        f"插件[{extension_id}]声明的仪表盘 {item!r} 不合登记契约，"
                        f"已跳过：{violation}"
                    )
                    continue
                accepted.append(item)
            result[extension_id] = accepted
        return result

    def _declared_dashboard_metadata(
        self, extension_id: str, plugin: Any, item: Any
    ) -> Dict[str, Any]:
        """把单条已通过契约校验的仪表盘声明投影为元信息条目。

        vue 模式下声明了 config_component 时附带该组件所在的联邦远程入口描述，
        与存储、认证提供方两族的 vue 模式配置组件描述同一形状。

        声明了服务实例作用对象时附带该坐标，供前端渲染实例选择器；未声明时整个键
        都不出现，元信息与该字段存在之前逐键相同。

        :param extension_id: 插件实例键
        :param plugin: 运行态插件实例
        :param item: 已通过契约校验的仪表盘声明
        :return: 含 id、name、key、instance_id、instance_key 的元信息字典，
            vue 模式声明了 config_component 时另含 component 与 remote，声明了
            作用对象时另含 requires_service_instance
        """
        key, name = declaration_dashboard_identity(item)
        entry: Dict[str, Any] = {
            "id": extension_id,
            "name": name or plugin.plugin_name,
            "key": key or "",
            "instance_id": split_instance_key(extension_id)[1],
            "instance_key": extension_id,
        }
        requirement = projected_service_instance_requirement(
            declaration_service_instance_requirement(item)
        )
        if requirement is not None:
            entry["requires_service_instance"] = requirement
        component = declaration_config_component(item)
        if component:
            _, dist_path = plugin.get_render_mode()
            entry["component"] = component
            entry["remote"] = self._remote_descriptor(extension_id, plugin, dist_path)
        return entry

    def _legacy_dashboard_metadata(
        self, extension: PluginExtension, extension_id: str, plugin: Any
    ) -> Optional[List[Dict[str, Any]]]:
        """取用插件 `get_dashboard_meta()` 声明的仪表盘元信息，并按既有规则记录废弃告警。

        :param extension: 插件扩展视图
        :param extension_id: 插件实例键
        :param plugin: 插件运行实例
        :return: 元信息条目列表；未声明该钩子、废弃阶段已默认关闭或返回空值时
            为 None，调用方据此区分「无声明」与「声明为空列表」
        """
        if not extension.supports_hook("get_dashboard_meta"):
            return None
        # 废弃阶段推进到默认关闭后，该钩子整体不再生效，按未声明处理；标识列入
        # DEPRECATION_ENABLED 可临时恢复，用于观察真实依赖方
        if not deprecation_is_active("plugin.get_dashboard_meta"):
            return None
        plugin_metadata = plugin.get_dashboard_meta()
        if not plugin_metadata:
            return None
        deprecation_warn("plugin.get_dashboard_meta", context=extension_id)
        # id 沿用既有语义继续填实例键；这两个字段显式拆出实例标识
        # 与实例键，供需要区分同一插件多个实例的调用方使用。
        return [{
            "id": extension_id,
            "name": item.get("name"),
            "key": item.get("key"),
            "instance_id": split_instance_key(extension_id)[1],
            "instance_key": extension_id,
        } for item in plugin_metadata if item]

    def dashboard_metadata(self) -> List[Dict[str, str]]:
        """投影启用插件的单仪表板或多仪表板元信息。

        聚合两条来源：`provides_dashboards()` 声明式登记（经契约校验，vue 模式
        可附带组件描述）与 `get_dashboard_meta()` 裸元信息列表（后者已进入
        废弃期，触达即告警一次）；同一实例声明式登记优先，两者皆未声明时退化
        为单一默认仪表盘，与既有行为一致。
        """
        metadata = []
        declared_by_instance = self.provided_dashboards()
        for extension in self._extensions(None):
            extension_id, plugin = extension.extension_id, extension.instance
            if not extension.supports_hook("get_dashboard"):
                continue
            try:
                if not extension.is_enabled():
                    continue
                declared = declared_by_instance.get(extension_id, [])
                if declared:
                    metadata.extend(
                        self._declared_dashboard_metadata(extension_id, plugin, item)
                        for item in declared
                    )
                    continue
                legacy_metadata = self._legacy_dashboard_metadata(
                    extension, extension_id, plugin
                )
                if legacy_metadata is not None:
                    metadata.extend(legacy_metadata)
                    continue
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
