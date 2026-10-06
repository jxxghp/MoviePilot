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


def test_finished_round_removes_its_checkpoint_only_with_the_current_lease(repository):
    repo, engine = repository
    repo.create(task_id="task", payload="{}", task_lease="old")
    repo.delete(task_id="task", task_lease="stale")
    assert repo.get(task_id="task") is not None
    repo.delete(task_id="task", task_lease="old")
    assert repo.get(task_id="task") is None
