"""
媒体身份的持久化不变量：app/db/models/_identity.py 的 mapper 事件。

这条不变量此前靠六个 Oper 各自在建模前调一次归一来保证，是调用点纪律；现在下沉成
flush 前的事件。因此这里断言的是「绕过 Oper 直接建模写库」时不变量仍然成立——那正是
下沉之前会漏掉的路径，也是这次改动唯一真正新增的保证。

半对身份在持久化侧是「清空 + 告警」而非抛错：这几张表都是记账性写入，因一个次要字段
就丢掉整条整理历史，代价比留下一条无身份记录更大。告警是这次一并补上的——此前它是
完全沉默的，脏数据只能靠翻库发现。DTO 侧仍然抛错，两边语义的差异是有意的。
"""
import pytest

from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.schemas.types import MediaSource


@pytest.fixture(autouse=True)
def _track(db):
    """把两张表纳入用例级回收。"""
    db.watermark(TransferHistory, TransferPending)


def _history(**identity) -> TransferHistory:
    """构造一条最小可写的整理历史，只有身份字段按用例变化。"""
    return TransferHistory(src="/downloads/x.mkv", src_storage="local",
                           dest="/media/x.mkv", dest_storage="local",
                           mode="link", title="身份用例", status=1, files=[],
                           **identity)


def _write(db, row):
    """落库并重新读回，确保拿到的是写入后的值而非内存里的原值。"""
    db.add(row)
    db.session.expire_all()
    return TransferHistory.get(db.session, row.id)


# --------------------------------------------------------------------------- #
# 归一
# --------------------------------------------------------------------------- #

def test_alias_source_is_persisted_as_canonical_value(db):
    """
    别名来源必须落成规范值。

    同一个来源以 tmdb / themoviedb 两种拼法写进去，按身份查重就会把同一部剧
    当成两条，洗版与去重全部失效。
    """
    row = _write(db, _history(media_source="tmdb", media_id="550"))

    assert row.media_source == MediaSource.TMDB.value


def test_media_id_is_stripped(db):
    """
    ID 两端空白必须去掉——带空格的 ID 与不带的是两个不同的字符串，查重对不上。
    """
    row = _write(db, _history(media_source=MediaSource.TMDB, media_id="  550  "))

    assert row.media_id == "550"


def test_enum_source_is_persisted_as_value_not_repr(db):
    """
    传枚举时落库的是它的值，不是枚举的字符串表示。
    """
    row = _write(db, _history(media_source=MediaSource.TMDB, media_id="550"))

    assert row.media_source == MediaSource.TMDB.value


# --------------------------------------------------------------------------- #
# 半对身份
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "identity",
    [
        pytest.param({"media_source": MediaSource.TMDB, "media_id": None}, id="缺ID"),
        pytest.param({"media_source": None, "media_id": "550"}, id="缺来源"),
        pytest.param({"media_source": MediaSource.TMDB, "media_id": "0"}, id="零值ID"),
        pytest.param({"media_source": MediaSource.TMDB, "media_id": "   "}, id="空白ID"),
        pytest.param({"media_source": "不存在的源", "media_id": "550"}, id="非法来源"),
    ],
)
def test_incomplete_identity_is_cleared_on_both_columns(db, identity):
    """
    身份不成对时两列一起清空，不能只留下半边。

    只留半边的行既匹配不上任何查重条件，也无法被后续的身份修复流程识别，
    等于永久躺在表里的脏数据。
    """
    row = _write(db, _history(**identity))

    assert row.media_source is None
    assert row.media_id is None


def test_incomplete_identity_is_not_silent(db, monkeypatch):
    """
    清空必须留下告警，且告警里带得上被丢弃的原值。

    这是本次下沉一并修掉的东西：此前归一是完全沉默的，写进一条无身份记录后没有任何
    痕迹，只能靠翻库才发现。告警让它变成日志里可检索的事件——因此消息里必须有原值，
    否则只知道"某处丢了身份"，仍然定位不到是谁写的。

    直接换掉模块里的 logger 而不用 caplog：项目的 LoggerManager 把 propagate 关了
    （app/runtime/log.py:333），caplog 挂在 root 上根本收不到。
    """
    warnings: list[str] = []
    monkeypatch.setattr("app.db.models._identity.logger",
                        type("_Spy", (), {"warn": staticmethod(warnings.append)})())

    _write(db, _history(media_source=MediaSource.TMDB, media_id=None))

    assert len(warnings) == 1
    assert "媒体身份不成对" in warnings[0]
    # 原值要出现在消息里，否则日志定位不到是哪条写入
    assert "themoviedb" in warnings[0] and "None" in warnings[0]


def test_complete_identity_does_not_warn(db, monkeypatch):
    """
    身份完整时不得告警——告警一旦对正常写入也响，就会被当成噪声忽略掉。
    """
    warnings: list[str] = []
    monkeypatch.setattr("app.db.models._identity.logger",
                        type("_Spy", (), {"warn": staticmethod(warnings.append)})())

    _write(db, _history(media_source="tmdb", media_id="  550  "))

    assert warnings == []


# --------------------------------------------------------------------------- #
# 覆盖范围
# --------------------------------------------------------------------------- #

def test_normalization_also_applies_on_update(db):
    """
    更新路径同样归一——只管 insert 会让一条合法记录被后续更新改成半对身份。
    """
    row = _write(db, _history(media_source=MediaSource.TMDB, media_id="550"))

    row.update(db.session, {"media_source": "douban", "media_id": "  1291546  "})
    db.session.commit()
    db.session.expire_all()
    updated = TransferHistory.get(db.session, row.id)

    assert updated.media_source == MediaSource.Douban.value
    assert updated.media_id == "1291546"


def test_tables_without_identity_columns_are_untouched(db):
    """
    不带身份列的表不受影响——事件挂在 Mapper 上覆盖全部映射，必须靠列名检查收窄，
    否则会去动一张根本没有这两列的表。
    """
    TransferPending.register(db.session, storage="local", src_path="/mnt/a.mkv",
                             now_time="2026-08-14 10:00:00")

    rows = [r for r in TransferPending.list_all(db.session) if r.src_path == "/mnt/a.mkv"]
    assert len(rows) == 1
