import asyncio
import json
from unittest.mock import Mock

from app.api.endpoints.message import clear_notification_message, get_notification_message
from app.application.orchestration import ChainBase
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.meta.metabase import MetaBase
from app.db import AsyncSessionFactory, SessionFactory
from app.db.oper.message import MessageOper
from app.db.models.message import Message as MessageModel
from app.db.oper.systemconfig import SystemConfigOper
from app.application.messaging.message import MessageHelper, MessageQueryService
from app.schemas import Message, MessageClearScope
from app.schemas.types import MediaType, MessageType, SystemConfigKey


def _clear_messages() -> None:
    """
    清空消息表，隔离通知测试数据。
    """
    with SessionFactory() as db:
        db.query(MessageModel).delete()
        db.commit()
    SystemConfigOper().delete(SystemConfigKey.NotificationClearBefore)


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
    通知历史应返回已发送消息，包含通过消息链登记的智能体消息。
    """
    _clear_messages()
    oper = MessageOper()
    oper.add(title="系统通知", text="下载完成", action=1, mtype=MessageType.Download)
    oper.add(title="用户消息", text="帮我搜索", action=0)
    oper.add(title="智能体回复", text="已处理", action=1, mtype=MessageType.Agent)

    messages = MessageOper().list_by_page(page=1, count=10)
    assert [message.title for message in messages if message.action == 1] == ["智能体回复", "系统通知"]


def test_web_message_history_returns_all_messages() -> None:
    """
    Web 消息历史返回消息表中的全部记录。
    """
    _clear_messages()
    oper = MessageOper()
    oper.add(title="智能体回复", text="已处理", action=1, mtype=MessageType.Agent)
    oper.add(title="用户消息", text="/ai 帮我处理", action=0)
    oper.add(title="普通通知", text="下载完成", action=1, mtype=MessageType.Download)

    messages = MessageOper().list_by_page(page=1, count=10)
    assert [message.title for message in messages] == ["普通通知", "用户消息", "智能体回复"]


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
                service=MessageQueryService(MessageOper(db))
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
