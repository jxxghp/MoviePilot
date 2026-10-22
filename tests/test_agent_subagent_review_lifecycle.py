"""
通过已注册子任务工具验证关闭、完成和更新的真实异步生命周期。

仅替换外部模型提供端，任务创建、取消、完成回调和状态生成均使用正式实现。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agent.middleware import subagents


def _controller(provider):
    """
    构造正式控制器，把远端模型边界替换为事件驱动的本地协程。
    """
    controller = subagents.SubAgentTaskControlMiddleware(model=None, profiles=(), tools=[])
    controller._provider = SimpleNamespace(run_task=provider)
    return controller


async def _call(controller, **arguments):
    """
    从正式注册的 StructuredTool 入口调用并读取公开 JSON 响应。
    """
    return json.loads(await controller.tools[0].ainvoke(arguments))


@pytest.mark.asyncio
async def test_repeated_close_retains_owner_and_does_not_cancel_cleanup_twice(monkeypatch):
    """
    关闭超时保留真实任务，只取消一次，拒绝更新，收敛后可完成最终关闭。
    """
    monkeypatch.setattr(subagents, "SUBAGENT_CANCEL_GRACE_SECONDS", 0.01)
    started, release = asyncio.Event(), asyncio.Event()
    cancellations = []

    async def provider(**_kwargs):
        """
        延迟取消清理，以观察重复关闭是否打断仍在途的同一任务。
        """
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellations.append("cancel")
        return "清理完成"

    controller = _controller(provider)
    try:
        started_result = await _call(controller, action="start", description="持有清理任务")
        task_id = started_result["tasks"][0]["task_id"]
        await asyncio.wait_for(started.wait(), 1)
        assert await controller.close() is False
        status = await _call(controller, action="status", task_id=task_id)
        assert status["tasks"][0]["status"] == "running"
        assert status["tasks"][0]["execution_outcome"] == "pending"
        updated = await _call(controller, action="update", task_id=task_id, description="不得替换")
        assert updated["success"] is False
        assert "关闭" in updated["error"]
        assert updated["tasks"][0]["description"] == "持有清理任务"
        assert await controller.close() is False
        assert cancellations == ["cancel"]
        release.set()
        finished = await _call(controller, action="wait", task_id=task_id, timeout_ms=1000)
        assert finished["tasks"][0]["result"] == "清理完成"
        # 已完成任务的wait可立即返回，完成回调在下一轮事件循环后发布结束时间。
        await asyncio.sleep(0)
        status = await _call(controller, action="status", task_id=task_id)
        assert status["tasks"][0]["finished_at"] is not None
        assert await controller.close() is True
        assert (await _call(controller, action="status"))["tasks"] == []
    finally:
        release.set()
        await controller.close()


@pytest.mark.asyncio
async def test_update_timeout_keeps_original_task_and_description(monkeypatch):
    """
    旧任务尚未完成取消时公开更新返回失败，不改描述或产生第二个执行者。
    """
    monkeypatch.setattr(subagents, "SUBAGENT_CANCEL_GRACE_SECONDS", 0.01)
    started, release = asyncio.Event(), asyncio.Event()
    descriptions = []

    async def provider(*, description, **_kwargs):
        """
        保留取消中的旧执行者，直到测试显式允许其清理完成。
        """
        descriptions.append(description)
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return description

    controller = _controller(provider)
    try:
        started_result = await _call(controller, action="start", description="原始任务")
        task_id = started_result["tasks"][0]["task_id"]
        await asyncio.wait_for(started.wait(), 1)
        updated = await _call(controller, action="update", task_id=task_id, description="替代任务")
        assert updated["success"] is False
        assert "取消窗口" in updated["error"]
        assert updated["execution_outcome"] == "failed"
        assert updated["tasks"][0]["description"] == "原始任务"
        assert updated["tasks"][0]["task_id"] == task_id
        assert descriptions == ["原始任务"]
        release.set()
        finished = await _call(controller, action="wait", task_id=task_id, timeout_ms=1000)
        assert finished["tasks"][0]["result"] == "原始任务"
    finally:
        release.set()
        await controller.close()


@pytest.mark.asyncio
async def test_failed_task_remains_observable_until_controller_close():
    """
    提供端执行失败经公开等待和查询返回失败证据，完成后关闭仍能收口。
    """
    async def provider(**_kwargs):
        """
        在模型边界报告确定失败，使用真实任务传播和完成回调。
        """
        raise LookupError("测试模型执行失败")

    controller = _controller(provider)
    try:
        completed = await _call(controller, action="run", description="失败任务", timeout_ms=1000)
        task = completed["tasks"][0]
        assert task["status"] == "failed"
        assert task["execution_outcome"] == "failed"
        assert "测试模型执行失败" in task["error"]
        assert task["finished_at"] is not None
        observed = await _call(controller, action="wait", task_id=task["task_id"], timeout_ms=1000)
        assert observed["tasks"] == completed["tasks"]
        assert await controller.close() is True
        assert (await _call(controller, action="status"))["tasks"] == []
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_completed_task_can_update_same_id_and_publish_new_completion():
    """
    完成后的任务可复用ID更新，旧完成时间不会冒充新任务完成状态。
    """
    replacement_started, release = asyncio.Event(), asyncio.Event()
    descriptions = []

    async def provider(*, description, **_kwargs):
        """
        首轮立即完成，替代任务通过事件固定在运行中便于查询。
        """
        descriptions.append(description)
        if description == "替代任务":
            replacement_started.set()
            await release.wait()
        return f"{description}结果"

    controller = _controller(provider)
    try:
        completed = await _call(controller, action="run", description="原始任务", timeout_ms=1000)
        first = completed["tasks"][0]
        assert first["status"] == "completed" and first["finished_at"] is not None
        updated = await _call(controller, action="update", task_id=first["task_id"], description="替代任务")
        assert updated["success"] is True
        assert updated["task_id"] == first["task_id"]
        await asyncio.wait_for(replacement_started.wait(), 1)
        status = await _call(controller, action="status", task_id=first["task_id"])
        assert status["tasks"][0]["status"] == "running"
        assert status["tasks"][0]["finished_at"] is None
        release.set()
        finished = await _call(controller, action="wait", task_id=first["task_id"], timeout_ms=1000)
        assert finished["tasks"][0]["status"] == "completed"
        assert finished["tasks"][0]["result"] == "替代任务结果"
        await asyncio.sleep(0)
        status = await _call(controller, action="status", task_id=first["task_id"])
        assert status["tasks"][0]["finished_at"] is not None
        assert descriptions == ["原始任务", "替代任务"]
    finally:
        release.set()
        await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments,expected_error", [
    ({"description": "新任务"}, "必须提供"),
    ({"task_id": "missing", "description": "  "}, "必须提供"),
    ({"task_id": "missing", "description": "新任务"}, "未知子代理任务"),
])
async def test_invalid_public_update_returns_recovery_without_starting(arguments, expected_error):
    """
    缺少更新身份、空描述和未知任务均返回恢复指引，模型边界不得被调用。
    """
    async def provider(**_kwargs):
        """
        无效更新若启动了执行者即报告测试失败。
        """
        raise AssertionError("无效更新不得启动子任务")

    controller = _controller(provider)
    try:
        response = await _call(controller, action="update", **arguments)
        assert response["success"] is False
        assert response["execution_outcome"] == "failed"
        assert expected_error in response["error"]
        assert response["recovery"]
        assert (await _call(controller, action="status"))["tasks"] == []
    finally:
        await controller.close()
