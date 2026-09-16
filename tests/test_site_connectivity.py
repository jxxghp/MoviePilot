"""站点连通性测试链路的数据库回归测试。"""

from unittest.mock import Mock

from app.application.site.contract import SiteSnapshot
from app.chain.site import SiteChain
from app.db.models.site import Site
from app.db.models.sitestatistic import SiteStatistic
from app.schemas.site import SiteUserData


def test_site_connectivity_records_result_without_injected_session(db, monkeypatch):
    """默认站点端口完成测试后应提交统计，而不是对空会话调用 execute。"""
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
    """站点资源声明登录页后，通用检测应访问该页且不修改站点快照。"""
    db.watermark(Site, SiteStatistic)
    db.add(Site(
        name="资源路径站点",
        domain="resource-path.test",
        url="https://resource-path.test/",
        is_active=True,
    ))
    observed: list[SiteSnapshot] = []

    class _SitesHelper:
        """为资源路径测试提供最小站点索引器接口。"""

        def get_indexer(self, domain: str) -> dict:
            """返回测试站点的登录页配置。"""
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
    """解析器未取得用户身份时不得写入空数据或发送刷新成功事件。"""
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
