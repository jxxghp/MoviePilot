"""API 后台任务必须进入宿主 TaskRegistry 的回归测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.api.dependencies import subscription as subscription_dependencies
from app.api.endpoints import anthropic, history, message, openai, site, subscribe, webhook
from app.application.subscription.search import SubscribeSearchActor
from app.runtime.loop import main_loop_registry
from app.runtime.tasks import TaskRegistry


class _TaskRegistry(TaskRegistry):
    """记录同步任务提交参数，不在端点测试中执行真实业务。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        super().__init__()
        self.calls: list[tuple] = []
        self.threadsafe_calls: list[tuple] = []

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

    def submit_threadsafe(
        self,
        coroutine,
        *,
        loop,
        owner: str,
        cancel_on_shutdown: bool = True,
    ) -> None:
        """保存跨线程任务参数，并关闭未执行的 coroutine。"""
        coroutine.close()
        self.threadsafe_calls.append((loop, owner, cancel_on_shutdown))


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


class _CompletedSearchTaskRegistry(_TaskRegistry):
    """返回已完成结果，验证手工搜索先保存安排再启动后台处理。"""

    def create_sync(self, function, *args, owner: str, **kwargs) -> asyncio.Future:
        """记录同步任务，并为测试提供可等待的安排结果。"""
        super().create_sync(function, *args, owner=owner, **kwargs)
        future = asyncio.get_running_loop().create_future()
        result = None
        if owner == "api.subscribe.search.enqueue":
            result = SimpleNamespace(
                active_batch_ids=("batch-1", "batch-2"),
                created_count=2,
                coalesced_count=0,
            )
        future.set_result(result)
        return future


class _SubscriptionSearchTargets:
    """为手工搜索命令提供当前用户可访问的订阅编号。"""

    async def list_search_ids(self, username, state) -> list[int]:
        """返回稳定目标，并校验超级用户读取全部运行中订阅。"""
        assert username is None
        assert state == "R"
        return [11, 12]


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
    monkeypatch.setattr(site, "get_scheduler", lambda: scheduler)

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
    monkeypatch.setattr(subscribe, "validate_api_credential_identity", lambda: None)

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


def test_manual_subscription_search_uses_task_registry() -> None:
    """手工订阅搜索命令应以稳定 owner 提交顺序搜索批次。"""
    registry = _CompletedSearchTaskRegistry()
    repository = _SubscriptionSearchTargets()
    search_repository = SimpleNamespace(enqueue=lambda **_kwargs: None)
    runtime = SimpleNamespace(
        subscription=SimpleNamespace(
            repository=lambda _db: repository,
            search_repository=search_repository,
        )
    )
    command = subscription_dependencies.get_search_subscriptions_command(
        task_registry=registry,
        db=object(),
        runtime=runtime,
    )

    found = asyncio.run(
        command.execute(SubscribeSearchActor(username="admin", is_superuser=True))
    )

    enqueue_function, enqueue_args, enqueue_kwargs, enqueue_owner = registry.calls[0]
    run_function, run_args, run_kwargs, run_owner = registry.calls[1]
    assert found is not None
    assert found.batch_ids == ("batch-1", "batch-2")
    assert found.queued_count == 2
    assert found.ongoing_count == 0
    assert enqueue_function is search_repository.enqueue
    assert enqueue_args == ()
    assert enqueue_kwargs == {
        "subscription_ids": (11, 12),
        "source": "manual",
        "priority": 100,
    }
    assert enqueue_owner == "api.subscribe.search.enqueue"
    assert run_function is subscription_dependencies._resume_submitted_subscription_search
    assert run_args == ((11, 12),)
    assert run_kwargs == {}
    assert run_owner == "api.subscribe.search.run"


def test_history_ai_redo_uses_task_registry() -> None:
    """单条历史 AI 重做应登记宿主任务并使用稳定 owner。"""
    registry = _TaskRegistry()
    loop = SimpleNamespace(is_running=lambda: True, is_closed=lambda: False)

    with patch.object(main_loop_registry, "require", return_value=loop), patch.object(
        history,
        "_build_progress_output_callback",
        return_value=lambda _text: None,
    ) as build_callback:
        history._start_ai_redo_task(
            history_id=7,
            prompt="整理记录",
            progress_key="progress-7",
            task_registry=registry,
        )
        build_callback.call_args.kwargs["submit"](asyncio.sleep(0))

    assert registry.calls == [
        (None, (), {"cancel_on_shutdown": True}, "api.history.ai_redo")
    ]
    assert build_callback.call_args.args[1] == {"history_id": 7}
    assert registry.threadsafe_calls == [
        (loop, "api.history.ai_redo.progress", True)
    ]


def test_history_progress_callback_uses_owned_threadsafe_submission() -> None:
    """历史 AI 输出进度应进入同一宿主登记器并保留 payload。"""
    registry = _TaskRegistry()
    progress = SimpleNamespace(update=AsyncMock())
    loop = SimpleNamespace(is_running=lambda: True, is_closed=lambda: False)

    callback = history._build_progress_output_callback(
        progress,
        {"history_id": 7},
        submit=lambda coroutine: registry.submit_threadsafe(
            coroutine,
            loop=loop,
            owner="api.history.ai_redo.progress",
        ),
    )
    callback("正在分析")

    progress.update.assert_called_once_with(
        text="正在分析", data={"history_id": 7}
    )
    assert registry.threadsafe_calls == [
        (loop, "api.history.ai_redo.progress", True)
    ]


def test_history_batch_ai_redo_uses_task_registry() -> None:
    """批量历史 AI 重做应登记宿主任务并区分批量 owner。"""
    registry = _TaskRegistry()
    loop = SimpleNamespace(is_running=lambda: True, is_closed=lambda: False)

    with patch.object(main_loop_registry, "require", return_value=loop), patch.object(
        history,
        "_build_progress_output_callback",
        return_value=lambda _text: None,
    ) as build_callback:
        history._start_batch_ai_redo_task(
            history_ids=[7, 8],
            prompt="批量整理",
            progress_key="progress-batch",
            task_registry=registry,
        )
        build_callback.call_args.kwargs["submit"](asyncio.sleep(0))

    assert registry.calls == [
        (None, (), {"cancel_on_shutdown": True}, "api.history.ai_redo_batch")
    ]
    assert build_callback.call_args.args[1] == {"history_ids": [7, 8]}
    assert registry.threadsafe_calls == [
        (loop, "api.history.ai_redo_batch.progress", True)
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
