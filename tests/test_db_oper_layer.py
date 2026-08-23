"""
各业务 Oper 的数据访问行为。

Oper 层大多是模型方法的薄封装，但薄封装恰恰是最容易出错的地方：参数改名、
默认值漏传、聚合逻辑写在这一层——这些都绕过了模型侧的测试。这里对着真实数据库
验证 Oper 的对外契约，而不是验证它调了哪个模型方法。
"""
import asyncio
from unittest.mock import Mock

import pytest

from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.mediaserver import MediaServerOper
from app.db.models.downloadhistory import DownloadFiles, DownloadHistory
from app.db.models.mediaserver import MediaServerItem
from app.db.models.plugindata import PluginData
from app.db.models.site import Site
from app.db.models.siteicon import SiteIcon
from app.db.models.sitestatistic import SiteStatistic
from app.db.models.siteuserdata import SiteUserData
from app.db.models.user import User
from app.db.models.userconfig import UserConfig
from app.db.models.workflow import Workflow
from app.db.oper.plugindata import PluginDataOper
from app.db.oper.site import SiteOper
from app.db.oper.user import UserOper
from app.db.oper.userconfig import UserConfigOper
from app.db.oper.workflow import WorkflowOper
from app.schemas.types import MediaSource, MediaType

TMDB = str(MediaSource.TMDB)


def test_oper_with_explicit_session_does_not_commit_caller_transaction(db, monkeypatch):
    """显式会话写入只暂存，提交权必须留给 Application UoW。"""
    commit = Mock(wraps=db.session.commit)
    monkeypatch.setattr(db.session, "commit", commit)

    UserOper(db=db.session).add(name="op-uow-owner", hashed_password="x")

    assert User.get_by_name(db.session, "op-uow-owner") is not None
    commit.assert_not_called()
    db.session.rollback()


@pytest.fixture(autouse=True)
def _track(db):
    """把本文件涉及的表纳入用例级回收。"""
    db.watermark(Site, SiteIcon, SiteStatistic, SiteUserData, PluginData, Workflow,
                 User, UserConfig, MediaServerItem, DownloadHistory, DownloadFiles)


# --------------------------------------------------------------------------- #
# SiteOper
# --------------------------------------------------------------------------- #

def _site_kwargs(name: str, domain: str, **extra) -> dict:
    """构造新增站点的参数。"""
    return dict(name=name, domain=domain, url=f"https://{domain}/", **extra)


def test_site_oper_add_rejects_duplicate_domain(db):
    """
    同域名不得重复新增，并如实返回原因。

    重复新增会让同一站点出现两条配置，Cookie 更新只命中其中一条。
    """
    oper = SiteOper(db=db.session)

    assert oper.add(**_site_kwargs("站点", "op-a.test")) == (True, "新增站点成功")
    assert oper.add(**_site_kwargs("站点重复", "op-a.test")) == (False, "站点已存在")
    assert oper.exists("op-a.test") is True
    assert oper.exists("op-missing.test") is False


def test_site_oper_crud_round_trip(db):
    """
    新增、按 ID 取、更新、按域名取、列举、删除构成完整闭环。
    """
    oper = SiteOper(db=db.session)
    oper.add(**_site_kwargs("站点", "op-crud.test", pri=5))
    site = oper.get_by_domain("op-crud.test")

    assert oper.get(site.id).id == site.id
    assert oper.update(site.id, {"pri": 9}).pri == 9
    assert {s.domain for s in oper.list()} >= {"op-crud.test"}
    assert oper.get_domains_by_ids([site.id]) == ["op-crud.test"]
    assert {s.domain for s in oper.list_order_by_pri()} >= {"op-crud.test"}

    oper.delete(site.id)
    assert oper.get_by_domain("op-crud.test") is None


def test_site_oper_async_accessors_match_sync(db):
    """
    异步访问器必须与同步给出一致的结果。

    Oper 持有的是同步会话，异步模型方法会自行取一个异步会话——传参位置写错时
    这里会直接失败。
    """
    oper = SiteOper(db=db.session)
    oper.add(**_site_kwargs("异步站点", "op-async.test"))
    site = oper.get_by_domain("op-async.test")
    db.session.commit()

    assert asyncio.run(oper.async_get(site.id)).id == site.id
    assert asyncio.run(oper.async_get_by_domain("op-async.test")).id == site.id
    assert asyncio.run(oper.async_get_by_name("异步站点")).id == site.id
    assert {s.id for s in asyncio.run(oper.async_list())} >= {site.id}
    assert {s.id for s in asyncio.run(oper.async_list_active())} >= {site.id}
    assert asyncio.run(oper.async_update(site.id, {"pri": 3})).pri == 3


def test_site_oper_list_active_excludes_disabled(db):
    """
    启用列表排除停用站点。
    """
    oper = SiteOper(db=db.session)
    oper.add(**_site_kwargs("启用", "op-on.test", is_active=True))
    oper.add(**_site_kwargs("停用", "op-off.test", is_active=False))

    domains = {s.domain for s in oper.list_active()}

    assert "op-on.test" in domains and "op-off.test" not in domains


def test_site_oper_cookie_and_rss_updates_report_missing_site(db):
    """
    对不存在的站点更新 Cookie / RSS 必须返回失败，而不是静默成功。

    静默成功会让 CookieCloud 同步以为已生效，实际站点仍然登录失效。
    """
    oper = SiteOper(db=db.session)
    oper.add(**_site_kwargs("站点", "op-cookie.test"))

    assert oper.update_cookie("op-cookie.test", "k=v") == (True, "更新站点Cookie成功")
    assert oper.update_rss("op-cookie.test", "https://rss") == (True, "更新站点RSS地址成功")
    assert oper.get_by_domain("op-cookie.test").cookie == "k=v"

    assert oper.update_cookie("op-none.test", "k=v")[0] is False
    assert oper.update_rss("op-none.test", "https://rss")[0] is False


def test_site_oper_update_userdata_upserts_per_day(db):
    """
    站点用户数据按「站点 + 当天」落一条，同日重复上报走更新。

    每次插入会让当天出现多条快照，站点数据页面的日环比随之失真。
    """
    oper = SiteOper(db=db.session)

    oper.update_userdata("op-ud.test", "站点", {"upload": 100})
    oper.update_userdata("op-ud.test", "站点", {"upload": 200})

    rows = oper.get_userdata_by_domain("op-ud.test")
    assert len(rows) == 1 and rows[0].upload == 200


def test_site_oper_update_userdata_keeps_last_good_snapshot_on_error(db):
    """
    上报带错误信息时不得覆盖当天已有的成功数据。

    抓取失败时用空数据覆盖，页面会显示成「上传量归零」。
    """
    oper = SiteOper(db=db.session)
    oper.update_userdata("op-err.test", "站点", {"upload": 100})

    oper.update_userdata("op-err.test", "站点", {"upload": 0, "err_msg": "登录失败"})

    assert oper.get_userdata_by_domain("op-err.test")[0].upload == 100


def test_site_oper_userdata_readers(db):
    """
    用户数据的四个读取入口都应命中同一条快照。
    """
    oper = SiteOper(db=db.session)
    oper.update_userdata("op-read.test", "站点", {"upload": 50})
    today = oper.get_userdata_by_domain("op-read.test")[0].updated_day
    db.session.commit()

    assert any(r.domain == "op-read.test" for r in oper.get_userdata())
    assert any(r.domain == "op-read.test" for r in oper.get_userdata_by_date(today))
    assert any(r.domain == "op-read.test" for r in oper.get_userdata_latest())
    assert [r.domain for r in asyncio.run(
        oper.async_get_userdata_by_domain("op-read.test"))] == ["op-read.test"]


def test_site_oper_update_icon_creates_then_only_overwrites_with_content(db):
    """
    图标首次写入后，只有拿到新的 base64 才覆盖。

    抓取失败返回空 base64 时覆盖，会把已有图标清成空白。
    """
    oper = SiteOper(db=db.session)

    oper.update_icon("站点", "op-icon.test", "https://op-icon.test/a.ico", "AAA")
    first = oper.get_icon_by_domain("op-icon.test").base64

    oper.update_icon("站点", "op-icon.test", "https://op-icon.test/b.ico", "")

    assert oper.get_icon_by_domain("op-icon.test").base64 == first
    assert first.startswith("data:image/ico;base64,")


def test_site_oper_icon_without_explicit_session_uses_transaction_runner(db):
    """无显式会话的图标写入应由兼容事务执行器完成提交。"""
    oper = SiteOper()

    oper.update_icon(
        "兼容站点",
        "op-icon-no-session.test",
        "https://op-icon-no-session.test/favicon.ico",
        "AAA",
    )

    icon = SiteIcon.get_by_domain(db.session, "op-icon-no-session.test")
    assert icon.name == "兼容站点"
    assert icon.base64 == "data:image/ico;base64,AAA"


def test_site_oper_success_accumulates_and_records_state(db):
    """
    访问成功累加计数并把最后状态标记为成功。
    """
    oper = SiteOper(db=db.session)
    for seconds in range(1, 5):
        oper.success("op-stat.test", seconds=seconds)

    stat = SiteStatistic.get_by_domain(db.session, "op-stat.test")

    assert stat.success == 4
    assert stat.lst_state == 0
    assert stat.seconds


def test_site_oper_statistics_without_explicit_session_use_transaction_runner(db):
    """无显式会话的站点统计必须由兼容事务执行器完成提交。"""
    oper = SiteOper()

    oper.success("op-stat-no-session.test", seconds=3)
    oper.fail("op-stat-no-session.test")

    stat = SiteStatistic.get_by_domain(db.session, "op-stat-no-session.test")
    assert (stat.success, stat.fail, stat.lst_state) == (1, 1, 1)


def test_site_oper_success_caps_the_timing_note_at_ten_entries(db):
    """
    耗时记录最多保留最近 10 条，超出时丢弃最旧的。

    不设上限时这个 JSON 字段会随每次访问无限增长，最终把整行撑大到影响查询。
    直接预置 10 条历史时间戳再上报一次——同一秒内连续调用会写进同一个键，
    只靠循环调用无法触及上限分支。
    """
    old_note = {f"2026-08-13 10:00:{index:02d}": index + 1 for index in range(10)}
    db.add(SiteStatistic(domain="op-cap.test", success=10, fail=0, seconds=5,
                         lst_state=0, note=old_note))

    SiteOper(db=db.session).success("op-cap.test", seconds=99)

    note = SiteStatistic.get_by_domain(db.session, "op-cap.test").note
    assert len(note) == 10
    assert "2026-08-13 10:00:00" not in note, "超出上限时应丢弃最旧的一条"


def test_site_oper_fail_creates_then_accumulates(db):
    """
    访问失败首次建行、其后累加，并把最后状态标记为失败。
    """
    oper = SiteOper(db=db.session)

    oper.fail("op-fail.test")
    oper.fail("op-fail.test")

    stat = SiteStatistic.get_by_domain(db.session, "op-fail.test")
    assert (stat.fail, stat.lst_state) == (2, 1)


def test_site_oper_async_success_and_fail_match_sync(db):
    """
    异步的成功/失败统计与同步同规则：首次建行、其后累加。
    """
    oper = SiteOper(db=db.session)

    asyncio.run(oper.async_success("op-async-stat.test", seconds=2))
    asyncio.run(oper.async_success("op-async-stat.test", seconds=4))
    asyncio.run(oper.async_fail("op-async-fail.test"))
    asyncio.run(oper.async_fail("op-async-fail.test"))

    assert SiteStatistic.get_by_domain(db.session, "op-async-stat.test").success == 2
    assert SiteStatistic.get_by_domain(db.session, "op-async-fail.test").fail == 2


# --------------------------------------------------------------------------- #
# PluginDataOper
# --------------------------------------------------------------------------- #

def test_plugindata_oper_save_is_upsert(db):
    """
    同一键重复保存走更新而不是新增。

    新增会让读取拿到旧值（取到第一条），插件配置改了却不生效。
    """
    oper = PluginDataOper(db=db.session)

    oper.save("PluginX", "k", {"v": 1})
    oper.save("PluginX", "k", {"v": 2})

    assert oper.get_data("PluginX", "k") == {"v": 2}
    assert len(oper.get_data_all("PluginX")) == 1


def test_plugindata_oper_get_without_key_returns_all_rows(db):
    """
    不给键时返回该插件的全部数据行；键不存在时返回 None。
    """
    oper = PluginDataOper(db=db.session)
    oper.save("PluginY", "a", {"v": 1})
    oper.save("PluginY", "b", {"v": 2})

    assert {row.key for row in oper.get_data("PluginY")} == {"a", "b"}
    assert oper.get_data("PluginY", "missing") is None


def test_plugindata_oper_delete_scopes_by_key_then_plugin(db):
    """
    删除可精确到键，也可整插件清空，且都不波及其他插件。
    """
    oper = PluginDataOper(db=db.session)
    oper.save("PluginZ", "a", {"v": 1})
    oper.save("PluginZ", "b", {"v": 2})
    oper.save("PluginW", "a", {"v": 3})

    oper.del_data("PluginZ", "a")
    assert {row.key for row in oper.get_data("PluginZ")} == {"b"}

    oper.del_data("PluginZ")
    assert oper.get_data("PluginZ") == []
    assert oper.get_data("PluginW", "a") == {"v": 3}


def test_plugindata_oper_async_accessors_match_sync(db):
    """
    异步读写与同步一致。
    """
    oper = PluginDataOper(db=db.session)
    asyncio.run(oper.async_save("PluginAsync", "k", {"v": 1}))
    asyncio.run(oper.async_save("PluginAsync", "k", {"v": 2}))

    assert asyncio.run(oper.async_get_data("PluginAsync", "k")) == {"v": 2}
    assert asyncio.run(oper.async_get_data("PluginAsync", "missing")) is None
    assert len(asyncio.run(oper.async_get_data_all("PluginAsync"))) == 1


# --------------------------------------------------------------------------- #
# WorkflowOper
# --------------------------------------------------------------------------- #

def _workflow_kwargs(name: str, **extra) -> dict:
    """构造新增工作流的参数。"""
    return dict(name=name, description=name, timer="0 * * * *", state="W",
                actions=[], flows=[], context={}, execution_state={}, **extra)


def test_workflow_oper_add_rejects_duplicate_name(db):
    """
    同名工作流不得重复新增。
    """
    oper = WorkflowOper(db=db.session)

    assert oper.add(**_workflow_kwargs("op-wf")) == (True, "新增工作流成功")
    assert oper.add(**_workflow_kwargs("op-wf")) == (False, "工作流已存在")


def test_workflow_oper_exposes_lists_and_lifecycle(db):
    """
    列表入口与生命周期方法都应透传到模型并落库。
    """
    oper = WorkflowOper(db=db.session)
    oper.add(**_workflow_kwargs("op-wf-life", trigger_type="timer"))
    flow = oper.get_by_name("op-wf-life")

    assert oper.get(flow.id).id == flow.id
    assert {w.name for w in oper.list()} >= {"op-wf-life"}
    assert {w.name for w in oper.list_enabled()} >= {"op-wf-life"}
    assert {w.name for w in oper.get_timer_triggered_workflows()} >= {"op-wf-life"}

    oper.start(flow.id)
    assert oper.get(flow.id).state == "R"
    oper.step(flow.id, "a1", {"n": 1})
    assert oper.get(flow.id).current_action == "a1"
    oper.success(flow.id, "完成")
    assert oper.get(flow.id).state == "S"
    oper.fail(flow.id, "出错")
    assert oper.get(flow.id).state == "F"
    oper.reset(flow.id, reset_count=True)
    assert (oper.get(flow.id).state, oper.get(flow.id).run_count) == ("W", 0)


def test_workflow_oper_no_session_uses_configured_uow_writer(db):
    """旧的无 Session Oper 写入口仍可用，但事务由组合根服务持有。"""
    flow = db.add(Workflow(**_workflow_kwargs("op-wf-legacy")))

    assert WorkflowOper().start(flow.id) is True

    db.session.expire_all()
    assert WorkflowOper(db=db.session).get(flow.id).state == "R"


def test_workflow_oper_event_list_and_async_accessors(db):
    """
    事件触发列表与异步访问器同样可用。
    """
    oper = WorkflowOper(db=db.session)
    oper.add(**_workflow_kwargs("op-wf-event", trigger_type="event"))
    flow = oper.get_by_name("op-wf-event")
    db.session.commit()

    assert {w.name for w in oper.get_event_triggered_workflows()} >= {"op-wf-event"}
    assert asyncio.run(oper.async_get(flow.id)).id == flow.id
    assert asyncio.run(oper.async_get_by_name("op-wf-event")).id == flow.id
    assert {w.id for w in asyncio.run(oper.async_list())} >= {flow.id}


# --------------------------------------------------------------------------- #
# UserOper / UserConfigOper
# --------------------------------------------------------------------------- #

def test_user_oper_reads_permissions_and_settings(db):
    """
    权限与个性化设置的读取在用户不存在时各有约定的空值。

    权限返回 {} 而设置返回 None——上层据此区分「没有权限」和「没有这个用户」。
    """
    oper = UserOper(db=db.session)
    oper.add(name="op-user", hashed_password="x",
             permissions={"discovery": True}, settings={"theme": "dark"})

    assert oper.get_by_name("op-user").name == "op-user"
    assert oper.get_permissions("op-user") == {"discovery": True}
    assert oper.get_settings("op-user") == {"theme": "dark"}
    assert oper.get_setting("op-user", "theme") == "dark"
    assert oper.get_setting("op-user", "missing") is None

    assert oper.get_permissions("op-nobody") == {}
    assert oper.get_settings("op-nobody") is None
    assert oper.get_setting("op-nobody", "theme") is None
    assert {u.name for u in oper.list()} >= {"op-user"}


def test_user_oper_async_accessors_match_sync(db):
    """
    异步按名、按 ID 取用户与同步结果一致。
    """
    oper = UserOper(db=db.session)
    oper.add(name="op-user-async", hashed_password="x")
    user = oper.get_by_name("op-user-async")
    db.session.commit()

    assert asyncio.run(oper.async_get_by_name("op-user-async")).id == user.id
    assert asyncio.run(oper.async_get_by_id(user.id)).id == user.id


def test_userconfig_oper_set_get_and_delete_on_empty_value(db):
    """
    用户配置写入后可读回；写入空值等同于删除该项。

    空值删除是「恢复默认」的实现方式，退化成写入空串会让默认值再也拿不回来。
    """
    oper = UserConfigOper()
    oper.set("op-cfg-user", "theme", "dark")

    assert oper.get("op-cfg-user", "theme") == "dark"
    assert oper.get("op-cfg-user")["theme"] == "dark"
    assert UserConfig.get_by_key(db.session, username="op-cfg-user", key="theme") is not None

    oper.set("op-cfg-user", "theme", None)
    assert UserConfig.get_by_key(db.session, username="op-cfg-user", key="theme") is None


def test_userconfig_oper_scopes_cache_by_username(db):
    """
    内存缓存必须按用户名隔离，且用户名为空时返回全量缓存。
    """
    oper = UserConfigOper()
    oper.set("op-cfg-a", "theme", "dark")
    oper.set("op-cfg-b", "theme", "light")

    assert oper.get("op-cfg-a", "theme") == "dark"
    assert oper.get("op-cfg-b", "theme") == "light"
    assert oper.get("op-cfg-missing", "theme") is None
    assert oper.get("op-cfg-missing") is None
    assert set(oper.get(None)) >= {"op-cfg-a", "op-cfg-b"}


# --------------------------------------------------------------------------- #
# MediaServerOper
# --------------------------------------------------------------------------- #

def _server_item(item_id: str, **extra) -> dict:
    """构造媒体服务器条目的写入参数。"""
    payload = dict(server="emby", library="lib", item_id=item_id, item_type="电影",
                   title="片名", year="2026", media_source=TMDB, media_id="5001")
    payload.update(extra)
    return payload


def test_mediaserver_oper_add_requires_item_id(db):
    """
    缺少条目 ID 的数据不得写入——它是媒体服务器侧的唯一标识，缺了就无法再更新。
    """
    oper = MediaServerOper(db=db.session)

    assert oper.add(**_server_item("ms-1")) is True
    assert oper.add(**_server_item(None)) is False


def test_mediaserver_oper_upsert_updates_existing_item(db):
    """
    同一服务器同一条目重复同步走更新而不是新增，返回值表示「是否新增」。

    调用方据这个布尔值统计本次同步新入库了多少条；把更新也算作新增会让
    同步报告每次都显示全量新增。
    """
    oper = MediaServerOper(db=db.session)

    assert oper.upsert(**_server_item("ms-up", title="旧标题")) is True
    assert oper.upsert(**_server_item("ms-up", title="新标题")) is False

    assert MediaServerItem.get_by_server_itemid(db.session, "emby", "ms-up").title == "新标题"


def test_mediaserver_oper_exists_by_identity_and_title(db):
    """
    存在性判断支持媒体身份与标题两条路径，条件不足时返回 None。
    """
    oper = MediaServerOper(db=db.session)
    oper.add(**_server_item("ms-ex", media_id="5100", title="存在的片"))

    assert oper.exists(media_source=TMDB, media_id="5100", mtype="电影") is not None
    assert oper.exists(title="存在的片", mtype="电影", year="2026") is not None
    assert oper.exists(title="不存在的片") is None
    assert oper.exists() is None


def test_mediaserver_oper_exists_checks_season_presence(db):
    """
    要求某一季时必须在季信息里真正存在，否则视为未入库。

    季信息缺失却判为已入库，会让整季订阅被跳过。
    """
    oper = MediaServerOper(db=db.session)
    oper.add(**_server_item("ms-season", media_id="5200", item_type="电视剧",
                            seasoninfo={"1": [1, 2]}))

    assert oper.exists(media_source=TMDB, media_id="5200", mtype="电视剧",
                       season="1") is not None
    assert oper.exists(media_source=TMDB, media_id="5200", mtype="电视剧",
                       season="2") is None

    oper.add(**_server_item("ms-noseason", media_id="5300", item_type="电视剧"))
    assert oper.exists(media_source=TMDB, media_id="5300", mtype="电视剧",
                       season="1") is None


def test_mediaserver_oper_get_item_id_and_async_twins(db):
    """
    取条目 ID 与异步版本必须给出相同结果，未命中时返回 None。
    """
    oper = MediaServerOper(db=db.session)
    oper.add(**_server_item("ms-id", media_id="5400"))
    db.session.commit()

    assert oper.get_item_id(media_source=TMDB, media_id="5400", mtype="电影") == "ms-id"
    assert oper.get_item_id(media_source=TMDB, media_id="5999", mtype="电影") is None
    assert asyncio.run(oper.async_get_item_id(
        media_source=TMDB, media_id="5400", mtype="电影")) == "ms-id"
    assert asyncio.run(oper.async_exists(title="片名", mtype="电影", year="2026")) is not None


def test_mediaserver_oper_cleanup_entry_points(db):
    """
    清理入口按服务器、按同步时间、按配置列表三种口径工作。
    """
    oper = MediaServerOper(db=db.session)
    oper.add(**_server_item("ms-c1", lst_mod_date="2026-08-13 12:00:00"))
    oper.add(**_server_item("ms-c2", lst_mod_date="2026-01-01 12:00:00"))

    assert oper.delete_stale("emby", "2026-08-13 12:00:00") == 1
    assert oper.delete_excluded_servers(["plex"]) == 1

    oper.add(**_server_item("ms-c3"))
    oper.empty("emby")
    assert MediaServerItem.get_by_itemid(db.session, "ms-c3") is None


# --------------------------------------------------------------------------- #
# DownloadHistoryOper
# --------------------------------------------------------------------------- #

def test_downloadhistory_oper_get_by_hashes_returns_a_mapping(db):
    """
    批量查询返回「hash -> 历史」映射，供上层直接按 hash 取用。

    上层拿到列表还要自己配对，正是 N+1 的温床；这里的契约是映射。
    """
    oper = DownloadHistoryOper(db=db.session)
    oper.add(path="/downloads/a", type=MediaType.TV.value, title="A",
             download_hash="oh-a", date="2026-08-13 10:00:00")
    oper.add(path="/downloads/b", type=MediaType.TV.value, title="B",
             download_hash="oh-b", date="2026-08-13 10:00:00")

    mapping = oper.get_by_hashes(["oh-a", "oh-b", "oh-missing"])

    assert set(mapping) == {"oh-a", "oh-b"}
    assert mapping["oh-a"].title == "A"
    assert oper.get_by_hashes([]) == {}


def test_downloadhistory_oper_file_entry_points(db):
    """
    文件记录的写入与四个读取入口构成完整闭环，删除只置状态。
    """
    oper = DownloadHistoryOper(db=db.session)
    oper.add_files([
        dict(downloader="qb", download_hash="oh-f", fullpath="/downloads/f/a.mkv",
             savepath="/downloads/f", filepath="a.mkv", torrentname="种子", state=1),
        dict(downloader="qb", download_hash="oh-f", fullpath="/downloads/f/b.mkv",
             savepath="/downloads/f", filepath="b.mkv", torrentname="种子", state=1),
    ])

    assert len(oper.get_files_by_hash("oh-f")) == 2
    assert len(oper.get_files_by_hash("oh-f", state=1)) == 2
    assert oper.get_file_by_fullpath("/downloads/f/a.mkv") is not None
    assert len(oper.get_files_by_fullpath("/downloads/f/a.mkv")) == 1
    assert len(oper.get_files_by_savepath("/downloads/f")) == 2
    assert oper.get_hash_by_fullpath("/downloads/f/a.mkv") == "oh-f"
    # 查不到时返回空串而非 None：调用方直接用它拼下载器请求，None 会变成字面量 "None"
    assert oper.get_hash_by_fullpath("/downloads/f/none.mkv") == ""

    oper.delete_file_by_fullpath("/downloads/f/a.mkv")
    assert oper.get_file_by_fullpath("/downloads/f/a.mkv").state == 0


def test_downloadhistory_oper_query_entry_points(db):
    """
    路径、hash、媒体身份、分页与时间窗口五个查询入口都应透传生效。
    """
    oper = DownloadHistoryOper(db=db.session)
    oper.add(path="/downloads/q", type=MediaType.TV.value, title="Q", year="2026",
             media_source=TMDB, media_id="4001", seasons="S01",
             download_hash="oh-q", username="alice", date="2026-08-13 10:00:00")

    assert oper.get_by_path("/downloads/q").title == "Q"
    assert oper.get_by_hash("oh-q").title == "Q"
    assert len(oper.get_by_media_identity(media_source=TMDB, media_id="4001")) == 1
    assert oper.list_by_page(page=1, count=1)[0].title == "Q"
    assert [h.title for h in oper.list_by_user_date("2026-08-20", username="alice")] == ["Q"]
    assert [h.title for h in oper.list_by_date("2026-08-01", MediaType.TV.value,
                                               TMDB, "4001", "S01")] == ["Q"]
    assert [h.title for h in oper.list_by_type(MediaType.TV.value, days=36500)] == ["Q"]
    assert [h.title for h in oper.get_last_by(mtype=MediaType.TV.value,
                                              media_source=TMDB, media_id="4001")] == ["Q"]


def test_downloadhistory_oper_delete_entry_points(db):
    """
    历史与文件记录的删除入口都应真正落库。
    """
    oper = DownloadHistoryOper(db=db.session)
    oper.add(path="/downloads/d", type=MediaType.TV.value, title="D",
             download_hash="oh-d", date="2026-08-13 10:00:00")
    history = oper.get_by_hash("oh-d")
    oper.add_files([dict(downloader="qb", download_hash="oh-d",
                         fullpath="/downloads/d/a.mkv", savepath="/downloads/d",
                         filepath="a.mkv", torrentname="种子", state=1)])
    file_row = oper.get_file_by_fullpath("/downloads/d/a.mkv")

    oper.delete_downloadfile(file_row.id)
    assert oper.get_file_by_fullpath("/downloads/d/a.mkv") is None

    oper.delete_history(history.id)
    assert oper.get_by_hash("oh-d") is None


def test_downloadhistory_oper_async_delete(db):
    """
    异步删除历史与同步等效。
    """
    oper = DownloadHistoryOper(db=db.session)
    oper.add(path="/downloads/ad", type=MediaType.TV.value, title="AD",
             download_hash="oh-ad", date="2026-08-13 10:00:00")
    history = oper.get_by_hash("oh-ad")
    db.session.commit()

    asyncio.run(oper.async_delete_history(history.id))

    assert oper.get_by_hash("oh-ad") is None
