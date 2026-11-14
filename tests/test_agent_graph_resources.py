"""
验证构建、图替换、取消和关闭之间的真实资源所有权转换。
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent.graph import AgentGraphCache, CompiledAgentBundle, GraphResources
from app.agent.llm.helper import LLMHelper


@pytest.fixture
def anyio_backend():
    """
    生命周期屏障使用 asyncio 事件循环。
    """
    return "asyncio"


def _bundle(resources: GraphResources, revision: int = 0) -> CompiledAgentBundle:
    """
    以真实资源范围构造不同签名的占位执行图。
    """
    return CompiledAgentBundle(
        signature=(revision,), agent=object(), streaming=False,
        created_at=datetime.now(), resources=resources,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [None, RuntimeError("构建失败"), asyncio.CancelledError()])
async def test_unpublished_build_closes_consumers_before_models(monkeypatch, failure):
    """
    正常复核返回、异常和取消均关闭未发布范围，消费者必须先于模型退出。
    """
    events = []
    cache = AgentGraphCache(models_in_use=lambda: False)
    model = object()

    async def close_consumer():
        """
        记录消费者退出，验证其使用模型期间不会提前释放连接。
        """
        events.append("consumer")
        return True

    async def close_model(value):
        """
        只允许关闭本范围接管的模型。
        """
        assert value is model
        events.append("model")
        return True

    monkeypatch.setattr(LLMHelper, "close_llm", close_model)

    async def build():
        """
        在登记完整资源后模拟三种构图退出路径。
        """
        async with cache.build() as resources:
            resources.own_model(model)
            resources.own_model(model)
            resources.own_middlewares([SimpleNamespace(close=close_consumer), object()])
            if failure is not None:
                raise failure

    if failure is None:
        await build()
    else:
        with pytest.raises(type(failure)):
            await build()
    assert events == ["consumer", "model"]
    assert await cache.invalidate() is True
    assert events == ["consumer", "model"]


@pytest.mark.anyio
async def test_cancel_during_post_publish_collection_keeps_new_graph_alive(monkeypatch):
    """
    新图已发布后取消旧图回收，不能把新图误当作未提交的临时资源关闭。
    """
    cache = AgentGraphCache(models_in_use=lambda: False)
    old_model, new_model = object(), object()
    started, release = asyncio.Event(), asyncio.Event()
    closed = []

    async def close_model(model):
        """
        在旧模型关闭前阻塞，使取消准确落在图发布后的回收阶段。
        """
        if model is old_model:
            started.set()
            await release.wait()
        closed.append(model)
        return True

    monkeypatch.setattr(LLMHelper, "close_llm", close_model)
    async with cache.build() as resources:
        resources.own_model(old_model)
        cache.publish(_bundle(resources))

    async def replace():
        """
        在真实构建范围内同步发布新图，再由退出逻辑回收旧图。
        """
        async with cache.build() as replacement:
            replacement.own_model(new_model)
            cache.publish(_bundle(replacement, 1))

    task = asyncio.create_task(replace())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cache.get((1,), False) is cache.current.agent
    assert closed == []
    release.set()
    await cache.collect()
    assert closed == [old_model]
    assert await cache.invalidate() is True
    assert closed == [old_model, new_model]


@pytest.mark.anyio
@pytest.mark.parametrize("first_result", [False, RuntimeError("关闭失败")])
async def test_pending_consumers_retain_only_their_own_models(monkeypatch, first_result):
    """
    某代消费者未退出时保留对应模型，但不能阻塞另一独立范围释放。
    """
    cache = AgentGraphCache(models_in_use=lambda: False)
    close_model = AsyncMock(return_value=True)
    monkeypatch.setattr(LLMHelper, "close_llm", close_model)
    model, independent_model = object(), object()
    consumer = SimpleNamespace(close=AsyncMock(side_effect=[first_result, False, True]))
    async with cache.build() as resources:
        resources.own_model(model)
        resources.own_middlewares([consumer])
    close_model.assert_not_awaited()
    async with cache.build() as independent:
        independent.own_model(independent_model)
    close_model.assert_awaited_once_with(independent_model)
    assert await cache.invalidate() is True
    assert [call.args[0] for call in close_model.await_args_list] == [independent_model, model]


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["consumer", "model"])
async def test_interrupted_collection_can_retry_without_losing_resources(monkeypatch, phase):
    """
    消费者或模型关闭中再次取消，资源必须仍由原 owner 保留并可重试。
    """
    cache = AgentGraphCache(models_in_use=lambda: False)
    started, release = asyncio.Event(), asyncio.Event()
    model = object()

    async def blocked_close(*_args):
        """
        在真实关闭边界等待，用屏障代替不稳定的时间等待。
        """
        started.set()
        await release.wait()
        return True

    consumer = SimpleNamespace(close=AsyncMock(return_value=True))
    close_model = AsyncMock(return_value=True)
    if phase == "consumer":
        consumer.close.side_effect = blocked_close
    else:
        close_model.side_effect = blocked_close
    monkeypatch.setattr(LLMHelper, "close_llm", close_model)
    async with cache.build() as resources:
        resources.own_model(model)
        resources.own_middlewares([consumer])
        cache.publish(_bundle(resources))
    task = asyncio.create_task(cache.invalidate())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cache.current is None
    assert not resources.empty
    release.set()
    assert await cache.invalidate() is True
    assert resources.empty
    assert consumer.close.await_count == (2 if phase == "consumer" else 1)
    assert close_model.await_count == (1 if phase == "consumer" else 2)


@pytest.mark.anyio
async def test_shutdown_tracks_inflight_build_and_seals_late_controller(monkeypatch):
    """
    关停与构建交错时不提前释放在用模型，迟到控制器立即封口，迟到图拒绝发布。
    """
    cache = AgentGraphCache(models_in_use=lambda: False)
    close_model = AsyncMock(return_value=True)
    monkeypatch.setattr(LLMHelper, "close_llm", close_model)
    model = object()
    consumer = SimpleNamespace(seal=Mock(), close=AsyncMock(return_value=True))
    async with cache.build() as resources:
        resources.own_model(model)
        cache.seal()
        assert await cache.invalidate() is False
        close_model.assert_not_awaited()
        resources.own_middlewares([consumer])
        consumer.seal.assert_called_once()
        with pytest.raises(RuntimeError, match="正在关闭"):
            cache.publish(_bundle(resources))
    close_model.assert_awaited_once_with(model)
    assert await cache.invalidate() is True
    with pytest.raises(RuntimeError, match="正在关闭"):
        async with cache.build():
            pytest.fail("关闭后不得进入构建范围")


@pytest.mark.anyio
async def test_concurrent_collection_does_not_double_close(monkeypatch):
    """
    两个清理请求并发时只执行一次实际模型关闭。
    """
    cache = AgentGraphCache(models_in_use=lambda: False)
    started, release = asyncio.Event(), asyncio.Event()

    async def close_model(_model):
        """
        保持首次关闭在途，让第二次清理与其真实交错。
        """
        started.set()
        await release.wait()
        return True

    close = AsyncMock(side_effect=close_model)
    monkeypatch.setattr(LLMHelper, "close_llm", close)
    async with cache.build() as resources:
        resources.own_model(object())
        cache.publish(_bundle(resources))
    first = asyncio.create_task(cache.invalidate())
    await started.wait()
    second = asyncio.create_task(cache.invalidate())
    release.set()
    assert await asyncio.gather(first, second) == [True, True]
    close.assert_awaited_once()


@pytest.mark.anyio
async def test_retirement_during_collection_keeps_shared_model_for_late_consumer(monkeypatch):
    """
    清理等待期间另一图退休时，也必须等待其消费者退出后再释放共享模型。
    """
    cache = AgentGraphCache(models_in_use=lambda: False)
    started, release = asyncio.Event(), asyncio.Event()
    model = object()
    close = AsyncMock(return_value=True)
    monkeypatch.setattr(LLMHelper, "close_llm", close)

    async def close_old_consumer():
        """
        阻塞旧消费者退出，为新图退休提供确定的交错点。
        """
        started.set()
        await release.wait()
        return True

    async with cache.build() as resources:
        resources.own_model(model)
        resources.own_middlewares([SimpleNamespace(close=close_old_consumer)])
        cache.publish(_bundle(resources))
    cache.retire()
    newer = GraphResources()
    consumer = SimpleNamespace(close=AsyncMock(return_value=True))
    newer.own_model(model)
    newer.own_middlewares([consumer])
    cache.publish(_bundle(newer, 1))
    collecting = asyncio.create_task(cache.collect())
    await started.wait()
    cache.retire()
    release.set()
    await collecting
    close.assert_not_awaited()
    consumer.close.assert_not_awaited()
    assert await cache.invalidate() is True
    consumer.close.assert_awaited_once()
    close.assert_awaited_once_with(model)


@pytest.mark.anyio
async def test_repeated_replacement_preserves_shared_resources_and_releases_once(monkeypatch):
    """
    多轮替换共享同一模型与控制器时，只有最终失效才释放各一次。
    """
    cache = AgentGraphCache(models_in_use=lambda: False)
    model = object()
    consumer = SimpleNamespace(close=AsyncMock(return_value=True))
    close = AsyncMock(return_value=True)
    monkeypatch.setattr(LLMHelper, "close_llm", close)
    for revision in range(20):
        async with cache.build() as resources:
            resources.own_model(model)
            resources.own_middlewares([consumer])
            bundle = _bundle(resources, revision)
            cache.publish(bundle)
            cache.publish(bundle)
        assert cache.get((revision,), False) is bundle.agent
        assert cache.get((revision,), True) is None
        close.assert_not_awaited()
        consumer.close.assert_not_awaited()
    assert await cache.invalidate() is True
    close.assert_awaited_once_with(model)
    consumer.close.assert_awaited_once()


@pytest.mark.anyio
async def test_learning_and_close_failure_keep_resource_until_both_converge(monkeypatch):
    """
    后台复盘退出后才能尝试关闭；关闭失败仍保留资源至后续成功。
    """
    learning = asyncio.get_running_loop().create_future()
    cache = AgentGraphCache(models_in_use=lambda: not learning.done())
    close = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(LLMHelper, "close_llm", close)
    async with cache.build() as resources:
        resources.own_model(object())
        cache.publish(_bundle(resources))
    assert await cache.invalidate() is False
    close.assert_not_awaited()
    learning.set_result(None)
    assert await cache.invalidate() is False
    assert not resources.empty
    assert await cache.invalidate() is True
    assert resources.empty
    assert close.await_count == 2
