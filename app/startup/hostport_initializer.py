"""扩展层宿主服务端口的组合根装配。

扩展经 ``app.runtime`` 下的端口取用目录、存储、命名、站点资源、规则组配置、
文件整理、自检诊断、工作流执行与候选种子分析。端口只保存 provider，注册本身不导入
应用服务实现；首次取用时才物化。
"""

from functools import lru_cache

from app.runtime.hostports.diagnostics import DiagnosticsProvider, diagnostics_port
from app.runtime.hostports.directories import DirectoryConfigProvider, directory_config_port
from app.runtime.hostports.filterrules import FilterRuleGroupProvider, filter_rule_group_port
from app.runtime.hostports.mediatransfer import MediaTransferProvider, media_transfer_port
from app.runtime.hostports.naming import NamingContextProvider, naming_context_port
from app.runtime.hostports.siteresource import SiteResourceProvider, site_resource_port
from app.runtime.hostports.storages import StorageConfigProvider, storage_config_port
from app.runtime.hostports.torrentanalysis import TorrentAnalysisProvider, torrent_analysis_port
from app.runtime.hostports.workflows import WorkflowExecutionProvider, workflow_execution_port


def _get_directory_config() -> DirectoryConfigProvider:
    """首个目录配置查询才导入目录服务。"""
    from app.application.directory import DirectoryHelper

    return DirectoryHelper()


def _get_storage_config() -> StorageConfigProvider:
    """首个存储配置读写才导入存储服务。"""
    from app.application.storage import StorageHelper

    return StorageHelper()


def _get_naming_context() -> NamingContextProvider:
    """首个命名上下文构建才导入模板服务。"""
    from app.application.messaging.message import NamingContextService

    return NamingContextService()


def _get_media_transfer() -> MediaTransferProvider:
    """首个文件整理请求才导入整理服务。"""
    from app.application.transferhandler import TransHandler

    return TransHandler()


def _get_site_resource() -> SiteResourceProvider:
    """首个站点资源查询才导入站点服务。"""
    from app.application.site.sites import SitesHelper  # pylint: disable=no-name-in-module

    return SitesHelper()


def _get_filter_rule_group() -> FilterRuleGroupProvider:
    """首个规则组查询才导入规则服务。"""
    from app.application.rules import RuleHelper

    return RuleHelper()


def _get_diagnostics() -> DiagnosticsProvider:
    """首个自检诊断查询才导入 doctor 扩展。"""
    import app.doctor as doctor

    return doctor


def _get_workflow_execution() -> WorkflowExecutionProvider:
    """首个工作流执行请求才导入 workflow 扩展。"""
    from app.workflow.service import WorkflowChain

    return WorkflowChain()


@lru_cache(maxsize=1)
def _get_torrent_analysis() -> TorrentAnalysisProvider:
    """首个候选分析请求才装配模块分发，之后复用同一个调度器。"""
    from app.application.orchestration.ports.dispatch import ModuleCapabilityDispatch
    from app.application.orchestration.ports.search import SearchPorts

    return SearchPorts(ModuleCapabilityDispatch())


def configure_host_ports() -> None:
    """
    为扩展注册目录、存储、命名、站点资源、规则组配置、文件整理、自检诊断与工作流执行端口实现。

    须先于模块加载阶段调用；注册只保存 provider，不导入应用服务实现。
    """
    directory_config_port.register(_get_directory_config)
    storage_config_port.register(_get_storage_config)
    naming_context_port.register(_get_naming_context)
    media_transfer_port.register(_get_media_transfer)
    site_resource_port.register(_get_site_resource)
    filter_rule_group_port.register(_get_filter_rule_group)
    diagnostics_port.register(_get_diagnostics)
    workflow_execution_port.register(_get_workflow_execution)


def configure_dispatch_host_ports() -> None:
    """
    为扩展注册需要模块分发的端口实现。

    这类端口在解析时会物化模块目录与插件目录，因此与只读宿主服务的端口分开注册，
    须在模块系统装配阶段调用；未注册时扩展只运行自身实现。
    """
    torrent_analysis_port.register(_get_torrent_analysis)
