"""站点连通性测试链路的数据库回归测试。"""

from app.chain.site import SiteChain
from app.db.models.site import Site
from app.db.models.sitestatistic import SiteStatistic


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
