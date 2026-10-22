"""
独立审查发现的订阅目标切换质量事实回归，使用真实事务与同事务outbox暂存。
"""

from unittest.mock import AsyncMock, Mock

from app.application.subscription.mutation import (
    SubscriptionActor,
    SubscriptionMutationService,
    SyncSubscriptionMutationService,
)
from app.db.adapters.outbox import SqlAlchemyAsyncOutboxStager, SqlAlchemyOutboxStager
from app.db.adapters.subscription import SessionSubscriptionRepository
from app.db.models.outbox import OutboxMessage
from app.db.models.subscribe import Subscribe
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork

_QUALITY_FACTS = ("current_audio_format", "current_bitrate", "current_bit_depth", "current_sample_rate")


def _old_album():
    """
    构造已完成部分下载且保存了旧作品音频质量的订阅。
    """
    return Subscribe(name="旧专辑", username="review", type="音乐", music_type="album",
        media_source="musicbrainz", media_id="old-release", total_episode=10, lack_episode=8,
        note=[1, 2], downloaded_tracks=["old-track"], current_priority=100,
        current_audio_format="FLAC", current_bitrate=1411200, current_bit_depth=24, current_sample_rate=96000)


def test_sync_album_retarget_clears_old_quality_in_real_transaction(db):
    """
    切换作品后真实行、业务结果与持久事件都不得保留旧版本质量事实。
    """
    db.watermark(Subscribe, OutboxMessage)
    album = db.add(_old_album())
    service = SyncSubscriptionMutationService(
        SessionSubscriptionRepository(db.session), SqlAlchemyUnitOfWork(db.session),
        SqlAlchemyOutboxStager(db.session), Mock(claim_by_event_key=Mock(return_value=None)), Mock())
    result = service.update(album.id, {"media_id": "new-release"}, SubscriptionActor("review", False))
    db.session.expire_all()
    stored = db.session.get(Subscribe, album.id)
    event = db.session.query(OutboxMessage).filter(
        OutboxMessage.event_key.startswith(f"subscribe.modified:{album.id}:"),
    ).one()
    assert result.business_committed
    assert stored.media_id == "new-release"
    assert stored.downloaded_tracks == []
    for field in _QUALITY_FACTS:
        assert getattr(stored, field) is None
        assert getattr(result.snapshot, field) is None
        assert event.payload["subscribe_info"][field] is None


def test_async_album_retarget_clears_old_quality_in_real_transaction(db):
    """
    请求级异步事务与同步入口保持同一质量清理和outbox提交合同。
    """
    db.watermark(Subscribe, OutboxMessage)
    album = db.add(_old_album())

    async def execute(session):
        """
        在真实AsyncSession中调用正式订阅修改服务。
        """
        service = SubscriptionMutationService(
            SessionSubscriptionRepository(session), SqlAlchemyAsyncUnitOfWork(session),
            SqlAlchemyAsyncOutboxStager(session), AsyncMock(claim_by_event_key=AsyncMock(return_value=None)), AsyncMock())
        return await service.update(album.id, {"media_id": "new-release"}, SubscriptionActor("review", False))

    result = db.run_async_session(execute)
    db.session.expire_all()
    stored = db.session.get(Subscribe, album.id)
    event = db.session.query(OutboxMessage).filter(
        OutboxMessage.event_key.startswith(f"subscribe.modified:{album.id}:"),
    ).one()
    assert result.business_committed
    assert stored.media_id == "new-release"
    assert stored.downloaded_tracks == []
    for field in _QUALITY_FACTS:
        assert getattr(stored, field) is None
        assert getattr(result.snapshot, field) is None
        assert event.payload["subscribe_info"][field] is None
