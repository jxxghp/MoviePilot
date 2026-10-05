"""订阅更新显式清空可选设置的回归测试。"""

from types import SimpleNamespace
from typing import Optional

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.endpoints.subscribe import update_subscribe
from app.application.subscription.mutation import SubscriptionMutationService
from app.db.adapters.outbox import SqlAlchemyAsyncOutboxDispatchStore, SqlAlchemyAsyncOutboxStager
from app.db.adapters.subscription import SessionSubscriptionRepository
from app.db.models.outbox import OutboxMessage
from app.db.models.subscribe import Subscribe as SubscribeModel
from app.db.session import async_session_scope
from app.db.uow import SqlAlchemyAsyncUnitOfWork
from app.schemas.subscribe import Subscribe
from app.schemas.types import MediaSource, MediaType

OPTIONAL_SETTINGS = {
    "filter": "custom-rule",
    "include": "CHS",
    "exclude": "CAM",
    "quality": "WEB-DL",
    "resolution": "4K",
    "effect": "HDR",
    "audio_quality": "lossless",
    "audio_format": "FLAC",
    "custom_words": " ",
    "keyword": "测试关键字",
    "save_path": "/downloads/custom",
    "episode_group": "custom-group",
    "downloader": "custom-downloader",
    "min_bitrate": 320000,
    "min_bit_depth": 24,
    "min_sample_rate": 96000,
}


class _SubscribeRow:
    """提供更新端点所需字段的最小订阅替身。"""

    def __init__(self) -> None:
        """构造包含已有分辨率限制的订阅。"""
        self.id = 1
        self.username = "alice"
        self.type = "电影"
        self.resolution = "4K"
        self.total_episode = 0
        self.lack_episode = 0

    def to_dict(self) -> dict:
        """返回当前订阅快照。"""
        return dict(self.__dict__)


class _MutationService:
    """记录端点交给订阅写服务的更新 payload。"""

    def __init__(self, subscribe: _SubscribeRow) -> None:
        """保存订阅替身并初始化待观察的更新载荷。"""
        self.subscribe = subscribe
        self.payload = None

    async def get_accessible(self, _subscribe_id: int, _actor) -> _SubscribeRow:
        """返回当前用户可访问的订阅。"""
        return self.subscribe

    async def update(self, _subscribe_id: int, payload: dict, _actor, **_kwargs):
        """应用更新并返回已发布事件的变更结果。"""
        old = self.subscribe.to_dict()
        self.payload = dict(payload)
        self.subscribe.__dict__.update(payload)
        return SimpleNamespace(
            old=old,
            new=self.subscribe.to_dict(),
            event_published=True,
        )


@pytest.mark.anyio
@pytest.mark.parametrize("field, previous", OPTIONAL_SETTINGS.items())
@pytest.mark.parametrize("empty", ["", None])
async def test_update_subscribe_clears_explicit_optional_setting(field, previous, empty) -> None:
    """表单空字符串与 JSON null 都应显式清空旧设置，包含历史纯空格识别词。"""
    subscribe = _SubscribeRow()
    setattr(subscribe, field, previous)
    mutation = _MutationService(subscribe)
    raw = {"id": 1, field: empty}
    subscribe_in = Subscribe.model_validate(raw)

    response = await update_subscribe(
        subscribe_in=subscribe_in,
        mutation=mutation,
        current_user=SimpleNamespace(name="alice", is_superuser=False),
    )

    assert response.success is True
    assert field in subscribe_in.model_fields_set
    assert mutation.payload == {field: None, "username": "alice"}
    assert getattr(subscribe, field) is None
    assert raw == {"id": 1, field: empty}


def test_subscribe_omitted_optional_settings_are_not_written() -> None:
    """省略可选设置时不能用模型默认值覆盖已有设置。"""
    subscribe = Subscribe(id=1, name="新名称")

    assert subscribe.to_public_write_payload(exclude_unset=True) == {"name": "新名称"}


@pytest.mark.parametrize("field, value", OPTIONAL_SETTINGS.items())
def test_subscribe_preserves_nonempty_optional_settings(field, value) -> None:
    """非空设置保持原值，识别词中的有意义空白也不得被自动裁剪。"""
    subscribe = Subscribe.model_validate({field: value})

    assert subscribe.to_public_write_payload(exclude_unset=True) == {field: value}


@pytest.mark.parametrize("field", ["min_bitrate", "min_bit_depth", "min_sample_rate"])
@pytest.mark.parametrize("value", [0, "0", "24"])
def test_subscribe_preserves_numeric_optional_settings(field: str, value) -> None:
    """音频下限中的显式零值和数字字符串继续按数值处理。"""
    subscribe = Subscribe.model_validate({field: value})

    assert subscribe.to_public_write_payload(exclude_unset=True) == {field: int(value)}


def test_subscribe_empty_metadata_fields_remain_omitted() -> None:
    """名称、类型、集数等字段仍沿用空字符串回退默认值的历史行为。"""
    subscribe = Subscribe(
        name="", type="", season="", total_episode="", start_episode="",
        sites="", filter_groups="",
    )

    assert subscribe.to_public_write_payload(exclude_unset=True) == {}
    assert subscribe.total_episode == 0
    assert subscribe.sites == []


@pytest.mark.parametrize("media_type", [MediaType.TV, MediaType.MUSIC])
@pytest.mark.parametrize("empty", ["", None])
def test_update_subscribe_persists_cleared_optional_settings(db, media_type: MediaType, empty: Optional[str]) -> None:
    """真实写服务提交后重新读库，清空结果和修改事件都必须保留，未提交字段不得改变。"""
    db.watermark(SubscribeModel, OutboxMessage)
    row = db.add(SubscribeModel(
        name="清空设置测试",
        type=media_type.value,
        username="alice",
        media_source=MediaSource.TMDB.value,
        media_id="6894",
        music_type="album" if media_type == MediaType.MUSIC else None,
        total_tracks=12 if media_type == MediaType.MUSIC else None,
        season=1,
        total_episode=12,
        lack_episode=7,
        manual_total_episode=0,
        search_interval=24,
        **OPTIONAL_SETTINGS,
    ))
    subscribe_id = row.id
    published = []

    async def execute(session: AsyncSession):
        """通过请求模型、端点、写服务和真实事务执行清空。"""
        async def publish(payload: dict) -> None:
            """只记录事件，避免向真实插件或外部服务派发。"""
            published.append(payload)

        mutation = SubscriptionMutationService(
            repository=SessionSubscriptionRepository(session),
            unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
            outbox=SqlAlchemyAsyncOutboxStager(session),
            dispatch_store=SqlAlchemyAsyncOutboxDispatchStore(async_session_scope),
            publish_modified=publish,
        )
        return await update_subscribe(
            subscribe_in=Subscribe.model_validate({
                "id": subscribe_id, **dict.fromkeys(OPTIONAL_SETTINGS, empty),
            }),
            mutation=mutation,
            current_user=SimpleNamespace(name="alice", is_superuser=False),
        )

    response = db.run_async_session(execute)

    db.session.expire_all()
    saved = db.session.get(SubscribeModel, subscribe_id)
    assert response.success is True
    assert all(getattr(saved, field) is None for field in OPTIONAL_SETTINGS)
    assert saved.name == "清空设置测试"
    assert saved.type == media_type.value
    assert saved.media_source == MediaSource.TMDB.value
    assert saved.media_id == "6894"
    assert (saved.season, saved.total_episode, saved.lack_episode, saved.manual_total_episode) == (1, 12, 7, 0)
    assert saved.search_interval == 24
    if media_type == MediaType.MUSIC:
        assert (saved.music_type, saved.total_tracks) == ("album", 12)
    assert len(published) == 1
    assert set(published[0]["fields"]) == set(OPTIONAL_SETTINGS)
    intent = db.session.query(OutboxMessage).filter_by(event_key=published[0]["idempotency_key"]).one()
    assert intent.status == "completed"
    assert all(intent.payload["subscribe_info"][field] is None for field in OPTIONAL_SETTINGS)
