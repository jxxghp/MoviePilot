"""订阅消息交互删除动作的应用边界测试。"""

from types import SimpleNamespace

from app.application.messaging.subscribe import SubscribeInteractionHandler


class _Repository:
    """只提供消息展示所需读取能力的订阅仓储替身。"""

    def __init__(self, subscribes):
        """按订阅 ID 保存测试快照。"""
        self._subscribes = subscribes

    def get(self, subscribe_id):
        """返回指定订阅快照。"""
        return self._subscribes.get(subscribe_id)


def test_interaction_delete_delegates_each_existing_id_to_application_action():
    """消息层只负责解析和展示，删除必须委托统一 Application 动作。"""
    deleted_ids = []
    handler = SubscribeInteractionHandler(
        messenger=SimpleNamespace(),
        actions=SimpleNamespace(),
        repository=_Repository({
            7: SimpleNamespace(name="电影订阅"),
            8: SimpleNamespace(name="剧集订阅"),
        }),
        delete_subscription=lambda subscribe_id: deleted_ids.append(subscribe_id) or True,
    )

    success, message = handler._delete_subscribes("7 8 9")

    assert success is True
    assert deleted_ids == [7, 8]
    assert message == "已删除 2 个订阅：电影订阅, 剧集订阅；未找到：9"


def test_interaction_delete_reports_transaction_race_as_missing():
    """读取后事务内目标消失时不得误报已删除。"""
    handler = SubscribeInteractionHandler(
        messenger=SimpleNamespace(),
        actions=SimpleNamespace(),
        repository=_Repository({7: SimpleNamespace(name="电影订阅")}),
        delete_subscription=lambda _subscribe_id: False,
    )

    success, message = handler._delete_subscribes("7")

    assert success is False
    assert message == "未找到订阅：7"
