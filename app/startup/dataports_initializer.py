"""应用数据端口的启动组合根。

api / application / agent / workflow 各层只声明持久化端口，具体 Oper 由本模块选定并登记。
装配只保存工厂或服务实例，不触发数据库访问；端口未登记时取用方一律抛出运行时错误，
因此本模块必须在数据库引擎就绪后、路由开始接收请求前执行。
"""

from __future__ import annotations

from app.api.data import configure_api_data_ports
from app.application.agentdata import configure_agent_data_ports
from app.application.configuration import (
    SystemConfigService,
    configure_system_config,
)
from app.application.history import configure_transfer_history_provider
from app.application.messaging.chat import (
    AgentChatService,
    configure_agent_chat_service,
)
from app.application.orchestration.data import configure_chain_data_ports
from app.application.security.auth import (
    AuthService,
    configure_auth_identity_ports,
    configure_auth_service,
)
from app.application.security.user import configure_user_lookups
from app.application.security.userconfig import (
    UserConfigurationService,
    configure_user_configuration,
)
from app.application.site.query import SiteQueryService, configure_site_query_service
from app.application.subscription.write import configure_subscribe_writer
from app.db.oper.agentchat import AgentChatOper
from app.db.oper.agenttask import AgentTaskOper
from app.db.oper.downloadfailure import DownloadFailureOper
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.mediaserver import MediaServerOper
from app.db.oper.message import MessageOper
from app.db.oper.passkey import PassKeyOper
from app.db.oper.plugindata import PluginDataOper
from app.db.oper.site import SiteOper
from app.db.oper.subscribe import SubscribeOper
from app.db.oper.subscribehistory import SubscribeHistoryOper
from app.db.oper.systemconfig import SystemConfigOper
from app.db.oper.transferhistory import TransferHistoryOper
from app.db.oper.transferpending import TransferPendingOper
from app.db.oper.user import UserOper
from app.db.oper.user_identity import UserIdentityOper
from app.db.oper.userconfig import UserConfigOper
from app.db.oper.workflow import WorkflowOper
from app.db.session import get_async_db, get_db
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork


def configure_request_data_ports() -> None:
    """登记 API 请求级会话、仓储与事务端口，供 ``app.api.deps`` 按能力名取用。"""
    configure_api_data_ports(
        sync_session=get_db,
        async_session=get_async_db,
        repositories={
            "agent_chat": AgentChatOper,
            "download_history": DownloadHistoryOper,
            "media_server": MediaServerOper,
            "message": MessageOper,
            "passkey": PassKeyOper,
            "site": SiteOper,
            "subscribe": SubscribeOper,
            "subscribe_history": SubscribeHistoryOper,
            "transfer_history": TransferHistoryOper,
            "user": UserOper,
            "user_identity": UserIdentityOper,
            "workflow": WorkflowOper,
        },
        standalone={
            "passkey": PassKeyOper,
            "system_config": SystemConfigOper,
            "user": UserOper,
            "user_identity": UserIdentityOper,
        },
        unit_of_work={
            "async": SqlAlchemyAsyncUnitOfWork,
            "sync": SqlAlchemyUnitOfWork,
        },
    )


def configure_orchestration_data_ports() -> None:
    """登记编排、工作流、监控与 Agent 工具共用的持久化端口。"""
    configure_chain_data_ports(
        site=SiteOper,
        subscribe=SubscribeOper,
        workflow=WorkflowOper,
        download_history=DownloadHistoryOper,
        transfer_history=TransferHistoryOper,
        transfer_pending=TransferPendingOper,
        media_server=MediaServerOper,
        download_failure=DownloadFailureOper,
        user=UserOper,
    )
    configure_agent_data_ports(
        agent_chat=AgentChatOper,
        agent_task=AgentTaskOper,
        user=UserOper,
        site=SiteOper,
        subscribe=SubscribeOper,
        subscribe_history=SubscribeHistoryOper,
        transfer_history=TransferHistoryOper,
        download_history=DownloadHistoryOper,
        workflow=WorkflowOper,
        plugin_data=PluginDataOper,
    )
    configure_subscribe_writer(SubscribeOper)
    configure_transfer_history_provider(TransferHistoryOper)


def configure_application_service_ports() -> None:
    """登记跨请求复用的单例应用服务，供无请求会话的调用方取用。"""
    users = UserOper()
    configure_system_config(SystemConfigService(repository=SystemConfigOper()))
    configure_site_query_service(SiteQueryService(repository=SiteOper()))
    configure_agent_chat_service(AgentChatService(repository=AgentChatOper()))
    configure_user_configuration(
        UserConfigurationService(repository=UserConfigOper())
    )
    configure_user_lookups(
        by_id=users.get_by_id,
        by_name=users.get_by_name,
        by_channel=users.get_name,
    )
    configure_auth_service(
        AuthService(
            users=UserOper(),
            config=SystemConfigOper(),
            passkeys=PassKeyOper(),
        )
    )
    configure_auth_identity_ports(
        identities=UserIdentityOper(),
        provisioning=UserOper(),
    )


def configure_data_ports() -> None:
    """登记全部应用数据端口，使各层在启动完成后按端口而非按 Oper 取用持久化能力。"""
    configure_request_data_ports()
    configure_orchestration_data_ports()
    configure_application_service_ports()
