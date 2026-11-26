"""
验证通知消息队列、历史和事件流投递行为。
"""

import asyncio
import json
from collections.abc import Iterator
from unittest.mock import Mock

import pytest

from app.api.endpoints import message as message_endpoint
from app.api.endpoints.message import (
    clear_notification_message,
    get_message_history,
    get_notification_message,
)
from app.api.endpoints.system import get_message
from app.application.messaging.message import (
    _SSE_QUEUE_MAXSIZE,
    MessageHelper,
    MessageQueryService,
    MessageViewer,
)
from app.chain.base import ChainBase
from app.db.models.message import Message as MessageModel
from app.db.oper.message import MessageOper
from app.db.oper.systemconfig import SystemConfigOper
from app.db.session import AsyncSessionFactory, SessionFactory
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.message import Message, MessageClearScope
from app.schemas.token import TokenPayload
from app.schemas.types import MediaType, MessageType, SystemConfigKey


def _clear_messages() -> None:
    """
    清空消息表，隔离通知测试数据。
    """
    with SessionFactory() as db:
        db.query(MessageModel).delete()
        db.commit()
    SystemConfigOper().delete(SystemConfigKey.NotificationClearBefore)


@pytest.fixture(autouse=True)
def _isolate_message_state() -> Iterator[None]:
    """
    在每条用例前后清理默认消息表与通知清理水位，避免跨测试文件残留。

    :yields: 测试执行上下文
    """
    _clear_messages()
    yield
    _clear_messages()


def _admin_token() -> TokenPayload:
    """
    构造超管令牌载荷，直调端点时用于关闭可见性过滤。
    """
    return TokenPayload(sub=1, username="admin", super_user=True)


def _reset_message_helper(helper: MessageHelper) -> None:
    """
    清空单例消息队列和去重缓存，避免用例间互相影响。
    """
    while helper.get() is not None:
        pass
    helper._recent_notification_keys.clear()


def _set_message_time(title: str, reg_time: str) -> None:
    """
    调整测试消息时间，避免消息写入时的当前秒影响清理边界断言。
    """
    with SessionFactory() as db:
        db.query(MessageModel).filter(MessageModel.title == title).update({"reg_time": reg_time})
        db.commit()


def test_notification_history_only_lists_sent_messages() -> None:
    """
    通知历史接口仅返回已发送消息（action=1），不混入用户发来的接收消息（action=0）。
    """
    _clear_messages()
    oper = MessageOper()
    oper.add(title="系统通知", text="下载完成", action=1, mtype=MessageType.Download)
    oper.add(title="用户消息", text="帮我搜索", action=0)
    oper.add(title="智能体回复", text="已处理", action=1, mtype=MessageType.Agent)

    async def _load_notifications() -> list[tuple[str | None, int | None]]:
        """
        通过通知历史端点读取已发送消息列表。
        """
        async with AsyncSessionFactory() as db:
            items = await get_notification_message(
                token_data=_admin_token(),
                service=MessageQueryService(MessageOper(db)),
                page=1,
                count=10,
            )
        return [(item.title, item.action) for item in items]

    assert asyncio.run(_load_notifications()) == [
        ("智能体回复", 1),
        ("系统通知", 1),
    ]


def test_message_history_endpoint_returns_all_directions_and_keeps_web_compat() -> None:
    """
    消息历史接口返回接收与发送全部记录，同时保留 /web 兼容路由并隐藏其公开文档。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.adapters.web.security.access import verify_token
    from app.api.dependencies.agent import get_message_query_service
    _clear_messages()
    oper = MessageOper()
    oper.add(title="智能体回复", text="已处理", action=1, mtype=MessageType.Agent)
    oper.add(title="用户消息", text="/ai 帮我处理", action=0)
    oper.add(title="普通通知", text="下载完成", action=1, mtype=MessageType.Download)

    async def _load_history() -> list[tuple[str | None, int | None]]:
        """
        通过消息历史端点读取全部方向记录。
        """
        async with AsyncSessionFactory() as db:
            items = await get_message_history(
                token_data=_admin_token(),
                service=MessageQueryService(MessageOper(db)),
                page=1,
                count=10,
            )
        return [(item.title, item.action) for item in items]

    assert asyncio.run(_load_history()) == [
        ("普通通知", 1),
        ("用户消息", 0),
        ("智能体回复", 1),
    ]

    app = FastAPI()
    app.include_router(message_endpoint.router, prefix="/api/v1/message")
    app.dependency_overrides[verify_token] = lambda: TokenPayload(sub="1", super_user=True)

    async def _override_service() -> MessageQueryService:
        """
        在测试请求内提供绑定异步会话的消息历史查询服务。
        """
        async with AsyncSessionFactory() as db:
            items = await MessageOper(db).async_list_by_page(page=1, count=20)

        class _SnapshotRepository:
            """
            回放预取消息对象的内存测试仓储。
            """

            async def async_list_by_page(self, page: int = 1, count: int = 30) -> list[MessageModel]:
                """
                返回当前测试批次中的消息快照。

                :param page: 页码
                :param count: 每页数量
                :return: 切片后的消息列表
                """
                start = (page - 1) * count
                return items[start:start + count]

            async def async_list_sent_by_page(
                self,
                page: int = 1,
                count: int = 30,
                all_clear_before: str | None = None,
                system_clear_before: str | None = None,
                media_clear_before: str | None = None,
            ) -> list[MessageModel]:
                """
                返回当前测试批次中发送方向的通知消息快照。

                :param page: 页码
                :param count: 每页数量
                :param all_clear_before: 全部清理水位
                :param system_clear_before: 系统通知清理水位
                :param media_clear_before: 媒体通知清理水位
                :return: 仅包含发送方向的消息列表
                """
                sent = [item for item in items if item.action == 1]
                start = (page - 1) * count
                return sent[start:start + count]

        return MessageQueryService(_SnapshotRepository())

    app.dependency_overrides[get_message_query_service] = _override_service
    with TestClient(app) as client:
        history_res = client.get("/api/v1/message/history?page=1&count=10")
        compat_res = client.get("/api/v1/message/web?page=1&count=10")
        notification_res = client.get("/api/v1/message/notification?page=1&count=10")
        openapi_paths = client.get("/openapi.json").json()["paths"]

    assert history_res.status_code == 200
    assert compat_res.status_code == 200
    assert history_res.json() == compat_res.json()
    assert [(row["title"], row["action"]) for row in history_res.json()["data"]] == [
        ("普通通知", 1),
        ("用户消息", 0),
        ("智能体回复", 1),
    ]
    assert [(row["title"], row["action"]) for row in notification_res.json()["data"]] == [
        ("普通通知", 1),
        ("智能体回复", 1),
    ]
    assert "get" in openapi_paths["/api/v1/message/history"]
    assert not openapi_paths["/api/v1/message/history"]["get"].get("deprecated")
    assert openapi_paths["/api/v1/message/web"]["get"].get("deprecated") is True


def test_message_query_service_skips_broken_row_projection() -> None:
    """
    单条历史记录序列化异常时，查询服务记录错误并继续返回其余有效消息。
    """

    class _BrokenRow:
        """
        模拟 to_dict 抛出异常的损坏持久化对象。
        """

        def to_dict(self) -> dict[str, object]:
            """
            抛出序列化错误以验证容错分支。

            :raises RuntimeError: 始终抛出模拟异常
            """
            raise RuntimeError("corrupted row")

    class _ValidRow:
        """
        模拟正常序列化为字典的消息持久化对象。
        """

        def to_dict(self) -> dict[str, object]:
            """
            返回最小有效消息字典。

            :return: 包含标题与方向的字典
            """
            return {"title": "正常消息", "action": 0}

    class _StubRepository:
        """
        混合返回损坏记录与有效记录的测试仓储。
        """

        async def async_list_by_page(self, page: int = 1, count: int = 30) -> list[object]:
            """
            返回一条损坏记录与一条有效记录。

            :param page: 页码
            :param count: 每页数量
            :return: 混合测试记录列表
            """
            return [_BrokenRow(), _ValidRow()]

        async def async_list_sent_by_page(
            self,
            page: int = 1,
            count: int = 30,
            all_clear_before: str | None = None,
            system_clear_before: str | None = None,
            media_clear_before: str | None = None,
        ) -> list[object]:
            """
            满足协议签名的空通知查询实现。

            :param page: 页码
            :param count: 每页数量
            :param all_clear_before: 全部清理水位
            :param system_clear_before: 系统通知清理水位
            :param media_clear_before: 媒体通知清理水位
            :return: 空列表
            """
            return []

    result = asyncio.run(MessageQueryService(_StubRepository()).list_history(page=1, count=10))
    assert result == [{"title": "正常消息", "action": 0}]


def test_notification_clear_marker_filters_history_across_requests() -> None:
    """
    通知清理时间写入后端后，后续通知历史查询应直接返回过滤后的结果。
    """
    _clear_messages()
    oper = MessageOper()
    oper.add(
        title="旧系统通知",
        text="任务失败",
        action=1,
        mtype=MessageType.Other,
    )
    oper.add(
        title="旧媒体通知",
        text="影片入库",
        image="https://example.com/poster.jpg",
        action=1,
    )
    _set_message_time("旧系统通知", "2026-01-01 00:00:00")
    _set_message_time("旧媒体通知", "2026-01-01 00:00:00")

    asyncio.run(clear_notification_message(scope=MessageClearScope.Media))

    oper.add(
        title="新媒体通知",
        text="影片入库",
        image="https://example.com/new.jpg",
        action=1,
    )
    _set_message_time("新媒体通知", "2999-01-01 00:00:00")

    async def _load_titles() -> list[str]:
        """
        通过异步接口读取通知标题。
        """
        async with AsyncSessionFactory() as db:
            messages = await get_notification_message(
                token_data=_admin_token(),
                service=MessageQueryService(MessageOper(db)),
            )
        return [message.title for message in messages]

    assert asyncio.run(_load_titles()) == ["新媒体通知", "旧系统通知"]


def test_system_helper_message_only_enters_sse_queue() -> None:
    """
    系统实时消息只进入前端 SSE 队列，不写入通知历史。
    """
    _clear_messages()
    helper = MessageHelper()
    _reset_message_helper(helper)

    helper.put("调度任务执行失败", role="system", title="系统错误")

    assert MessageOper().list_by_page(page=1, count=10) == []
    realtime_message = json.loads(helper.get())
    assert realtime_message["type"] == "system"
    assert realtime_message["title"] == "系统错误"
    assert realtime_message["text"] == "调度任务执行失败"


def test_system_helper_message_carries_business_type_and_level() -> None:
    """
    实时消息应携带业务类型和严重级别，未标记级别的系统消息不应被当作错误。
    """
    helper = MessageHelper()
    _reset_message_helper(helper)

    helper.put("测试剧集 搜索完成！", role="system", title="订阅搜索", mtype=MessageType.Subscribe)
    helper.put("用户认证失败", role="system", title="用户认证", level="warning")

    subscribe_message = json.loads(helper.get())
    assert subscribe_message["mtype"] == "订阅"
    assert subscribe_message["level"] is None
    warning_message = json.loads(helper.get())
    assert warning_message["mtype"] is None
    assert warning_message["level"] == "warning"


def test_plugin_helper_message_deduplicates_recent_sse_messages() -> None:
    """
    短时间内相同插件实时消息只应推送一次，不写入通知历史。
    """
    _clear_messages()
    helper = MessageHelper()
    _reset_message_helper(helper)

    helper.put("站点刷流任务出错，获取下载器实例失败，请检查配置", role="plugin", title="站点刷流")
    helper.put("站点刷流任务出错，获取下载器实例失败，请检查配置", role="plugin", title="站点刷流")

    assert MessageOper().list_by_page(page=1, count=10) == []
    assert json.loads(helper.get())["title"] == "站点刷流"
    assert helper.get() is None


def test_helper_message_queue_drops_oldest_when_full() -> None:
    """
    没有 SSE 消费者时实时消息队列有上限，超出后丢弃最旧消息，保留最新消息。
    """
    helper = MessageHelper()
    _reset_message_helper(helper)

    for index in range(_SSE_QUEUE_MAXSIZE + 5):
        helper.put(f"积压消息 {index}", role="system", title="队列上限")

    assert helper.sys_queue.qsize() == _SSE_QUEUE_MAXSIZE
    texts = [json.loads(detail)["text"] for detail in helper.drain()]
    assert texts == [f"积压消息 {index}" for index in range(5, _SSE_QUEUE_MAXSIZE + 5)]
    assert helper.get() is None


def test_sse_endpoint_flushes_backlog_in_one_tick() -> None:
    """
    SSE 重新连接后应一次推送全部积压消息，而不是每 3 秒推送一条。
    """
    helper = MessageHelper()
    _reset_message_helper(helper)
    for index in range(3):
        helper.put(f"重连积压 {index}", role="plugin", title="积压推送")

    class _ConnectedRequest:
        async def is_disconnected(self) -> bool:
            """
            始终保持连接，确保测试可完整消费积压事件。
            """
            return False

    async def _read_backlog() -> list[str]:
        response = await get_message(request=_ConnectedRequest(), role="notification", _=None)
        stream = response.body_iterator
        try:
            # 3 秒间隔只发生在一轮推送之后，整轮积压应在超时前全部到达
            return [await asyncio.wait_for(anext(stream), timeout=1) for _ in range(3)]
        finally:
            await stream.aclose()

    frames = asyncio.run(_read_backlog())

    texts = [json.loads(frame.removeprefix("data: ").strip())["text"] for frame in frames]
    assert texts == [f"重连积压 {index}" for index in range(3)]
    assert helper.get() is None


def test_incoming_verify_log_omits_token(monkeypatch) -> None:
    """
    回调验证日志只记录排查所需的参数，不输出 token。
    """
    logged: list[str] = []
    monkeypatch.setattr(message_endpoint.logger, "info", lambda msg, *args, **kwargs: logged.append(str(msg)))

    result = message_endpoint.incoming_verify(token="callback-token-value", source="vocechat")

    assert result == {"status": "OK"}
    assert logged
    assert all("callback-token-value" not in line for line in logged)


def test_agent_helper_message_does_not_enter_sse_queue() -> None:
    """
    智能体消息不进入前端 SSE 队列。
    """
    helper = MessageHelper()
    _reset_message_helper(helper)

    helper.put("智能体回复", role="agent", title="MoviePilot助手")

    assert helper.get() is None


def test_user_helper_message_does_not_enter_sse_queue() -> None:
    """
    用户消息不进入前端 SSE 队列。
    """
    helper = MessageHelper()
    _reset_message_helper(helper)

    helper.put("用户输入", role="user", title="admin")

    assert helper.get() is None


def test_notification_post_message_is_persisted_without_sse_queue(monkeypatch) -> None:
    """
    业务通知通过消息链发送时只登记数据库，不进入前端 SSE 队列。
    """
    _clear_messages()
    helper = MessageHelper()
    _reset_message_helper(helper)
    chain = ChainBase()

    # messagequeue 是全局单例，用 monkeypatch 避免用例间污染
    monkeypatch.setattr(
        chain.user_repository,
        "get_notification_settings",
        lambda _username: {"telegram_userid": "admin-1"},
    )
    send_message = Mock()
    monkeypatch.setattr(chain.messagequeue, "send_message", send_message)
    monkeypatch.setattr(chain.eventmanager, "send_event", Mock())

    chain.post_message(
        Message(
            mtype=MessageType.Download,
            title="下载完成",
            text="影片已加入下载器",
        )
    )

    messages = MessageOper().list_by_page(page=1, count=10)
    assert len(messages) == 1
    assert messages[0].title == "下载完成"
    assert messages[0].mtype == MessageType.Download.value
    assert helper.get() is None
    send_message.assert_called_once()


def test_agent_notification_post_message_is_persisted_without_sse_queue(monkeypatch) -> None:
    """
    智能体消息通过消息链发送时登记数据库，但不进入前端 SSE 队列。
    """
    _clear_messages()
    helper = MessageHelper()
    _reset_message_helper(helper)
    chain = ChainBase()

    # messagequeue 是全局单例，用 monkeypatch 避免用例间污染
    monkeypatch.setattr(
        chain.user_repository,
        "get_notification_settings",
        lambda _username: {"telegram_userid": "admin-1"},
    )
    send_message = Mock()
    monkeypatch.setattr(chain.messagequeue, "send_message", send_message)
    monkeypatch.setattr(chain.eventmanager, "send_event", Mock())

    chain.post_message(
        Message(
            mtype=MessageType.Agent,
            title="MoviePilot助手",
            text="已完成处理",
        )
    )

    messages = MessageOper().list_by_page(page=1, count=10)
    assert len(messages) == 1
    assert messages[0].title == "MoviePilot助手"
    assert messages[0].mtype == MessageType.Agent.value
    assert helper.get() is None
    send_message.assert_called_once()


def test_transient_notification_post_message_skips_history_but_dispatches(monkeypatch) -> None:
    """
    标记为不保存历史的过程消息应跳过数据库登记，但仍正常派发。
    """
    _clear_messages()
    chain = ChainBase()

    # messagequeue 是全局单例，用 monkeypatch 避免用例间污染
    monkeypatch.setattr(
        chain.user_repository,
        "get_notification_settings",
        lambda _username: {"telegram_userid": "admin-1"},
    )
    send_message = Mock()
    monkeypatch.setattr(chain.messagequeue, "send_message", send_message)
    send_event = Mock()
    monkeypatch.setattr(chain.eventmanager, "send_event", send_event)

    chain.post_message(
        Message(
            title="请选择下载目录",
            text="1. 默认目录",
            save_history=False,
        )
    )

    assert MessageOper().list_by_page(page=1, count=10) == []
    assert "save_history" not in send_event.call_args.kwargs["data"]
    send_event.assert_called_once()
    send_message.assert_called_once()


def test_transient_media_and_torrent_lists_skip_history_but_dispatch(monkeypatch) -> None:
    """
    传统交互候选列表标记为不保存历史时，只发送到渠道，不写入消息表。
    """
    _clear_messages()
    chain = ChainBase()
    media = MediaInfo(type=MediaType.MOVIE, title="星际穿越", year="2014")
    torrent = Context(
        meta_info=MetaBase("星际穿越"),
        media_info=media,
        torrent_info=TorrentInfo(
            title="星际穿越.2014.1080p",
            site_name="TestSite",
            enclosure="https://example.com/demo.torrent",
        ),
    )

    # messagequeue 是全局单例，用 monkeypatch 避免用例间污染
    send_message = Mock()
    monkeypatch.setattr(chain.messagequeue, "send_message", send_message)

    chain.post_medias_message(
        Message(title="请选择媒体", save_history=False),
        medias=[media],
    )
    chain.post_torrents_message(
        Message(title="请选择资源", save_history=False),
        torrents=[torrent],
    )

    assert MessageOper().list_by_page(page=1, count=10) == []
    assert send_message.call_count == 2


def test_user_notification_types_keep_only_broadcast_scope(monkeypatch) -> None:
    """
    只有发送范围为全部用户的场景进入普通用户可见类型。
    """
    from app.application import notification as notification_application

    monkeypatch.setattr(
        notification_application,
        "get_notification_switch",
        lambda mtype: "all" if mtype == MessageType.Organize else "admin",
    )

    assert notification_application.get_user_notification_types() == (
        MessageType.Organize.value,
    )


def test_message_query_service_hides_records_outside_visible_scope(monkeypatch) -> None:
    """
    普通用户读取历史时只返回广播通知与本人记录，超管不受限制。
    """
    _clear_messages()
    oper = MessageOper()
    oper.add(title="入库广播", text="已入库", action=1, mtype=MessageType.Organize)
    oper.add(title="站点告警", text="站点失效", action=1, mtype=MessageType.SiteMessage)
    oper.add(title="本人提问", text="/ai 帮我搜索", action=0, userid="7")
    oper.add(title="他人提问", text="/ai 帮我搜索", action=0, userid="9")
    monkeypatch.setattr(
        "app.application.messaging.message.get_user_notification_types",
        lambda: (MessageType.Organize.value,),
    )

    async def _titles(viewer: MessageViewer | None) -> list[str]:
        """
        按指定读取主体查询消息历史标题。
        """
        async with AsyncSessionFactory() as db:
            items = await MessageQueryService(MessageOper(db)).list_history(
                page=1,
                count=10,
                viewer=viewer,
            )
        return [item["title"] for item in items]

    assert asyncio.run(_titles(None)) == ["他人提问", "本人提问", "站点告警", "入库广播"]
    assert asyncio.run(_titles(MessageViewer(super_user=True))) == [
        "他人提问",
        "本人提问",
        "站点告警",
        "入库广播",
    ]
    assert asyncio.run(_titles(MessageViewer(super_user=False, userid="7"))) == [
        "本人提问",
        "入库广播",
    ]
    assert asyncio.run(_titles(MessageViewer(super_user=False))) == ["入库广播"]


def test_message_endpoints_filter_admin_scope_for_normal_user(monkeypatch) -> None:
    """
    普通用户的消息历史与通知接口只返回广播通知与本人记录。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.adapters.web.security.access import verify_token
    from app.api.dependencies.agent import get_message_query_service

    _clear_messages()
    oper = MessageOper()
    oper.add(title="入库广播", text="已入库", action=1, mtype=MessageType.Organize)
    oper.add(title="站点告警", text="站点失效", action=1, mtype=MessageType.SiteMessage)
    oper.add(title="无类型通知", text="未分类", action=1)
    oper.add(title="本人提问", text="/ai 帮我搜索", action=0, userid="7")
    oper.add(title="他人提问", text="/ai 帮我搜索", action=0, userid="9")
    monkeypatch.setattr(
        "app.application.messaging.message.get_user_notification_types",
        lambda: (MessageType.Organize.value,),
    )

    class _SessionRepository:
        """
        在测试数据库会话中执行可见性查询的消息仓储。
        """

        async def async_list_by_page(self, **kwargs) -> list[MessageModel]:
            """
            按可见性参数查询全部方向的消息记录。
            """
            async with AsyncSessionFactory() as db:
                return await MessageModel.async_list_by_page(db, **kwargs)

        async def async_list_sent_by_page(self, **kwargs) -> list[MessageModel]:
            """
            按可见性参数查询发送方向的消息记录。
            """
            async with AsyncSessionFactory() as db:
                return await MessageModel.async_list_sent_by_page(db, **kwargs)

    def _override_service() -> MessageQueryService:
        """
        提供绑定测试数据库会话的消息历史查询服务。
        """
        return MessageQueryService(_SessionRepository())

    app = FastAPI()
    app.include_router(message_endpoint.router, prefix="/api/v1/message")
    app.dependency_overrides[verify_token] = lambda: TokenPayload(
        sub=7,
        username="user",
        super_user=False,
    )
    app.dependency_overrides[get_message_query_service] = _override_service
    with TestClient(app) as client:
        history = client.get("/api/v1/message/history?page=1&count=10").json()["data"]
        notifications = client.get("/api/v1/message/notification?page=1&count=10").json()["data"]

    assert [row["title"] for row in history] == ["本人提问", "入库广播"]
    assert [row["title"] for row in notifications] == ["入库广播"]
