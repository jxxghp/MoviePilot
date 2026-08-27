import importlib

import pytest

from app.application.transfer.workflow import TransferTask as CanonicalTransferTask
from app.db.models.transferhistory import TransferHistory
from app.schemas.file import FileItem


def test_legacy_workflow_writes_delegate_to_configured_execution_port(monkeypatch):
    """旧 WorkflowOper 无 Session 写入必须完整委托类型化事务端口。"""
    legacy = importlib.import_module("app.db.workflow_oper")
    calls = []

    class ExecutionPort:
        """记录五种旧工作流写入调用。"""

        def start(self, workflow_id):
            """记录启动。"""
            calls.append(("start", workflow_id))
            return True

        def success(self, workflow_id, result=None):
            """记录成功。"""
            calls.append(("success", workflow_id, result))
            return True

        def fail(self, workflow_id, result):
            """记录失败。"""
            calls.append(("fail", workflow_id, result))
            return True

        def step(self, workflow_id, action_id, context, execution_state=None):
            """记录步骤。"""
            calls.append(
                ("step", workflow_id, action_id, context, execution_state)
            )
            return True

        def reset(self, workflow_id, reset_count=False):
            """记录重置。"""
            calls.append(("reset", workflow_id, reset_count))
            return True

    monkeypatch.setattr(
        legacy,
        "get_configured_workflow_execution",
        lambda: ExecutionPort(),
    )
    oper = legacy.WorkflowOper()

    assert oper.start(7) is True
    assert oper.success(7, "done") is True
    assert oper.fail(7, "failed") is True
    assert oper.step(7, "A", {"value": 1}, {"runtime": {}}) is True
    assert oper.reset(7, reset_count=True) is True
    assert calls == [
        ("start", 7),
        ("success", 7, "done"),
        ("fail", 7, "failed"),
        ("step", 7, "A", {"value": 1}, {"runtime": {}}),
        ("reset", 7, True),
    ]


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


def test_legacy_transfer_history_mutations_preserve_durable_receipts(db):
    """旧插件 delete/truncate/add_force 不能删除或覆盖 durable 终态回执。"""
    legacy = importlib.import_module("app.db.transferhistory_oper")
    durable = TransferHistory(
        src="/downloads/durable.mkv",
        src_storage="local",
        status=True,
        transfer_task_id="task-durable",
        transfer_settlement_revision=1,
    )
    legacy_row = TransferHistory(
        src="/downloads/legacy.mkv",
        src_storage="local",
        status=True,
    )
    db.add(durable, legacy_row)
    oper = legacy.TransferHistoryOper(db.session)

    oper.delete(durable.id)
    oper.truncate()

    assert TransferHistory.get_by_transfer_task_id(
        db.session,
        task_id="task-durable",
    ) is not None
    assert TransferHistory.get_by_src(
        db.session,
        "/downloads/legacy.mkv",
        "local",
    ) is None
    with pytest.raises(ValueError, match="持久整理回执"):
        oper.add_force(
            src="/downloads/durable.mkv",
            src_storage="local",
            status=False,
        )
    receipt = TransferHistory.get_by_transfer_task_id(
        db.session,
        task_id="task-durable",
    )
    assert receipt is not None
    assert receipt.status is True


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
