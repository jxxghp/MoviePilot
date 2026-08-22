"""
插件数据、消息、Agent 会话、Agent 定时任务与下载失败冷却五张表的查询行为。

这一组的共同风险是「按用户/插件归属收窄」和「分页 + 排序」：归属条件丢失就是越权，
分页排序错乱则表现为消息重复或漏掉，两者都不会抛异常。
"""
import asyncio

import pytest

from app.db.models.agentchat import AgentChat
from app.db.models.agenttask import AgentTask
from app.db.models.downloadfailure import DownloadFailure
from app.db.models.message import Message
from app.db.models.plugindata import PluginData
from app.db.oper.agenttask import AgentTaskOper


@pytest.fixture(autouse=True)
def _track(db):
    """把本文件涉及的表纳入用例级回收。"""
    db.watermark(PluginData, Message, AgentChat, AgentTask, DownloadFailure)


# --------------------------------------------------------------------------- #
# PluginData
# --------------------------------------------------------------------------- #

def test_plugindata_is_scoped_by_plugin_id(db):
    """
    插件数据必须按插件隔离——串读会让一个插件拿到另一个插件的配置。
    """
    db.add(PluginData(plugin_id="PluginA", key="k1", value={"v": 1}),
           PluginData(plugin_id="PluginA", key="k2", value={"v": 2}),
           PluginData(plugin_id="PluginB", key="k1", value={"v": 3}))

    rows = PluginData.get_plugin_data(db.session, "PluginA")

    assert {r.key for r in rows} == {"k1", "k2"}
    assert {r.key for r in asyncio.run(PluginData.async_get_plugin_data(plugin_id="PluginA"))} \
        == {"k1", "k2"}


def test_plugindata_get_by_key_needs_both_plugin_and_key(db):
    """
    按键取值必须同时匹配插件与键，只匹配键会取到同名键的别家数据。
    """
    db.add(PluginData(plugin_id="PluginA", key="shared", value={"v": 1}),
           PluginData(plugin_id="PluginB", key="shared", value={"v": 2}))

    assert PluginData.get_plugin_data_by_key(db.session, "PluginA", "shared").value == {"v": 1}
    assert PluginData.get_plugin_data_by_key(db.session, "PluginB", "shared").value == {"v": 2}
    assert PluginData.get_plugin_data_by_key(db.session, "PluginC", "shared") is None
    assert asyncio.run(PluginData.async_get_plugin_data_by_key(
        plugin_id="PluginA", key="shared")).value == {"v": 1}


def test_plugindata_delete_by_key_removes_only_that_entry(db):
    """
    删除单个键不能波及同插件的其他键，也不能波及别的插件。
    """
    db.add(PluginData(plugin_id="PluginA", key="drop", value={"v": 1}),
           PluginData(plugin_id="PluginA", key="keep", value={"v": 2}),
           PluginData(plugin_id="PluginB", key="drop", value={"v": 3}))

    PluginData.del_plugin_data_by_key(db.session, "PluginA", "drop")

    assert PluginData.get_plugin_data_by_key(db.session, "PluginA", "drop") is None
    assert PluginData.get_plugin_data_by_key(db.session, "PluginA", "keep") is not None
    assert PluginData.get_plugin_data_by_key(db.session, "PluginB", "drop") is not None


def test_plugindata_delete_all_clears_only_that_plugin(db):
    """
    卸载插件时清空其数据，不能连带清掉其他插件——那等于误删用户配置。
    """
    db.add(PluginData(plugin_id="PluginA", key="k1", value={"v": 1}),
           PluginData(plugin_id="PluginB", key="k1", value={"v": 2}))

    PluginData.del_plugin_data(db.session, "PluginA")

    assert PluginData.get_plugin_data(db.session, "PluginA") == []
    assert len(PluginData.get_plugin_data_by_plugin_id(db.session, "PluginB")) == 1


# --------------------------------------------------------------------------- #
# Message
# --------------------------------------------------------------------------- #

def _message(reg_time: str, title: str, source: str = None, action: int = 1,
             image: str = None) -> Message:
    """构造一条消息记录。"""
    return Message(channel="wechat", source=source, mtype="Manual", title=title,
                   text=title, reg_time=reg_time, action=action, image=image)


def test_message_list_by_page_is_newest_first_and_paged(db):
    """
    消息列表按登记时间倒序、同时间按主键倒序，并遵守分页。

    排序不稳定时相邻两页会出现重复或漏掉的消息，用户看到的是「消息丢了」。
    """
    for index in range(5):
        db.add(_message(f"2026-08-13 10:00:0{index}", f"msg-{index}"))

    first_page = Message.list_by_page(db.session, page=1, count=2)
    second_page = Message.list_by_page(db.session, page=2, count=2)

    assert [m.title for m in first_page] == ["msg-4", "msg-3"]
    assert [m.title for m in second_page] == ["msg-2", "msg-1"]


def test_message_list_by_page_matches_async_twin(db):
    """
    同步与异步分页必须返回同一批消息，前端两条链路才不会互相矛盾。
    """
    for index in range(3):
        db.add(_message(f"2026-08-13 11:00:0{index}", f"par-{index}"))

    sync_titles = [m.title for m in Message.list_by_page(db.session, page=1, count=3)]
    async_titles = [m.title for m in asyncio.run(Message.async_list_by_page(page=1, count=3))]

    assert sync_titles == async_titles


def test_message_exists_by_source_detects_duplicates(db):
    """
    来源标识存在性判断用于消息去重，判错会导致同一条通知重复推送。
    """
    db.add(_message("2026-08-13 10:00:00", "有来源", source="uniq-source-1"))

    assert Message.exists_by_source(db.session, "uniq-source-1") is True
    assert Message.exists_by_source(db.session, "uniq-source-missing") is False


def test_message_delete_before_is_batched_and_keeps_recent(db):
    """
    历史消息清理必须分批、遵守上限，且不碰保留期内的消息。
    """
    for index in range(4):
        db.add(_message(f"2026-01-01 10:00:0{index}", f"old-{index}"))
    recent = db.add(_message("2026-08-13 10:00:00", "recent"))

    assert Message.delete_before(db.session, before_time="2026-08-01", limit=2) == 2
    assert Message.delete_before(db.session, before_time="2026-08-01", limit=100) == 2
    assert Message.delete_before(db.session, before_time="2026-08-01", limit=100) == 0

    assert Message.list_by_page(db.session, page=1, count=1)[0].id == recent.id


def test_message_delete_before_keeps_the_row_exactly_at_the_boundary(db):
    """
    保留时间点上的消息属于「保留期内」，不能被清理掉（``reg_time < before_time``）。

    比较符若写成 ``<=``，每次清理都会多吃掉恰好落在保留起点的那一批消息；
    数据从不压在边界上时这一字之差完全不可观测，故此处专门把行摆在边界上。
    """
    boundary = "2026-05-01 00:00:00"
    at_boundary = db.add(_message(boundary, "边界上"))
    db.add(_message("2026-04-30 23:59:59", "边界前一秒"))

    assert Message.delete_before(db.session, before_time=boundary, limit=100) == 1

    assert db.session.get(Message, at_boundary.id) is not None


def test_message_async_list_sent_excludes_the_clear_boundary(db):
    """
    三个清理水位都取「严格晚于水位」的消息，正好落在水位上的必须被滤掉。

    水位是「本次清空动作发生的时刻」，与它同一秒的消息属于已清空的那一批；
    比较符放宽成 ``>=`` 会让用户清空后又看见最后一条旧消息。
    """
    boundary, after = "2026-03-01 10:00:00", "2026-03-01 10:00:01"
    db.add(_message(boundary, "bd-系统-边界上"),
           _message(after, "bd-系统-边界后"),
           _message(boundary, "bd-媒体-边界上", image="http://img/1.jpg"),
           _message(after, "bd-媒体-边界后", image="http://img/2.jpg"))

    def _titles(**clears) -> set:
        """取本用例写入的消息标题集合，隔离其他用例可能残留的消息。"""
        rows = asyncio.run(Message.async_list_sent_by_page(page=1, count=100, **clears))
        return {m.title for m in rows if m.title.startswith("bd-")}

    # 全量清空水位：边界上的两条都属于被清空的那一批
    assert _titles(all_clear_before=boundary) == {"bd-系统-边界后", "bd-媒体-边界后"}
    # 系统消息（无图）清空水位：只吃无图消息，带图的媒体消息不受影响
    assert _titles(system_clear_before=boundary) == {
        "bd-系统-边界后", "bd-媒体-边界上", "bd-媒体-边界后"}
    # 媒体消息（有图）清空水位：只吃带图消息，无图的系统消息不受影响
    assert _titles(media_clear_before=boundary) == {
        "bd-系统-边界上", "bd-系统-边界后", "bd-媒体-边界后"}


def test_message_create_and_to_dict_returns_persisted_fields(db):
    """
    创建后返回的字典必须已带上数据库生成的主键。

    返回未落库的字段会让调用方拿到 id 为 None 的消息，后续更新无从下手。
    """
    created = _message("2026-08-13 10:00:00", "新消息").create_and_to_dict(db.session)

    assert created["id"] is not None
    assert created["title"] == "新消息"


# --------------------------------------------------------------------------- #
# AgentChat
# --------------------------------------------------------------------------- #

def _chat(session_id: str, user_id: str = "u1", updated_at: str = "2026-08-13 10:00:00",
          username: str = None) -> AgentChat:
    """构造一条 Agent 会话记录。"""
    return AgentChat(session_id=session_id, user_id=user_id, username=username,
                     channel="web", title=session_id, updated_at=updated_at,
                     created_at=updated_at, message_count=0)


def test_agentchat_get_by_session_takes_the_newest_row(db):
    """
    同一会话 ID 存在多行时取主键最大的那条——它才是最新的会话状态。
    """
    db.add(_chat("s-dup"), _chat("s-dup"))
    newest = db.add(_chat("s-dup"))

    assert AgentChat.get_by_session(db.session, "s-dup").id == newest.id
    assert asyncio.run(AgentChat.async_get_by_session(session_id="s-dup")).id == newest.id


def test_agentchat_get_by_session_enforces_user_scope(db):
    """
    传入用户 ID 时必须同时匹配，否则一个用户能读到另一个用户的会话内容。
    """
    db.add(_chat("s-owned", user_id="alice"))

    assert AgentChat.get_by_session(db.session, "s-owned", user_id="alice") is not None
    assert AgentChat.get_by_session(db.session, "s-owned", user_id="bob") is None
    assert asyncio.run(AgentChat.async_get_by_session(session_id="s-owned", user_id="bob")) is None


def test_agentchat_list_by_page_matches_either_user_or_username(db):
    """
    同时给出用户 ID 与用户名时按「或」匹配。

    渠道侧只有用户名、前端只有用户 ID，改成「与」会让两边各自都查不到自己的会话。
    """
    db.add(_chat("s-by-id", user_id="uid-1", username=None),
           _chat("s-by-name", user_id="uid-other", username="alice"),
           _chat("s-neither", user_id="uid-x", username="bob"))

    listed = AgentChat.list_by_page(db.session, user_id="uid-1", username="alice")

    assert {c.session_id for c in listed} == {"s-by-id", "s-by-name"}


@pytest.mark.parametrize("kwargs,expected", [
    ({"user_id": "uid-1"}, {"s-by-id"}),
    ({"username": "alice"}, {"s-by-name"}),
])
def test_agentchat_list_by_page_single_scope(db, kwargs, expected):
    """
    只给用户 ID 或只给用户名时，各自按单一条件收窄。
    """
    db.add(_chat("s-by-id", user_id="uid-1", username=None),
           _chat("s-by-name", user_id="uid-other", username="alice"))

    assert {c.session_id for c in AgentChat.list_by_page(db.session, **kwargs)} == expected


def test_agentchat_list_by_page_is_newest_first_and_paged(db):
    """
    会话列表按更新时间倒序分页，顺序错乱会让用户的最近会话沉到后面。
    """
    for index in range(4):
        db.add(_chat(f"s-p{index}", user_id="uid-page",
                     updated_at=f"2026-08-13 10:00:0{index}"))

    page1 = AgentChat.list_by_page(db.session, page=1, count=2, user_id="uid-page")
    page2 = AgentChat.list_by_page(db.session, page=2, count=2, user_id="uid-page")

    assert [c.session_id for c in page1] == ["s-p3", "s-p2"]
    assert [c.session_id for c in page2] == ["s-p1", "s-p0"]
    assert [c.session_id for c in asyncio.run(
        AgentChat.async_list_by_page(page=1, count=2, user_id="uid-page"))] == ["s-p3", "s-p2"]


# --------------------------------------------------------------------------- #
# AgentTask
# --------------------------------------------------------------------------- #

def _task(name: str, user_id: str = "u1", enabled: bool = True,
          created_at: str = "2026-08-13 10:00:00") -> dict:
    """构造 Agent 定时任务的新增参数。"""
    return dict(name=name, content="做点什么", trigger_type="cron",
                cron_expression="0 * * * *", enabled=enabled, user_id=user_id,
                session_id=f"sess-{name}", created_at=created_at,
                updated_at=created_at, last_status="waiting", run_count=0)


def test_agenttask_get_for_user_enforces_ownership(db):
    """
    带用户 ID 查询时必须匹配归属，否则任意用户都能读到别人的定时任务。
    """
    task_id = AgentTask.add_task(db.session, **_task("t1", user_id="alice"))

    assert AgentTask.get_for_user(db.session, task_id).id == task_id
    assert AgentTask.get_for_user(db.session, task_id, user_id="alice").id == task_id
    assert AgentTask.get_for_user(db.session, task_id, user_id="bob") is None


def test_agenttask_oper_reads_with_explicit_session(db, monkeypatch):
    """AgentTaskOper 的宿主查询使用调用方 Session，不再经过旧事务兼容执行器。"""
    task_id = AgentTask.add_task(db.session, **_task("canonical", user_id="alice"))

    monkeypatch.setattr(
        "app.db.oper.agenttask.run_sync_transaction",
        lambda _query: pytest.fail("显式 Session 查询不应创建兼容事务"),
    )

    oper = AgentTaskOper(db.session)
    task = oper.get(task_id, user_id="alice")
    tasks = oper.list(user_id="alice", enabled=True)

    assert task is not None and task.id == task_id
    assert [item.id for item in tasks] == [task_id]


def test_agenttask_list_for_user_filters_by_owner_and_enabled(db):
    """
    列表按归属与启用状态收窄，并按创建时间倒序。

    调度器取的是「已启用」这一批，条件失效会把用户停掉的任务重新跑起来。
    """
    AgentTask.add_task(db.session, **_task("t-on", user_id="alice",
                                           created_at="2026-08-13 10:00:00"))
    AgentTask.add_task(db.session, **_task("t-off", user_id="alice", enabled=False,
                                           created_at="2026-08-13 11:00:00"))
    AgentTask.add_task(db.session, **_task("t-other", user_id="bob"))

    mine = AgentTask.list_for_user(db.session, user_id="alice")
    assert [t.name for t in mine] == ["t-off", "t-on"]

    assert [t.name for t in AgentTask.list_for_user(db.session, user_id="alice", enabled=True)] \
        == ["t-on"]
    assert [t.name for t in AgentTask.list_for_user(db.session, user_id="alice", enabled=False)] \
        == ["t-off"]


def test_agenttask_update_enforces_ownership(db):
    """
    更新必须校验归属，并如实返回是否命中。

    删除、认领执行（mark_running）与收尾计数（finish_task）已随运行记录的引入迁出本模型，
    改由 AgentTaskRun / AgentTaskOper 承担，对应用例见 tests/test_agent_task_runs.py 与
    tests/test_agent_scheduled_tasks.py，此处不再重复覆盖。
    """
    task_id = AgentTask.add_task(db.session, **_task("t-own", user_id="alice"))

    assert AgentTask.update_task(db.session, task_id, {"name": "改名"}, user_id="bob") is False
    assert AgentTask.update_task(db.session, task_id, {"name": "改名"}, user_id="alice") is True
    assert AgentTask.get_for_user(db.session, task_id).name == "改名"


# --------------------------------------------------------------------------- #
# DownloadFailure
# --------------------------------------------------------------------------- #

def _failure(fingerprint: str, next_retry_at: str) -> dict:
    """构造下载失败冷却记录的写入参数。"""
    return dict(fingerprint=fingerprint, now_time="2026-08-13 10:00:00",
                next_retry_at=next_retry_at, title="片名", type="电影")


def test_download_failure_active_lookup_excludes_expired_cooldowns(db):
    """
    只返回仍在冷却期内的记录。

    冷却已过却仍被判为「冷却中」，资源会被永久跳过、订阅永远下不下来。
    """
    DownloadFailure.record_failure(db.session, **_failure("fp-cold", "2026-08-13 20:00:00"))
    DownloadFailure.record_failure(db.session, **_failure("fp-expired", "2026-08-13 09:00:00"))

    active = DownloadFailure.get_active_by_fingerprints(
        db.session, ["fp-cold", "fp-expired"], now_time="2026-08-13 12:00:00")

    assert [f.fingerprint for f in active] == ["fp-cold"]


def test_download_failure_active_lookup_excludes_the_expiry_boundary(db):
    """
    冷却到点即结束：``next_retry_at`` 恰好等于当前时刻的记录不再算「冷却中」。

    条件是 ``next_retry_at > now_time``；放宽成 ``>=`` 会让资源在到点那一秒仍被跳过，
    而两侧数据都离边界一小时时，这一字之差查不出来。
    """
    now_time = "2026-08-13 12:00:00"
    DownloadFailure.record_failure(db.session, **_failure("fp-at-boundary", now_time))
    DownloadFailure.record_failure(
        db.session, **_failure("fp-past-boundary", "2026-08-13 12:00:01"))

    active = DownloadFailure.get_active_by_fingerprints(
        db.session, ["fp-at-boundary", "fp-past-boundary"], now_time=now_time)

    assert [f.fingerprint for f in active] == ["fp-past-boundary"]


def test_download_failure_active_lookup_dedupes_and_ignores_blanks(db):
    """
    指纹列表去重并剔除空值，空列表直接短路返回。

    条件为空的 IN 查询在部分方言下会退化成全表匹配，把所有资源判成冷却中。
    """
    DownloadFailure.record_failure(db.session, **_failure("fp-a", "2026-08-13 20:00:00"))

    assert DownloadFailure.get_active_by_fingerprints(db.session, [], "2026-08-13 12:00:00") == []
    assert DownloadFailure.get_active_by_fingerprints(
        db.session, ["", None], "2026-08-13 12:00:00") == []

    found = DownloadFailure.get_active_by_fingerprints(
        db.session, ["fp-a", "fp-a", ""], "2026-08-13 12:00:00")
    assert [f.fingerprint for f in found] == ["fp-a"]


def test_download_failure_record_increments_retry_count(db):
    """
    同一指纹再次失败时累加重试次数，而不是新增一行。

    每次新增会让冷却窗口永远停留在第一档，退避策略形同虚设。
    """
    first = DownloadFailure.record_failure(db.session, **_failure("fp-retry", "2026-08-13 20:00:00"))
    assert first.retry_count == 1

    second = DownloadFailure.record_failure(
        db.session, **_failure("fp-retry", "2026-08-14 20:00:00"))

    assert second.id == first.id
    assert second.retry_count == 2
    assert second.next_retry_at == "2026-08-14 20:00:00"


def test_download_failure_delete_expired_is_batched(db):
    """
    过期记录清理分批执行，且不碰仍在冷却期内的记录。
    """
    for index in range(3):
        DownloadFailure.record_failure(
            db.session, **_failure(f"fp-old-{index}", "2026-01-0%d 10:00:00" % (index + 1)))
    DownloadFailure.record_failure(db.session, **_failure("fp-live", "2026-12-01 10:00:00"))

    assert DownloadFailure.delete_expired(db.session, before_time="2026-08-01", limit=2) == 2
    assert DownloadFailure.delete_expired(db.session, before_time="2026-08-01", limit=100) == 1
    assert DownloadFailure.delete_expired(db.session, before_time="2026-08-01", limit=100) == 0

    assert DownloadFailure.get_active_by_fingerprints(
        db.session, ["fp-live"], "2026-08-13 12:00:00")


def test_download_failure_delete_expired_keeps_the_row_exactly_at_the_boundary(db):
    """
    ``next_retry_at`` 恰好等于清理水位的记录不算过期，必须留下（``next_retry_at < before_time``）。

    比较符若写成 ``<=``，正好排到水位那一秒的冷却记录会被提前抹掉，该资源随即被重新
    下载一遍——退避直接失效。上面那条分批用例的数据离水位有半年之遥，压不到边界。
    """
    boundary = "2026-05-01 00:00:00"
    DownloadFailure.record_failure(db.session, **_failure("fp-at-boundary", boundary))
    DownloadFailure.record_failure(
        db.session, **_failure("fp-before-boundary", "2026-04-30 23:59:59"))

    assert DownloadFailure.delete_expired(db.session, before_time=boundary, limit=100) == 1

    remaining = DownloadFailure.get_active_by_fingerprints(
        db.session, ["fp-at-boundary", "fp-before-boundary"], now_time="2026-01-01 00:00:00")
    assert [f.fingerprint for f in remaining] == ["fp-at-boundary"]
