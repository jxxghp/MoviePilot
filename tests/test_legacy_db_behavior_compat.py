import importlib

import pytest

from app.application.transfer import TransferTask as CanonicalTransferTask
from app.schemas.file import FileItem


def test_legacy_subscribe_add_delegates_to_application_service(monkeypatch):
    """旧 SubscribeOper.add 应保留 mediainfo 写入签名。"""
    legacy = importlib.import_module("app.db.subscribe_oper")
    oper = object.__new__(legacy.SubscribeOper)
    mediainfo = object()
    captured = {}

    def fake_add_subscribe(*, mediainfo, subscribe_oper, **kwargs):
        """记录同步兼容门面转交的参数。"""
        captured.update({
            "mediainfo": mediainfo,
            "subscribe_oper": subscribe_oper,
            "kwargs": kwargs,
        })
        return 7, "新增订阅成功"

    monkeypatch.setattr(legacy, "add_subscribe", fake_add_subscribe)

    assert oper.add(mediainfo=mediainfo, season=1) == (7, "新增订阅成功")
    assert captured == {
        "mediainfo": mediainfo,
        "subscribe_oper": oper,
        "kwargs": {"season": 1},
    }


def test_legacy_subscribe_facade_accepts_application_dictionary_callback(
        monkeypatch,
):
    """应用服务回调兼容 Oper 时应进入新字典签名，不能再次转回应用服务。"""
    legacy = importlib.import_module("app.db.subscribe_oper")
    canonical = importlib.import_module("app.db.oper.subscribe")
    oper = object.__new__(legacy.SubscribeOper)
    captured = {}

    def fake_canonical_add(self, identity, payload, username=None):
        """记录兼容类转交给 canonical Oper 的持久化参数。"""
        captured.update({
            "self": self,
            "identity": identity,
            "payload": payload,
            "username": username,
        })
        return 9, "新增订阅成功"

    monkeypatch.setattr(canonical.SubscribeOper, "add", fake_canonical_add)

    result = oper.add(
        identity={"media_source": "themoviedb", "media_id": "1"},
        payload={"name": "Test"},
        username="admin",
    )

    assert result == (9, "新增订阅成功")
    assert captured == {
        "self": oper,
        "identity": {"media_source": "themoviedb", "media_id": "1"},
        "payload": {"name": "Test"},
        "username": "admin",
    }


@pytest.mark.asyncio
async def test_legacy_subscribe_async_add_delegates_to_application_service(
        monkeypatch,
):
    """旧 SubscribeOper.async_add 应保留异步 mediainfo 写入签名。"""
    legacy = importlib.import_module("app.db.subscribe_oper")
    oper = object.__new__(legacy.SubscribeOper)
    mediainfo = object()
    captured = {}

    async def fake_async_add_subscribe(*, mediainfo, subscribe_oper, **kwargs):
        """记录异步兼容门面转交的参数。"""
        captured.update({
            "mediainfo": mediainfo,
            "subscribe_oper": subscribe_oper,
            "kwargs": kwargs,
        })
        return 8, "新增订阅成功"

    monkeypatch.setattr(legacy, "async_add_subscribe", fake_async_add_subscribe)

    result = await oper.async_add(mediainfo=mediainfo, season=2)

    assert result == (8, "新增订阅成功")
    assert captured == {
        "mediainfo": mediainfo,
        "subscribe_oper": oper,
        "kwargs": {"season": 2},
    }


@pytest.mark.parametrize(
    ("method_name", "service_name"),
    [
        ("add_success", "add_transfer_success"),
        ("add_fail", "add_transfer_fail"),
    ],
)
def test_legacy_transfer_history_writes_delegate_to_application_service(
        monkeypatch,
        method_name,
        service_name,
):
    """旧整理历史写入方法应只做代理，不把业务逻辑搬回 Oper。"""
    legacy = importlib.import_module("app.db.transferhistory_oper")
    oper = object.__new__(legacy.TransferHistoryOper)
    arguments = {
        "fileitem": object(),
        "mode": "copy",
        "meta": object(),
        "mediainfo": object(),
        "transferinfo": object(),
        "downloader": "qb",
        "download_hash": "hash",
    }
    captured = {}

    def fake_service(**kwargs):
        """记录整理历史兼容门面转交的参数。"""
        captured.update(kwargs)
        return "history"

    monkeypatch.setattr(legacy, service_name, fake_service)

    assert getattr(oper, method_name)(**arguments) == "history"
    assert captured == {**arguments, "transfer_history_oper": oper}


class LegacyPydanticValue:
    """模拟旧插件放进 TransferTask 的 Pydantic 风格对象。"""

    def model_dump(self):
        """返回测试用序列化结果。"""
        return {"kind": "pydantic"}


class LegacyDomainValue:
    """模拟新领域对象的 to_dict 序列化接口。"""

    def to_dict(self):
        """返回测试用序列化结果。"""
        return {"kind": "domain"}


def test_legacy_transfer_task_keeps_wide_plugin_input_contract():
    """旧 schemas.TransferTask 应接受自定义对象且仍可进入新整理链。"""
    schemas_package = importlib.import_module("app.schemas")
    task = schemas_package.TransferTask(
        fileitem=FileItem(path="/downloads/test.mkv", storage="local"),
        meta=LegacyPydanticValue(),
        mediainfo=LegacyDomainValue(),
    )

    assert isinstance(task, CanonicalTransferTask)
    assert task.to_dict()["meta"] == {"kind": "pydantic"}
    assert task.to_dict()["mediainfo"] == {"kind": "domain"}
