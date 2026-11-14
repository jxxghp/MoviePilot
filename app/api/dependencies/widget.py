"""
小组件只读授权的专用 Header 依赖与既有业务查询装配。
"""

from functools import partial
from typing import NoReturn

from fastapi import Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.context import get_api_runtime_config, get_host_runtime
from app.api.dependencies.dashboard import sample_downloader, sample_system
from app.api.dependencies.history import get_history_query_service
from app.application.configuration import ApiRuntimeConfig
from app.application.history import HistoryQueryService
from app.application.query import get_configured_data_query_service
from app.application.security.widget import WidgetAccessError, WidgetAuthService, WidgetGrant
from app.application.widget import WidgetSnapshotService
from app.chain.download import DownloadChain
from app.startup.composition.context import HostRuntime

WIDGET_CACHE_HEADERS = {"Cache-Control": "no-store, private", "Pragma": "no-cache", "Vary": "Authorization"}
_bearer = HTTPBearer(
    scheme_name="WidgetBearer", description="仅限小组件的受限 Bearer，不接受主登录令牌", auto_error=False
)


def widget_http_error(error: WidgetAccessError) -> NoReturn:
    """
    固定业务错误转换为无缓存 HTTP 错误，不包含凭据或内部异常。

    :param error: 专用授权服务的公开失败原因
    :raises HTTPException: 当前鉴权或登记操作失败
    """
    headers = dict(WIDGET_CACHE_HEADERS)
    if error.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    raise HTTPException(error.status, str(error), headers=headers) from error


def get_widget_auth_service(runtime: HostRuntime = Depends(get_host_runtime)) -> WidgetAuthService:
    """
    从当前 lifespan 获取短事务工厂，不创建 Session 或备用认证系统。

    :param runtime: 已由启动组合根装配的宿主运行时
    :return: 不跨线程保留会话的授权用例
    """
    scope = runtime.authentication.widget_scope
    if scope is None:
        raise HTTPException(503, "小组件服务尚未就绪", headers=WIDGET_CACHE_HEADERS)
    return WidgetAuthService(scope)


def get_widget_grant(
    response: Response,
    credential: HTTPAuthorizationCredentials | None = Depends(_bearer),
    service: WidgetAuthService = Depends(get_widget_auth_service),
) -> WidgetGrant:
    """
    专用凭据只从 Authorization Header 读取，不回退 Cookie、查询参数或 API Key。

    :param response: 为成功响应声明禁止共享缓存
    :param credential: HTTP Bearer 依赖提供的 Header 凭据
    :param service: 当前宿主授权服务
    :return: 已重新核验用户和权限的只读授权
    """
    response.headers.update(WIDGET_CACHE_HEADERS)
    try:
        return service.authenticate(credential.credentials if credential else "")
    except WidgetAccessError as error:
        widget_http_error(error)


def get_widget_snapshot_service(
    history: HistoryQueryService = Depends(get_history_query_service),
    runtime_config: ApiRuntimeConfig = Depends(get_api_runtime_config),
) -> WidgetSnapshotService:
    """
    只连接现有的查询与业务链，普通页面不会调用小组件专用接口。

    :param history: 原历史列表查询服务
    :param runtime_config: 本次请求的空间去重配置，不从组件凭据或参数读取
    :return: 使用原有 owner、计数和成功筛选的快照投影服务
    """
    return WidgetSnapshotService(
        data=get_configured_data_query_service(),
        history=history,
        system=sample_system,
        downloads=partial(DownloadChain().downloading, require_source=True),
        downloader=partial(sample_downloader, btrfs_fsid_dedup=runtime_config.btrfs_fsid_dedup, require_source=True),
    )
