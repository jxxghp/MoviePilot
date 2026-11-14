"""
社区勋章查询的身份隔离、名单缓存与 Wiki 名单协议边界回归覆盖。
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.external.github import COMMUNITY_MEDALS_URL, GithubAuthClient
from app.adapters.external.server import MoviePilotServerHelper
from app.api.endpoints.github import get_community_profile
from app.application.community.medals import CommunityMedalService
from app.domain.github import GithubAuthTransportError, GithubMedalSnapshot, GithubUser
from app.startup.composition.system import _ServerInfoAdapter

_CODE = {
    "key": "code", "tier": 2, "specialty": "frontend", "at": "2025-03-01", "basis": "reached",
    "serial": None, "facts": {"total": 12, "repositories": []},
}
_PIONEER = {"key": "pioneer", "at": "2024", "note": "Logo 设计"}


async def _updated_profile(service, refresh=False):
    """
    显式运行一次后台维护再读取快照，保留原有缓存和传输边界用例的验证范围。

    :param service: 测试勋章服务
    :param refresh: 是否主动核验名单
    :return: 后台维护后的公开快照
    """
    await service.update(force=refresh)
    return await service.profile()


@pytest.fixture
def community():
    """
    提供不访问真实凭据或网络的可控勋章服务。
    """
    settings = {"token": "first", "time": 1000, "medals": {42: (_CODE,)}, "enabled": True}

    async def token_reader():
        """
        返回测试凭据。
        """
        return settings["token"]

    async def snapshot(user_id):
        """
        按数字 ID 返回注入的名单记录。
        """
        return GithubMedalSnapshot("2026-10-15T00:00:00Z", settings["medals"].get(user_id, ()))

    transport = SimpleNamespace(
        get_user=AsyncMock(return_value=GithubUser("artist", 42)),
        get_medal_snapshot=AsyncMock(side_effect=snapshot),
    )
    async def enabled_reader():
        """
        返回测试注入的服务端开关。
        """
        return settings["enabled"]

    service = CommunityMedalService(
        token_reader=token_reader, token_snapshot=lambda: settings["token"],
        local_enabled=lambda: settings["enabled"], transport=transport, enabled_reader=enabled_reader,
        clock=lambda: settings["time"],
    )
    return service, transport, settings


@pytest.mark.asyncio
async def test_medals_are_looked_up_by_verified_numeric_id(community):
    """
    名单按 GET /user 返回的数字 ID 查表，而不是登录名。
    """
    service, transport, _ = community
    result = await _updated_profile(service)
    assert result.state == "ready"
    assert (result.login, result.user_id) == ("artist", 42)
    assert result.medals == (_CODE,)
    assert result.generated_at == "2026-10-15T00:00:00Z"
    assert result.checked_at == 1000
    assert not result.stale
    transport.get_medal_snapshot.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_user_absent_from_registry_is_ready_without_medals(community):
    """
    名单中没有该 ID 是已核对的空结果，不是不可用。
    """
    service, _, settings = community
    settings["medals"] = {}
    result = await _updated_profile(service)
    assert result.state == "ready"
    assert result.medals == ()


@pytest.mark.asyncio
async def test_cache_refresh_cooldown_and_concurrent_readers(community):
    """
    同一 Token 的并发读取合并，刷新按钮不能绕过一分钟冷却，缓存六小时。
    """
    service, transport, settings = community
    first, second = await asyncio.gather(_updated_profile(service), _updated_profile(service))
    assert first == second
    assert transport.get_medal_snapshot.await_count == 1
    await _updated_profile(service, refresh=True)
    assert transport.get_medal_snapshot.await_count == 1
    settings["time"] += 61
    await _updated_profile(service)
    assert transport.get_medal_snapshot.await_count == 1
    await _updated_profile(service, refresh=True)
    assert transport.get_medal_snapshot.await_count == 2
    settings["time"] += 21600
    await _updated_profile(service)
    assert transport.get_medal_snapshot.await_count == 3


@pytest.mark.asyncio
async def test_registry_failure_preserves_only_same_token_snapshot(community):
    """
    名单拉取失败保留同 Token 旧结果并标记过期，换 Token 后不能借用上一人的勋章。
    """
    service, transport, settings = community
    original = await _updated_profile(service)
    settings["time"] += 61
    transport.get_medal_snapshot.side_effect = GithubAuthTransportError("registry down")
    stale = await _updated_profile(service, refresh=True)
    assert stale.state == "ready"
    assert stale.medals == original.medals
    assert stale.checked_at == original.checked_at
    assert stale.stale
    settings["token"] = "second"
    failed = await _updated_profile(service)
    assert failed.state == "unavailable"
    assert failed.medals == ()
    assert failed.login is None


@pytest.mark.asyncio
async def test_stale_result_retries_after_cooldown_and_recovers(community):
    """
    过期结果不享受六小时缓存，冷却后普通读取即重试，成功后清除过期标记。
    """
    service, transport, settings = community
    transport.get_medal_snapshot.side_effect = GithubAuthTransportError("registry down")
    assert (await _updated_profile(service)).state == "unavailable"
    transport.get_medal_snapshot.side_effect = None
    transport.get_medal_snapshot.return_value = GithubMedalSnapshot("t", (_PIONEER,))
    settings["time"] += 61
    recovered = await _updated_profile(service)
    assert recovered.state == "ready"
    assert recovered.medals == (_PIONEER,)
    assert not recovered.stale


@pytest.mark.asyncio
async def test_unauthorized_or_removed_token_removes_medals(community):
    """
    失效和删除凭据都不能继续显示已撤销身份的勋章。
    """
    service, transport, settings = community
    assert (await _updated_profile(service)).medals
    settings["time"] += 61
    transport.get_user.side_effect = GithubAuthTransportError("expired", unauthorized=True)
    revoked = await _updated_profile(service, refresh=True)
    assert revoked.state == "unavailable"
    assert revoked.medals == ()
    settings["token"] = ""
    assert (await _updated_profile(service)).state == "unconfigured"


@pytest.mark.asyncio
async def test_token_switch_during_query_discards_late_result(community):
    """
    网络请求期间变更凭据，旧身份的迟到结果不能点亮菜单。
    """
    service, transport, settings = community

    async def user(_token):
        """
        模拟用户在身份查询期间清除凭据。
        """
        settings["token"] = ""
        return GithubUser("artist", 42)

    transport.get_user.side_effect = user
    result = await _updated_profile(service)
    assert result.medals == ()
    assert result.login is None


@pytest.mark.asyncio
async def test_endpoint_returns_registry_records_without_token(community):
    """
    API 原样投影名单记录，省略的字段不补 null，响应不含服务器 Token。
    """
    service, _, settings = community
    settings["medals"] = {42: (_PIONEER, {**_CODE, "glow": "gold"})}
    await service.update()
    reply = await get_community_profile(refresh=False, community=service)
    payload = json.loads(reply.model_dump_json())
    assert reply.success
    assert payload["data"]["medals"] == [_PIONEER, {**_CODE, "glow": "gold"}]
    assert set(payload["data"]) == {
        "state", "login", "user_id", "medals", "generated_at", "checked_at", "stale", "refreshing",
    }
    assert "first" not in reply.model_dump_json()


@pytest.mark.asyncio
async def test_disabled_switch_skips_identity_and_registry(community):
    """
    服务端开关关闭时返回 disabled，不核验身份也不拉取名单，重新打开后照常查询。
    """
    service, transport, settings = community
    settings["enabled"] = False
    result = await _updated_profile(service, refresh=True)
    assert result.state == "disabled"
    assert result.medals == ()
    transport.get_user.assert_not_awaited()
    transport.get_medal_snapshot.assert_not_awaited()
    settings["enabled"] = True
    assert (await _updated_profile(service)).state == "ready"


@pytest.mark.asyncio
async def test_endpoint_reports_disabled_without_medals(community):
    """
    开关关闭时接口仍成功返回，state 为 disabled 且没有勋章。
    """
    service, transport, settings = community
    settings["enabled"] = False
    await service.update()
    reply = await get_community_profile(refresh=False, community=service)
    payload = json.loads(reply.model_dump_json())
    assert reply.success
    assert payload["data"]["state"] == "disabled"
    assert payload["data"]["medals"] == []
    transport.get_medal_snapshot.assert_not_awaited()


_FEATURES = {
    "on": (200, {"community_medals": True}),
    "off": (200, {"community_medals": False}),
    "missing": (200, {}),
    "error": (502, {}),
    "unreachable": None,
}


def _reply(response):
    """
    把 (状态码, 载荷) 转成响应替身；None 表示服务端不可达。
    """
    if response is None:
        return None
    status_code, payload = response
    return SimpleNamespace(status_code=status_code, json=lambda: payload)


async def _clear_server_caches():
    """
    清空全局开关与权限短时缓存，避免用例间串值。
    """
    await MoviePilotServerHelper.async_get_features.cache_clear()
    await MoviePilotServerHelper._async_fetch_user_permissions.cache_clear()


@pytest.fixture
def server(monkeypatch):
    """
    在边界替换服务端全局开关、用户权限与 GitHub 登录，返回请求替身与可切换的场景。
    """
    asyncio.run(_clear_server_caches())
    scenario = {"login": "artist", "features": "off", "preview": False, "permissions_up": True}
    monkeypatch.setattr(MoviePilotServerHelper, "_github_user", None)
    monkeypatch.setattr(MoviePilotServerHelper, "get_user_uuid", lambda: "uid")

    async def github_user():
        """
        返回场景中的 GitHub 登录名，空串表示未登录。
        """
        return scenario["login"]

    async def permissions(_github_user):
        """
        按场景返回带预览标记的用户权限。
        """
        if not scenario["permissions_up"]:
            return None
        return _reply((200, {"is_admin": False, "community_medals_preview": scenario["preview"]}))

    async def features():
        """
        按场景返回全局开关。
        """
        return _reply(_FEATURES[scenario["features"]])

    monkeypatch.setattr(MoviePilotServerHelper, "async_get_github_user", github_user)
    calls = SimpleNamespace(
        permissions=AsyncMock(side_effect=permissions), features=AsyncMock(side_effect=features),
    )
    monkeypatch.setattr(MoviePilotServerHelper, "async_user_permissions", calls.permissions)
    monkeypatch.setattr(MoviePilotServerHelper, "async_user_features", calls.features)
    yield calls, scenario
    asyncio.run(_clear_server_caches())


@pytest.mark.asyncio
@pytest.mark.parametrize("features", ["on", "off", "missing", "error", "unreachable"])
@pytest.mark.parametrize("preview", [True, False], ids=["preview", "no-preview"])
@pytest.mark.parametrize("login", ["artist", ""], ids=["github-login", "no-github-login"])
async def test_effective_switch_with_local_setting_on(server, features, preview, login):
    """
    有效开关 = 全局开关，或已登录且在预览名单；未登录时只看全局开关。

    global/user 与勋章服务各读一次，全局开关只请求一次，权限每个身份最多请求一次。
    """
    calls, scenario = server
    scenario.update(features=features, preview=preview, login=login)
    expected = features == "on" or (bool(login) and preview)
    adapter = _ServerInfoAdapter({"COMMUNITY_MEDALS_ENABLE": True})
    adapter.community = CommunityMedalService(
        token_reader=AsyncMock(return_value=""), token_snapshot=lambda: "", local_enabled=lambda: True,
        transport=SimpleNamespace(), enabled_reader=adapter.community_medals_enabled,
    )
    await adapter.community.update()
    assert (await adapter.user_global())["COMMUNITY_MEDALS_ENABLED"] is expected
    assert await adapter.community_medals_enabled() is expected
    assert calls.features.await_count == 1
    assert calls.permissions.await_count == (1 if login else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("login", ["artist", ""], ids=["github-login", "no-github-login"])
async def test_local_setting_off_skips_medal_lookup(server, login):
    """
    本机关闭时有效开关为 False，且不请求全局开关或为勋章查询权限。
    """
    calls, scenario = server
    scenario.update(login=login, features="on", preview=True)
    adapter = _ServerInfoAdapter({"COMMUNITY_MEDALS_ENABLE": False})
    assert await adapter.community_medals_enabled() is False
    assert (await adapter.user_global())["COMMUNITY_MEDALS_ENABLED"] is False
    calls.features.assert_not_awaited()
    # 已登录时 global/user 仍有原本的共享管理权限查询，与勋章无关。
    assert calls.permissions.await_count == (1 if login else 0)


@pytest.mark.asyncio
async def test_preview_lookup_failure_counts_as_off(server):
    """
    全局关闭且权限查询失败时，预览不成立。
    """
    _, scenario = server
    scenario.update(features="off", permissions_up=False)
    assert await _ServerInfoAdapter({"COMMUNITY_MEDALS_ENABLE": True}).community_medals_enabled() is False


@pytest.mark.asyncio
async def test_features_transport_error_is_off(server):
    """
    全局开关请求抛错时按关闭处理，并回落到预览判定。
    """
    calls, scenario = server
    calls.features.side_effect = RuntimeError("offline")
    scenario["preview"] = False
    assert await _ServerInfoAdapter({"COMMUNITY_MEDALS_ENABLE": True}).community_medals_enabled() is False


@pytest.mark.asyncio
async def test_features_request_carries_no_identity(monkeypatch):
    """
    全局开关请求不携带用户名与实例 UID，响应可被反向代理统一缓存。
    """
    get = AsyncMock(return_value=None)
    monkeypatch.setattr(MoviePilotServerHelper, "_async_get", get)
    await MoviePilotServerHelper.async_user_features()
    assert get.await_args.args[0].endswith("/user/features")
    assert get.await_args.kwargs.get("params") is None
    assert get.await_args.kwargs["include_user_uid"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("local", "login", "features", "preview", "state"),
    [
        (False, "artist", "on", True, "disabled"),
        (True, "artist", "off", False, "disabled"),
        (True, "artist", "unreachable", False, "disabled"),
        (True, "", "on", False, "unconfigured"),
        (True, "", "off", True, "disabled"),
        (True, "", "unreachable", False, "disabled"),
    ],
)
async def test_community_state_follows_effective_switch(server, local, login, features, preview, state):
    """
    接口状态由有效开关与 Token 共同决定；disabled 与 unconfigured 都不拉取名单。
    """
    _, scenario = server
    scenario.update(login=login, features=features, preview=preview)
    adapter = _ServerInfoAdapter({"COMMUNITY_MEDALS_ENABLE": local})

    async def token_reader():
        """
        已登录场景返回 Token，未登录返回空串。
        """
        return "token" if login else ""

    transport = SimpleNamespace(get_user=AsyncMock(), get_medal_snapshot=AsyncMock())
    service = CommunityMedalService(
        token_reader=token_reader, token_snapshot=lambda: "token" if login else "",
        local_enabled=lambda: local, transport=transport,
        enabled_reader=adapter.community_medals_enabled,
    )
    await service.update()
    reply = await get_community_profile(refresh=False, community=service)
    assert reply.success
    assert reply.data.state == state
    assert reply.data.medals == []
    transport.get_medal_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_user_reaches_registry_while_global_off(server):
    """
    全局关闭时预览名单内的已登录用户照常核验身份并查表。
    """
    _, scenario = server
    scenario.update(features="off", preview=True)
    adapter = _ServerInfoAdapter({"COMMUNITY_MEDALS_ENABLE": True})

    async def token_reader():
        """
        返回测试凭据。
        """
        return "token"

    transport = SimpleNamespace(
        get_user=AsyncMock(return_value=GithubUser("artist", 42)),
        get_medal_snapshot=AsyncMock(return_value=GithubMedalSnapshot("t", (_PIONEER,))),
    )
    service = CommunityMedalService(
        token_reader=token_reader, token_snapshot=lambda: "token", local_enabled=lambda: True,
        transport=transport, enabled_reader=adapter.community_medals_enabled,
    )
    result = await _updated_profile(service)
    assert result.state == "ready"
    assert result.medals == (_PIONEER,)


def _registry(**overrides):
    """
    构造一份符合 schema 1 的名单。
    """
    payload = {
        "schema": 1, "generated_at": "2026-10-15T00:00:00Z", "unlinked": [],
        "holders": {
            "42": {"login": "artist", "medals": [_CODE, _PIONEER]},
            "7": {"login": "keeper", "medals": [{"key": "security", "at": "2026-01-02"}]},
        },
    }
    payload.update(overrides)
    return payload


def _client(monkeypatch, responses, settings=None):
    """
    返回只回放给定响应的适配器及其请求记录。
    """
    client = GithubAuthClient(settings or {})
    get_res = AsyncMock(side_effect=responses)
    built = []

    def request(**kwargs):
        """
        记录每次构造请求时的代理选择。
        """
        built.append(kwargs)
        return SimpleNamespace(get_res=get_res)

    monkeypatch.setattr(client, "_request", request)
    return client, get_res, built


def _response(payload, status_code=200):
    """
    构造 JSON 响应替身。
    """
    return SimpleNamespace(status_code=status_code, json=lambda: payload)


@pytest.mark.asyncio
async def test_adapter_returns_only_requested_holder(monkeypatch):
    """
    适配器只取出目标 ID 的记录，未登记的 ID 得到空记录而不是错误。
    """
    client, *_ = _client(monkeypatch, [_response(_registry()), _response(_registry())])
    snapshot = await client.get_medal_snapshot(42)
    assert snapshot == GithubMedalSnapshot("2026-10-15T00:00:00Z", (_CODE, _PIONEER))
    assert (await client.get_medal_snapshot(99)).medals == ()


@pytest.mark.asyncio
async def test_adapter_passes_unknown_keys_and_drops_malformed_records(monkeypatch):
    """
    未知勋章键与字段透传；非对象、缺键或已知字段类型错误的记录整条丢弃。
    """
    future = {"key": "future", "aura": {"level": 3}}
    # years 是保留的可选字段，类型正确时与 at 并存仍可发布。
    reserved_years = {"key": "security", "at": "2026-01-02", "years": [2025, 2026]}
    bad = [
        "code", {"tier": 1}, {"key": ""}, {"key": "code", "tier": 5}, {"key": "code", "tier": True},
        {"key": "pioneer", "at": "2024/01/01"}, {"key": "security", "years": [2024, "2025"]},
        {"key": "code", "facts": []}, {"key": "pioneer", "note": 1},
    ]
    holders = {"42": {"login": "artist", "medals": [future, *bad, reserved_years, _PIONEER]}}
    client, *_ = _client(monkeypatch, [_response(_registry(holders=holders))])
    assert (await client.get_medal_snapshot(42)).medals == (future, reserved_years, _PIONEER)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    _registry(schema=2),
    _registry(schema=True),
    _registry(schema="1"),
    _registry(generated_at=""),
    _registry(holders=[]),
    _registry(holders={"42": [_PIONEER]}),
    _registry(holders={"42": "pioneer"}),
    _registry(holders={"42": {"login": "artist"}}),
    _registry(holders={"42": {"login": "artist", "medals": {"key": "pioneer"}}}),
    ["not", "an", "object"],
])
async def test_adapter_rejects_unsupported_or_malformed_registry(monkeypatch, payload):
    """
    不支持的版本或顶层结构损坏按拉取失败处理，交由服务保留旧结果。
    """
    client, *_ = _client(monkeypatch, [_response(payload)])
    with pytest.raises(GithubAuthTransportError):
        await client.get_medal_snapshot(42)


@pytest.mark.asyncio
async def test_adapter_parses_producer_excerpt(monkeypatch):
    """
    以 Wiki 生成器真实产物的节选固定名单契约：持有人对象、阶梯与人工记录、unlinked 均可解析。
    """
    excerpt = json.loads((Path(__file__).parent / "fixtures/community/medals_excerpt.json").read_text("utf-8"))
    client, *_ = _client(monkeypatch, [_response(excerpt), _response(excerpt), _response(excerpt)])
    snapshot = await client.get_medal_snapshot(12345678)
    assert snapshot.generated_at == "2026-10-15T11:45:29Z"
    assert snapshot.medals == tuple(excerpt["holders"]["12345678"]["medals"])
    assert [medal["key"] for medal in snapshot.medals] == ["code", "plugin", "pioneer"]
    assert snapshot.medals[0]["facts"]["total"] == 2
    assert (await client.get_medal_snapshot(87654321)).medals == ({"at": "2026-01-02", "key": "security", "serial": None},)
    # unlinked 只供荣誉榜展示，实例按数字 ID 查表时既不报错也不匹配其中的署名。
    assert (await client.get_medal_snapshot(99)).medals == ()


@pytest.mark.asyncio
async def test_adapter_ignores_unknown_holder_fields_and_unlinked(monkeypatch):
    """
    持有人对象的未知字段与任意形态的 unlinked 都不影响查表。
    """
    holders = {"42": {"login": "artist", "avatar": "x", "medals": [_PIONEER]}}
    payload = _registry(holders=holders, unlinked={"unexpected": True})
    client, *_ = _client(monkeypatch, [_response(payload)])
    assert (await client.get_medal_snapshot(42)).medals == (_PIONEER,)


@pytest.mark.asyncio
async def test_adapter_reports_http_failure(monkeypatch):
    """
    名单请求的 HTTP 错误不能被当作空名单。
    """
    client, *_ = _client(monkeypatch, [_response({}, status_code=404)])
    with pytest.raises(GithubAuthTransportError):
        await client.get_medal_snapshot(42)


@pytest.mark.asyncio
async def test_adapter_prefers_github_mirror_then_falls_back_to_proxy(monkeypatch):
    """
    配置 GitHub 镜像时先走镜像且不叠加代理，镜像返回错误页后再经部署代理访问原地址。
    """
    html = SimpleNamespace(status_code=200, json=lambda: (_ for _ in ()).throw(ValueError("html")))
    client, get_res, built = _client(
        monkeypatch, [html, _response(_registry())],
        settings={"GITHUB_PROXY": "mirror.invalid", "PROXY": {"https": "http://proxy.invalid"}},
    )
    snapshot = await client.get_medal_snapshot(42)
    assert snapshot.medals == (_CODE, _PIONEER)
    assert built == [{"direct": True}, {"direct": False}]
    assert [call.args[0] for call in get_res.call_args_list] == [
        f"http://mirror.invalid/{COMMUNITY_MEDALS_URL}", COMMUNITY_MEDALS_URL,
    ]


@pytest.mark.asyncio
async def test_adapter_requests_registry_without_token(monkeypatch):
    """
    公开名单请求不携带实例 Token，不经镜像时访问固定 Wiki 地址。
    """
    client, get_res, built = _client(monkeypatch, [_response(_registry())])
    await client.get_medal_snapshot(42)
    assert get_res.call_args.args[0] == COMMUNITY_MEDALS_URL
    assert built == [{"direct": False}]
