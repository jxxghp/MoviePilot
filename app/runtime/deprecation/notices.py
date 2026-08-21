"""废弃登记与文案。

全仓所有「即将废弃」的对外文案集中在本模块的 ``NOTICES`` 里，调用点只引用稳定的
``key``，不各自拼写提示语。
"""
from dataclasses import dataclass
from enum import IntEnum
from typing import Mapping, Optional


class DeprecationStage(IntEnum):
    """废弃生命周期阶段，数值越大距离物理删除越近。"""

    # 标记预警：功能完全照常，仅在首次触达时输出告警
    WARN = 1
    # 默认关闭：功能默认不再生效，需在 DEPRECATION_ENABLED 中显式列出才恢复，用于观察真实依赖方
    DISABLED = 2
    # 彻底删除：实现已从代码中移除，触达即报错并给出迁移指引
    REMOVED = 3


@dataclass(frozen=True)
class DeprecationNotice:
    """
    单条废弃登记

    :param key: 稳定标识，用作开关配置项与告警去重的键
    :param subject: 被废弃的符号或能力
    :param stage: 当前所处阶段
    :param since: 开始废弃的版本
    :param remove_in: 计划物理删除的版本
    :param replacement: 替代方案
    :param reason: 废弃原因
    """

    key: str
    subject: str
    stage: DeprecationStage
    since: str
    remove_in: str
    replacement: str
    reason: str

    def message(self, context: Optional[str] = None) -> str:
        """
        构造面向调用方的提示语

        :param context: 触发来源，例如插件标识
        :return: 单行提示语
        """
        parts = [f"{self.subject} 已于 {self.since} 废弃，计划在 {self.remove_in} 移除"]
        if context:
            parts.append(f"触发来源：{context}")
        parts.append(f"原因：{self.reason}")
        parts.append(f"请改用：{self.replacement}")
        if self.stage is DeprecationStage.DISABLED:
            parts.append(f"当前已默认停用，如需临时恢复请将 {self.key} 加入 DEPRECATION_ENABLED")
        elif self.stage is DeprecationStage.REMOVED:
            parts.append("当前已彻底移除，无法恢复")
        return "；".join(parts)


NOTICES: Mapping[str, DeprecationNotice] = {
    "plugin.get_module": DeprecationNotice(
        key="plugin.get_module",
        subject="_PluginBase.get_module()",
        stage=DeprecationStage.WARN,
        since="v3.1.0",
        remove_in="v3.3.0",
        replacement="provides_modules() 等 provides_* 声明式注册钩子",
        reason="按方法名胁持内建模块实现，无契约校验、无归属记账、卸载不可回收",
    ),
    "plugin.get_agent_tools": DeprecationNotice(
        key="plugin.get_agent_tools",
        subject="_PluginBase.get_agent_tools()",
        stage=DeprecationStage.WARN,
        since="v3.1.0",
        remove_in="v3.3.0",
        replacement="provides_agent_tools()",
        reason="返回裸工具类列表，无契约校验、无归属记账、卸载不可回收",
    ),
    "plugin.get_command": DeprecationNotice(
        key="plugin.get_command",
        subject="_PluginBase.get_command()",
        stage=DeprecationStage.WARN,
        since="v3.1.0",
        remove_in="v3.3.0",
        replacement="provides_commands()",
        reason="返回裸描述字典列表，命令词不经文法校验，不合文法的命令词要到用户敲它或"
               "渠道菜单整批注册失败时才暴露；重名只能按加载顺序取其一",
    ),
    "plugin.get_channel_capabilities": DeprecationNotice(
        key="plugin.get_channel_capabilities",
        subject="_PluginBase.get_channel_capabilities()",
        stage=DeprecationStage.WARN,
        since="v3.1.0",
        remove_in="v3.3.0",
        replacement="provides_channel_capabilities()",
        reason="返回裸能力列表，无契约校验，标识缺失或能力集合形状不合法只能悄悄跳过",
    ),
    "plugin.get_auth_providers": DeprecationNotice(
        key="plugin.get_auth_providers",
        subject="_PluginBase.get_auth_providers()",
        stage=DeprecationStage.WARN,
        since="v3.1.0",
        remove_in="v3.3.0",
        replacement='provides_service_instances() 声明 capability="auth" 的登录入口类型',
        reason="登录入口绑在分身上，接第二台服务器要再建一个分身，而那本该是同一个入口类型的第二份配置",
    ),
    "plugin.clone_by_source_copy": DeprecationNotice(
        key="plugin.clone_by_source_copy",
        subject="以复制源码目录方式创建插件分身",
        stage=DeprecationStage.WARN,
        since="v3.1.0",
        remove_in="v3.3.0",
        replacement="插件实例分身，同一插件类按 instance_id 运行多个实例",
        reason="复制目录并改写类名会产生磁盘副本与重复模块，且分身无法随原插件升级",
    ),
    "plugin.get_media_source": DeprecationNotice(
        key="plugin.get_media_source",
        subject="_PluginBase.get_media_source()",
        stage=DeprecationStage.WARN,
        since="v3.1.0",
        remove_in="v3.3.0",
        replacement="provides_media_sources()",
        reason="返回裸描述字典列表，无契约校验、无归属记账、卸载不可回收",
    ),
    "plugin.get_actions": DeprecationNotice(
        key="plugin.get_actions",
        subject="_PluginBase.get_actions()",
        stage=DeprecationStage.WARN,
        since="v3.1.0",
        remove_in="v3.3.0",
        replacement="provides_actions()",
        reason="返回裸描述字典列表，无契约校验、无归属记账、卸载不可回收",
    ),
    "plugin.service_instance_without_config_schema": DeprecationNotice(
        key="plugin.service_instance_without_config_schema",
        subject="不带 config_schema 的服务实例类型声明",
        stage=DeprecationStage.WARN,
        since="v3.1.0",
        remove_in="v3.3.0",
        replacement="在 ServiceInstanceDeclaration 上给出 config_schema",
        reason="没有配置契约时宿主判定不了该类型的配置形状，畸形配置只能存下去、"
               "留到构造实例时才失败",
    ),
    "plugin.get_service": DeprecationNotice(
        key="plugin.get_service",
        subject="_PluginBase.get_service()",
        stage=DeprecationStage.WARN,
        since="v3.1.0",
        remove_in="v3.3.0",
        replacement="provides_schedules()",
        reason="交出的是活的触发器对象与方法对象，过不了进程边界，且调度表达式写错"
               "要等到该任务本该触发的那一刻才失败",
    ),
    "plugin.get_dashboard_meta": DeprecationNotice(
        key="plugin.get_dashboard_meta",
        subject="_PluginBase.get_dashboard_meta()",
        stage=DeprecationStage.WARN,
        since="v3.1.0",
        remove_in="v3.3.0",
        replacement="provides_dashboards()",
        reason="返回裸元信息列表，无契约校验，且无法声明专属配置界面",
    ),
}
