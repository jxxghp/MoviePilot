"""InteractionRouter 单元测试：会话选择、回调派发顺序和未消费回退语义。"""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.testing.bootstrap import ensure_optional_stub

ensure_optional_stub("qbittorrentapi", TorrentFilesList=list)
ensure_optional_stub("transmission_rpc", File=object)
ensure_optional_stub("psutil")
ensure_optional_stub("aioshutil")
ensure_optional_stub("pyquery", PyQuery=object)

from app.application.messaging.interaction import InteractionContext, InteractionDispatch
from app.application.messaging.router import (
    CallbackRoute,
    InteractionRouter,
    SessionRoute,
    has_pending_interaction,
)
from app.application.messaging.site import site_interaction_manager
from app.application.messaging.skill import skill_interaction_manager
from app.schemas.types import NotificationChannel


def _context(user_id="10001") -> InteractionContext:
    """构造最小交互上下文。"""
    return InteractionContext(
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        user_id=user_id,
        username="tester",
    )


def _session_route(name: str, pending=None, consumed=True) -> tuple[SessionRoute, MagicMock]:
    """构造带可控返回值的会话路由，同时返回 handler 便于断言。"""
    handler = MagicMock(return_value=consumed)
    route = SessionRoute(
        name=name,
        get_pending=lambda _user_id, _pending=pending: _pending,
        handle_text=handler,
    )
    return route, handler


def _callback_route(name: str, matched=True, handled=True) -> CallbackRoute:
    """构造带可控匹配和处理结果的回调路由。"""
    dispatcher = MagicMock(return_value=InteractionDispatch(handled=handled))
    return CallbackRoute(
        name=name,
        matches=lambda _data, _matched=matched: _matched,
        dispatch=dispatcher,
    )


class TestInteractionRouterSessions(unittest.TestCase):
    def test_latest_session_prefers_newest_created_at(self):
        """多个待处理会话时选择创建时间最近的一条。"""
        now = datetime.now()
        old_route, _ = _session_route(
            "sites", pending=SimpleNamespace(created_at=now - timedelta(minutes=10))
        )
        new_route, _ = _session_route(
            "media", pending=SimpleNamespace(created_at=now)
        )
        router = InteractionRouter(
            session_routes=[old_route, new_route], callback_routes=[]
        )

        self.assertEqual(router.latest_session("10001"), new_route)

    def test_latest_session_missing_timestamp_treated_as_oldest(self):
        """缺少时间戳的会话不应抢占有时间戳的会话。"""
        plain_route, _ = _session_route("sites", pending=SimpleNamespace())
        stamped_route, _ = _session_route(
            "media", pending=SimpleNamespace(created_at=datetime.now())
        )
        router = InteractionRouter(
            session_routes=[plain_route, stamped_route], callback_routes=[]
        )

        self.assertEqual(router.latest_session("10001"), stamped_route)

    def test_dispatch_active_text_consumed_by_latest_session(self):
        """文本应只派发给最近会话并返回其消费结果。"""
        old_route, old_handler = _session_route(
            "sites", pending=SimpleNamespace(created_at=None)
        )
        new_route, new_handler = _session_route(
            "media", pending=SimpleNamespace(created_at=datetime.now())
        )
        router = InteractionRouter(
            session_routes=[old_route, new_route], callback_routes=[]
        )

        self.assertTrue(router.dispatch_active_text(_context(), "输入内容"))
        new_handler.assert_called_once()
        old_handler.assert_not_called()

    def test_dispatch_active_text_returns_false_without_session(self):
        """没有待处理会话时不消费文本。"""
        router = InteractionRouter(
            session_routes=[_session_route("sites", pending=None)[0]], callback_routes=[]
        )

        self.assertFalse(router.dispatch_active_text(_context(), "输入内容"))

    def test_has_pending_checks_all_routes(self):
        """任意路由存在待处理会话即视为有待处理交互。"""
        router = InteractionRouter(
            session_routes=[
                _session_route("sites", pending=None)[0],
                _session_route("media", pending=SimpleNamespace())[0],
            ],
            callback_routes=[],
        )

        self.assertTrue(router.has_pending("10001"))
        empty_router = InteractionRouter(
            session_routes=[_session_route("sites", pending=None)[0]], callback_routes=[]
        )
        self.assertFalse(empty_router.has_pending("10001"))


class TestInteractionRouterCallbacks(unittest.TestCase):
    def test_dispatch_callback_respects_registration_order(self):
        """回调按注册顺序匹配，首个匹配并消费的路由生效。"""
        first = _callback_route("transfer", matched=True, handled=True)
        second = _callback_route("skill", matched=True, handled=True)
        router = InteractionRouter(
            session_routes=[], callback_routes=[first, second]
        )

        result = router.dispatch_callback(_context(), "any")

        self.assertTrue(result.handled)
        first.dispatch.assert_called_once()
        second.dispatch.assert_not_called()

    def test_dispatch_callback_continues_when_matched_route_not_handled(self):
        """匹配但未消费的路由不拦截后续路由。"""
        unmatched = _callback_route("transfer", matched=False, handled=True)
        skipped = _callback_route("skill", matched=True, handled=False)
        consumer = _callback_route("site", matched=True, handled=True)
        router = InteractionRouter(
            session_routes=[], callback_routes=[unmatched, skipped, consumer]
        )

        result = router.dispatch_callback(_context(), "any")

        self.assertTrue(result.handled)
        unmatched.dispatch.assert_not_called()
        skipped.dispatch.assert_called_once()
        consumer.dispatch.assert_called_once()

    def test_dispatch_callback_unhandled_when_no_route_matches(self):
        """所有路由均不匹配时返回未处理。"""
        router = InteractionRouter(
            session_routes=[],
            callback_routes=[_callback_route("transfer", matched=False)],
        )

        result = router.dispatch_callback(_context(), "unknown")

        self.assertFalse(result.handled)
        self.assertFalse(result.defer_processing_finish)


class TestHasPendingInteraction(unittest.TestCase):
    def tearDown(self):
        site_interaction_manager.clear()
        skill_interaction_manager.clear()

    def test_has_pending_interaction_detects_real_sessions(self):
        """WebAgent 判断应覆盖真实交互会话管理器。"""
        self.assertFalse(has_pending_interaction("10001"))

        site_interaction_manager.create_or_replace(
            user_id="10001",
            command="/sites",
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            username="tester",
        )
        self.assertTrue(has_pending_interaction("10001"))
        self.assertFalse(has_pending_interaction("10002"))


if __name__ == "__main__":
    unittest.main()
