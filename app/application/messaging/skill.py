import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from typing import Dict, List, Optional, Tuple, Union

from app.agent.skills.registry import SkillHelper, SkillInfo
from app.application.messaging.interaction import (
    MessageGateway,
    build_navigation_buttons,
    page_items,
    supports_interaction_buttons,
    update_or_post_message,
)
from app.schemas.message import Message
from app.schemas.types import NotificationChannel


@dataclass
class PendingSkillInteraction:
    """
    记录一次 /skills 会话的上下文，便于按钮和文本回复共用同一状态。
    """

    request_id: str
    user_id: str
    channel: Optional[NotificationChannel]
    source: Optional[str]
    username: Optional[str]
    view: str = "root"
    local_page: int = 0
    market_page: int = 0
    market_query: str = ""
    awaiting_input: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)


class SkillInteractionManager:
    """
    管理用户当前的技能交互状态。

    每个用户同一时间只保留一个有效会话，避免旧按钮继续生效。
    """

    _ttl = timedelta(hours=24)

    def __init__(self):
        """初始化按请求和用户索引的技能交互会话表。"""
        self._by_id: Dict[str, PendingSkillInteraction] = {}
        self._by_user: Dict[str, str] = {}
        self._lock = Lock()

    def _cleanup_locked(self):
        """
        清理超时会话，避免按钮回调无限积累。
        """
        expire_before = datetime.now() - self._ttl
        expired = [
            request_id
            for request_id, request in self._by_id.items()
            if request.created_at < expire_before
        ]
        for request_id in expired:
            request = self._by_id.pop(request_id, None)
            if request:
                self._by_user.pop(str(request.user_id), None)

    def create_or_replace(
            self,
            user_id: Union[str, int],
            channel: Optional[NotificationChannel],
            source: Optional[str],
            username: Optional[str],
    ) -> PendingSkillInteraction:
        """
        为用户创建新会话，并替换掉旧的技能交互状态。
        """
        with self._lock:
            self._cleanup_locked()
            user_key = str(user_id)
            old_request_id = self._by_user.get(user_key)
            if old_request_id:
                self._by_id.pop(old_request_id, None)
            request_id = uuid.uuid4().hex[:12]
            request = PendingSkillInteraction(
                request_id=request_id,
                user_id=user_key,
                channel=channel,
                source=source,
                username=username,
            )
            self._by_id[request_id] = request
            self._by_user[user_key] = request_id
            return request

    def get_by_user(
            self, user_id: Union[str, int]
    ) -> Optional[PendingSkillInteraction]:
        """
        按用户获取当前有效会话，供纯文本回复路由使用。
        """
        with self._lock:
            self._cleanup_locked()
            request_id = self._by_user.get(str(user_id))
            if not request_id:
                return None
            return self._by_id.get(request_id)

    def get_by_id(
            self, request_id: str, user_id: Union[str, int]
    ) -> Optional[PendingSkillInteraction]:
        """
        按请求 ID 获取会话，并校验会话归属用户。
        """
        with self._lock:
            self._cleanup_locked()
            request = self._by_id.get(request_id)
            if not request or str(request.user_id) != str(user_id):
                return None
            return request

    def remove(self, request_id: str) -> None:
        """
        主动结束会话，释放用户和请求 ID 的双向索引。
        """
        with self._lock:
            request = self._by_id.pop(request_id, None)
            if request:
                self._by_user.pop(str(request.user_id), None)

    def clear(self):
        """
        清空所有会话，主要用于测试场景。
        """
        with self._lock:
            self._by_id.clear()
            self._by_user.clear()


skill_interaction_manager = SkillInteractionManager()


class SkillInteractionHandler:
    """
    处理 /skills 指令、按钮回调和文本式技能管理交互。
    """

    _button_page_size = 6
    _text_page_size = 8

    def __init__(
            self,
            messenger: MessageGateway,
            skill_helper: Optional[SkillHelper] = None,
    ):
        """注入消息接口和技能管理能力。"""
        self._messenger = messenger
        self.skillhelper = skill_helper or SkillHelper()

    def remote_manage(
            self,
            arg_str: str,
            channel: NotificationChannel,
            userid: Union[str, int],
            source: Optional[str] = None,
    ):
        """
        /skills 入口。创建新会话并渲染首屏菜单。
        """
        request = skill_interaction_manager.create_or_replace(
            user_id=userid,
            channel=channel,
            source=source,
            username=None,
        )
        normalized_arg = (arg_str or "").strip()
        force = normalized_arg.lower() in {"refresh", "刷新"}
        search_query = self._extract_market_search_query(normalized_arg) or (
            "" if force else normalized_arg
        )
        if search_query:
            request.view = "market"
            request.market_query = search_query
        self._render_interaction(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
            username=None,
            force_market_refresh=force,
        )

    @staticmethod
    def parse_callback(callback_data: str) -> Optional[Tuple[str, str, Optional[int]]]:
        """
        解析 /skills 按钮回调。

        回调格式：skills:{request_id}:{action}[:index]
        """
        if not callback_data.startswith("skills:"):
            return None
        parts = callback_data.split(":")
        if len(parts) < 3:
            return None
        request_id = parts[1]
        action = parts[2]
        index = None
        if len(parts) >= 4 and parts[3].isdigit():
            index = int(parts[3])
        return request_id, action, index

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
        处理按钮交互，并在同一条消息上刷新当前视图。
        """
        parsed = self.parse_callback(callback_data)
        if not parsed:
            return False

        request_id, action, index = parsed
        request = skill_interaction_manager.get_by_id(request_id, userid)
        if not request:
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="技能交互已失效，请重新发送 /skills",
                )
            )
            return True

        request.channel = channel
        request.source = source
        request.username = username

        if action == "close":
            skill_interaction_manager.remove(request.request_id)
            self._update_or_post_message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="技能管理",
                text="技能交互已结束",
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return True

        if action == "root":
            request.view = "root"
            request.awaiting_input = None
        elif action == "installed":
            request.view = "installed"
            request.local_page = 0
            request.awaiting_input = None
        elif action == "market":
            request.view = "market"
            request.market_page = 0
            request.awaiting_input = None
        elif action == "sources":
            request.view = "sources"
            request.awaiting_input = None
        elif action == "search":
            request.view = "market"
            request.awaiting_input = "market-search"
        elif action == "source-add":
            request.view = "sources"
            request.awaiting_input = "source-add"
        elif action == "clear-search":
            self._clear_market_search(request)
        elif action == "refresh":
            request.awaiting_input = None
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                force_market_refresh=True,
            )
            return True
        elif action == "page-next":
            request.awaiting_input = None
            if request.view == "installed":
                request.local_page += 1
            elif request.view == "market":
                request.market_page += 1
        elif action == "page-prev":
            request.awaiting_input = None
            if request.view == "installed":
                request.local_page = max(0, request.local_page - 1)
            elif request.view == "market":
                request.market_page = max(0, request.market_page - 1)
        elif action == "install" and index:
            request.awaiting_input = None
            success, message = self._install_market_skill(request, index)
            if success:
                self._messenger.post_message(
                    Message(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title=message,
                    )
                )
            else:
                self._messenger.post_message(
                    Message(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title=message,
                    )
                )
        elif action == "remove" and index:
            request.awaiting_input = None
            success, message = self._remove_local_skill(request, index)
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=message,
                )
            )
            if not success:
                # 保持当前页
                pass
        elif action == "source-remove" and index:
            request.view = "sources"
            request.awaiting_input = None
            success, message = self._remove_market_source(index)
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=message,
                )
            )

        self._render_interaction(
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
        处理不支持按钮渠道上的文本指令，也兼容用户直接回复文字操作。
        """
        request = skill_interaction_manager.get_by_user(userid)
        if not request:
            return False

        request.channel = channel
        request.source = source
        request.username = username

        normalized = (text or "").strip()
        lowered = normalized.lower()
        if lowered in {"退出", "关闭", "q", "quit", "exit"}:
            skill_interaction_manager.remove(request.request_id)
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="技能交互已结束",
                    save_history=False,
                )
            )
            return True

        if lowered in {"返回", "back"}:
            request.view = "root"
            request.awaiting_input = None
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        add_source = self._extract_market_source_input(normalized)
        remove_source_match = re.match(
            r"^(?:删除源|移除源|删除仓库|移除仓库|remove source)\s*(\d+)$",
            normalized,
            re.IGNORECASE,
        )

        if add_source:
            request.view = "sources"
            request.awaiting_input = None
            _, message = self.skillhelper.add_custom_market_source(add_source)
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=message,
                )
            )
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if remove_source_match:
            request.view = "sources"
            request.awaiting_input = None
            _, message = self._remove_market_source(
                page_index=int(remove_source_match.group(1))
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
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if lowered in {"刷新", "refresh"}:
            request.awaiting_input = None
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                force_market_refresh=True,
            )
            return True

        if lowered in {"p", "prev", "上一页"}:
            request.awaiting_input = None
            if request.view == "installed":
                request.local_page = max(0, request.local_page - 1)
            elif request.view == "market":
                request.market_page = max(0, request.market_page - 1)
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if lowered in {"n", "next", "下一页"}:
            request.awaiting_input = None
            if request.view == "installed":
                request.local_page += 1
            elif request.view == "market":
                request.market_page += 1
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if request.view == "root":
            if lowered in {"1", "已安装", "本地", "local"}:
                request.view = "installed"
            elif lowered in {"2", "市场", "market"}:
                request.view = "market"
            elif lowered in {"3", "技能源", "源", "sources", "source"}:
                request.view = "sources"
            elif self._extract_market_search_query(normalized):
                self._apply_market_search(
                    request,
                    self._extract_market_search_query(normalized),
                )
            else:
                self._messenger.post_message(
                    Message(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title="请输入 `1` 查看已安装技能，`2` 查看技能市场，3 管理技能源，或回复 `刷新/退出`",
                    )
                )
                return True
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if request.awaiting_input == "source-add":
            if lowered in {"取消", "cancel"}:
                request.awaiting_input = None
            else:
                _, message = self.skillhelper.add_custom_market_source(normalized)
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
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if lowered in {"清除搜索", "取消搜索", "clear", "clear search"}:
            if request.view == "market" or request.market_query:
                self._clear_market_search(request)
                self._render_interaction(
                    request=request,
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                )
                return True

        if request.awaiting_input == "market-search":
            if lowered in {"取消", "cancel"}:
                request.awaiting_input = None
            else:
                search_query = self._extract_market_search_query(normalized) or normalized
                self._apply_market_search(request, search_query)
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        search_query = self._extract_market_search_query(normalized)
        if search_query:
            self._apply_market_search(request, search_query)
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        install_match = re.match(r"^(?:安装|装)\s*(\d+)$", normalized)
        remove_match = re.match(r"^(?:删除|删)\s*(\d+)$", normalized)
        if request.view == "market" and install_match:
            request.awaiting_input = None
            success, message = self._install_market_skill(
                request=request,
                page_index=int(install_match.group(1)),
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
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True
        if request.view == "installed" and remove_match:
            request.awaiting_input = None
            _, message = self._remove_local_skill(
                request=request,
                page_index=int(remove_match.group(1)),
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
            self._render_interaction(
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
                title=self._build_usage_hint(request.view),
            )
        )
        return True

    def _install_market_skill(
            self,
            request: PendingSkillInteraction,
            page_index: int,
    ) -> Tuple[bool, str]:
        """
        按当前市场页的可见序号安装技能，避免跨页序号歧义。
        """
        market_skills = self._get_market_skills(request=request)
        items, page, _ = self._page_items(
            items=market_skills,
            page=request.market_page,
            page_size=self._page_size(request.channel),
        )
        request.market_page = page
        if page_index < 1 or page_index > len(items):
            return False, "安装序号无效"
        return self.skillhelper.install_market_skill(items[page_index - 1])

    def _remove_local_skill(
            self,
            request: PendingSkillInteraction,
            page_index: int,
    ) -> Tuple[bool, str]:
        """
        按当前已安装页的可见序号删除技能，并拦截内置技能。
        """
        local_skills = self.skillhelper.list_local_skills()
        items, page, _ = self._page_items(
            items=local_skills,
            page=request.local_page,
            page_size=self._page_size(request.channel),
        )
        request.local_page = page
        if page_index < 1 or page_index > len(items):
            return False, "删除序号无效"
        target = items[page_index - 1]
        if not target.removable:
            return False, f"技能 {target.id} 是内置技能，不能删除"
        return self.skillhelper.remove_local_skill(target.id)

    def _remove_market_source(self, page_index: int) -> Tuple[bool, str]:
        """
        按当前源列表序号删除自定义技能源，避免误删内置默认源。
        """
        sources = self.skillhelper.list_market_source_entries()
        if page_index < 1 or page_index > len(sources):
            return False, "删除源序号无效"
        target = sources[page_index - 1]
        if not target.removable:
            return False, f"技能源 {target.label} 是内置默认源，不能删除"
        return self.skillhelper.remove_custom_market_source(target.source)

    def _render_interaction(
            self,
            request: PendingSkillInteraction,
            channel: NotificationChannel,
            source: Optional[str],
            userid: Union[str, int],
            username: Optional[str],
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
            force_market_refresh: bool = False,
    ) -> None:
        """
        根据当前视图生成内容，并选择编辑原消息或发送新消息。
        """
        if request.view == "installed":
            title, text, buttons = self._build_installed_view(
                request=request
            )
        elif request.view == "market":
            title, text, buttons = self._build_market_view(
                request=request,
                force_market_refresh=force_market_refresh,
            )
        elif request.view == "sources":
            title, text, buttons = self._build_sources_view(
                request=request,
            )
        else:
            title, text, buttons = self._build_root_view(
                request=request,
                force_market_refresh=force_market_refresh,
            )

        self._update_or_post_message(
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            title=title,
            text=text,
            buttons=buttons,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

    def _build_root_view(
            self,
            request: PendingSkillInteraction,
            force_market_refresh: bool = False,
    ) -> Tuple[str, str, Optional[List[List[dict]]]]:
        """
        构建根菜单视图，汇总本地技能和市场概览。
        """
        local_skills = self.skillhelper.list_local_skills()
        market_skills = [
            skill
            for skill in self.skillhelper.list_market_skills(force=force_market_refresh)
            if not skill.installed
        ]
        source_entries = self.skillhelper.list_market_source_entries()
        source_lines = []
        for index, source_entry in enumerate(source_entries, start=1):
            state = "内置" if source_entry.builtin else "自定义"
            source_lines.append(
                f"{index}. {source_entry.label}（{state}）"
            )

        text_lines = [
            f"已安装技能：{len(local_skills)}",
            f"市场可安装技能：{len(market_skills)}",
        ]
        if source_lines:
            text_lines.extend(["", "当前技能源：", *source_lines])
        text_lines.extend(
            [
                "",
                "1. 查看已安装技能",
                "2. 浏览技能市场",
                "3. 管理技能源",
                "回复 `刷新` 重新获取市场数据，回复 `退出` 结束交互",
            ]
        )

        buttons = None
        if self._supports_interactive_buttons(request.channel):
            buttons = [
                [{"text": "已安装技能", "callback_data": f"skills:{request.request_id}:installed"}],
                [{"text": "技能市场", "callback_data": f"skills:{request.request_id}:market"}],
                [{"text": "技能源管理", "callback_data": f"skills:{request.request_id}:sources"}],
                [
                    {"text": "刷新市场", "callback_data": f"skills:{request.request_id}:refresh"},
                    {"text": "关闭", "callback_data": f"skills:{request.request_id}:close"},
                ],
            ]
        return "技能管理", "\n".join(text_lines), buttons

    def _build_installed_view(
            self,
            request: PendingSkillInteraction
    ) -> Tuple[str, str, Optional[List[List[dict]]]]:
        """
        构建已安装技能视图，列出来源和可删除状态。
        """
        local_skills = self.skillhelper.list_local_skills()
        items, page, total_pages = self._page_items(
            items=local_skills,
            page=request.local_page,
            page_size=self._page_size(request.channel),
        )
        request.local_page = page

        text_lines = [f"第 {page + 1}/{total_pages} 页，共 {len(local_skills)} 个技能"]
        if not items:
            text_lines.append("")
            text_lines.append("当前没有已安装技能")
        else:
            for index, skill in enumerate(items, start=1):
                action = "可删除" if skill.removable else "内置不可删"
                text_lines.extend(
                    [
                        "",
                        f"{index}. {skill.id} ({skill.source_label}，{action})",
                        self._truncate(skill.description),
                    ]
                )

        text_lines.extend(
            [
                "",
                "回复 `删除 <序号>` 删除技能，回复 `n/p` 翻页，回复 `返回` 回到菜单，回复 `退出` 结束交互",
            ]
        )

        buttons = None
        if self._supports_interactive_buttons(request.channel):
            buttons = []
            for index, skill in enumerate(items, start=1):
                if not skill.removable:
                    continue
                buttons.append(
                    [
                        {
                            "text": f"删除 {index}",
                            "callback_data": f"skills:{request.request_id}:remove:{index}",
                        }
                    ]
                )
            buttons.extend(self._navigation_buttons(request, page, total_pages))
            buttons.append(
                [
                    {"text": "返回", "callback_data": f"skills:{request.request_id}:root"},
                    {"text": "关闭", "callback_data": f"skills:{request.request_id}:close"},
                ]
            )
        return "已安装技能", "\n".join(text_lines), buttons

    def _build_market_view(
            self,
            request: PendingSkillInteraction,
            force_market_refresh: bool = False,
    ) -> Tuple[str, str, Optional[List[List[dict]]]]:
        """
        构建技能市场视图，仅展示尚未安装的技能。
        """
        market_skills = self._get_market_skills(
            request=request,
            force_market_refresh=force_market_refresh,
        )
        items, page, total_pages = self._page_items(
            items=market_skills,
            page=request.market_page,
            page_size=self._page_size(request.channel),
        )
        request.market_page = page

        text_lines = [f"第 {page + 1}/{total_pages} 页，共 {len(market_skills)} 个可安装技能"]
        if request.market_query:
            text_lines.append(f"当前搜索：{request.market_query}")
        if request.awaiting_input == "market-search":
            text_lines.extend(
                [
                    "",
                    "搜索输入中：直接回复关键词即可筛选市场技能，回复 `取消` 结束输入。",
                ]
            )
        if not items:
            text_lines.append("")
            if request.market_query:
                text_lines.append("当前搜索没有匹配的市场技能")
            else:
                text_lines.append("当前没有可安装的市场技能")
        else:
            for index, skill in enumerate(items, start=1):
                text_lines.extend(
                    [
                        "",
                        f"{index}. {skill.id} ({skill.source_label})",
                        self._truncate(skill.description),
                    ]
                )
                if skill.source_type == "registry":
                    text_lines.append("社区源，安装前请自行甄别安全性")

        if any(skill.source_type == "registry" for skill in items):
            text_lines.extend(
                [
                    "",
                    "提示：ClawHub 属于社区注册表，技能质量与安全性需要自行甄别。",
                ]
            )

        text_lines.extend(
            [
                "",
                "回复 `搜索 <关键词>` 筛选技能，回复 `清除搜索` 恢复全量列表，回复 `安装 <序号>` 安装技能，回复 `刷新` 重新拉取市场，回复 `n/p` 翻页，回复 `返回` 回到菜单，回复 `退出` 结束交互",
            ]
        )

        buttons = None
        if self._supports_interactive_buttons(request.channel):
            buttons = []
            search_row = []
            if request.market_query:
                search_row.append(
                    {
                        "text": "清除搜索",
                        "callback_data": f"skills:{request.request_id}:clear-search",
                    }
                )
            else:
                search_row.append(
                    {
                        "text": "搜索",
                        "callback_data": f"skills:{request.request_id}:search",
                    }
                )
            if search_row:
                buttons.append(search_row)
            for index, _skill in enumerate(items, start=1):
                buttons.append(
                    [
                        {
                            "text": f"安装 {index}",
                            "callback_data": f"skills:{request.request_id}:install:{index}",
                        }
                    ]
                )
            buttons.extend(self._navigation_buttons(request, page, total_pages))
            buttons.append(
                [
                    {"text": "刷新", "callback_data": f"skills:{request.request_id}:refresh"},
                    {"text": "返回", "callback_data": f"skills:{request.request_id}:root"},
                    {"text": "关闭", "callback_data": f"skills:{request.request_id}:close"},
                ]
            )
        return "技能市场", "\n".join(text_lines), buttons

    def _build_sources_view(
            self,
            request: PendingSkillInteraction,
    ) -> Tuple[str, str, Optional[List[List[dict]]]]:
        """
        构建技能源管理视图，提供自定义 GitHub 源的增删入口。
        """
        sources = self.skillhelper.list_market_source_entries()
        custom_count = len([source for source in sources if not source.builtin])
        text_lines = [
            f"当前技能源：{len(sources)}",
            f"自定义技能源：{custom_count}",
        ]
        if request.awaiting_input == "source-add":
            text_lines.extend(
                [
                    "",
                    "添加输入中：直接回复 GitHub 仓库地址即可。",
                    "支持 owner/repo、https://github.com/owner/repo，或 /tree/<branch>/<skills_path> 形式。",
                    "回复 `取消` 结束输入。",
                ]
            )

        if not sources:
            text_lines.extend(["", "当前没有可用技能源"])
        else:
            for index, market_source in enumerate(sources, start=1):
                state = "自定义可删" if market_source.removable else "内置默认"
                text_lines.extend(
                    [
                        "",
                        f"{index}. {market_source.label}（{state}）",
                        self._truncate(market_source.source, limit=200),
                    ]
                )

        text_lines.extend(
            [
                "",
                "回复 `添加源 <GitHub仓库地址>` 添加自定义源，回复 `删除源 <序号>` 删除自定义源，回复 `返回` 回到菜单，回复 `退出` 结束交互",
            ]
        )

        buttons = None
        if self._supports_interactive_buttons(request.channel):
            buttons = [
                [
                    {
                        "text": "添加自定义源",
                        "callback_data": f"skills:{request.request_id}:source-add",
                    }
                ]
            ]
            for index, market_source in enumerate(sources, start=1):
                if not market_source.removable:
                    continue
                buttons.append(
                    [
                        {
                            "text": f"删除 {index}",
                            "callback_data": f"skills:{request.request_id}:source-remove:{index}",
                        }
                    ]
                )
            buttons.append(
                [
                    {"text": "返回", "callback_data": f"skills:{request.request_id}:root"},
                    {"text": "关闭", "callback_data": f"skills:{request.request_id}:close"},
                ]
            )
        return "技能源管理", "\n".join(text_lines), buttons

    @staticmethod
    def _truncate(text: str, limit: int = 140) -> str:
        """
        对技能描述做轻量截断，避免消息过长。
        """
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    @staticmethod
    def _page_items(
            items: List[SkillInfo],
            page: int,
            page_size: int,
    ) -> Tuple[List[SkillInfo], int, int]:
        """
        返回当前页的数据，并把页码钳制到有效范围内。
        """
        return page_items(items=items, page=page, page_size=page_size)

    def _page_size(self, channel: Optional[NotificationChannel]) -> int:
        """
        按渠道能力选择分页大小，按钮渠道单页更短，便于直接操作。
        """
        return (
            self._button_page_size
            if self._supports_interactive_buttons(channel)
            else self._text_page_size
        )

    @staticmethod
    def _supports_interactive_buttons(channel: Optional[NotificationChannel]) -> bool:
        """
        判断当前渠道是否同时支持按钮展示和回调。
        """
        return supports_interaction_buttons(channel)

    @staticmethod
    def _navigation_buttons(
            request: PendingSkillInteraction,
            page: int,
            total_pages: int,
    ) -> List[List[dict]]:
        """
        为分页视图生成上一页和下一页按钮。
        """
        return build_navigation_buttons(
            prefix="skills",
            request=request,
            page=page,
            total_pages=total_pages,
        )

    def _update_or_post_message(
            self,
            channel: NotificationChannel,
            source: Optional[str],
            userid: Union[str, int],
            username: Optional[str],
            title: str,
            text: str,
            buttons: Optional[List[List[dict]]] = None,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        优先编辑原消息，编辑失败时再回退为发送新消息。
        """
        update_or_post_message(
            chain=self._messenger,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            title=title,
            text=text,
            buttons=buttons,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

    @staticmethod
    def _build_usage_hint(view: str) -> str:
        """
        根据当前视图返回可执行的文本命令提示。
        """
        if view == "market":
            return "请输入 搜索 <关键词>、清除搜索、安装 <序号>、刷新、n、p、返回 或 退出"
        if view == "installed":
            return "请输入 删除 <序号>、n、p、返回 或 退出"
        if view == "sources":
            return "请输入 添加源 <GitHub仓库地址>、删除源 <序号>、返回 或 退出"
        return "请输入 1、2、3、搜索 <关键词>、刷新 或 退出"

    def _get_market_skills(
            self,
            request: PendingSkillInteraction,
            force_market_refresh: bool = False,
    ) -> List[SkillInfo]:
        """
        获取当前 /skills 会话可见的市场技能，并应用搜索词过滤。
        """
        skills = [
            skill
            for skill in self.skillhelper.list_market_skills(force=force_market_refresh)
            if not skill.installed
        ]
        if not request.market_query:
            return skills
        return self.skillhelper.filter_market_skills(
            skills=skills,
            query=request.market_query,
        )

    @staticmethod
    def _extract_market_search_query(text: str) -> str:
        """
        从文本命令中提取市场搜索词，兼容“搜索/查找/查”前缀。
        """
        normalized = (text or "").strip()
        if not normalized:
            return ""
        match = re.match(r"^(?:搜索|查找|查)\s+(.+)$", normalized)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _extract_market_source_input(text: str) -> str:
        """
        从文本命令中提取自定义技能源地址。
        """
        normalized = (text or "").strip()
        if not normalized:
            return ""
        match = re.match(
            r"^(?:添加源|新增源|添加仓库|新增仓库|add source)\s+(.+)$",
            normalized,
            re.IGNORECASE,
        )
        return match.group(1).strip() if match else ""

    @staticmethod
    def _apply_market_search(
            request: PendingSkillInteraction,
            query: str,
    ) -> None:
        """
        将会话切到市场搜索结果视图，并重置分页状态。
        """
        request.view = "market"
        request.market_query = (query or "").strip()
        request.market_page = 0
        request.awaiting_input = None

    @staticmethod
    def _clear_market_search(request: PendingSkillInteraction) -> None:
        """
        清除当前市场搜索状态，恢复全量市场列表。
        """
        request.market_query = ""
        request.market_page = 0
        request.awaiting_input = None
