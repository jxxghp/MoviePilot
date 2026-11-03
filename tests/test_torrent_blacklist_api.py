"""
黑名单管理员 REST、异步历史查询和 Agent 固定路由契约测试。
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.policy.api import API_OPERATION_BY_ID, API_OPERATION_ROUTES
from app.agent.policy.contracts import ActionEffect, PrincipalRole, RecoveryMode
from app.agent.tools.impl.api import MoviePilotApiTool
from app.api.dependencies.auth import get_current_active_user_async
from app.api.dependencies.history import get_history_query_service
from app.api.endpoints import blacklist
from app.application.configuration import get_configured_system_config
from app.application.history import DownloadHistoryWrite, HistoryQueryService
from app.db.adapters.history.download import SessionDownloadHistoryRepository, TransactionalDownloadHistoryRepository
from app.db.models.downloadhistory import DownloadHistory
from app.db.session import SessionFactory, async_session_scope
from app.schemas.history import DownloadHistory as HistorySchema
from app.schemas.types import SystemConfigKey


@pytest.fixture
def api():
    """
    使用真实异步管理员依赖与配置服务，只替换可信登录身份和历史查询。
    """
    config = get_configured_system_config()
    previous = config.get(SystemConfigKey.TorrentBlacklist)
    config.set(SystemConfigKey.TorrentBlacklist, None)
    app = FastAPI()
    app.include_router(blacklist.router, prefix="/torrent/blacklist")
    user = SimpleNamespace(is_superuser=True)
    history = AsyncMock()
    history.get_download.return_value = HistorySchema(id=7, download_hash="ab" * 20)
    app.dependency_overrides[get_current_active_user_async] = lambda: user
    app.dependency_overrides[get_history_query_service] = lambda: history
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, user=user, history=history, config=config)
    finally:
        config.set(SystemConfigKey.TorrentBlacklist, previous)


def test_admin_management_envelopes_and_idempotency(api):
    """
    新增、分页和删除返回类型化信封，重复写入为幂等操作。
    """
    response = api.client.post("/torrent/blacklist", json={"keyword": " Bad "})
    assert response.status_code == 200
    created = response.json()
    assert created["success"] is True
    assert created["data"]["created"] is True
    entry = created["data"]["entry"]
    repeated = api.client.post("/torrent/blacklist", json={"keyword": "bad"}).json()
    assert repeated["data"]["created"] is False
    assert repeated["data"]["entry"] == entry
    page = api.client.get("/torrent/blacklist", params={"page": 1, "count": 1}).json()["data"]
    assert page["items"] == [entry]
    assert page["total"] == 1
    for removed in (True, False):
        response = api.client.delete(f"/torrent/blacklist/{entry['id']}")
        assert response.status_code == 200
        assert response.json()["data"]["removed"] is removed


@pytest.mark.parametrize("method,path,body", [
    ("get", "/torrent/blacklist", None),
    ("post", "/torrent/blacklist", {"keyword": "bad"}),
    ("post", "/torrent/blacklist/history/7", None),
    ("delete", "/torrent/blacklist/" + "a" * 32, None),
])
def test_all_management_routes_require_active_superuser(api, method, path, body):
    """
    普通登录用户不能读取或更改全局黑名单。
    """
    api.user.is_superuser = False
    response = api.client.request(method, path, json=body)
    assert response.status_code == 403
    assert api.config.get(SystemConfigKey.TorrentBlacklist) is None
    api.history.get_download.assert_not_awaited()


@pytest.mark.parametrize("query", [{"page": 0}, {"count": 0}, {"count": 101}])
def test_pagination_bounds(api, query):
    """
    分页边界由接口校验，禁止负页或无界读取。
    """
    assert api.client.get("/torrent/blacklist", params=query).status_code == 422


def test_history_one_click_is_server_side_and_refuses_missing_identity(api):
    """
    一键屏蔽只接受历史 ID，不信任客户端种子字段，也不以标题猜测身份。
    """
    response = api.client.post("/torrent/blacklist/history/7")
    assert response.status_code == 200
    assert response.json()["data"]["entry"]["identity"]["hashes"] == ["ab" * 20]
    api.history.get_download.assert_awaited_once_with(7)
    api.history.get_download.return_value = None
    assert api.client.post("/torrent/blacklist/history/999").status_code == 404
    api.history.get_download.return_value = HistorySchema(id=8, torrent_name="title", download_hash="invalid")
    response = api.client.post("/torrent/blacklist/history/8")
    assert response.status_code == 422
    assert "手动" in str(response.json())


def test_invalid_request_and_corrupt_persisted_rules_are_visible(api):
    """
    用户输入不能指定服务器 ID，损坏配置不能返回空列表掩盖限制。
    """
    for body in ({}, {"keyword": "x", "id": "forged"}, {"site_id": 0}):
        assert api.client.post("/torrent/blacklist", json=body).status_code == 422
    api.config.set(SystemConfigKey.TorrentBlacklist, {"version": 99, "entries": []})
    response = api.client.get("/torrent/blacklist")
    assert response.status_code == 409
    assert "配置无效" in str(response.json())


@pytest.mark.parametrize("value", [{}, {"version": 1}, {"entries": []}])
@pytest.mark.parametrize("method,path,body", [
    ("get", "/torrent/blacklist", None),
    ("post", "/torrent/blacklist", {"keyword": "bad"}),
    ("post", "/torrent/blacklist/history/7", None),
    ("delete", "/torrent/blacklist/" + "a" * 32, None),
])
def test_incomplete_persisted_config_rejects_management(api, value, method, path, body):
    """
    不完整的持久化黑名单阻止所有管理读写并保留原值，不能被请求自动重置为空。
    """
    api.config.set(SystemConfigKey.TorrentBlacklist, value)
    response = api.client.request(method, path, json=body)
    assert response.status_code == 409
    assert "配置无效" in str(response.json())
    assert api.config.get(SystemConfigKey.TorrentBlacklist) == value


@pytest.mark.asyncio
async def test_async_history_by_id_projects_inside_session(db):
    """
    新查询端口返回独立 DTO，支持请求会话和独立短事务仓储。
    """
    db.watermark(DownloadHistory)
    repository = TransactionalDownloadHistoryRepository(sync_session=SessionFactory, async_session=async_session_scope)
    history_id = repository.add(DownloadHistoryWrite(path="/downloads/a", type="电影", title="a",
                                                    download_hash="ab" * 20, note={"custom": [1]}))
    snapshot = await repository.async_get_by_id(history_id)
    assert snapshot.id == history_id
    assert snapshot.note["custom"] == [1]
    assert await repository.async_get_by_id(-1) is None
    async with async_session_scope() as session:
        query = HistoryQueryService(download_repository=SessionDownloadHistoryRepository(session),
                                    transfer_repository=AsyncMock())
        dto = await query.get_download(history_id)
    assert dto.id == history_id
    assert dto.note == {"custom": [1]}


def test_agent_routes_are_fixed_and_admin_only():
    """
    Agent 黑名单操作只调用四条固定路由，写入具备可逆幂等合同。
    """
    routes = {
        "download.blacklist.list": ("GET", "/api/v1/torrent/blacklist"),
        "download.blacklist.add": ("POST", "/api/v1/torrent/blacklist"),
        "download.blacklist.history": ("POST", "/api/v1/torrent/blacklist/history/{history_id}"),
        "download.blacklist.remove": ("DELETE", "/api/v1/torrent/blacklist/{entry_id}"),
    }
    for operation, (method, path) in routes.items():
        route = API_OPERATION_ROUTES[operation]
        assert (route.method, route.path) == (method, path)
        spec = API_OPERATION_BY_ID[operation]
        assert spec.required_role is PrincipalRole.SYSTEM_ADMIN
        if method == "GET":
            assert spec.effect is ActionEffect.SAFE_READ
        else:
            assert spec.effect is ActionEffect.REVERSIBLE_WRITE
            assert spec.recovery is RecoveryMode.IDEMPOTENT


@pytest.mark.asyncio
async def test_agent_gateway_validates_blacklist_body_and_exposes_contract():
    """
    网关发现暴露受限请求模型，不允许附加 ID 或任意执行路径。
    """
    executor = AsyncMock()
    executor.execute.return_value = '{"success":true}'
    gateway = MoviePilotApiTool(session_id="blacklist", user_id="1", executor=executor)
    gateway.set_agent_context({"is_admin": True})
    result = await gateway.run(operation_id="download.blacklist.add", body={"keyword": "bad"})
    assert json.loads(result)["success"] is True
    executor.execute.assert_awaited_once_with(
        "download.blacklist.add", path_params=None, query=None, body={"keyword": "bad"})
    executor.execute.reset_mock()
    result = await gateway.run(operation_id="download.blacklist.add", body={"keyword": "bad", "id": "forged"})
    assert json.loads(result)["success"] is False
    executor.execute.assert_not_awaited()
    schema = gateway.get_mcp_input_schema()
    branches = {item["properties"]["operation_id"]["const"]: item for item in schema["oneOf"]}
    assert branches["download.blacklist.add"]["properties"]["body"]["$ref"].endswith("/BlacklistRule")
    assert branches["download.blacklist.history"]["properties"]["path_params"]["required"] == ["history_id"]
    gateway.set_agent_context({"is_admin": False})
    result = await gateway.run(operation_id="download.blacklist.list")
    assert json.loads(result)["error"] == "permission_denied"
    executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_settings_cannot_bypass_atomic_blacklist_service():
    """
    通用设置写入口拒绝整表替换，必须使用有界且原子更新的专用操作。
    """
    from app.application.settings.contract import ALL_SETTING_SPECS
    from app.application.settings.service import SystemSettingsService

    spec = ALL_SETTING_SPECS[SystemConfigKey.TorrentBlacklist.value]
    assert not spec.generic_write_allowed
    assert spec.update_operations == ()
    assert "download.blacklist.add" in spec.preferred_operation_ids
    config = Mock()
    service = SystemSettingsService(Mock(), config, AsyncMock())
    with pytest.raises(ValueError, match="禁止通用写入"):
        await service.update(setting_key=SystemConfigKey.TorrentBlacklist.value,
                             value={"version": 1, "entries": []})
    config.update_atomically.assert_not_called()
    config.async_set_with_normalized_value.assert_not_called()
