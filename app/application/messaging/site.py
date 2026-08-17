import re
from typing import Callable, List, Optional, Tuple, Union

from app.db.models.site import Site
from app.db.oper.site import SiteOper
from app.domain import site as site_rules
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
from app.runtime.log import logger
from app.schemas.message import Message
from app.schemas.types import NotificationChannel


site_interaction_manager = SlashInteractionManager()


class SiteInteractionHandler:
    """
    管理 /sites 交互会话、输入解析和站点列表渲染。
    """

    _button_page_size = 6
    _text_page_size = 10

    def __init__(
            self,
            messenger: MessageGateway,
            cookie_updater: Callable[..., Tuple[bool, str]],
    ):
        """
        注入消息投递接口和站点 Cookie 更新动作。
        """
        self._messenger = messenger
        self._cookie_updater = cookie_updater

    def remote_list(
            self,
            arg_str: str = "",
            channel: NotificationChannel = None,
            userid: Union[str, int] = None,
            source: Optional[str] = None,
    ):
        """
        /sites 统一入口。
        """
        request = site_interaction_manager.create_or_replace(
            user_id=userid,
            command="/sites",
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
        self._render_site_interaction(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
            username="",
        )

    @staticmethod
    def parse_callback(callback_data: str) -> Optional[Tuple[str, str]]:
        """
        解析 /sites 按钮回调。
        """
        if not callback_data.startswith("sites:"):
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
        处理 /sites 按钮交互。
        """
        parsed = self.parse_callback(callback_data)
        if not parsed:
            return False

        request_id, action = parsed
        request = site_interaction_manager.get_by_id(request_id, userid)
        if not request:
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="站点交互已失效，请重新发送 /sites",
                )
            )
            return True

        request.channel = channel
        request.source = source
        request.username = username

        if action == "close":
            site_interaction_manager.remove(request.request_id)
            update_or_post_message(
                chain=self._messenger,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="站点管理",
                text="站点交互已结束",
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
        elif action in {"cookie", "enable", "disable"}:
            request.awaiting_input = action
        elif action == "refresh":
            request.awaiting_input = None

        self._render_site_interaction(
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
        处理 /sites 文本补充输入。
        """
        request = site_interaction_manager.get_by_user(userid)
        if not request:
            return False

        request.channel = channel
        request.source = source
        request.username = username

        normalized = (text or "").strip()
        lowered = normalized.lower()

        if lowered in {"退出", "关闭", "q", "quit", "exit"}:
            site_interaction_manager.remove(request.request_id)
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="站点交互已结束",
                    save_history=False,
                )
            )
            return True

        if lowered in {"取消", "cancel", "返回", "back"}:
            request.awaiting_input = None
            self._render_site_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if lowered in {"刷新", "refresh", "列表", "list"}:
            request.awaiting_input = None
            self._render_site_interaction(
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
            self._render_site_interaction(
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
            self._render_site_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        cookie_match = re.match(
            r"^(?:cookie|更新cookie|更新\s*cookie)\s+(.+)$",
            normalized,
            re.IGNORECASE,
        )
        enable_match = re.match(r"^(?:启用|enable)\s+(.+)$", normalized, re.IGNORECASE)
        disable_match = re.match(
            r"^(?:禁用|disable)\s+(.+)$", normalized, re.IGNORECASE
        )

        if request.awaiting_input == "cookie":
            success, message = self._update_site_cookie_from_input(normalized)
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
            self._render_site_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if request.awaiting_input == "enable":
            success, message = self._set_sites_enabled(normalized, enabled=True)
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
            self._render_site_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if request.awaiting_input == "disable":
            success, message = self._set_sites_enabled(normalized, enabled=False)
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
            self._render_site_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if cookie_match:
            success, message = self._update_site_cookie_from_input(cookie_match.group(1))
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=message,
                )
            )
            self._render_site_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if enable_match:
            success, message = self._set_sites_enabled(enable_match.group(1), enabled=True)
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=message,
                )
            )
            self._render_site_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if disable_match:
            success, message = self._set_sites_enabled(
                disable_match.group(1), enabled=False
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
            self._render_site_interaction(
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
                title=self._site_usage_hint(request.awaiting_input),
            )
        )
        return True

    def _render_site_interaction(
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
        渲染 /sites 当前页面。
        """
        site_list = SiteOper().list()
        page_size = self._button_page_size if supports_interaction_buttons(channel) else self._text_page_size
        page_sites, page, total_pages = page_items(site_list, request.page, page_size)
        request.page = page

        if site_list:
            body = self._format_site_list(page_sites, channel=channel)
            footer = [
                f"第 {page + 1}/{total_pages} 页，共 {len(site_list)} 个站点",
                self._site_prompt(request.awaiting_input),
                self._site_usage_hint(request.awaiting_input),
            ]
            text = "\n\n".join([body, *[line for line in footer if line]])
        else:
            text = "当前没有任何站点。\n\n输入 `退出` 结束交互。"

        buttons = None
        if supports_interaction_buttons(channel):
            buttons = build_navigation_buttons("sites", request, page, total_pages)
            buttons.extend(
                [
                    [
                        {
                            "text": "更新 Cookie",
                            "callback_data": f"sites:{request.request_id}:cookie",
                        },
                        {
                            "text": "禁用站点",
                            "callback_data": f"sites:{request.request_id}:disable",
                        },
                        {
                            "text": "启用站点",
                            "callback_data": f"sites:{request.request_id}:enable",
                        },
                    ],
                    [
                        {
                            "text": "刷新列表",
                            "callback_data": f"sites:{request.request_id}:refresh",
                        },
                        {
                            "text": "关闭",
                            "callback_data": f"sites:{request.request_id}:close",
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
            title="站点管理",
            text=text,
            buttons=buttons,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

    @staticmethod
    def _format_site_list(
            site_list: List[Site], channel: Optional[NotificationChannel]
    ) -> str:
        """
        根据渠道能力格式化站点列表。
        """
        if supports_markdown(channel):
            rows = [
                [
                    site.id,
                    site.name,
                    "启用" if site.is_active else "禁用",
                    "已配置" if site.cookie else "未配置",
                    "是" if site.render else "否",
                    site.domain or site_rules.extract_domain(site.url or ""),
                ]
                for site in site_list
            ]
            return format_markdown_table(
                headers=["ID", "站点", "状态", "Cookie", "渲染", "域名"],
                rows=rows,
            )

        lines = []
        for site in site_list:
            lines.append(
                f"{site.id}. {site.name} | 状态：{'启用' if site.is_active else '禁用'}"
                f" | Cookie：{'已配置' if site.cookie else '未配置'}"
                f" | 渲染：{'是' if site.render else '否'}"
                f" | 域名：{site.domain or site_rules.extract_domain(site.url or '')}"
            )
        return "\n".join(lines)

    @staticmethod
    def _site_prompt(awaiting_input: Optional[str]) -> str:
        """
        返回当前输入模式提示。
        """
        if awaiting_input == "cookie":
            return "当前操作：更新站点 Cookie，请输入：<id> <username> <password> [2fa_code/secret]"
        if awaiting_input == "enable":
            return "当前操作：启用站点，请输入站点 ID，多个 ID 用空格分隔。"
        if awaiting_input == "disable":
            return "当前操作：禁用站点，请输入站点 ID，多个 ID 用空格分隔。"
        return ""

    @staticmethod
    def _site_usage_hint(awaiting_input: Optional[str]) -> str:
        """
        返回 /sites 的文本操作提示。
        """
        if awaiting_input == "cookie":
            return "输入站点 ID、用户名、密码和可选 2FA；输入 `取消` 返回列表，输入 `退出` 结束交互。"
        if awaiting_input in {"enable", "disable"}:
            return "输入一个或多个站点 ID；输入 `取消` 返回列表，输入 `退出` 结束交互。"
        return (
            "可输入：`cookie <id> <username> <password> [2fa]`、`启用 <id...>`、`禁用 <id...>`、"
            "`n`、`p`、`刷新`、`退出`。"
        )

    @staticmethod
    def _parse_site_ids(arg_str: str) -> List[int]:
        """
        从输入中提取站点 ID。
        """
        return [int(item) for item in re.findall(r"\d+", arg_str or "")]

    def _set_sites_enabled(self, arg_str: str, enabled: bool) -> Tuple[bool, str]:
        """
        批量启用或禁用站点。
        """
        site_ids = self._parse_site_ids(arg_str)
        if not site_ids:
            return False, "请输入至少一个有效的站点 ID"

        siteoper = SiteOper()
        changed = []
        missing = []
        for site_id in site_ids:
            site = siteoper.get(site_id)
            if not site:
                missing.append(str(site_id))
                continue
            siteoper.update(site_id, {"is_active": enabled})
            changed.append(site.name)

        action = "启用" if enabled else "禁用"
        if not changed and missing:
            return False, f"未找到站点：{', '.join(missing)}"

        message = f"已{action} {len(changed)} 个站点"
        if changed:
            message += f"：{', '.join(changed)}"
        if missing:
            message += f"；未找到：{', '.join(missing)}"
        return True, message

    def _update_site_cookie_from_input(self, arg_str: str) -> Tuple[bool, str]:
        """
        根据输入更新单个站点 Cookie。
        """
        args = str(arg_str or "").split()
        if len(args) not in {3, 4} or not args[0].isdigit():
            return (
                False,
                "格式错误，请输入：cookie <id> <username> <password> [2fa_code/secret]",
            )

        site_id = int(args[0])
        site_info = SiteOper().get(site_id)
        if not site_info:
            return False, f"站点编号 {site_id} 不存在"

        status, msg = self._cookie_updater(
            site_info=site_info,
            username=args[1],
            password=args[2],
            two_step_code=args[3] if len(args) == 4 else None,
        )
        if not status:
            logger.error(msg)
            return False, f"【{site_info.name}】Cookie&UA 更新失败：{msg}"
        return True, f"【{site_info.name}】Cookie&UA 更新成功"
