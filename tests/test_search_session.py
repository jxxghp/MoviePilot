"""订阅搜索检查点的任务租约、版本 CAS 与凭据排除回归。"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

from app.application.search.session import encode_search_state, torrent_snapshot
from app.db.adapters.searchsession import TransactionalSearchSessionRepository
from app.db.base import Base
from app.db.models.subscriptionsearch import SubscriptionSearchTask
from app.domain.context import TorrentInfo


@pytest.fixture
def repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'search.db'}")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc).isoformat()
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    with sessionmaker(bind=engine)() as session:
        session.add(SubscriptionSearchTask(task_id="task", batch_id="batch", subscription_id=1,
                    source="manual", position=0, state="running", created_at=now, updated_at=now,
                    lease_token="old", lease_expires_at=expiry))
        session.commit()
    yield TransactionalSearchSessionRepository(sessionmaker(bind=engine)), engine
    engine.dispose()


def _set_lease(engine, token):
    with sessionmaker(bind=engine)() as session:
        session.execute(update(SubscriptionSearchTask).values(lease_token=token))
        session.commit()


def test_only_current_task_lease_and_version_can_advance(repository):
    repo, engine = repository
    assert repo.create(task_id="task", payload="{}", task_lease="wrong") is None
    first = repo.create(task_id="task", payload='{"page":0}', task_lease="old")
    saved = repo.save(snapshot=first, payload='{"page":1}', task_lease="old")
    assert saved.version == 1
    # 迟到 worker 持有旧版本，不能覆盖新进度。
    assert repo.save(snapshot=first, payload='{"page":999}', task_lease="old") is None
    # 队列重新派发后旧租约立即失效，新租约无需等待即可继续。
    _set_lease(engine, "new")
    assert repo.save(snapshot=saved, payload='{"page":2}', task_lease="old") is None
    assert repo.save(snapshot=saved, payload='{"page":2}', task_lease="new").version == 2
    assert repo.get(task_id="task").payload == '{"page":2}'


def test_cancel_request_stops_checkpoint_writes(repository):
    repo, engine = repository
    first = repo.create(task_id="task", payload="{}", task_lease="old")
    with sessionmaker(bind=engine)() as session:
        session.execute(update(SubscriptionSearchTask).values(cancel_requested=1))
        session.commit()
    assert repo.save(snapshot=first, payload="{}", task_lease="old") is None


def test_checkpoint_excludes_credentials_and_indirect_download_recipe():
    torrent = TorrentInfo(site=1, title="Show", site_cookie="cookie-secret",
                          enclosure="[encoded-api-key]https://api.example/download",
                          page_url="https://site.example/details.php?id=23")
    text = encode_search_state(torrent_snapshot(torrent))
    assert "cookie-secret" not in text
    assert "encoded-api-key" not in text
    assert '"enclosure": null' in text
    assert 'details.php?id=23' in text


def test_unrecognized_download_query_is_not_persisted():
    torrent = TorrentInfo(enclosure="https://site.example/download?id=1&passkey=secret")
    assert torrent_snapshot(torrent)["enclosure"] is None
    torrent.enclosure = "https://site.example/ticket/opaque-private-capability"
    assert torrent_snapshot(torrent)["enclosure"] is None


@pytest.mark.parametrize("system_stop,cancelled,expected", [(True, False, "requeued"), (False, True, "cancelled")])
def test_partial_submission_does_not_finish_an_interrupted_search_round(system_stop, cancelled, expected):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.application.subscription.observability import finish_returned_search_task
    queue = Mock()
    execution = SimpleNamespace(download_started=True, incremental_search=True, scan_finished=False,
                                is_expired=lambda: False)
    _subscription, state, _reason = finish_returned_search_task(queue=queue, task_id="task", lease_token="lease",
         subscription_id=1, execution_context=execution, system_stopped=system_stop, cancel_requested=cancelled)
    assert state == expected
    queue.finish_task.assert_not_called()
    queue.release_task.assert_called_once()


def test_finished_round_removes_its_checkpoint_only_with_the_current_lease_and_version(repository):
    repo, _ = repository
    first = repo.create(task_id="task", payload="{}", task_lease="old")
    repo.delete(snapshot=first, task_lease="stale")
    assert repo.get(task_id="task") is not None
    repo.delete(snapshot=first, task_lease="old")
    assert repo.get(task_id="task") is None


def test_stale_worker_cannot_delete_progress_saved_after_lease_handover(repository):
    # 旧执行者持有 version=0 快照；租约交接后新执行者保存 version=1，旧执行者随后删除不得生效。
    repo, engine = repository
    stale = repo.create(task_id="task", payload='{"page":0}', task_lease="old")
    _set_lease(engine, "new")
    fresh = repo.save(snapshot=stale, payload='{"page":1}', task_lease="new")
    assert fresh.version == 1
    repo.delete(snapshot=stale, task_lease="old")
    assert repo.get(task_id="task").version == 1
    # 即使旧执行者碰巧持有新租约，旧版本也删不掉新版本。
    repo.delete(snapshot=stale, task_lease="new")
    assert repo.get(task_id="task").version == 1


def _queue(tmp_path):
    from app.db.adapters.subscriptionsearch import TransactionalSubscriptionSearchRepository

    engine = create_engine(f"sqlite:///{tmp_path / 'lifecycle.db'}")
    Base.metadata.create_all(engine)
    return TransactionalSubscriptionSearchRepository(sessionmaker(bind=engine)), engine


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled", "skipped"])
def test_terminal_task_releases_its_checkpoint(tmp_path, state):
    # 同一订阅之后的搜索是新任务，终态任务的检查点不会再被续用。
    queue, engine = _queue(tmp_path)
    queue.enqueue(subscription_ids=(1,), source="timer", priority=10)
    task = queue.claim_next(owner="worker")
    assert queue.sessions.create(task_id=task.task_id, payload="{}", task_lease=task.lease_token)
    assert queue.finish_task(task_id=task.task_id, lease_token=task.lease_token, state=state, error=None)
    assert queue.sessions.get(task_id=task.task_id) is None
    engine.dispose()


def test_deferred_task_keeps_checkpoint_until_batch_is_cancelled(tmp_path):
    queue, engine = _queue(tmp_path)
    enqueued = queue.enqueue(subscription_ids=(1,), source="timer", priority=10)
    task = queue.claim_next(owner="worker")
    queue.sessions.create(task_id=task.task_id, payload="{}", task_lease=task.lease_token)
    assert queue.defer_task(task_id=task.task_id, lease_token=task.lease_token,
                            available_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            phase="waiting_site", message=None)
    assert queue.sessions.get(task_id=task.task_id) is not None
    # 排队中的任务被整批取消时直接进入终态，检查点同步删除。
    assert queue.request_cancel(enqueued.batch.batch_id)
    assert queue.sessions.get(task_id=task.task_id) is None
    engine.dispose()


def test_checkpoints_not_updated_for_fourteen_days_are_purged(repository):
    from app.db.models.searchsession import SearchSession

    repo, engine = repository
    now = datetime.now(timezone.utc)
    with sessionmaker(bind=engine)() as session:
        for task_id, days in (("orphan", 15), ("recent", 13)):
            session.add(SearchSession(task_id=task_id, version=0, payload="{}",
                                      updated_at=(now - timedelta(days=days)).isoformat(timespec="seconds")))
        session.commit()
    assert repo.create(task_id="task", payload="{}", task_lease="old")
    assert repo.get(task_id="orphan") is None
    assert repo.get(task_id="recent") is not None


@pytest.mark.parametrize("enclosure", [
    "https://site.example/download/0123456789abcdef0123456789abcdef?id=1",
    "https://site.example/dl/AbCdEfGh12345678IjKlMnOp9.torrent?id=1",
])
def test_passkey_in_download_path_is_not_persisted(enclosure):
    assert torrent_snapshot(TorrentInfo(enclosure=enclosure))["enclosure"] is None


def test_readable_page_paths_are_still_persisted():
    torrent = TorrentInfo(page_url="https://site.example/torrents/1234567890123456789",
                          enclosure="https://site.example/download.php?id=1&https=1")
    snapshot = torrent_snapshot(torrent)
    assert snapshot["page_url"] == torrent.page_url
    assert snapshot["enclosure"] == torrent.enclosure


def test_internal_candidate_id_is_not_exported():
    torrent = TorrentInfo(site=1, title="Show")
    torrent.search_resource_id = "internal"
    assert "search_resource_id" not in torrent.to_dict()
    assert TorrentInfo(1).site == 1


def test_late_first_create_after_lease_handover_cannot_fail_the_new_owner(repository):
    # 审查复现的交错：旧执行者在租约交接后才插入首个检查点，新执行者此前读到的是空。
    from app.db.models.searchsession import SearchSession

    repo, engine = repository
    _set_lease(engine, "new")
    # 租约条件在插入语句内校验：旧执行者交接后的首次创建不落库。
    assert repo.create(task_id="task", payload='{"by":"old"}', task_lease="old") is None
    assert repo.get(task_id="task") is None
    # 即便旧行已经写入（如交接前已通过校验的迟到事务），新执行者也以版本递增接管，不因唯一键失败。
    with sessionmaker(bind=engine)() as session:
        session.add(SearchSession(task_id="task", version=0, payload='{"by":"old"}',
                                  updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds")))
        session.commit()
    stale = repo.get(task_id="task")
    owned = repo.create(task_id="task", payload='{"by":"new"}', task_lease="new")
    assert (owned.version, owned.payload) == (1, '{"by":"new"}')
    # 旧执行者既不能再创建覆盖，也不能用旧版本推进或删除。
    assert repo.create(task_id="task", payload='{"by":"old"}', task_lease="old") is None
    assert repo.save(snapshot=stale, payload='{"by":"old"}', task_lease="old") is None
    repo.delete(snapshot=stale, task_lease="old")
    assert repo.get(task_id="task").payload == '{"by":"new"}'


def test_first_create_statement_compiles_for_postgresql():
    from sqlalchemy import literal, select
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.dialects.postgresql import insert

    from app.db.models.searchsession import SearchSession

    statement = insert(SearchSession).from_select(
        ["task_id", "version", "payload", "updated_at"], select(literal("t"), literal(0), literal("{}"), literal("now")),
    ).on_conflict_do_nothing(index_elements=[SearchSession.task_id])
    assert "ON CONFLICT (task_id) DO NOTHING" in str(statement.compile(dialect=postgresql.dialect()))
