"""
工作流上下文兼容识别的独立边界：普通文本完整恢复，旧载荷只静态识别。
"""

import base64
import pickle

import pytest

from app.chain import workflow as workflow_module
from tests.test_workflow_execution import _build_workflow, _FakeWorkflowManager


@pytest.mark.parametrize("content", [
    "gA==",
    base64.b64encode(b"\x80\x05").decode(),
    base64.b64encode(b"\x80\x05invalid").decode(),
    base64.b64encode(b"\x80\x05N.trailing").decode(),
    base64.b64encode(b"\x80\xffN.").decode(),
    base64.b64encode(b"\x80\x04.").decode(),
])
def test_pickle_like_plain_text_survives_real_context_restore(content, monkeypatch):
    """
    看似协议头的合法文本及损坏载荷不得被当成有效旧包装丢弃。
    """
    monkeypatch.setattr(workflow_module, "get_workflow_manager", lambda: _FakeWorkflowManager([]))
    executor = workflow_module.WorkflowExecutor(_build_workflow(context={"content": content}))
    try:
        assert executor.context.content == content
    finally:
        executor.executor.shutdown_bounded(timeout=1)


@pytest.mark.parametrize("protocol", [2, 4, 5])
def test_real_legacy_context_is_identified_without_loading(protocol, monkeypatch):
    """
    完整旧协议包装仍从空上下文恢复，识别流程不得执行反序列化。
    """
    content = base64.b64encode(pickle.dumps({"legacy": "旧上下文"}, protocol=protocol)).decode()

    def reject_load(*_args, **_kwargs):
        """
        任何尝试执行载荷的行为都直接使安全回归失败。
        """
        raise AssertionError("旧载荷不得反序列化")

    monkeypatch.setattr(pickle, "loads", reject_load)
    monkeypatch.setattr(pickle, "load", reject_load)
    monkeypatch.setattr(workflow_module, "get_workflow_manager", lambda: _FakeWorkflowManager([]))
    executor = workflow_module.WorkflowExecutor(_build_workflow(context={"content": content}))
    try:
        assert executor.context.content is None
    finally:
        executor.executor.shutdown_bounded(timeout=1)
