"""API 后台任务必须进入宿主 TaskRegistry 的回归测试。"""

import asyncio
from types import SimpleNamespace

from app.api.endpoints import history, message, site, subscribe, webhook
from app.runtime.tasks import TaskRegistry


class _TaskRegistry(TaskRegistry):
    """记录同步任务提交参数，不在端点测试中执行真实业务。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        super().__init__()
        self.calls: list[tuple] = []

    def create_sync(self, function, *args, owner: str, **kwargs) -> None:
        """保存函数、参数和 owner。"""
        self.calls.append((function, args, kwargs, owner))

    def create(
        self,
        coroutine,
        *,
        owner: str,
        cancel_on_shutdown: bool = True,
    ) -> None:
        """保存异步任务登记参数，并关闭未执行的 coroutine。"""
        coroutine.close()
        self.calls.append((None, (), {"cancel_on_shutdown": cancel_on_shutdown}, owner))


class _WebhookRequest:
    """提供 webhook 端点读取的最小请求接口。"""

    query_params = {"source": "jellyfin"}

    async def body(self) -> bytes:
        """返回最小请求体。"""
        return b"{}"

    async def form(self) -> dict:
        """返回空表单。"""
        return {}


class _MessageRequest(_WebhookRequest):
    """复用 webhook 请求替身，覆盖用户消息入口所需字段。"""

    headers = {"content-type": "application/json"}


class _SeerrRequest:
    """提供 Seerr 电影订阅 webhook 所需的最小 JSON 请求。"""

    async def json(self) -> dict:
        """返回一个已批准的电影订阅通知。"""
        return {
            "notification_type": "MEDIA_APPROVED",
            "subject": "测试电影",
            "media": {"media_type": "movie", "tmdbId": 123},
            "request": {"requestedBy_username": "tester"},
        }


def test_webhook_post_uses_task_registry() -> None:
    """POST webhook 应登记解析任务，响应仍只表示宿主已接受。"""
    registry = _TaskRegistry()
    response = asyncio.run(
        webhook.webhook_message(registry, _WebhookRequest(), "token")
    )

    function, args, kwargs, owner = registry.calls[0]
    assert response.success is True
    assert function is webhook.start_webhook_chain
    assert args == (b"{}", {}, {"source": "jellyfin"})
    assert kwargs == {}
    assert owner == "api.webhook.message"


def test_webhook_get_uses_task_registry() -> None:
    """GET webhook 应保留旧参数形状并进入相同 owner。"""
    registry = _TaskRegistry()
    response = asyncio.run(
        webhook.webhook_message_get(registry, _WebhookRequest(), "token")
    )

    function, args, kwargs, owner = registry.calls[0]
    assert response.success is True
    assert function is webhook.start_webhook_chain
    assert args == (None, None, {"source": "jellyfin"})
    assert kwargs == {}
    assert owner == "api.webhook.message"


def test_cookiecloud_sync_uses_task_registry(monkeypatch) -> None:
    """CookieCloud 手工同步应登记 Scheduler E1 任务而非 Starlette 后台回调。"""
    registry = _TaskRegistry()
    scheduler = SimpleNamespace(start=lambda **_kwargs: None)
    monkeypatch.setattr(site, "Scheduler", lambda: scheduler)

    response = asyncio.run(site.cookie_cloud_sync(registry, SimpleNamespace()))

    function, args, kwargs, owner = registry.calls[0]
    assert response.success is True
    assert function is scheduler.start
    assert args == ()
    assert kwargs == {"job_id": "cookiecloud"}
    assert owner == "api.site.cookiecloud_sync"


def test_user_message_uses_task_registry() -> None:
    """消息入口应登记 E0 链任务并保持原始载荷。"""
    registry = _TaskRegistry()
    response = asyncio.run(message.user_message(registry, _MessageRequest(), None))

    function, args, kwargs, owner = registry.calls[0]
    assert response.success is True
    assert function is message.start_message_chain
    assert args == (b"{}", {}, {"source": "jellyfin"})
    assert kwargs == {}
    assert owner == "api.message.user"


def test_seerr_subscribe_uses_task_registry(monkeypatch) -> None:
    """Seerr webhook 应登记订阅创建任务且保持旧参数投影。"""
    registry = _TaskRegistry()
    monkeypatch.setattr(
        subscribe,
        "get_api_runtime_config_snapshot",
        lambda: SimpleNamespace(api_token="token"),
    )

    response = asyncio.run(
        subscribe.seerr_subscribe(_SeerrRequest(), registry, "token")
    )

    function, args, kwargs, owner = registry.calls[0]
    assert response.success is True
    assert function is subscribe.start_subscribe_add
    assert args == ()
    assert kwargs == {
        "mtype": subscribe.MediaType.MOVIE,
        "media_source": subscribe.MediaSource.TMDB,
        "media_id": "123",
        "title": "测试电影",
        "year": "",
        "season": None,
        "username": "tester",
    }
    assert owner == "api.subscribe.seerr"


def test_history_ai_redo_uses_task_registry() -> None:
    """单条历史 AI 重做应登记宿主任务并使用稳定 owner。"""
    registry = _TaskRegistry()

    history._start_ai_redo_task(
        history_id=7,
        prompt="整理记录",
        progress_key="progress-7",
        task_registry=registry,
    )

    assert registry.calls == [
        (None, (), {"cancel_on_shutdown": True}, "api.history.ai_redo")
    ]


def test_history_batch_ai_redo_uses_task_registry() -> None:
    """批量历史 AI 重做应登记宿主任务并区分批量 owner。"""
    registry = _TaskRegistry()

    history._start_batch_ai_redo_task(
        history_ids=[7, 8],
        prompt="批量整理",
        progress_key="progress-batch",
        task_registry=registry,
    )

    assert registry.calls == [
        (None, (), {"cancel_on_shutdown": True}, "api.history.ai_redo_batch")
    ]
