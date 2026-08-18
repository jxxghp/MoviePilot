"""扩展层宿主服务端口的组合根装配。

扩展经 ``app.runtime`` 下的端口取用目录、存储、命名、站点资源与规则配置。
端口只保存 provider，注册本身不导入应用服务实现；首次取用时才物化。
"""

from app.runtime.directories import DirectoryConfigProvider, directory_config_port
from app.runtime.filterrules import FilterRuleGroupProvider, filter_rule_group_port
from app.runtime.naming import NamingContextProvider, naming_context_port
from app.runtime.ruleexpression import RuleExpressionProvider, rule_expression_port
from app.runtime.siteresource import SiteResourceProvider, site_resource_port
from app.runtime.storages import StorageConfigProvider, storage_config_port


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


def _get_site_resource() -> SiteResourceProvider:
    """首个站点资源查询才导入站点服务。"""
    from app.application.site.sites import SitesHelper  # pylint: disable=no-name-in-module

    return SitesHelper()


def _get_filter_rule_group() -> FilterRuleGroupProvider:
    """首个规则组查询才导入规则服务。"""
    from app.application.rules import RuleHelper

    return RuleHelper()


def _get_rule_expression() -> RuleExpressionProvider:
    """首个规则表达式解析才导入规则服务。"""
    from app.application.rules import RuleExpressionService

    return RuleExpressionService()


def configure_host_ports() -> None:
    """
    为扩展注册目录、存储、命名、站点资源与规则表达式端口实现。

    须先于模块加载阶段调用；注册只保存 provider，不导入应用服务实现。
    """
    directory_config_port.register(_get_directory_config)
    storage_config_port.register(_get_storage_config)
    naming_context_port.register(_get_naming_context)
    site_resource_port.register(_get_site_resource)
    filter_rule_group_port.register(_get_filter_rule_group)
    rule_expression_port.register(_get_rule_expression)
