import unittest
from types import SimpleNamespace

from app.core.config import global_vars
from app.helper.webpush import is_webpush_subscription_gone


class WebPushSubscriptionTest(unittest.TestCase):
    def setUp(self):
        """清理跨用例共享的 WebPush 订阅。"""
        with global_vars.SUBSCRIPTIONS_LOCK:
            global_vars.SUBSCRIPTIONS.clear()

    def tearDown(self):
        """清理测试产生的 WebPush 订阅。"""
        with global_vars.SUBSCRIPTIONS_LOCK:
            global_vars.SUBSCRIPTIONS.clear()

    def test_push_subscription_upserts_by_endpoint(self):
        """相同 endpoint 的 WebPush 订阅应更新而不是重复追加。"""
        global_vars.push_subscription(
            {"endpoint": "https://push.example/a", "keys": {"p256dh": "old"}}
        )
        global_vars.push_subscription(
            {"endpoint": "https://push.example/a", "keys": {"p256dh": "new"}}
        )

        subscriptions = global_vars.get_subscriptions()

        self.assertEqual(1, len(subscriptions))
        self.assertEqual("new", subscriptions[0]["keys"]["p256dh"])

    def test_remove_subscription_deletes_by_endpoint(self):
        """失效订阅应能按 endpoint 从全局订阅表删除。"""
        subscription = {"endpoint": "https://push.example/a", "keys": {}}
        global_vars.push_subscription(subscription)

        self.assertTrue(global_vars.remove_subscription(subscription))
        self.assertEqual([], global_vars.get_subscriptions())

    def test_is_webpush_subscription_gone_matches_404_and_410(self):
        """推送服务返回 404/410 时应识别为订阅已失效。"""
        self.assertTrue(
            is_webpush_subscription_gone(
                SimpleNamespace(response=SimpleNamespace(status_code=410))
            )
        )
        self.assertTrue(
            is_webpush_subscription_gone(
                SimpleNamespace(response=SimpleNamespace(status=404))
            )
        )
        self.assertFalse(
            is_webpush_subscription_gone(
                SimpleNamespace(response=SimpleNamespace(status_code=500))
            )
        )
