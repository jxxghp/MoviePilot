"""验证实际整理提交回执与预览隔离，并由真实执行阶段决定完成状态。"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints import transfer
from app.application.transfer.execution import TransferExecutionState
from app.chain.transfer.facade import TransferChain
from app.schemas.transfer import TransferInfo
from tests.test_manual_transfer_history import _patch_transfer_planning
from tests.test_transfer_sync_extra_files import make_fileitem, make_transfer_chain


@pytest.fixture
def submission_client(monkeypatch):
    """挂载真实路由与响应校验，仅替换管理授权、历史端口和整理业务外部边界。"""
    app = FastAPI()
    app.include_router(transfer.router, prefix="/api/v1/transfer")
    app.dependency_overrides[transfer.get_current_active_manage_user] = lambda: object()
    app.dependency_overrides[transfer.get_transfer_history_lookup_service] = lambda: SimpleNamespace(get=lambda _id: None)
    calls = []
    outcomes = []

    class FakeChain:
        """按源文件返回可控回执，验证 HTTP 响应不会丢失状态和部分成功项。"""

        def manual_transfer(self, **kwargs):
            """保存请求参数并返回下一个预设结果。"""
            calls.append(kwargs)
            return outcomes.pop(0)

    monkeypatch.setattr(transfer, "TransferChain", FakeChain)
    with TestClient(app) as client:
        yield client, outcomes, calls


@pytest.mark.parametrize("state", ["accepted", "completed", "failed", "retry_wait", "skipped", "manual_review"])
def test_submission_http_preserves_typed_state_and_failure_details(submission_client, state):
    """实际阶段必须经过 FastAPI 序列化保留，不能降级为预览并丢失 state。"""
    client, outcomes, calls = submission_client
    accepted = state in {"accepted", "completed", "retry_wait"}
    outcomes.append((accepted, {"items": [{
        "source": "/downloads/a.mkv", "target": "/media/a.mkv", "state": state,
        "success": accepted, "message": "目标路径不可写" if not accepted else "",
        "overwrite_skipped": state == "skipped",
    }]}))

    response = client.post("/api/v1/transfer/manual?background=true", json={
        "fileitem": make_fileitem("/downloads/a.mkv").model_dump(mode="json"),
    })

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is accepted
    item = payload["data"]["items"][0]
    assert item["state"] == state
    assert item["target"] == "/media/a.mkv"
    assert item["overwrite_skipped"] is (state == "skipped")
    if not accepted:
        assert item["failure_stage"]
        assert item["recovery_action"]
    assert calls[0]["report_results"] is True
    assert calls[0]["background"] is True


def test_submission_batch_preserves_success_when_another_file_fails(submission_client):
    """批次总失败仍返回完整逐项结果，并在业务总提示中保留失败文件路径。"""
    client, outcomes, calls = submission_client
    first, second = make_fileitem("/downloads/a.mkv"), make_fileitem("/downloads/b.mkv")
    outcomes.extend([
        (True, {"items": [{"source": first.path, "state": "accepted", "success": True}]}),
        (False, "目标路径不可写"),
    ])

    response = client.post("/api/v1/transfer/manual?background=true", json={
        "fileitems": [item.model_dump(mode="json") for item in (first, second)],
        "target_path": "/media",
    })

    payload = response.json()
    assert response.status_code == 200
    assert payload["success"] is False
    assert [item["state"] for item in payload["data"]["items"]] == ["accepted", "failed"]
    assert [item["source"] for item in payload["data"]["items"]] == [first.path, second.path]
    assert second.path in payload["message"]
    assert payload["data"]["items"][1]["failure_stage"] == "destination_access"
    assert len(calls) == 2


def test_preview_http_keeps_its_summary_and_does_not_claim_execution(submission_client):
    """预览继续返回原预览 schema，预览成功不得凭空生成实际接收状态。"""
    client, outcomes, calls = submission_client
    outcomes.append((True, {"summary": {"total": 1, "success": 1, "failed": 0}, "items": [{
        "source": "/downloads/a.mkv", "success": True, "title": "Example",
    }]}))

    response = client.post("/api/v1/transfer/manual", json={
        "fileitem": make_fileitem("/downloads/a.mkv").model_dump(mode="json"), "preview": True,
    })

    payload = response.json()
    assert response.status_code == 200
    assert payload["data"]["summary"]["total"] == 1
    assert payload["data"]["items"][0]["title"] == "Example"
    assert "state" not in payload["data"]["items"][0]
    assert calls[0]["report_results"] is False


@pytest.fixture
def submission_chain(monkeypatch):
    """保留真实手动工作流，仅隔离文件、下载器、媒体识别和执行副作用。"""
    chain = make_transfer_chain()
    fileitem = make_fileitem("/downloads/Test.Show.S01E01.mkv")
    _patch_transfer_planning(monkeypatch, chain, fileitem, None, [], [])
    monkeypatch.setattr(chain, "_TransferChain__claim_task_for_execution", lambda task: None)
    monkeypatch.setattr(chain, "_TransferChain__start_job_execution", lambda task: None)
    monkeypatch.setattr(chain, "_TransferChain__release_task_claim", lambda task, **kwargs: None)
    return chain, fileitem


def test_background_submission_reports_queue_acceptance_without_execution(submission_chain, monkeypatch):
    """后台成功仅确认真正入队，提交请求本身不会执行文件也不能报告 completed。"""
    chain, fileitem = submission_chain
    queued = []
    monkeypatch.setattr(chain, "put_to_queue", lambda task: queued.append(task) or True)
    success, result = chain.manual_transfer(fileitem=fileitem, background=True, report_results=True)
    assert success is True
    assert len(queued) == 1
    assert result["items"][0]["state"] == "accepted"
    assert result["items"][0]["source"] == fileitem.path


@pytest.mark.parametrize("queued, expected", [(False, "failed"), (True, "accepted")])
def test_background_duplicate_is_not_reported_as_accepted(submission_chain, monkeypatch, queued, expected):
    """只有队列入口确认新接收才返回 accepted，重复准入必须明确失败。"""
    chain, fileitem = submission_chain
    monkeypatch.setattr(chain, "put_to_queue", lambda task: queued)
    success, result = chain.manual_transfer(fileitem=fileitem, background=True, report_results=True)
    assert success is queued
    assert result["items"][0]["state"] == expected


@pytest.mark.parametrize("settled, overwrite, expected", [
    (True, False, "completed"), (False, False, "retry_wait"), (True, True, "skipped"),
])
def test_sync_submission_waits_for_atomic_settlement(submission_chain, monkeypatch, settled, overwrite, expected):
    """执行成功但原子结算未确认时必须等待恢复，覆盖跳过也不能被误报已入库。"""
    chain, fileitem = submission_chain
    target = make_fileitem("/media/Test.Show.S01E01.mkv")

    def settle(task, info):
        """模拟 writer 的明确结算回执，保留工作流中真实完成判定。"""
        if settled:
            task.mark_terminal_settled()
        return info.success, info.message or ""

    def execute(task, callback):
        """模拟一次文件操作并进入真实工作流回调包装。"""
        task.bind_plan_checkpoint(SimpleNamespace())
        task.bind_admission_task_id("test-submission")
        return callback(task, TransferInfo(
            success=not overwrite, target_item=target,
            message="目标已存在，跳过覆盖" if overwrite else "", overwrite_skipped=overwrite,
        ))

    monkeypatch.setattr(chain, "_TransferChain__default_callback", settle)
    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", execute)
    chain.transfer_execution_repository = SimpleNamespace(
        get_snapshot=lambda **kwargs: SimpleNamespace(state=TransferExecutionState.RETRY_WAIT),
    )
    _success, result = chain.manual_transfer(fileitem=fileitem, background=False, report_results=True)
    assert result["items"][0]["state"] == expected
    assert result["items"][0]["target"] == target.path
    assert result["items"][0]["overwrite_skipped"] is overwrite


@pytest.mark.parametrize("accepted, expected", [(True, "retry_wait"), (False, "failed")])
def test_existing_durable_retry_keeps_its_actual_state(submission_chain, monkeypatch, accepted, expected):
    """历史失败的原计划重试由现有调度器接管，不标记为同步完成。"""
    chain, fileitem = submission_chain
    history = SimpleNamespace(status=False, transfer_task_id="task-a")
    monkeypatch.setattr(chain, "_get_manual_transfer_history", lambda **kwargs: history)
    monkeypatch.setattr(chain, "_request_durable_transfer_retry", lambda *args, **kwargs: (accepted, "重试申请"))
    success, result = chain.manual_transfer(fileitem=fileitem, background=False, report_results=True)
    assert success is accepted
    assert result["items"][0]["state"] == expected


def test_legacy_manual_caller_keeps_string_result(submission_chain):
    """未启用回执的插件和既有调用仍收到原来的布尔与字符串二元组。"""
    chain, fileitem = submission_chain
    success, message = chain.manual_transfer(fileitem=fileitem, background=False)
    assert success is True
    assert message == ""


@pytest.mark.parametrize("background", [True, False])
def test_cancellation_keeps_processed_and_unprocessed_file_receipts(submission_chain, monkeypatch, background):
    """中途取消保留已入队或完成项，同时为剩余文件返回失败，禁止整批重提。"""
    chain, first = submission_chain
    second = make_fileitem("/downloads/Test.Show.S01E02.mkv")
    stop = SimpleNamespace(is_system_stopped=False)
    registered = []
    monkeypatch.setattr("app.chain.transfer.workflow.runtime_stop_state", stop)
    monkeypatch.setattr(chain, "_TransferChain__get_trans_fileitems", lambda *args, **kwargs: [(first, False), (second, False)])

    def register(task):
        """使用真实作业视图记录等待任务，以验证取消后可重新准入。"""
        registered.append(task)
        return TransferChain._TransferChain__put_to_jobview(chain, task)

    def queue(task):
        """第一个文件真实被接收后模拟用户取消整个批次。"""
        assert task.fileitem.path == first.path
        stop.is_system_stopped = True
        return True

    def execute(task, callback):
        """第一个文件已结算后模拟取消，不执行剩余文件。"""
        assert callback
        assert task.fileitem.path == first.path
        task.mark_terminal_settled()
        stop.is_system_stopped = True
        return True, ""

    monkeypatch.setattr(chain, "put_to_queue", queue)
    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", execute)
    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", register)
    success, result = chain.manual_transfer(fileitem=first, background=background, report_results=True)
    assert success is False
    assert [(item["source"], item["state"]) for item in result["items"]] == [
        (first.path, "accepted" if background else "completed"), (second.path, "failed"),
    ]
    assert "取消" in result["items"][1]["message"]
    if not background:
        assert TransferChain._TransferChain__put_to_jobview(chain, registered[1]) is True


def test_post_settlement_failure_preserves_completed_receipt(submission_chain, monkeypatch):
    """入库原子提交后通知等后续副作用失败，回执仍确认已入库，避免重复整理。"""
    chain, fileitem = submission_chain

    def settle(task, _info):
        """原子提交成功后模拟通知异常。"""
        task.mark_terminal_settled()
        raise RuntimeError("通知失败")

    def execute(task, callback):
        """把成功文件操作交给真实回调包装，保留提交前后的时间顺序。"""
        return callback(task, TransferInfo(success=True, target_item=make_fileitem("/media/done.mkv")))

    monkeypatch.setattr(chain, "_TransferChain__default_callback", settle)
    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", execute)
    monkeypatch.setattr(chain, "_TransferChain__fail_transfer_task", lambda *args: None)
    success, result = chain.manual_transfer(fileitem=fileitem, background=False, report_results=True)
    assert success is False
    assert result["items"][0]["state"] == "completed"
    assert result["items"][0]["success"] is True
    assert result["items"][0]["target"] == "/media/done.mkv"
    assert "文件已完成整理" in result["items"][0]["message"]
    assert "无需重复整理" in result["items"][0]["message"]
    assert "稍后重试" not in result["items"][0]["message"]


def test_cancellation_during_build_releases_waiting_job(submission_chain, monkeypatch):
    """同步任务尚未开始执行时取消，也要移除预登记占位，随后允许重新准入。"""
    chain, first = submission_chain
    second = make_fileitem("/downloads/Test.Show.S01E02.mkv")
    stop = SimpleNamespace(is_system_stopped=False)
    registered = []
    monkeypatch.setattr("app.chain.transfer.workflow.runtime_stop_state", stop)
    monkeypatch.setattr(chain, "_TransferChain__get_trans_fileitems", lambda *args, **kwargs: [(first, False), (second, False)])

    def register(task):
        """第一个等待项登记后取消，模拟候选构建阶段被用户中断。"""
        registered.append(task)
        queued = TransferChain._TransferChain__put_to_jobview(chain, task)
        stop.is_system_stopped = True
        return queued

    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", register)
    success, result = chain.manual_transfer(fileitem=first, background=False, report_results=True)
    assert success is False
    assert [item["state"] for item in result["items"]] == ["failed", "failed"]
    assert len(registered) == 1
    assert TransferChain._TransferChain__put_to_jobview(chain, registered[0]) is True


@pytest.mark.parametrize("persisted, expected", [
    (TransferExecutionState.NOT_STARTED, "retry_wait"),
    (TransferExecutionState.RETRY_WAIT, "retry_wait"),
    (TransferExecutionState.MANUAL_REVIEW, "manual_review"),
    (None, "failed"),
])
@pytest.mark.parametrize("planned", [True, False])
def test_execution_failure_uses_remaining_durable_state(submission_chain, monkeypatch, persisted, expected, planned):
    """检查点前失败、延迟重试、人工复核与已清理损坏任务分别保留真实恢复语义。"""
    chain, fileitem = submission_chain

    def execute(task, callback):
        """模拟已准入任务在完成检查点前离开执行流程。"""
        assert callback
        task.bind_admission_task_id("test-submission")
        if planned:
            task.bind_plan_checkpoint(SimpleNamespace())
        raise RuntimeError("执行暂时失败")

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", execute)
    monkeypatch.setattr(chain, "_TransferChain__fail_transfer_task", lambda *args: None)
    chain.transfer_execution_repository = SimpleNamespace(
        get_snapshot=lambda **kwargs: SimpleNamespace(state=persisted) if persisted else None,
    )
    success, result = chain.manual_transfer(fileitem=fileitem, background=False, report_results=True)
    assert success is False
    assert result["items"][0]["state"] == expected
    if expected == "manual_review":
        assert "人工复核" in result["items"][0]["recovery_action"]
