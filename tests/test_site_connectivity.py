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

    indexer.assert_called_once_with("colapt.com")
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
