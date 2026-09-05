"""
交互路由层：统一选择活动文本会话，并按固定顺序派发按钮回调。

文本会话候选覆盖 Site、Subscribe、Skill、Media、Update 五类，
按会话创建时间选择最近激活的一条，避免旧会话抢占新会话的输入。
"""

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence, Union

from app.application.messaging.interaction import (
    InteractionContext,
    InteractionDispatch,
)
from app.application.messaging.media import media_interaction_manager
from app.application.messaging.site import site_interaction_manager
from app.application.messaging.skill import skill_interaction_manager
from app.application.messaging.subscribe import subscribe_interaction_manager
from app.application.messaging.update import update_interaction_manager


@dataclass(frozen=True, slots=True)
class SessionRoute:
    """描述一种可继续接收文本的交互会话。"""

    # 会话名称，与对应 Slash 命令一致
    name: str
    # 返回用户当前待处理会话对象（含 created_at），无会话返回 None
    get_pending: Callable[[Union[str, int]], Optional[Any]]
    # 将一条文本派发给该会话，返回是否已消费
    handle_text: Callable[[InteractionContext, str], bool]


@dataclass(frozen=True, slots=True)
class CallbackRoute:
    """描述一种按钮回调的匹配和处理方式。"""

    # 路由名称，用于日志和排查
    name: str
    # 按回调内容判断是否归本路由处理
    matches: Callable[[str], bool]
    # 执行回调处理并返回派发结果
    dispatch: Callable[[str, InteractionContext], InteractionDispatch]


def adapt_session_text_handler(
    handle: Callable[..., Any],
) -> Callable[[InteractionContext, str], bool]:
    """把传统关键字参数入口适配为文本会话路由处理器。"""

    def _handle(context: InteractionContext, text: str) -> bool:
        """使用统一交互上下文调用传统文本入口。"""
        return bool(
            handle(
                channel=context.channel,
                source=context.source,
                userid=context.user_id,
                username=context.username,
                text=text,
            )
        )

    return _handle


def adapt_callback_handler(
    handle: Callable[..., Any],
) -> Callable[[str, InteractionContext], InteractionDispatch]:
    """把传统关键字参数入口适配为按钮回调路由处理器。"""

    def _dispatch(
        callback_data: str,
        context: InteractionContext,
    ) -> InteractionDispatch:
        """使用统一交互上下文调用传统回调入口。"""
        return InteractionDispatch(
            handled=bool(
                handle(
                    callback_data=callback_data,
                    channel=context.channel,
                    source=context.source,
                    userid=context.user_id,
                    username=context.username,
                    original_message_id=context.original_message_id,
                    original_chat_id=context.original_chat_id,
                )
            )
        )

    return _dispatch


class InteractionRouter:
    """统一选择活动文本会话并按顺序派发按钮回调。"""

    def __init__(
            self,
            session_routes: Sequence[SessionRoute],
            callback_routes: Sequence[CallbackRoute],
    ):
        """按注册顺序保存会话路由和回调路由，回调顺序即优先级。"""
        self._session_routes = list(session_routes)
        self._callback_routes = list(callback_routes)

    def latest_session(self, user_id: Union[str, int]) -> Optional[SessionRoute]:
        """返回最近创建的待处理文本会话路由，无会话返回 None。"""
        best_route: Optional[SessionRoute] = None
        best_created_at = None
        for route in self._session_routes:
            pending = route.get_pending(user_id)
            if pending is None:
                continue
            created_at = getattr(pending, "created_at", None)
            # 缺少时间戳的会话视为最早，保证有时间戳的新会话优先
            if best_route is None or (
                    created_at is not None
                    and (best_created_at is None or created_at > best_created_at)
            ):
                best_route = route
                best_created_at = created_at
        return best_route

    def has_pending(self, user_id: Union[str, int]) -> bool:
        """判断用户是否存在任意待处理文本会话。"""
        return any(
            route.get_pending(user_id) is not None
            for route in self._session_routes
        )

    def dispatch_active_text(
            self,
            context: InteractionContext,
            text: str,
    ) -> bool:
        """把文本派发给最近活动的会话，返回是否被消费。"""
        route = self.latest_session(context.user_id)
        if route is None:
            return False
        return route.handle_text(context, text)

    def dispatch_callback(
            self,
            context: InteractionContext,
            callback_data: str,
    ) -> InteractionDispatch:
        """按注册顺序匹配并派发按钮回调，均不匹配时返回未处理。

        匹配到的路由未消费回调（handled=False）时继续尝试后续路由，
        与既有顺序式派发的行为保持一致。
        """
        for route in self._callback_routes:
            if route.matches(callback_data):
                result = route.dispatch(callback_data, context)
                if result.handled:
                    return result
        return InteractionDispatch(handled=False)


def has_pending_interaction(user_id: Union[str, int]) -> bool:
    """供 WebAgent 判断用户是否处于传统交互会话。"""
    return any(
        manager.get_by_user(user_id) is not None
        for manager in (
            site_interaction_manager,
            subscribe_interaction_manager,
            skill_interaction_manager,
            media_interaction_manager,
            update_interaction_manager,
        )
    )
