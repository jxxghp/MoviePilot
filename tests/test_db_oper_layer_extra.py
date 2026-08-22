"""
整理历史、订阅、消息与下载冷却四个 Oper 的数据访问行为。

与 test_db_oper_layer 同源，拆开只是为了每个文件保持可读的长度。这一组的共同点是
方法多、每个都很薄——薄封装最容易在参数改名或默认值上出偏差，而调用方拿到的是
空列表或 None，看起来像「本来就没有数据」。
"""
import asyncio

import pytest

from app.db.oper.downloadfailure import DownloadFailureOper
from app.db.oper.message import MessageOper
from app.db.models.downloadfailure import DownloadFailure
from app.db.models.message import Message
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.db.models.transferhistory import TransferHistory
from app.db.oper.subscribe import SubscribeOper
from app.db.oper.subscribehistory import SubscribeHistoryOper
from app.db.oper.transferhistory import TransferHistoryOper
from app.schemas.types import MediaSource, MediaType

TMDB = str(MediaSource.TMDB)


@pytest.fixture(autouse=True)
def _track(db):
    """把本文件涉及的表纳入用例级回收。"""
    db.watermark(TransferHistory, Subscribe, SubscribeHistory, Message, DownloadFailure)


# --------------------------------------------------------------------------- #
# TransferHistoryOper
# --------------------------------------------------------------------------- #

def _transfer_kwargs(title: str, src: str, **extra) -> dict:
    """构造整理历史的写入参数。"""
    payload = dict(src=src, src_storage="local", dest=f"/media/{title}.mkv",
                   dest_storage="local", mode="move", type=MediaType.TV.value,
                   title=title, year="2026", media_source=TMDB, media_id="3001",
                   status=True, date="2026-08-13 10:00:00", files=[])
    payload.update(extra)
    return payload


def test_transferhistory_oper_path_lookups(db):
    """
    源路径、目标路径、成功记录三个入口都应透传存储参数。
    """
    oper = TransferHistoryOper(db=db.session)
    oper.add(**_transfer_kwargs("路径", "/data/op-th.mkv"))

    assert oper.get_by_src("/data/op-th.mkv").title == "路径"
    assert oper.get_by_src("/data/op-th.mkv", storage="alist") is None
    assert oper.get_success_by_src("/data/op-th.mkv", storage="local").title == "路径"
    assert oper.get_by_dest("/media/路径.mkv").title == "路径"
    assert oper.get_by_dest("/media/路径.mkv", storage="alist") is None


def test_transferhistory_oper_recursive_listings(db):
    """
    源侧与目标侧的递归列举都应包含目录自身与子项，不含同前缀的兄弟目录。
    """
    oper = TransferHistoryOper(db=db.session)
    oper.add(**_transfer_kwargs("自身", "/data/op-dir", dest="/media/op-dir"))
    oper.add(**_transfer_kwargs("子项", "/data/op-dir/a.mkv", dest="/media/op-dir/a.mkv"))
    oper.add(**_transfer_kwargs("兄弟", "/data/op-dir2/b.mkv", dest="/media/op-dir2/b.mkv"))

    assert {h.title for h in oper.list_success_by_src("/data/op-dir", recursive=True)} == \
        {"自身", "子项"}
    assert {h.title for h in oper.list_success_move_by_dest("/media/op-dir", recursive=True)} == \
        {"自身", "子项"}
    assert [h.title for h in oper.list_success_by_src("/data/op-dir")] == ["自身"]
    assert [h.title for h in oper.list_success_move_by_dest("/media/op-dir")] == ["自身"]


def test_transferhistory_oper_identity_and_hash_lookups(db):
    """
    按标题、hash、媒体身份查询与按条件组合查询都应命中同一条记录。
    """
    oper = TransferHistoryOper(db=db.session)
    oper.add(**_transfer_kwargs("身份", "/data/op-id.mkv", media_id="3100",
                                download_hash="op-th-hash", seasons="S01"))

    assert [h.title for h in oper.get_by_title("身份")] == ["身份"]
    assert [h.title for h in oper.list_by_hash("op-th-hash")] == ["身份"]
    assert oper.get_by_media_identity(media_source=TMDB, media_id="3100",
                                      mtype=MediaType.TV.value).title == "身份"
    assert [h.title for h in oper.get_by(mtype=MediaType.TV.value, media_source=TMDB,
                                         media_id="3100", season="S01")] == ["身份"]
    assert [h.title for h in oper.list_by_date("2026-08-01")] == ["身份"]
    assert oper.statistic(days=36500)


def test_transferhistory_oper_add_force_replaces_same_source(db):
    """
    强制新增用同源新记录替换旧的，同一源路径只留一条。
    """
    oper = TransferHistoryOper(db=db.session)
    oper.add(**_transfer_kwargs("旧记录", "/data/op-force.mkv"))

    created = oper.add_force(**_transfer_kwargs("新记录", "/data/op-force.mkv"))

    assert created.title == "新记录"
    assert [h.title for h in oper.list_success_by_src("/data/op-force.mkv")] == ["新记录"]


def test_transferhistory_oper_update_hash_and_delete(db):
    """
    补写下载 hash、按 ID 取、删除三个入口都应真正落库。
    """
    oper = TransferHistoryOper(db=db.session)
    oper.add(**_transfer_kwargs("待改", "/data/op-upd.mkv"))
    history = oper.get_by_src("/data/op-upd.mkv")

    oper.update_download_hash(history.id, "op-new-hash")
    assert oper.get(history.id).download_hash == "op-new-hash"

    oper.delete(history.id)
    assert oper.get_by_src("/data/op-upd.mkv") is None


def test_transferhistory_oper_async_accessors_match_sync(db):
    """
    异步的取单条、检索、分页与计数必须与同步一致。
    """
    oper = TransferHistoryOper(db=db.session)
    oper.add(**_transfer_kwargs("AsyncTitle", "/data/op-async.mkv"))
    history = oper.get_by_src("/data/op-async.mkv")

    assert asyncio.run(oper.async_get(history.id)).id == history.id
    assert [h.title for h in asyncio.run(
        oper.async_list_by_title("AsyncTitle", count=-1))] == ["AsyncTitle"]
    assert {h.id for h in asyncio.run(oper.async_list_by_page(page=1, count=100))} >= {history.id}
    assert asyncio.run(oper.async_count()) >= 1
    assert asyncio.run(oper.async_count_by_title("AsyncTitle")) == 1

    asyncio.run(oper.async_delete(history.id))
    assert oper.get(history.id) is None


def test_transferhistory_oper_truncate_empties_the_table(db):
    """
    清空整理历史后不再有任何记录，供「重置」入口使用。
    """
    oper = TransferHistoryOper(db=db.session)
    oper.add(**_transfer_kwargs("待清", "/data/op-truncate.mkv"))

    oper.truncate()

    assert oper.get_by_src("/data/op-truncate.mkv") is None


# --------------------------------------------------------------------------- #
# SubscribeOper / SubscribeHistoryOper
# --------------------------------------------------------------------------- #

def _subscribe(name: str, media_id: str = "2001", state: str = "N",
               username: str = "op-alice", mtype: str = None) -> Subscribe:
    """构造一条订阅记录。"""
    return Subscribe(name=name, type=mtype or MediaType.TV.value, state=state,
                     media_source=TMDB, media_id=media_id, season=1,
                     username=username, date="2026-08-13 10:00:00")


def test_subscribe_oper_read_entry_points(db):
    """
    按 ID、按条件、按状态、按 owner、按类型五个读取入口都应透传生效。
    """
    row = db.add(_subscribe("订阅一"))
    oper = SubscribeOper(db=db.session)

    assert oper.get(row.id).id == row.id
    assert oper.get_by(type=MediaType.TV.value, media_source=TMDB,
                       media_id="2001").id == row.id
    assert {s.id for s in oper.list("N")} >= {row.id}
    assert {s.id for s in oper.list()} >= {row.id}
    assert [s.name for s in oper.list_by_username("op-alice", state="N",
                                                  mtype=MediaType.TV.value)] == ["订阅一"]
    assert [s.name for s in oper.list_by_type(MediaType.TV.value, days=36500)] == ["订阅一"]


def test_subscribe_oper_update_and_delete(db):
    """
    更新与删除都应落库，删除后按 ID 取不到。
    """
    row = db.add(_subscribe("待改订阅", media_id="2100"))
    oper = SubscribeOper(db=db.session)

    assert oper.update(row.id, {"state": "R"}).state == "R"

    oper.delete(row.id)
    assert oper.get(row.id) is None


def test_subscribe_oper_async_entry_points(db):
    """
    异步的取单条、按条件取、列举、更新、删除必须与同步等效。
    """
    row = db.add(_subscribe("异步订阅", media_id="2200"))
    oper = SubscribeOper(db=db.session)

    assert asyncio.run(oper.async_get(row.id)).id == row.id
    assert asyncio.run(oper.async_get_by(type=MediaType.TV.value, media_source=TMDB,
                                         media_id="2200")).id == row.id
    assert {s.id for s in asyncio.run(oper.async_list("N"))} >= {row.id}
    assert asyncio.run(oper.async_update(row.id, {"state": "R"})).state == "R"
    assert asyncio.run(oper.async_update_filter_groups(row.id, ["g1"])).filter_groups == ["g1"]

    asyncio.run(oper.async_delete(row.id))
    assert oper.get(row.id) is None


def test_subscribe_oper_history_round_trip(db):
    """
    订阅完成后写入历史，随后存在性判断应命中。

    历史判定失效会让用户能重复订阅一部已经追完的剧。
    """
    oper = SubscribeOper(db=db.session)
    oper.add_history(name="历史剧", type=MediaType.TV.value, media_source=TMDB,
                     media_id="2300", season=1, date="2026-08-13 10:00:00",
                     username="op-alice", best_version=False)

    assert oper.exist_history(media_source=MediaSource.TMDB, media_id="2300",
                              season=1) is True
    assert oper.exist_history(media_source=MediaSource.TMDB, media_id="2999",
                              season=1) is False
    assert "历史剧" in {h.name for h in asyncio.run(
        SubscribeHistoryOper(db=db.session).async_list_by_type(
            mtype=MediaType.TV.value, page=1, count=100))}


# --------------------------------------------------------------------------- #
# MessageOper
# --------------------------------------------------------------------------- #

def test_message_oper_add_returns_persisted_payload(db):
    """
    新增消息返回已落库的字段字典，其中必须带上主键。
    """
    oper = MessageOper(db=db.session)

    created = oper.add(title="标题", text="正文", source="op-msg-1",
                       reg_time="2026-08-13 10:00:00")

    assert created["id"] is not None
    assert created["title"] == "标题"
    assert oper.exists_by_source("op-msg-1") is True
    assert oper.exists_by_source("op-msg-none") is False


def test_message_oper_listing_entry_points(db):
    """
    分页列举的同步与异步入口都应返回刚写入的消息。
    """
    oper = MessageOper(db=db.session)
    oper.add(title="分页消息", text="正文", source="op-msg-2",
             reg_time="2026-08-13 10:00:00")
    db.session.commit()

    assert [m.title for m in oper.list_by_page(page=1, count=1)] == ["分页消息"]
    assert [m.title for m in asyncio.run(oper.async_list_by_page(page=1, count=1))] == \
        ["分页消息"]
    assert [m.title for m in asyncio.run(oper.async_list_sent_by_page(page=1, count=1))] == \
        ["分页消息"]


def test_message_oper_async_add_returns_the_model_not_a_dict(db):
    """
    异步新增返回的是模型实例，而同步新增返回字段字典——两者返回类型并不一致。

    把这条不对称固定下来：调用方按字典下标访问异步结果会直接抛 TypeError，
    这里明确它当前的契约，避免后续改写时无意中翻转。
    """
    oper = MessageOper(db=db.session)

    created = asyncio.run(oper.async_add(title="异步消息", text="正文",
                                         source="op-msg-3",
                                         reg_time="2026-08-13 10:00:00"))

    assert isinstance(created, Message)
    assert created.id is not None
    assert oper.exists_by_source("op-msg-3") is True


# --------------------------------------------------------------------------- #
# DownloadFailureOper
# --------------------------------------------------------------------------- #

def test_downloadfailure_oper_round_trip(db):
    """
    记录失败、查询冷却中、清理过期三个入口构成完整闭环。
    """
    oper = DownloadFailureOper(db=db.session)
    oper.record_failure(fingerprint="op-fp-1", now_time="2026-08-13 10:00:00",
                        next_retry_at="2026-08-13 20:00:00", title="片名")
    oper.record_failure(fingerprint="op-fp-old", now_time="2026-01-01 10:00:00",
                        next_retry_at="2026-01-01 20:00:00", title="旧的")

    # Oper 侧返回「指纹 -> 记录」映射，供上层直接按指纹判定是否仍在冷却
    active = oper.get_active_by_fingerprints(["op-fp-1", "op-fp-old"],
                                             now_time="2026-08-13 12:00:00")
    assert set(active) == {"op-fp-1"}
    assert active["op-fp-1"].title == "片名"

    assert oper.delete_expired(before_time="2026-08-01", limit=100) == 1
    assert oper.delete_expired(before_time="2026-08-01", limit=100) == 0
