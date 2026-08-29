"""MoviePilot Server 应用服务的宿主组合根。"""

from app.adapters.external.server import (
    MoviePilotServerHelper,
    configure_server_application_services,
    reset_server_application_services,
)
from app.application.configuration import get_configured_system_config
from app.application.server.report import ServerReportService
from app.application.server.share import ServerSharingService
from app.application.subscription.contract import SubscriptionRepository
from app.application.workflow import WorkflowQueryService
from app.schemas.types import SystemConfigKey


def configure_server_services(
    workflow_query: WorkflowQueryService,
    subscription_repository: SubscriptionRepository,
) -> None:
    """构造并登记中心服务上报与分享用例，构造阶段不执行外部请求。"""
    configure_server_application_services(
        report_service=ServerReportService(
            config_reader=lambda key: get_configured_system_config().get(key),
            config_writer=lambda key, value: get_configured_system_config().set(key, value),
            async_config_writer=lambda key, value: get_configured_system_config().async_set(key, value),
            installed_plugins_provider=lambda: (
                get_configured_system_config().get(SystemConfigKey.UserInstalledPlugins) or []
            ),
            subscribes_provider=subscription_repository.list,
            async_subscribes_provider=subscription_repository.async_list,
            plugin_report_sender=MoviePilotServerHelper.plugin_install_report,
            async_plugin_report_sender=MoviePilotServerHelper.async_plugin_install_report,
            subscribe_report_sender=MoviePilotServerHelper.subscribe_report,
            async_subscribe_report_sender=MoviePilotServerHelper.async_subscribe_report,
            repo_url_sanitizer=MoviePilotServerHelper.sanitize_plugin_repo_url,
        ),
        sharing_service=ServerSharingService(
            subscribe_provider=subscription_repository.get,
            async_subscribe_provider=subscription_repository.async_get,
            workflow_provider=workflow_query.get_sync,
            async_workflow_provider=workflow_query.get,
            user_uuid_provider=MoviePilotServerHelper.get_user_uuid,
            subscribe_sender=MoviePilotServerHelper.subscribe_share,
            async_subscribe_sender=MoviePilotServerHelper.async_subscribe_share,
            workflow_sender=MoviePilotServerHelper.workflow_share,
            async_workflow_sender=MoviePilotServerHelper.async_workflow_share,
            response_handler=MoviePilotServerHelper._handle_response,
            subscribe_cache_clearer=MoviePilotServerHelper._clear_subscribe_share_cache,
            workflow_cache_clearer=MoviePilotServerHelper._clear_workflow_share_cache,
        ),
    )


def reset_server_services() -> None:
    """撤销当前 lifespan 发布的中心服务上报与分享用例。"""
    reset_server_application_services()
