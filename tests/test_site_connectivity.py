"""
站点连通性的协议路由、认证响应与统计持久化回归测试。
"""

from unittest.mock import ANY, MagicMock, Mock

import pytest

from app.application.site.contract import SiteSnapshot
from app.chain.site import SiteChain
from app.db.models.site import Site
from app.db.models.sitestatistic import SiteStatistic
from app.schemas.site import SiteUserData


@pytest.mark.parametrize("render", [0, 1])
@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (200, {"code": "0", "data": {"id": "42"}}, (True, "连接成功")),
        (200, {"code": "401", "message": "API Key 无效"}, (False, "API Key 无效")),
        (200, {}, (False, "鉴权已过期或无效")),
        (401, {}, (False, "错误：401 Unauthorized")),
        (None, {}, (False, "无法打开网站！")),
    ],
)
def test_mtorrent_schema_uses_profile_api(
    monkeypatch, render, status_code, payload, expected,
):
    """
    MTorrent 兼容站只请求认证 API，并记录真实结果、关闭响应上下文。
    """
    site = SiteSnapshot(
        id=1,
        name="MTorrent 兼容站",
        domain="colapt.com",
        url="https://www.colapt.com/",
        apikey="test-api-key",
        cookie="unusable-cookie",
        ua="Connectivity-Test",
        proxy=1,
        timeout=25,
        render=render,
    )
    chain = SiteChain()
    repository = Mock()
    repository.get_by_domain.return_value = site
    chain.site_repository = repository
    indexer = Mock(return_value={"schema": "MTorrent", "login_path": "login"})
    monkeypatch.setattr("app.chain.site.SitesHelper", Mock(return_value=Mock(get_indexer=indexer)))
    generic_test = Mock(side_effect=AssertionError("MTorrent 不应检查首页 HTML"))
    monkeypatch.setattr(SiteChain, "_SiteChain__test", generic_test)
    response = None if status_code is None else Mock(status_code=status_code, reason="Unauthorized")
    if response is not None:
        response.json.return_value = payload
    response_context = MagicMock()
    response_context.__enter__.return_value = response
    open_response = Mock(return_value=response_context)
    monkeypatch.setattr("app.chain.site._open_site_response", open_response)

    assert chain.test(site.url) == expected

    indexer.assert_called_with("colapt.com")
    open_response.assert_called_once_with(
        method="POST",
        url="https://api.colapt.com/api/member/profile",
        headers={
            "User-Agent": "Connectivity-Test",
            "Accept": "application/json, text/plain, */*",
            "x-api-key": "test-api-key",
        },
        proxies=chain.runtime_config.proxy or {},
        timeout=25,
    )
    response_context.__exit__.assert_called_once_with(None, None, None)
    generic_test.assert_not_called()
    if expected[0]:
        repository.success.assert_called_once_with(domain="colapt.com", seconds=ANY)
        repository.fail.assert_not_called()
    else:
        repository.fail.assert_called_once_with("colapt.com")
        repository.success.assert_not_called()


@pytest.mark.parametrize("domain", ["m-team.cc", "milkie.cc", "sunnypt.top"])
def test_special_site_test_precedes_schema_lookup(monkeypatch, domain):
    """
    已注册的专用站点保持优先，不加载资源或改走协议兜底。
    """
    chain = SiteChain()
    handler = Mock(return_value=(True, "连接成功"))
    monkeypatch.setitem(chain.special_site_test, domain, handler)
    site = SiteSnapshot(id=1, name="专用站点", domain=domain, url=f"https://{domain}/")
    chain.site_repository = Mock()
    chain.site_repository.get_by_domain.return_value = site
    helper = Mock(side_effect=AssertionError("专用站点不应查询协议配置"))
    monkeypatch.setattr("app.chain.site.SitesHelper", helper)

    assert chain.test(site.url) == (True, "连接成功")

    handler.assert_called_once_with(site)
    helper.assert_not_called()


@pytest.mark.parametrize("schema", [None, "NexusPhp"])
@pytest.mark.parametrize("render", [0, 1])
def test_other_schemas_keep_generic_connectivity_path(monkeypatch, schema, render):
    """
    普通站点仍使用 Cookie 或浏览器检测，并保留资源声明的登录页。
    """
    chain = SiteChain()
    site = SiteSnapshot(
        id=1, name="普通站点", domain="generic.test", url="https://generic.test/",
        cookie="test-cookie", apikey="unrelated-key", render=render,
    )
    chain.site_repository = Mock()
    chain.site_repository.get_by_domain.return_value = site
    indexer = Mock(return_value={"schema": schema, "login_path": "login.php"})
    monkeypatch.setattr("app.chain.site.SitesHelper", Mock(return_value=Mock(get_indexer=indexer)))
    generic_test = Mock(return_value=(False, "Cookie已失效！"))
    monkeypatch.setattr(SiteChain, "_SiteChain__test", generic_test)
    api_test = Mock(side_effect=AssertionError("普通站点不应请求 MTorrent API"))
    monkeypatch.setattr(SiteChain, "_SiteChain__mteam_test", api_test)

    assert chain.test(site.url) == (False, "Cookie已失效！")

    tested_site = generic_test.call_args.args[0]
    assert tested_site.url == "https://generic.test/login.php"
    assert tested_site.cookie == site.cookie
    assert tested_site.render == render
    assert site.url == "https://generic.test/"
    api_test.assert_not_called()


@pytest.mark.parametrize("resource_key", ["schema", "parser"])
@pytest.mark.parametrize("successful", [True, False])
@pytest.mark.parametrize(
    ("protocol", "method", "endpoint", "auth_header", "success_payload", "failure_payload"),
    [
        (
            "MTorrent", "POST", "https://api.protocol.test/api/member/profile", "x-api-key",
            {"code": "0", "data": {"id": "1"}}, {"code": "401", "data": {"id": "1"}},
        ),
        (
            "HDDolby", "GET", "https://api.protocol.test/api/v1/user/data", "x-api-key",
            {"status": 0, "data": [{"id": "1"}]}, {"status": 1},
        ),
        (
            "RousiPro", "GET", "https://protocol.test/api/v1/profile", "Authorization",
            {"code": 0, "data": {"id": "1"}}, {"code": 401},
        ),
        (
            "SunnyPT", "GET", "https://api.protocol.test/api/v1/mp/profile", "X-API-Key",
            {"code": 0, "data": {"download_allowed": True}}, {"code": 401},
        ),
        (
            "Yema", "POST", "https://www.protocol.test/openApi/user/fetchBasicInfo.json", "Authorization",
            {"success": True, "data": {"id": "1"}}, {"success": False, "errorMessage": "AuthKey 无效"},
        ),
        (
            "Milkie", "GET", "https://www.protocol.test/api/v1/torrents?ps=1", "x-milkie-auth",
            {"torrents": []}, {"error": "API Key 无效"},
        ),
    ],
)
def test_api_protocols_use_their_own_authentication(
    monkeypatch, resource_key, successful, protocol, method, endpoint,
    auth_header, success_payload, failure_payload,
):
    """
    所有已适配的 API Key 协议按资源声明选择接口，并验证协议自己的成功判据。
    """
    chain = SiteChain()
    site = SiteSnapshot(
        id=1, name="协议兼容站", domain="protocol.test", url="https://www.protocol.test/",
        apikey="test-key", cookie="unused-cookie", render=1,
    )
    chain.site_repository = Mock()
    chain.site_repository.get_by_domain.return_value = site
    # mTorrent 是搜索适配器的历史大小写，与资料 schema 不同。
    resource_value = "mTorrent" if resource_key == "parser" and protocol == "MTorrent" else protocol
    indexer = Mock(return_value={resource_key: resource_value})
    monkeypatch.setattr("app.chain.site.SitesHelper", Mock(return_value=Mock(get_indexer=indexer)))
    response = Mock(status_code=200)
    response.json.return_value = success_payload if successful else failure_payload
    context = MagicMock()
    context.__enter__.return_value = response
    open_response = Mock(return_value=context)
    monkeypatch.setattr("app.chain.site._open_site_response", open_response)
    generic_test = Mock(side_effect=AssertionError("API 协议不应检查首页 HTML"))
    monkeypatch.setattr(SiteChain, "_SiteChain__test", generic_test)

    state, _message = chain.test(site.url)

    assert state is successful
    open_response.assert_called_once()
    request = open_response.call_args.kwargs
    assert request["method"] == method
    assert request["url"] == endpoint
    expected_key = "Bearer test-key" if protocol == "RousiPro" else "test-key"
    assert request["headers"][auth_header] == expected_key
    assert "cookie" not in request
    if protocol == "Yema":
        assert request["json_body"] == {}
    generic_test.assert_not_called()
    context.__exit__.assert_called_once_with(None, None, None)


@pytest.mark.parametrize(
    ("protocol", "host_key", "expected_url"),
    [
        ("MTorrent", "api_domain", "https://custom-api.test:8443/api/member/profile"),
        ("MTorrent", "api_host", "https://custom-api.test:8443/api/member/profile"),
        ("SunnyPT", "api_url", "https://custom-api.test:8443/api/profile"),
        ("HDDolby", "api_url", "https://custom-api.test:8443/api/user/data"),
    ],
)
def test_api_probe_honors_resource_endpoint(monkeypatch, protocol, host_key, expected_url):
    """
    API 协议探测与搜索使用同一资源 API 地址配置。
    """
    chain = SiteChain()
    site = SiteSnapshot(id=1, name="独立 API", domain="custom.test", url="https://www.custom.test/", apikey="key")
    chain.site_repository = Mock()
    chain.site_repository.get_by_domain.return_value = site
    indexer = Mock(return_value={"schema": protocol, host_key: "https://custom-api.test:8443/api"})
    monkeypatch.setattr("app.chain.site.SitesHelper", Mock(return_value=Mock(get_indexer=indexer)))
    context = MagicMock()
    context.__enter__.return_value = Mock(
        status_code=200, json=Mock(return_value={"code": "0", "status": 0, "data": {"id": "1"}}),
    )
    open_response = Mock(return_value=context)
    monkeypatch.setattr("app.chain.site._open_site_response", open_response)

    assert chain.test(site.url) == (True, "连接成功")

    assert open_response.call_args.kwargs["url"] == expected_url


@pytest.mark.parametrize("domain", ["hddolby.com", "uhddolby.com"])
@pytest.mark.parametrize("api_url", ["https://api.hddolby.com/api/v1", "https://api.hddolby.com/api/v1/"])
def test_hddolby_probe_uses_migrated_resource_for_both_domains(monkeypatch, domain, api_url):
    """
    旧域专用路由和新域协议路由均按资源访问旧域 API，并保留认证与连接参数。
    """
    chain = SiteChain()
    site = SiteSnapshot(
        id=1, name="高清杜比", domain=domain, url=f"https://www.{domain}/",
        apikey="test-key", proxy=1, timeout=25,
    )
    chain.site_repository = Mock()
    chain.site_repository.get_by_domain.return_value = site
    indexer = Mock(return_value={"schema": "HDDolby", "api_url": api_url})
    monkeypatch.setattr("app.chain.site.SitesHelper", Mock(return_value=Mock(get_indexer=indexer)))
    context = MagicMock()
    context.__enter__.return_value = Mock(status_code=200, json=Mock(return_value={"status": 0}))
    open_response = Mock(return_value=context)
    monkeypatch.setattr("app.chain.site._open_site_response", open_response)

    assert chain.test(site.url) == (True, "连接成功")

    indexer.assert_called_with(domain)
    request = open_response.call_args.kwargs
    assert request["method"] == "GET"
    assert request["url"] == "https://api.hddolby.com/api/v1/user/data"
    assert request["headers"]["x-api-key"] == "test-key"
    assert request["proxies"] == (chain.runtime_config.proxy or {})
    assert request["timeout"] == 25
    context.__exit__.assert_called_once_with(None, None, None)


@pytest.mark.parametrize(
    ("api_url", "host"),
    [
        (None, "api.uhddolby.com"),
        ("https://api.hddolby.com/api/v1", "api.hddolby.com"),
        ("https://user:secret@custom-api.test:8443/api?token=secret", "custom-api.test"),
    ],
)
@pytest.mark.parametrize("status_code", [None, 405])
def test_hddolby_api_failure_identifies_host_without_page_fallback(monkeypatch, api_url, host, status_code):
    """
    接口失败提示真实 API 主机且不泄露凭据，网页可用也不能掩盖 API 搜索不可用。
    """
    chain = SiteChain()
    site = SiteSnapshot(
        id=1, name="高清杜比", domain="uhddolby.com", url="https://www.uhddolby.com/", apikey="test-key",
    )
    chain.site_repository = Mock()
    chain.site_repository.get_by_domain.return_value = site
    indexer = Mock(return_value={"schema": "HDDolby", "api_url": api_url})
    monkeypatch.setattr("app.chain.site.SitesHelper", Mock(return_value=Mock(get_indexer=indexer)))
    context = MagicMock()
    context.__enter__.return_value = None if status_code is None else Mock(status_code=405, reason="Not Allowed")
    monkeypatch.setattr("app.chain.site._open_site_response", Mock(return_value=context))
    generic_test = Mock(return_value=(True, "连接成功"))
    monkeypatch.setattr(SiteChain, "_SiteChain__test", generic_test)

    state, message = chain.test(site.url)

    assert state is False
    if status_code is None:
        assert message == f"HDDolby API 主机 {host} 无法连接，请检查 API 地址及网络代理"
    else:
        assert message == f"HDDolby API 主机 {host} 返回 HTTP 405 Not Allowed"
    assert "secret" not in message
    assert "无法打开网站" not in message
    generic_test.assert_not_called()
    chain.site_repository.fail.assert_called_once_with("uhddolby.com")
    context.__exit__.assert_called_once_with(None, None, None)


@pytest.mark.parametrize("apikey", [None, "test-auth-key"])
def test_yema_registered_domain_prefers_authkey_and_preserves_legacy_cookie(monkeypatch, apikey):
    """
    Yema 主站已配置 AuthKey 时使用开放 API，否则兼容历史 Cookie 连接测试。
    """
    chain = SiteChain()
    site = SiteSnapshot(
        id=1, name="YemaPT", domain="yemapt.org", url="https://www.yemapt.org/",
        cookie="session=test", apikey=apikey,
    )
    chain.site_repository = Mock()
    chain.site_repository.get_by_domain.return_value = site
    context = MagicMock()
    context.__enter__.return_value = Mock(
        status_code=200, json=Mock(return_value={"success": True, "data": {"id": "1"}}),
    )
    open_response = Mock(return_value=context)
    monkeypatch.setattr("app.chain.site._open_site_response", open_response)

    assert chain.test(site.url) == (True, "连接成功")

    request = open_response.call_args.kwargs
    if apikey:
        assert request["method"] == "POST"
        assert request["url"] == "https://www.yemapt.org/openApi/user/fetchBasicInfo.json"
        assert request["headers"]["Authorization"] == apikey
        assert "cookie" not in request
    else:
        assert request["method"] == "GET"
        assert request["url"] == "https://www.yemapt.org/api/consumer/fetchSelfDetail"
        assert request["cookie"] == "session=test"


@pytest.mark.parametrize("resource_key, resource_value", [("schema", "TNode"), ("parser", "TNodeSpider")])
def test_tnode_probe_uses_cookie_and_csrf_api(monkeypatch, resource_key, resource_value):
    """
    Cookie 认证的 TNode SPA 通过 CSRF 和当前用户 API 验证会话。
    """
    chain = SiteChain()
    site = SiteSnapshot(id=1, name="TNode 兼容站", domain="tnode.test", url="https://tnode.test/", cookie="session=test")
    chain.site_repository = Mock()
    chain.site_repository.get_by_domain.return_value = site
    indexer = Mock(return_value={resource_key: resource_value})
    monkeypatch.setattr("app.chain.site.SitesHelper", Mock(return_value=Mock(get_indexer=indexer)))
    page = MagicMock()
    page.__enter__.return_value = Mock(status_code=200, text='<meta name="x-csrf-token" content="csrf-test">')
    api = MagicMock()
    api.__enter__.return_value = Mock(status_code=200, json=Mock(return_value={"data": {"id": "1"}}))
    open_response = Mock(side_effect=[page, api])
    monkeypatch.setattr("app.chain.site._open_site_response", open_response)

    assert chain.test(site.url) == (True, "连接成功")

    request = open_response.call_args.kwargs
    assert request["url"] == "https://tnode.test/api/user/getInfo"
    assert request["headers"]["X-CSRF-TOKEN"] == "csrf-test"
    assert request["cookie"] == "session=test"


def test_site_connectivity_records_result_without_injected_session(db, monkeypatch):
    """
    默认站点端口完成测试后应提交统计，而不是对空会话调用 execute。
    """
    db.watermark(Site, SiteStatistic)
    db.add(Site(
        name="连通性测试站点",
        domain="connectivity.test",
        url="https://connectivity.test/",
        is_active=True,
    ))
    monkeypatch.setattr(
        SiteChain,
        "_SiteChain__test",
        lambda _self, _site: (True, "连接成功"),
    )

    status, message = SiteChain().test("https://connectivity.test/")

    statistic = SiteStatistic.get_by_domain(db.session, "connectivity.test")
    assert (status, message) == (True, "连接成功")
    assert statistic.success == 1
    assert statistic.lst_state == 0


def test_resource_login_path_uses_generic_connectivity_test(db, monkeypatch):
    """
    站点资源声明登录页后，通用检测应访问该页且不修改站点快照。
    """
    db.watermark(Site, SiteStatistic)
    db.add(Site(
        name="资源路径站点",
        domain="resource-path.test",
        url="https://resource-path.test/",
        is_active=True,
    ))
    observed: list[SiteSnapshot] = []

    class _SitesHelper:
        """
        为资源路径测试提供最小站点索引器接口。
        """

        def get_indexer(self, domain: str) -> dict:
            """
            返回测试站点的登录页配置。
            """
            assert domain == "resource-path.test"
            return {"login_path": "index.php"}

    monkeypatch.setattr("app.chain.site.SitesHelper", _SitesHelper)
    monkeypatch.setattr(
        SiteChain,
        "_SiteChain__test",
        lambda _self, site: observed.append(site) or (True, "连接成功"),
    )

    status, message = SiteChain().test("https://resource-path.test/")

    assert (status, message) == (True, "连接成功")
    assert observed[0].url == "https://resource-path.test/index.php"


def test_failed_userdata_is_not_persisted_as_a_successful_refresh(monkeypatch):
    """
    解析器未取得用户身份时不得写入空数据或发送刷新成功事件。
    """
    chain = object.__new__(SiteChain)
    chain.site_repository = Mock()
    chain.eventmanager = Mock()
    failed_data = SiteUserData(
        domain="www.musopia.vip",
        err_msg="未检测到已登陆，请检查cookies是否过期",
    )
    monkeypatch.setattr(
        SiteChain,
        "run_module",
        lambda _self, _method, **_kwargs: failed_data,
    )

    result = chain.refresh_userdata(
        site={"id": 1, "domain": "www.musopia.vip", "name": "音乐乌托邦"}
    )

    assert result is failed_data
    chain.site_repository.update_userdata.assert_not_called()
    chain.eventmanager.send_event.assert_not_called()
