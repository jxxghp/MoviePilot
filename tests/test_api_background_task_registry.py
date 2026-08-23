"""API 后台任务必须进入宿主 TaskRegistry 的回归测试。"""

import asyncio
from types import SimpleNamespace

from app.api.endpoints import anthropic, history, message, openai, site, subscribe, webhook
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


class _RunningTaskRegistry(TaskRegistry):
    """执行协议流任务并保留 owner，验证真实 TaskRegistry 行为。"""

    def __init__(self) -> None:
        """初始化 owner 调用记录。"""
        super().__init__()
        self.owners: list[str] = []

    def create(
        self,
        coroutine,
        *,
        owner: str,
        cancel_on_shutdown: bool = True,
    ) -> asyncio.Task:
        """记录 owner 后委托真实登记器创建任务。"""
        self.owners.append(owner)
        return super().create(
            coroutine,
            owner=owner,
            cancel_on_shutdown=cancel_on_shutdown,
        )


class _ProtocolManager:
    """提供兼容协议流结束时需要的最小 AgentManager 接口。"""

    async def clear_session(self, **_kwargs) -> None:
        """模拟清理临时协议会话。"""

    async def stop_current_task(self, _session_id: str) -> None:
        """模拟停止保留会话的当前任务。"""


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


def test_openai_stream_uses_task_registry(monkeypatch) -> None:
    """OpenAI SSE Agent 执行应登记为请求级后台任务。"""

    async def run_agent(**kwargs):
        """向协议队列写入一个增量后结束。"""
        await kwargs["event_queue"].put("reply")
        return "", []

    monkeypatch.setattr(openai, "_run_managed_agent", run_agent)

    async def scenario() -> None:
        registry = _RunningTaskRegistry()
        events = [
            event
            async for event in openai._stream_response(
                manager=_ProtocolManager(),
                session_id="session",
                user_id="user",
                username="tester",
                prompt="hello",
                images=[],
                cleanup_session=True,
                task_registry=registry,
            )
        ]

        assert events[-1] == "data: [DONE]\n\n"
        assert registry.owners == ["api.openai.stream"]

    asyncio.run(scenario())


def test_anthropic_stream_uses_task_registry(monkeypatch) -> None:
    """Anthropic SSE Agent 执行应登记为请求级后台任务。"""

    async def run_agent(**kwargs):
        """向协议队列写入一个增量后结束。"""
        await kwargs["event_queue"].put("reply")
        return "", []

    monkeypatch.setattr(anthropic, "_run_managed_agent", run_agent)

    async def scenario() -> None:
        registry = _RunningTaskRegistry()
        events = [
            event
            async for event in anthropic._stream_anthropic_response(
                manager=_ProtocolManager(),
                session_id="session",
                user_id="user",
                prompt="hello",
                images=[],
                task_registry=registry,
            )
        ]

        assert "event: message_stop" in events[-1]
        assert registry.owners == ["api.anthropic.stream"]

    asyncio.run(scenario())
