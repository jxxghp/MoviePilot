import re
from typing import List, Optional, Protocol, Tuple, Union

from app.adapters.external.server import MoviePilotServerHelper
from app.application.messaging.interaction import (
    MessageGateway,
    SlashInteractionManager,
    build_navigation_buttons,
    format_markdown_table,
    page_items,
    supports_interaction_buttons,
    supports_markdown,
    update_or_post_message,
)
from app.db.models.subscribe import Subscribe
from app.db.oper.subscribe import SubscribeOper
from app.schemas.message import Message
from app.schemas.types import NotificationChannel, MediaType


subscribe_interaction_manager = SlashInteractionManager()


class SubscribeInteractionActions(Protocol):
    """
    声明订阅交互需要调用的业务动作。
    """

    def refresh(self):
        """执行订阅刷新。"""
        ...

    def check(self):
        """执行订阅元数据检查。"""
        ...

    def search(self, **kwargs):
        """执行订阅搜索。"""
        ...


class SubscribeInteractionHandler:
    """
    管理 /subscribes 交互会话、按钮、文本输入和列表视图。
    """

    _button_page_size = 6
    _text_page_size = 10

    def __init__(
            self,
            messenger: MessageGateway,
            actions: SubscribeInteractionActions,
    ):
        """
        注入消息投递接口和订阅业务动作。
        """
        self._messenger = messenger
        self._actions = actions

    def remote_list(
            self,
            arg_str: str = "",
            channel: NotificationChannel = None,
            userid: Union[str, int] = None,
            source: Optional[str] = None,
    ):
        """
        /subscribes 统一入口。
        """
        request = subscribe_interaction_manager.create_or_replace(
            user_id=userid,
            command="/subscribes",
            channel=channel,
            source=source,
            username=None,
        )
        normalized_arg = (arg_str or "").strip()
        if normalized_arg and self.handle_text_interaction(
                channel=channel,
                source=source,
                userid=userid,
                username="",
                text=normalized_arg,
        ):
            return
        self._render_subscribe_interaction(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
            username="",
        )

    @staticmethod
    def parse_callback(callback_data: str) -> Optional[Tuple[str, str]]:
        """
        解析 /subscribes 按钮回调。
        """
        if not callback_data.startswith("subscribes:"):
            return None
        parts = callback_data.split(":")
        if len(parts) < 3:
            return None
        return parts[1], parts[2]

    def handle_callback_interaction(
            self,
            callback_data: str,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> bool:
        """
        处理 /subscribes 按钮交互。
        """
        parsed = self.parse_callback(callback_data)
        if not parsed:
            return False

        request_id, action = parsed
        request = subscribe_interaction_manager.get_by_id(request_id, userid)
        if not request:
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="订阅交互已失效，请重新发送 /subscribes",
                )
            )
            return True

        request.channel = channel
        request.source = source
        request.username = username

        if action == "close":
            subscribe_interaction_manager.remove(request.request_id)
            update_or_post_message(
                chain=self._messenger,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="订阅管理",
                text="订阅交互已结束",
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return True

        if action == "page-prev":
            request.page = max(0, request.page - 1)
            request.awaiting_input = None
        elif action == "page-next":
            request.page += 1
            request.awaiting_input = None
        elif action in {"search", "delete"}:
            request.awaiting_input = action
        elif action == "refresh":
            request.awaiting_input = None
            self._run_refresh_action(channel, source, userid, username)
        elif action == "refresh-list":
            request.awaiting_input = None
        elif action == "metadata":
            request.awaiting_input = None
            self._run_metadata_refresh_action(channel, source, userid, username)

        self._render_subscribe_interaction(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )
        return True

    def handle_text_interaction(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            text: str,
    ) -> bool:
        """
        处理 /subscribes 文本补充输入。
        """
        request = subscribe_interaction_manager.get_by_user(userid)
        if not request:
            return False

        request.channel = channel
        request.source = source
        request.username = username

        normalized = (text or "").strip()
        lowered = normalized.lower()

        if lowered in {"退出", "关闭", "q", "quit", "exit"}:
            subscribe_interaction_manager.remove(request.request_id)
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="订阅交互已结束",
                    save_history=False,
                )
            )
            return True

        if lowered in {"取消", "cancel", "返回", "back"}:
            request.awaiting_input = None
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if lowered in {"刷新列表", "列表", "list"}:
            request.awaiting_input = None
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if lowered in {"刷新", "refresh"}:
            request.awaiting_input = None
            self._run_refresh_action(channel, source, userid, username)
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if lowered in {"元数据", "刷新元数据", "metadata"}:
            request.awaiting_input = None
            self._run_metadata_refresh_action(channel, source, userid, username)
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if lowered in {"p", "prev", "上一页"}:
            request.awaiting_input = None
            request.page = max(0, request.page - 1)
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if lowered in {"n", "next", "下一页"}:
            request.awaiting_input = None
            request.page += 1
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        search_match = re.match(r"^(?:搜索|search)\s+(.+)$", normalized, re.IGNORECASE)
        delete_match = re.match(r"^(?:删除|delete)\s+(.+)$", normalized, re.IGNORECASE)

        if request.awaiting_input == "search":
            success, message = self._run_search_action(
                normalized, channel, source, userid, username
            )
            request.awaiting_input = None
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=message,
                )
            )
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if request.awaiting_input == "delete":
            success, message = self._delete_subscribes(normalized)
            request.awaiting_input = None
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=message,
                )
            )
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if search_match:
            success, message = self._run_search_action(
                search_match.group(1), channel, source, userid, username
            )
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=message,
                )
            )
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if delete_match:
            success, message = self._delete_subscribes(delete_match.group(1))
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=message,
                )
            )
            self._render_subscribe_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        self._messenger.post_message(
            Message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title=self._subscribe_usage_hint(request.awaiting_input),
            )
        )
        return True

    def _render_subscribe_interaction(
            self,
            request,
            channel: NotificationChannel,
            source: Optional[str],
            userid: Union[str, int],
            username: Optional[str],
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        渲染 /subscribes 当前页面。
        """
        subscribes = SubscribeOper().list()
        page_size = (
            self._button_page_size
            if supports_interaction_buttons(channel)
            else self._text_page_size
        )
        page_subscribes, page, total_pages = page_items(
            subscribes, request.page, page_size
        )
        request.page = page

        if subscribes:
            body = self._format_subscribe_list(page_subscribes, channel=channel)
            footer = [
                f"第 {page + 1}/{total_pages} 页，共 {len(subscribes)} 个订阅",
                self._subscribe_prompt(request.awaiting_input),
                self._subscribe_usage_hint(request.awaiting_input),
            ]
            text = "\n\n".join([body, *[line for line in footer if line]])
        else:
            text = "当前没有任何订阅。\n\n输入 `退出` 结束交互。"

        buttons = None
        if supports_interaction_buttons(channel):
            buttons = build_navigation_buttons(
                "subscribes", request, page, total_pages
            )
            buttons.extend(
                [
                    [
                        {
                            "text": "搜索订阅",
                            "callback_data": f"subscribes:{request.request_id}:search",
                        },
                        {
                            "text": "删除订阅",
                            "callback_data": f"subscribes:{request.request_id}:delete",
                        },
                        {
                            "text": "刷新订阅",
                            "callback_data": f"subscribes:{request.request_id}:refresh",
                        },
                    ],
                    [
                        {
                            "text": "刷新元数据",
                            "callback_data": f"subscribes:{request.request_id}:metadata",
                        },
                        {
                            "text": "刷新列表",
                            "callback_data": f"subscribes:{request.request_id}:refresh-list",
                        },
                        {
                            "text": "关闭",
                            "callback_data": f"subscribes:{request.request_id}:close",
                        },
                    ],
                ]
            )

        update_or_post_message(
            chain=self._messenger,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            title="订阅管理",
            text=text,
            buttons=buttons,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

    def _format_subscribe_list(
            self, subscribes: List[Subscribe], channel: Optional[NotificationChannel]
    ) -> str:
        """
        根据渠道能力格式化订阅列表。
        """
        if supports_markdown(channel):
            rows = [
                [
                    subscribe.id,
                    subscribe.name,
                    subscribe.type,
                    subscribe.year or "-",
                    self._format_subscribe_progress(subscribe),
                    self._format_subscribe_state(subscribe.state),
                ]
                for subscribe in subscribes
            ]
            return format_markdown_table(
                headers=["ID", "名称", "类型", "年份", "季/进度", "状态"],
                rows=rows,
            )

        lines = []
        for subscribe in subscribes:
            lines.append(
                f"{subscribe.id}. {subscribe.name}（{subscribe.year or '-'}）"
                f" | {subscribe.type}"
                f" | {self._format_subscribe_progress(subscribe)}"
                f" | 状态：{self._format_subscribe_state(subscribe.state)}"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_subscribe_state(state: Optional[str]) -> str:
        """
        订阅状态显示文本。
        """
        mapping = {
            "N": "新建",
            "R": "订阅中",
            "P": "待定",
            "S": "暂停",
        }
        return mapping.get(state or "", state or "-")

    @staticmethod
    def _format_subscribe_progress(subscribe: Subscribe) -> str:
        """
        构造订阅的季和进度说明。
        """
        if subscribe.type == MediaType.MOVIE.value:
            return "电影"
        season = subscribe.season if subscribe.season is not None else 1
        if subscribe.total_episode:
            lack_episode = (
                subscribe.lack_episode
                if subscribe.lack_episode is not None
                else subscribe.total_episode
            )
            downloaded = max(subscribe.total_episode - lack_episode, 0)
            return f"第{season}季 [{downloaded}/{subscribe.total_episode}]"
        return f"第{season}季"

    @staticmethod
    def _subscribe_prompt(awaiting_input: Optional[str]) -> str:
        """
        返回当前输入模式提示。
        """
        if awaiting_input == "search":
            return "当前操作：搜索订阅，请输入订阅 ID，多个 ID 用空格分隔，或输入 all 搜索全部。"
        if awaiting_input == "delete":
            return "当前操作：删除订阅，请输入订阅 ID，多个 ID 用空格分隔。"
        return ""

    @staticmethod
    def _subscribe_usage_hint(awaiting_input: Optional[str]) -> str:
        """
        返回 /subscribes 的文本操作提示。
        """
        if awaiting_input == "search":
            return "输入订阅 ID 或 all；输入 `取消` 返回列表，输入 `退出` 结束交互。"
        if awaiting_input == "delete":
            return "输入一个或多个订阅 ID；输入 `取消` 返回列表，输入 `退出` 结束交互。"
        return (
            "可输入：`搜索 <id...|all>`、`删除 <id...>`、`刷新`、`刷新元数据`、`n`、`p`、`退出`。"
        )

    def _run_refresh_action(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
    ) -> None:
        """
        执行订阅刷新。
        """
        self._messenger.post_message(
            Message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="开始刷新订阅...",
            )
        )
        self._actions.refresh()
        self._messenger.post_message(
            Message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="订阅刷新执行完成",
            )
        )

    def _run_metadata_refresh_action(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
    ) -> None:
        """
        执行订阅元数据刷新。
        """
        self._messenger.post_message(
            Message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="开始刷新订阅元数据...",
            )
        )
        self._actions.check()
        self._messenger.post_message(
            Message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="订阅元数据刷新完成",
            )
        )

    @staticmethod
    def _parse_subscribe_ids(arg_str: str) -> List[int]:
        """
        从输入中提取订阅 ID。
        """
        return [int(item) for item in re.findall(r"\d+", arg_str or "")]

    def _run_search_action(
            self,
            arg_str: str,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
    ) -> Tuple[bool, str]:
        """
        手动执行订阅搜索。
        """
        normalized = (arg_str or "").strip()
        if not normalized or normalized.lower() in {"all", "全部", "所有"}:
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="开始搜索所有订阅...",
                )
            )
            self._actions.search(state="N,R,P", manual=True)
            return True, "所有订阅搜索完成"

        subscribe_ids = self._parse_subscribe_ids(normalized)
        if not subscribe_ids:
            return False, "请输入订阅 ID，多个 ID 用空格分隔，或输入 all"

        subscribeoper = SubscribeOper()
        missing = []
        searched = []
        for subscribe_id in subscribe_ids:
            subscribe = subscribeoper.get(subscribe_id)
            if not subscribe:
                missing.append(str(subscribe_id))
                continue
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=f"开始搜索订阅【{subscribe.name}】...",
                )
            )
            self._actions.search(sid=subscribe_id, manual=True)
            searched.append(subscribe.name)

        if not searched and missing:
            return False, f"未找到订阅：{', '.join(missing)}"

        message = f"已完成 {len(searched)} 个订阅搜索"
        if searched:
            message += f"：{', '.join(searched)}"
        if missing:
            message += f"；未找到：{', '.join(missing)}"
        return True, message

    def _delete_subscribes(self, arg_str: str) -> Tuple[bool, str]:
        """
        批量删除订阅。
        """
        subscribe_ids = self._parse_subscribe_ids(arg_str)
        if not subscribe_ids:
            return False, "请输入至少一个有效的订阅 ID"

        subscribeoper = SubscribeOper()
        deleted = []
        missing = []
        for subscribe_id in subscribe_ids:
            subscribe = subscribeoper.get(subscribe_id)
            if not subscribe:
                missing.append(str(subscribe_id))
                continue
            deleted.append(subscribe.name)
            subscribeoper.delete(subscribe_id)
            MoviePilotServerHelper.sub_done_async(
                {
                    "media_source": subscribe.media_source,
                    "media_id": subscribe.media_id,
                    "season": subscribe.season,
                }
            )

        if not deleted and missing:
            return False, f"未找到订阅：{', '.join(missing)}"

        message = f"已删除 {len(deleted)} 个订阅"
        if deleted:
            message += f"：{', '.join(deleted)}"
        if missing:
            message += f"；未找到：{', '.join(missing)}"
        return True, message
