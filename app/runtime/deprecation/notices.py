"""废弃登记与文案。

全仓所有「即将废弃」的对外文案集中在本模块的 ``NOTICES`` 里，调用点只引用稳定的
``key``，不各自拼写提示语。旧 Facade 的登记标识与 ``compat.facade.hit`` 指标的
``facade``/``operation`` 标签保持一致：整个 Facade 用 ``Facade``，单个方法用
``Facade.method``。
"""
from dataclasses import dataclass
from enum import IntEnum
from typing import Dict, Optional


class DeprecationStage(IntEnum):
    """废弃生命周期阶段，数值越大距离物理删除越近。"""

    # 仅登记：功能照常且不打扰用户，只靠指标观察真实用量
    SILENT = 0
    # 标记预警：功能照常，首次触达时输出一次告警
    WARN = 1
    # 默认停用：触达即报错，需把标识写进 DEPRECATION_ENABLED 才临时恢复
    DISABLED = 2
    # 彻底移除：实现已从代码中删除，触达即报错且无法恢复
    REMOVED = 3


@dataclass(frozen=True)
class DeprecationNotice:
    """
    单条废弃登记

    :param key: 稳定标识，用作开关配置项与告警去重的键
    :param subject: 被废弃的符号或能力
    :param stage: 当前所处阶段
    :param since: 开始废弃的版本
    :param replacement: 替代方案
    :param reason: 废弃原因
    :param remove_in: 计划物理删除的版本，未定时为 None
    """

    key: str
    subject: str
    stage: DeprecationStage
    since: str
    replacement: str
    reason: str
    remove_in: Optional[str] = None

    def message(self, context: Optional[str] = None) -> str:
        """
        构造面向调用方的提示语

        :param context: 触发来源，例如具体方法名或插件标识
        :return: 单行提示语
        """
        parts = [f"{self.subject} 自 {self.since} 起进入废弃流程"]
        parts.append(f"计划在 {self.remove_in} 移除" if self.remove_in else "移除版本待定")
        if context:
            parts.append(f"触发来源：{context}")
        parts.append(f"原因：{self.reason}")
        parts.append(f"请改用：{self.replacement}")
        if self.stage is DeprecationStage.DISABLED:
            parts.append(f"当前已默认停用，如需临时恢复请将 {self.key} 加入 DEPRECATION_ENABLED")
        elif self.stage is DeprecationStage.REMOVED:
            parts.append("当前已彻底移除，无法恢复")
        return "；".join(parts)


NOTICES: Dict[str, DeprecationNotice] = {
    notice.key: notice
    for notice in (
        DeprecationNotice(
            key="PluginManager._modify_plugin_files",
            subject="PluginManager._modify_plugin_files()",
            stage=DeprecationStage.SILENT,
            since="v3.0.0",
            replacement="get_plugin_system().package._modify_plugin_files()",
            reason="分身文件改写已由插件包适配器实现，此处只为旧内部调用保留转发",
        ),
        DeprecationNotice(
            key="PluginManager._modify_python_file",
            subject="PluginManager._modify_python_file()",
            stage=DeprecationStage.SILENT,
            since="v3.0.0",
            replacement="get_plugin_system().package._modify_python_file()",
            reason="Python 文件改写已由插件包适配器实现，此处只为旧内部调用保留转发",
        ),
        DeprecationNotice(
            key="PluginManager._modify_federation_files",
            subject="PluginManager._modify_federation_files()",
            stage=DeprecationStage.SILENT,
            since="v3.0.0",
            replacement="get_plugin_system().package._modify_federation_files()",
            reason="联邦文件改写已由插件包适配器实现，此处只为旧内部调用保留转发",
        ),
        DeprecationNotice(
            key="PluginManager._rename_federation_assets",
            subject="PluginManager._rename_federation_assets()",
            stage=DeprecationStage.SILENT,
            since="v3.0.0",
            replacement="get_plugin_system().package._rename_federation_assets()",
            reason="联邦资源重命名已由插件包适配器实现，此处只为旧内部调用保留转发",
        ),
        DeprecationNotice(
            key="PluginHelper.find_missing_dependencies",
            subject="PluginHelper.find_missing_dependencies()",
            stage=DeprecationStage.SILENT,
            since="v3.0.0",
            replacement="PluginDependencyInstaller.find_missing()",
            reason="依赖处理已拆分到独立依赖适配器，此处只为旧市场入口保留转发",
        ),
        DeprecationNotice(
            key="PluginHelper.install_dependencies",
            subject="PluginHelper.install_dependencies()",
            stage=DeprecationStage.SILENT,
            since="v3.0.0",
            replacement="PluginDependencyInstaller.install()",
            reason="依赖处理已拆分到独立依赖适配器，此处只为旧市场入口保留转发",
        ),
        DeprecationNotice(
            key="PluginHelper.async_find_missing_dependencies",
            subject="PluginHelper.async_find_missing_dependencies()",
            stage=DeprecationStage.SILENT,
            since="v3.0.0",
            replacement="PluginDependencyInstaller.async_find_missing()",
            reason="依赖处理已拆分到独立依赖适配器，此处只为旧异步市场入口保留转发",
        ),
        DeprecationNotice(
            key="PluginHelper.async_install_dependencies",
            subject="PluginHelper.async_install_dependencies()",
            stage=DeprecationStage.SILENT,
            since="v3.0.0",
            replacement="PluginDependencyInstaller.async_install()",
            reason="依赖处理已拆分到独立依赖适配器，此处只为旧异步市场入口保留转发",
        ),
        DeprecationNotice(
            key="SystemUtils.is_bluray_dir",
            subject="SystemUtils.is_bluray_dir()",
            stage=DeprecationStage.WARN,
            since="v3.0.0",
            replacement="StorageChain().is_bluray_folder()",
            reason="只按本地路径判断蓝光目录，无法覆盖非本地存储",
        ),
    )
}
