"""
待整理登记表的查询行为。

这张表是「挂载挂死后重启不漏件」的唯一依据：登记去重、回放顺序、终态注销三件事
任何一件出偏差，都直接表现为文件被漏整理或被重复整理，而不是一个可见的报错。
因此这里对着真实数据库断言查回的内容，而不是断言调用了什么。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import base as db_base
from app.db.adapters.transfer import TransactionalTransferAdmissionRepository
from app.db.models.transferpending import TransferPending
from app.db.oper.transferpending import TransferPendingOper


@pytest.fixture(autouse=True)
def _track(db):
    """把待整理表纳入用例级回收。"""
    db.watermark(TransferPending)


def test_register_is_idempotent_and_keeps_first_time(db):
    """
    同一文件重复登记只保留一条，且登记时间保持首次的值。

    监控在挂载抖动时会对同一个文件反复触发事件，若每次都新增一条，回放时同一个
    文件会被送进整理链多次。保留首次时间则保证回放顺序仍是「最早发现」的顺序。
    """
    TransferPending.register(db.session, storage="local", src_path="/mnt/a.mkv",
                             now_time="2026-08-13 10:00:00")
    TransferPending.register(db.session, storage="local", src_path="/mnt/a.mkv",
                             now_time="2026-08-13 12:00:00")

    rows = TransferPending.list_all(db.session)
    same_path = [r for r in rows if r.src_path == "/mnt/a.mkv"]
    assert len(same_path) == 1
    assert same_path[0].created_at == "2026-08-13 10:00:00"


def test_register_scopes_by_storage(db):
    """
    存储不同即为不同文件——路径相同但分属不同存储时不能互相去重。
    """
    TransferPending.register(db.session, storage="local", src_path="/data/x.mkv",
                             now_time="2026-08-13 10:00:00")
    TransferPending.register(db.session, storage="alist", src_path="/data/x.mkv",
                             now_time="2026-08-13 10:00:01")

    rows = [r for r in TransferPending.list_all(db.session) if r.src_path == "/data/x.mkv"]
    assert {r.storage for r in rows} == {"local", "alist"}


@pytest.mark.parametrize("storage,src_path", [("", "/mnt/a.mkv"), ("local", ""), ("", "")])
def test_register_rejects_incomplete_identity(db, storage, src_path):
    """
    缺少存储或路径的登记必须直接丢弃，不能写入半条记录。

    半条记录回放时既定位不到文件、也无法被 discard 匹配，会永久留在表里。
    """
    assert TransferPending.register(db.session, storage=storage, src_path=src_path,
                                    now_time="2026-08-13 10:00:00") is None


def test_list_all_replays_in_registration_order(db):
    """
    回放顺序必须是登记时间升序、同时间按主键升序。

    乱序回放会让后发现的文件先进整理链，与原入队顺序不一致。
    """
    for path, moment in [("/mnt/c.mkv", "2026-08-13 12:00:00"),
                         ("/mnt/a.mkv", "2026-08-13 10:00:00"),
                         ("/mnt/b.mkv", "2026-08-13 11:00:00")]:
        TransferPending.register(db.session, storage="local", src_path=path, now_time=moment)

    ordered = [r.src_path for r in TransferPending.list_all(db.session)
               if r.src_path.startswith("/mnt/")]
    assert ordered == ["/mnt/a.mkv", "/mnt/b.mkv", "/mnt/c.mkv"]


def test_list_all_honours_limit(db):
    """
    回放上限必须生效——异常积压时一次性全放会把整理链直接压垮。
    """
    for index in range(5):
        TransferPending.register(db.session, storage="local", src_path=f"/mnt/{index}.mkv",
                                 now_time=f"2026-08-13 10:00:0{index}")

    assert len(TransferPending.list_all(db.session, limit=3)) == 3


def test_discard_removes_only_the_matching_row(db):
    """
    注销只应删除匹配的那一条，并返回删除条数。

    整理到达终态时按「存储 + 路径」注销，误删其他登记等于把别的文件也判成已完成。
    """
    TransferPending.register(db.session, storage="local", src_path="/mnt/a.mkv",
                             now_time="2026-08-13 10:00:00")
    TransferPending.register(db.session, storage="local", src_path="/mnt/b.mkv",
                             now_time="2026-08-13 10:00:01")

    assert TransferPending.discard(db.session, storage="local", src_path="/mnt/a.mkv") == 1

    remaining = [r.src_path for r in TransferPending.list_all(db.session)
                 if r.src_path.startswith("/mnt/")]
    assert remaining == ["/mnt/b.mkv"]


@pytest.mark.parametrize("storage,src_path", [("", "/mnt/a.mkv"), ("local", "")])
def test_discard_rejects_incomplete_identity(db, storage, src_path):
    """
    身份不全时必须直接返回 0，不能退化成「条件为空」的全表删除。
    """
    TransferPending.register(db.session, storage="local", src_path="/mnt/a.mkv",
                             now_time="2026-08-13 10:00:00")

    assert TransferPending.discard(db.session, storage=storage, src_path=src_path) == 0
    assert TransferPending.list_all(db.session)


def test_discard_returns_zero_when_absent(db):
    """
    注销不存在的登记返回 0，不抛异常——整理链的终态回调不应因此中断。
    """
    assert TransferPending.discard(db.session, storage="local", src_path="/nope.mkv") == 0


def test_clear_empties_the_table(db):
    """
    清空返回删除条数且表内不再有登记。
    """
    TransferPending.register(db.session, storage="local", src_path="/mnt/a.mkv",
                             now_time="2026-08-13 10:00:00")

    assert TransferPending.clear(db.session) >= 1
    assert TransferPending.list_all(db.session) == []


def test_oper_returns_plain_tuples_not_orm_instances(db):
    """
    回放接口必须返回纯元组。

    回放发生在会话之外，ORM 实例脱离 session 后访问属性会抛
    DetachedInstanceError——那时启动流程已经在跑，报错等于整批漏件。
    """
    oper = TransferPendingOper(db=db.session)
    oper.register(storage="local", src_path="/mnt/a.mkv")

    listed = oper.list_all()

    assert ("local", "/mnt/a.mkv") in listed
    assert all(isinstance(item, tuple) for item in listed)


def test_oper_reuses_explicit_query_session(db, monkeypatch):
    """TransferPendingOper 查询必须复用调用方会话。"""
    db.add(TransferPending(
        storage="local",
        src_path="/mnt/explicit.mkv",
        created_at="2026-08-13 10:00:00",
    ))
    monkeypatch.setattr(
        db_base,
        "run_sync_transaction",
        lambda _operation: (_ for _ in ()).throw(
            AssertionError("不应创建额外同步事务")
        ),
    )

    assert ("local", "/mnt/explicit.mkv") in TransferPendingOper(db.session).list_all()


def test_oper_drops_rows_with_missing_fields(db):
    """
    回放时必须跳过字段残缺的历史遗留行，不能把空存储送进整理链。

    列上有 NOT NULL 约束，残缺只可能表现为空串；直接绕过 register 写入，
    模拟历史数据或外部写库留下的半条记录。
    """
    db.add(TransferPending(storage="", src_path="/mnt/broken.mkv",
                           created_at="2026-08-13 10:00:00"))
    oper = TransferPendingOper(db=db.session)
    oper.register(storage="local", src_path="/mnt/ok.mkv")

    assert oper.list_all() == [("local", "/mnt/ok.mkv")]


def test_oper_discard_and_clear_report_counts(db):
    """
    注销与清空都要如实返回条数，调用方据此判断是否真的清理掉了。
    """
    oper = TransferPendingOper(db=db.session)
    oper.register(storage="local", src_path="/mnt/a.mkv")
    oper.register(storage="local", src_path="/mnt/b.mkv")

    assert oper.discard(storage="local", src_path="/mnt/a.mkv") == 1
    assert oper.clear() >= 1
    assert oper.list_all() == []


def test_stage_admit_is_idempotent_and_keeps_stable_task_id(db):
    """显式准入重复执行时必须复用首个稳定任务标识。"""
    first = TransferPending.stage_admit(
        db.session,
        task_id="task-first",
        storage="local",
        src_path="/mnt/durable.mkv",
        state="accepted",
        now_time="2026-08-27 10:00:00",
    )
    second = TransferPending.stage_admit(
        db.session,
        task_id="task-second",
        storage="local",
        src_path="/mnt/durable.mkv",
        state="accepted",
        now_time="2026-08-27 11:00:00",
    )

    assert first is second
    assert second.task_id == "task-first"
    assert second.updated_at == "2026-08-27 10:00:00"


def test_state_queries_failure_record_and_task_discard(db):
    """状态查询、失败留痕和按任务删除应共享同一稳定身份。"""
    TransferPending.stage_admit(
        db.session,
        task_id="task-accepted",
        storage="local",
        src_path="/mnt/accepted.mkv",
        state="accepted",
        now_time="2026-08-27 10:00:00",
    )
    db.add(TransferPending(
        task_id="task-other",
        storage="local",
        src_path="/mnt/other.mkv",
        state="other",
        created_at="2026-08-27 10:00:01",
        updated_at="2026-08-27 10:00:01",
    ))
    db.session.flush()

    accepted = TransferPending.list_by_state(
        db.session,
        state="accepted",
    )
    assert [item.task_id for item in accepted] == ["task-accepted"]
    assert TransferPending.record_enqueue_failure(
        db.session,
        task_id="task-accepted",
        error="queue full",
        now_time="2026-08-27 10:01:00",
    ) == 1
    db.session.expire_all()
    failed = TransferPending.get_by_identity(
        db.session,
        storage="local",
        src_path="/mnt/accepted.mkv",
    )
    assert failed.last_error == "queue full"
    assert failed.updated_at == "2026-08-27 10:01:00"
    assert TransferPending.discard_task(
        db.session,
        task_id="task-accepted",
    ) == 1


def test_oper_staging_reuses_explicit_write_session(db, monkeypatch):
    """Oper 的新暂存入口必须服从调用方 Session，不得隐式提交。"""
    monkeypatch.setattr(
        db_base,
        "run_sync_transaction",
        lambda _operation: (_ for _ in ()).throw(
            AssertionError("不应创建额外同步事务")
        ),
    )
    oper = TransferPendingOper(db.session)

    pending = oper.stage_admit(
        task_id="task-explicit",
        storage="local",
        src_path="/mnt/explicit-stage.mkv",
        state="accepted",
        now_time="2026-08-27 10:00:00",
    )
    assert pending.task_id == "task-explicit"
    assert [item.task_id for item in oper.list_by_state(state="accepted")] == [
        "task-explicit"
    ]
    assert oper.stage_record_enqueue_failure(
        task_id="task-explicit",
        error="queue full",
        now_time="2026-08-27 10:01:00",
    ) == 1
    assert oper.stage_discard_task(task_id="task-explicit") == 1


def test_transactional_repository_commits_frozen_projections(tmp_path):
    """适配器应独立提交 UoW，并在会话关闭前冻结应用 DTO。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'transfer.db'}")
    TransferPending.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    repository = TransactionalTransferAdmissionRepository(factory)

    admitted = repository.admit(
        storage="local",
        src_path="/mnt/repository.mkv",
    )
    repeated = repository.admit(
        storage="local",
        src_path="/mnt/repository.mkv",
    )
    assert repeated == admitted
    assert admitted.task_id
    assert admitted.state == "accepted"
    assert repository.list_accepted() == [admitted]

    repository.record_enqueue_failure(
        task_id=admitted.task_id,
        error="queue full",
    )
    failed = repository.list_accepted()[0]
    assert failed.last_error == "queue full"
    assert repository.discard_task(task_id=admitted.task_id) == 1
    assert repository.list_accepted() == []
    engine.dispose()


def test_transactional_repository_rolls_back_failed_write(monkeypatch):
    """适配器写入异常时必须回滚自身 UoW 并传播原异常。"""
    class SessionContext:
        """为回滚断言提供最小 Session 上下文。"""

        def __init__(self):
            """初始化提交与回滚计数。"""
            self.commits = 0
            self.rollbacks = 0

        def __enter__(self):
            """返回当前伪会话。"""
            return self

        def __exit__(self, _exc_type, _exc_value, _traceback):
            """不吞掉被测异常。"""
            return False

        def commit(self):
            """记录提交调用。"""
            self.commits += 1

        def rollback(self):
            """记录回滚调用。"""
            self.rollbacks += 1

    session = SessionContext()
    repository = TransactionalTransferAdmissionRepository(lambda: session)
    monkeypatch.setattr(
        TransferPendingOper,
        "stage_record_enqueue_failure",
        lambda self, **_kwargs: (_ for _ in ()).throw(ValueError("write failed")),
    )

    with pytest.raises(ValueError, match="write failed"):
        repository.record_enqueue_failure(task_id="task", error="failure")

    assert session.rollbacks == 1
    assert session.commits == 0
