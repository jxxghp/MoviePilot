"""订阅更新显式清空筛选条件的回归测试。"""

from types import SimpleNamespace

import pytest

from app.api.endpoints.subscribe import update_subscribe
from app.schemas.subscribe import Subscribe


class _SubscribeRow:
    """提供更新端点所需字段的最小订阅替身。"""

    def __init__(self) -> None:
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
async def test_update_subscribe_clears_explicit_empty_resolution() -> None:
    """从 4K 切换到全部时，空字符串必须作为显式 None 写入而非被忽略。"""
    subscribe = _SubscribeRow()
    mutation = _MutationService(subscribe)
    subscribe_in = Subscribe(id=1, resolution="")

    response = await update_subscribe(
        subscribe_in=subscribe_in,
        mutation=mutation,
        current_user=SimpleNamespace(name="alice", is_superuser=False),
    )

    assert response.success is True
    assert "resolution" in subscribe_in.model_fields_set
    assert mutation.payload["resolution"] is None
    assert subscribe.resolution is None
