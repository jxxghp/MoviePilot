"""服务实例健康探针的工具实现类。

探针只调宿主必填集里那个**只读**的方法。下载器与媒体服务器两族的必填集是
``is_inactive`` 与 ``reconnect``，后者会重建连接，读状态的工具不该顺手改状态；消息通知
族的 ``get_state`` 本身就是只读的。哪个方法可读由必填集与本模块的只读方法表取交集判定，
交集不是恰好一个时本族只报配置不报状态——一族出现两个可读探针意味着口径不止一种，挑一个
用即是替宿主裁决。

实例状态不带主机地址、账号与令牌：这些是配置里的凭据，读状态用不着它们，而工具的返回值
会整段进入模型上下文并可能被复述给用户。因此本工具不要求管理员权限。
"""

from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Type

from pydantic import BaseModel, Field

from app.sdk.agent import MoviePilotTool, ToolTag
from app.sdk.logging import logger
from app.sdk.service_instances import (
    service_capabilities,
    service_instance_required_methods,
)
from app.sdk.services import DownloaderHelper, MediaServerHelper, NotificationHelper

# 本工具覆盖的服务族及其实例发现入口。键是能力标签，取值须是宿主服务族登记表里的族。
# 只收必填契约里带只读状态方法的族：存储族的形状按 StorageBase 的继承判定、不进必填集，
# 登录认证族宿主对实例一个方法都不调，两族按本工具的契约驱动口径都找不到探针
FAMILY_HELPERS: Mapping[str, Callable[[], Any]] = MappingProxyType({
    "downloader": DownloaderHelper,
    "mediaserver": MediaServerHelper,
    "notification": NotificationHelper,
})

# 可用作只读探针的实例方法，及其返回 True/False 时对应的状态文案
READONLY_PROBES: Mapping[str, Tuple[str, str]] = MappingProxyType({
    "is_inactive": ("需要重连", "在线"),
    "get_state": ("就绪", "未就绪"),
})

# 配置存在但用户已停用时的状态文案，此时宿主不会构造实例，探不到也不该探
STATE_DISABLED = "未启用"

# 配置已启用却在实例登记里找不到对应实例时的状态文案，通常是所属模块尚未装载
STATE_ABSENT = "未装载"

# 本族没有恰好一个只读探针时的状态文案
STATE_NO_PROBE = "该族无只读探针"

# 探针本身抛异常时的状态文案，具体异常进日志不进返回值
STATE_PROBE_FAILED = "探测失败"

# 整族取不出实例时的文案。异常文本同样不进返回值：配置读取路径上的异常常带着主机地址，
# 而返回值会整段进入模型上下文
FAMILY_QUERY_FAILED = "查询失败，详见日志"

# 工具名与工具描述。两者同时是声明数据与实现类的字段默认值，因此提到模块级各写一份，
# 避免声明与实现各自维护一份说法。描述用英文，与内建工具一致——它进的是模型的工具清单，
# 与界面文案不是同一类文字
TOOL_NAME = "query_service_instance_health"
TOOL_DESCRIPTION = (
    "Check whether the configured downloader, media server and notification "
    "instances are currently reachable. Returns each instance name, type, enabled "
    "flag and live state, without any host address or credential value."
)


class ServiceInstanceHealthInput(BaseModel):
    """服务实例健康查询的输入参数模型。"""

    capability: Optional[str] = Field(
        default=None,
        description=(
            "Service family capability tag to inspect, such as downloader, mediaserver "
            "or notification. Omit it to inspect every supported family."
        ),
    )


class ServiceInstanceHealthTool(MoviePilotTool):
    """查询已配置服务实例的在线状态。"""

    name: str = TOOL_NAME
    # ``ToolTag.Read`` 不是可省的装饰：只读子代理按它筛选可用工具，不带该标签的工具
    # 在只读场景里一次都不会被选中
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.System,
    ]
    description: str = TOOL_DESCRIPTION
    args_schema: Type[BaseModel] = ServiceInstanceHealthInput
    # 返回值不含凭据，普通用户也能用来判断「我的下载器是不是掉线了」
    require_admin: bool = False

    def get_tool_message(self, **kwargs: Any) -> Optional[str]:
        """
        生成工具执行时的提示消息

        :param kwargs: 工具参数
        :return: 面向用户的提示消息
        """
        capability = (kwargs.get("capability") or "").strip()
        return f"查询 {capability} 服务实例状态" if capability else "查询服务实例状态"

    async def run(self, **kwargs: Any) -> str:
        """
        逐族收集服务实例的配置与在线状态

        实例发现与状态探测都会读数据库并可能连外部服务，整段放进宿主的受控线程池，按族
        分桶避免一族的慢 IO 拖住其它族。

        :param kwargs: 工具参数，``capability`` 给定时只查该族
        :return: 各族实例状态的可读文本
        """
        logger.info(f"执行工具: {self.name}")
        requested = (kwargs.get("capability") or "").strip()
        registered = service_capabilities()
        if requested and requested not in registered:
            return (
                f"宿主未登记名为 {requested} 的服务族，"
                f"当前可查的族为 {sorted(FAMILY_HELPERS)}"
            )
        if requested and requested not in FAMILY_HELPERS:
            return (
                f"{registered[requested]}（{requested}）族的必填契约里没有只读状态方法，"
                f"本工具探不了；当前可查的族为 {sorted(FAMILY_HELPERS)}"
            )

        targets = [requested] if requested else sorted(FAMILY_HELPERS)
        sections: List[str] = []
        for capability in targets:
            display_name = registered.get(capability, capability)
            try:
                instances = await self.run_blocking(
                    capability, self._collect_family, capability
                )
            except Exception as error:
                logger.error(f"查询 {capability} 族服务实例状态失败: {error}")
                sections.append(f"【{display_name}】{FAMILY_QUERY_FAILED}")
                continue
            sections.append(self._render_family(display_name, instances))
        return "\n\n".join(sections)

    @staticmethod
    def _collect_family(capability: str) -> List[Dict[str, Any]]:
        """
        收集一族的全部实例配置及其状态

        以配置为准而不是以已装载的实例为准：用户配了却没装载出来，正是他要问的那种情况，
        只列已装载的实例会让这条恰好消失。

        :param capability: 能力标签
        :return: 每个实例的名称、类型、启用态与状态
        """
        helper = FAMILY_HELPERS[capability]()
        configs = helper.get_configs(include_disabled=True)
        services = helper.get_services()
        probe = ServiceInstanceHealthTool._probe_method(capability)
        collected: List[Dict[str, Any]] = []
        for name, config in sorted(configs.items()):
            service = services.get(name)
            collected.append({
                "name": name,
                "type": config.type,
                "enabled": bool(config.enabled),
                "state": ServiceInstanceHealthTool._instance_state(
                    config, service, probe
                ),
            })
        return collected

    @staticmethod
    def _probe_method(capability: str) -> Optional[str]:
        """
        挑出本族必填集里可以只读判定状态的那个方法

        必填集由宿主给出，只读方法表由本模块给出，取交集恰好一个才用它：零个说明本族没有
        可读状态，多于一个说明口径不止一种，两种情况都不猜。

        :param capability: 能力标签
        :return: 只读探针方法名；交集不是恰好一个时为 None
        """
        candidates = [
            method
            for method in service_instance_required_methods(capability)
            if method in READONLY_PROBES
        ]
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _instance_state(config: Any, service: Any, probe: Optional[str]) -> str:
        """
        判定单个实例当前的状态

        :param config: 该实例的配置
        :param service: 该实例在宿主实例登记里的服务信息；未装载时为 None
        :param probe: 本族的只读探针方法名；本族没有探针时为 None
        :return: 面向用户的状态文案
        """
        if not config.enabled:
            return STATE_DISABLED
        if probe is None:
            return STATE_NO_PROBE
        instance = getattr(service, "instance", None)
        if instance is None:
            return STATE_ABSENT
        truthy, falsy = READONLY_PROBES[probe]
        try:
            return truthy if getattr(instance, probe)() else falsy
        except Exception as error:
            logger.warning(f"探测服务实例 {config.name} 状态失败: {error}")
            return STATE_PROBE_FAILED

    @staticmethod
    def _render_family(display_name: str, instances: List[Dict[str, Any]]) -> str:
        """
        把一族的实例状态渲染为可读文本

        返回值整段进入模型上下文，因此只写用户问得到答案所需的那几项：必填方法名之类的
        宿主内部约定不进来，模型转述它只会把话题带偏。

        :param display_name: 族的展示名称
        :param instances: 该族的实例状态列表
        :return: 该族的一段文本
        """
        if not instances:
            return f"【{display_name}】未配置任何实例"
        lines = [
            f"- {item['name']}（{item['type']}）：{item['state']}"
            for item in instances
        ]
        return "\n".join([f"【{display_name}】共 {len(instances)} 个实例", *lines])
