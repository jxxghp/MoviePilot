"""站点连通性测试链路的数据库回归测试。"""

from app.application.site.contract import SiteSnapshot
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


def test_indexphp_connectivity_uses_derived_snapshot_without_mutation(monkeypatch):
    """index.php 特殊测试应派生新快照，不得修改冻结站点配置。"""
    observed: list[SiteSnapshot] = []
    monkeypatch.setattr(
        SiteChain,
        "_SiteChain__test",
        lambda _self, site: observed.append(site) or (True, "连接成功"),
    )
    chain = SiteChain.__new__(SiteChain)
    original = SiteSnapshot(
        id=1,
        name="Index PHP",
        url="https://index.example/",
    )

    result = chain._SiteChain__indexphp_test(original)

    assert result == (True, "连接成功")
    assert original.url == "https://index.example/"
    assert observed[0].url == "https://index.example/index.php"
