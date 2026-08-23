"""关停型进程级单例的用例间复位（主程序与插件仓共享）。

提供一个 autouse 的 pytest fixture，把「一旦关停就不再接纳新任务」的进程级单例在每个
用例开始前复位到可接纳态。主程序 ``tests/conftest.py`` 与各插件仓 conftest 只需
``from app.testing.singleton_reset import reset_process_singletons`` 即复用同一道复位——
pytest 会把 conftest 命名空间内（含 import 进来的）fixture 一并识别，autouse 自动作用于
每个用例，无需逐用例改动。

这类单例的关停语义是生产要求的：登记器封口后仍留在发布位，才能拒掉晚到的 stop hook 任务；
阻塞执行器封口后仍拒绝提交，才能保证关停窗口内不再有新的同步调用挤进线程池。二者都没有
「关停后自动复原」的路径——进程只关停一次。测试进程却要在一个解释器里跑完整套用例，任何
一个用例走完真实关停路径，其后所有用例都会撞上封口。复位因此属于测试载具，而不是给生产
语义开的后门。

单例经 ``sys.modules`` 取用而不是 import：未被本进程加载过的模块，其单例仍是初始态，没有
可复位的内容；而 ``app.agent.tools.base`` 会牵入整条 LLM 依赖链，为复位而 import 会让每个
用例都付出这份代价。模块已加载时直接读取其公开符号，符号被改名会立刻 ``AttributeError``，
复位不会静默失效。

仅供测试使用，不参与运行时逻辑。
"""
from __future__ import annotations

import sys

import pytest

# 关停后不再接纳新任务、且没有自动复原路径的进程级单例所在模块
_TASK_REGISTRY_MODULE = "app.runtime.tasks"
_AGENT_TOOL_MODULE = "app.agent.tools.base"


def _publish_test_task_registry() -> None:
    """给当前用例发布一个独占的后台任务登记器。

    生产关停刻意把已封口的登记器继续留在发布位（见 ``stop_task_registry``），下一次
    ``initialize_task_registry`` 才显式换发。用例间按 lifespan 的同一方式换发新登记器，
    既保留封口语义本身，又让每个用例从可接纳态开始。
    """
    module = sys.modules.get(_TASK_REGISTRY_MODULE)
    if module is None:
        return
    module.configure_task_registry(module.TaskRegistry())


def _withdraw_test_task_registry() -> None:
    """撤回本用例的登记器，回到「未启动 lifespan」的兼容回退状态。"""
    module = sys.modules.get(_TASK_REGISTRY_MODULE)
    if module is None:
        return
    module.configure_task_registry(None)


def _reopen_agent_tool_executors() -> None:
    """重开 Agent 工具阻塞执行器的提交门禁。

    门禁只在旧 Future 与退休 executor 全部收敛后才会重开；返回 False 意味着上一个用例
    遗留了仍在运行的同步调用，此时保持关闭是正确的，本用例会以「执行器正在关闭」这一
    指名道姓的错误失败，而不是拿到一个仍被上个用例占着的线程池。
    """
    module = sys.modules.get(_AGENT_TOOL_MODULE)
    if module is None:
        return
    module.reopen_blocking_executors()


@pytest.fixture(autouse=True)
def reset_process_singletons():
    """用例开始前复位关停型进程级单例，收尾撤回本用例发布的登记器。"""
    _publish_test_task_registry()
    _reopen_agent_tool_executors()
    yield
    _withdraw_test_task_registry()
