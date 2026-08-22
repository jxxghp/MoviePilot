"""FastAPI 依赖兼容聚合入口。

新代码按领域从 ``app.api.dependencies`` 导入；本模块保留全部历史名字，避免端点、测试和
旧 SDK 在依赖拆分期间同步改动导入路径。
"""

from app.api.dependencies.agent import (
    get_agent_chat_service,
    get_message_query_service,
)
from app.api.dependencies.auth import (
    get_auth_service,
    get_current_active_manage_user,
    get_current_active_manage_user_async,
    get_current_active_superuser,
    get_current_active_superuser_async,
    get_current_active_user,
    get_current_active_user_async,
    get_current_user,
    get_current_user_async,
    get_passkey_service,
    get_user_service,
)
from app.api.dependencies.history import (
    get_dashboard_query_service,
    get_download_history_mutation_command,
    get_history_query_service,
    get_mediaserver_query_service,
    get_transfer_history_lookup_service,
    get_transfer_history_mutation_command,
)
from app.api.dependencies.plugin import get_plugin_config_command
from app.api.dependencies.site import (
    get_site_mutation_command,
    get_site_query_service,
    get_site_sync_query_service,
)
from app.api.dependencies.subscription import (
    get_delete_subscribe_command,
    get_delete_subscriptions_by_identity_command,
    get_search_subscriptions_command,
    get_servarr_subscription_service,
    get_subscription_mutation_service,
    get_subscription_query_service,
    get_subscription_sync_mutation_service,
)
from app.api.dependencies.workflow import (
    get_workflow_definition_command,
    get_workflow_mutation_command,
    get_workflow_query_service,
)

# 兼容聚合入口只显式列出既有 FastAPI 依赖，不向插件制造新的动态导出规则。
__all__ = [
    "get_agent_chat_service",
    "get_auth_service",
    "get_current_active_manage_user",
    "get_current_active_manage_user_async",
    "get_current_active_superuser",
    "get_current_active_superuser_async",
    "get_current_active_user",
    "get_current_active_user_async",
    "get_current_user",
    "get_current_user_async",
    "get_dashboard_query_service",
    "get_delete_subscribe_command",
    "get_delete_subscriptions_by_identity_command",
    "get_download_history_mutation_command",
    "get_history_query_service",
    "get_mediaserver_query_service",
    "get_message_query_service",
    "get_passkey_service",
    "get_plugin_config_command",
    "get_search_subscriptions_command",
    "get_servarr_subscription_service",
    "get_site_mutation_command",
    "get_site_query_service",
    "get_site_sync_query_service",
    "get_subscription_mutation_service",
    "get_subscription_query_service",
    "get_subscription_sync_mutation_service",
    "get_transfer_history_lookup_service",
    "get_transfer_history_mutation_command",
    "get_user_service",
    "get_workflow_definition_command",
    "get_workflow_mutation_command",
    "get_workflow_query_service",
]
