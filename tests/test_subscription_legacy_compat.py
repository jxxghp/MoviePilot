"""旧订阅 Oper 插件导入、签名和无 Session 行为门禁。"""

import asyncio
import importlib
import inspect

from app.application.subscription.contract import (
    SubscriptionIdentity,
    SubscriptionPatch,
)
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.db.oper.subscribe import SubscribeOper as CanonicalSubscribeOper
from app.db.oper.subscribehistory import (
    SubscribeHistoryOper as CanonicalSubscribeHistoryOper,
)
from app.runtime.compat.manifest import MODULE_ALIASES
from app.schemas.types import MediaSource, MediaType


def test_legacy_subscription_imports_target_one_private_sdk_facade() -> None:
    """两个旧模块与 Oper 包根只经同一私有 Legacy SDK 解析。"""
    subscribe_alias = MODULE_ALIASES["app.db.subscribe_oper"]
    history_alias = MODULE_ALIASES["app.db.subscribehistory_oper"]
    legacy_subscribe = importlib.import_module("app.db.subscribe_oper")
    legacy_history = importlib.import_module("app.db.subscribehistory_oper")

    assert subscribe_alias.target == "app.sdk._legacy.subscribe"
    assert subscribe_alias.owner == "sdk"
    assert history_alias.target == "app.sdk._legacy.subscribe"
    assert history_alias.owner == "sdk"
    assert legacy_subscribe is legacy_history
    assert legacy_subscribe.__all__ == ["SubscribeHistoryOper", "SubscribeOper"]
    assert issubclass(legacy_subscribe.SubscribeOper, CanonicalSubscribeOper)
    assert issubclass(
        legacy_subscribe.SubscribeHistoryOper,
        CanonicalSubscribeHistoryOper,
    )
    assert legacy_subscribe.SubscribeOper is not CanonicalSubscribeOper
    assert legacy_subscribe.SubscribeHistoryOper is not CanonicalSubscribeHistoryOper

    oper_package = importlib.import_module("app.db.oper")
    assert oper_package.SubscribeOper is legacy_subscribe.SubscribeOper
    assert oper_package.SubscribeHistoryOper is legacy_subscribe.SubscribeHistoryOper
    assert "SubscribeOper" not in oper_package.__all__
    assert "SubscribeHistoryOper" not in oper_package.__all__


def test_legacy_subscription_oper_preserves_plugin_method_signatures() -> None:
    """官方插件使用的订阅读写方法与旧历史分页入口保持调用 ABI。"""
    legacy = importlib.import_module("app.db.subscribe_oper")

    add = inspect.signature(legacy.SubscribeOper.add)
    async_add = inspect.signature(legacy.SubscribeOper.async_add)
    listing = inspect.signature(legacy.SubscribeOper.list)
    update = inspect.signature(legacy.SubscribeOper.update)
    history = inspect.signature(legacy.SubscribeHistoryOper.async_list_by_type)

    assert tuple(add.parameters) == ("self", "mediainfo", "kwargs")
    assert add.parameters["mediainfo"].default is None
    assert add.parameters["kwargs"].kind is inspect.Parameter.VAR_KEYWORD
    assert tuple(async_add.parameters) == ("self", "mediainfo", "kwargs")
    assert async_add.parameters["mediainfo"].default is None
    assert tuple(listing.parameters) == ("self", "state")
    assert listing.parameters["state"].default is None
    assert tuple(update.parameters) == ("self", "sid", "payload")
    assert tuple(history.parameters) == ("self", "mtype", "page", "count")
    assert history.parameters["page"].default == 1
    assert history.parameters["count"].default == 30


def test_legacy_subscription_facade_unwraps_typed_write_contract(monkeypatch) -> None:
    """旧 facade 回调 raw Oper 前必须在兼容边界解包 typed 写 DTO。"""
    legacy = importlib.import_module("app.db.subscribe_oper")
    captured = {}

    def fake_add(self, identity, payload, username=None, after_commit=None):
        """记录 Legacy facade 解包后的 raw Oper 参数。"""
        captured.update({
            "self": self,
            "identity": identity,
            "payload": payload,
            "username": username,
            "after_commit": after_commit,
        })
        return 12, "新增订阅成功"

    monkeypatch.setattr(CanonicalSubscribeOper, "add", fake_add)
    oper = object.__new__(legacy.SubscribeOper)

    def callback(_subscribe_id):
        """模拟提交后的插件回调。"""
        return True

    result = oper.add(
        identity=SubscriptionIdentity(
            media_source=MediaSource.TMDB,
            media_id="12",
            season=1,
        ),
        payload=SubscriptionPatch({"name": "兼容订阅"}),
        username="legacy-user",
        after_commit=callback,
    )

    assert result == (12, "新增订阅成功")
    assert captured == {
        "self": oper,
        "identity": {
            "media_source": "themoviedb",
            "media_id": "12",
            "season": 1,
            "episode_group": None,
            "music_type": None,
        },
        "payload": {"name": "兼容订阅"},
        "username": "legacy-user",
        "after_commit": callback,
    }


def test_legacy_subscription_opers_preserve_no_session_behavior(db) -> None:
    """旧插件不注入 Session 也可列举、更新订阅并异步读取历史。"""
    db.watermark(Subscribe, SubscribeHistory)
    subscribe = Subscribe(
        name="兼容订阅",
        type=MediaType.TV.value,
        state="R",
        media_source="themoviedb",
        media_id="legacy-subscription-1",
        season=1,
        date="2026-08-28 12:00:00",
    )
    history = SubscribeHistory(
        name="兼容订阅历史",
        type=MediaType.TV.value,
        media_source="themoviedb",
        media_id="legacy-subscription-history-1",
        season=1,
        date="2026-08-28 12:01:00",
    )
    db.add(subscribe, history)
    db.session.commit()

    legacy = importlib.import_module("app.db.subscribe_oper")
    oper = legacy.SubscribeOper()

    assert subscribe.id in {row.id for row in oper.list("R")}
    assert oper.update(subscribe.id, {"state": "S"}).state == "S"
    histories = asyncio.run(
        legacy.SubscribeHistoryOper().async_list_by_type(
            MediaType.TV.value,
            page=1,
            count=100,
        )
    )
    assert history.id in {row.id for row in histories}
