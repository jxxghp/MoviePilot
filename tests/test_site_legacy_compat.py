"""旧 SiteOper 插件导入、签名和无 Session 行为门禁。"""

import importlib
import inspect

from app.db.models.site import Site
from app.db.models.sitestatistic import SiteStatistic
from app.db.models.siteuserdata import SiteUserData
from app.db.oper.site import SiteOper as CanonicalSiteOper
from app.runtime.compat.manifest import MODULE_ALIASES


def test_legacy_site_import_targets_private_sdk_facade() -> None:
    """旧 DB 路径与 Oper 包根只经私有 Legacy SDK 解析 SiteOper。"""
    alias = MODULE_ALIASES["app.db.site_oper"]
    legacy = importlib.import_module("app.db.site_oper")

    assert alias.target == "app.sdk._legacy.site"
    assert alias.owner == "sdk"
    assert alias.replacement == "app.application.site.contract.SiteRepository"
    assert legacy is importlib.import_module(alias.target)
    assert legacy.__all__ == ["SiteOper"]
    assert issubclass(legacy.SiteOper, CanonicalSiteOper)
    assert legacy.SiteOper is not CanonicalSiteOper

    oper_package = importlib.import_module("app.db.oper")
    assert oper_package.SiteOper is legacy.SiteOper
    assert "SiteOper" not in oper_package.__all__


def test_legacy_site_oper_preserves_plugin_method_signatures() -> None:
    """官方插件实际调用的八个 SiteOper 方法保持参数和返回 ABI。"""
    oper_type = importlib.import_module("app.db.site_oper").SiteOper

    assert str(inspect.signature(oper_type.get)) == (
        "(self, sid: int) -> app.db.models.site.Site | None"
    )
    assert str(inspect.signature(oper_type.list)) == (
        "(self) -> List[app.db.models.site.Site]"
    )
    assert str(inspect.signature(oper_type.list_order_by_pri)) == (
        "(self) -> List[app.db.models.site.Site]"
    )
    assert str(inspect.signature(oper_type.list_active)) == (
        "(self) -> List[app.db.models.site.Site]"
    )
    assert str(inspect.signature(oper_type.get_userdata_latest)) == (
        "(self) -> List[app.db.models.siteuserdata.SiteUserData]"
    )
    assert str(inspect.signature(oper_type.get_userdata_by_date)) == (
        "(self, date: str) -> List[app.db.models.siteuserdata.SiteUserData]"
    )
    assert str(inspect.signature(oper_type.success)) == (
        "(self, domain: str, seconds: int | None = None)"
    )
    assert str(inspect.signature(oper_type.fail)) == "(self, domain: str)"


def test_legacy_site_oper_preserves_no_session_plugin_behavior(db) -> None:
    """旧插件无需注入 Session 即可查询站点、用户数据并写访问统计。"""
    db.watermark(Site, SiteStatistic, SiteUserData)
    active = Site(
        name="兼容站点",
        domain="legacy-site.test",
        url="https://legacy-site.test/",
        pri=1,
        is_active=True,
    )
    inactive = Site(
        name="停用站点",
        domain="legacy-disabled.test",
        url="https://legacy-disabled.test/",
        pri=2,
        is_active=False,
    )
    userdata = SiteUserData(
        domain="legacy-site.test",
        name="兼容站点",
        username="user",
        upload=100,
        updated_day="2026-08-28",
        updated_time="10:00:00",
        err_msg="",
    )
    db.add(active, inactive, userdata)
    db.session.commit()

    oper = importlib.import_module("app.db.site_oper").SiteOper()

    assert oper.get(active.id).domain == "legacy-site.test"
    assert [site.domain for site in oper.list_order_by_pri()] == [
        "legacy-site.test",
        "legacy-disabled.test",
    ]
    assert [site.domain for site in oper.list_active()] == ["legacy-site.test"]
    assert [row.domain for row in oper.get_userdata_latest()] == [
        "legacy-site.test"
    ]
    assert [row.domain for row in oper.get_userdata_by_date("2026-08-28")] == [
        "legacy-site.test"
    ]

    oper.success("legacy-site.test", seconds=3)
    oper.fail("legacy-site.test")
    statistic = SiteStatistic.get_by_domain(db.session, "legacy-site.test")
    assert statistic is not None
    assert (statistic.success, statistic.fail, statistic.lst_state) == (1, 1, 1)
