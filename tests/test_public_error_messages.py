"""统一前台错误文案的转换规则测试。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.application.outbox import PostCommitEffectError, PostCommitResult
from app.factory import localized_unhandled_exception_handler
from app.runtime.errors import public_error_message
from app.schemas.history import TransferHistory
from app.schemas.response import Response


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("已提交重新整理，后台将自动处理", "已提交重新整理，后台将自动处理"),
        ("这条整理任务需要先完成人工确认，再重试", "这条整理任务需要先完成人工确认，再重试"),
        ("这条整理任务当前无法重试，请刷新后再试", "这条整理任务当前无法重试，请刷新后再试"),
        ("整理任务正在处理中，暂时无法放弃，请稍后重试", "整理任务正在处理中，暂时无法放弃，请稍后重试"),
    ],
)
def test_transfer_status_uses_human_readable_messages(source: str, expected: str) -> None:
    """整理状态不应把内部调度术语直接返回给用户。"""
    assert public_error_message(source, context="transfer") == expected


def test_technical_transfer_message_is_hidden() -> None:
    """整理检查点、操作身份等内部信息应统一降级为可执行提示。"""
    message = public_error_message(
        "整理步骤意图 operation_id=op-1 的 checkpoint evidence 无效",
        context="transfer",
    )

    assert message == "整理失败，请刷新后重试"
    assert "意图" not in message
    assert "checkpoint" not in message


def test_subscription_status_does_not_expose_provider_timeout() -> None:
    """订阅执行状态不应把服务商和超时实现细节展示给前端。"""
    assert (
        public_error_message("provider timeout", context="subscription")
        == "订阅操作失败，请刷新后重试"
    )


def test_post_commit_failure_explains_background_retry() -> None:
    """提交后的副作用失败应说明业务已进入后台补偿，而不是暴露 Outbox 术语。"""
    assert (
        public_error_message("提交后的相关处理未完成，系统将自动重试", context="outbox")
        == "提交后的相关处理未完成，系统将自动重试"
    )


def test_standard_response_applies_the_same_public_message_policy() -> None:
    """标准接口响应和显式调用转换函数必须使用同一套规则。"""
    response = Response(
        success=False,
        message="整理结果确认失败，后台将自动重试",
    )

    assert response.message == "整理结果确认失败，后台将自动重试"


def test_clear_business_message_is_preserved() -> None:
    """已有明确的业务提示不能因统一处理而丢失具体操作建议。"""
    assert public_error_message("源目录不存在：/downloads/demo") == "源目录不存在：/downloads/demo"


def test_transfer_history_hides_internal_error_in_nested_public_data() -> None:
    """整理历史的嵌套失败原因也不能绕过前台文案边界。"""
    history = TransferHistory(
        id=1,
        status=False,
        errmsg="整理步骤意图 operation_id=op-1 的 checkpoint evidence 无效",
    )

    assert history.errmsg == "整理失败，请刷新后重试"


def test_post_commit_error_keeps_background_retry_message() -> None:
    """未捕获的提交后效果异常应说明业务已提交且会自动补偿。"""
    error = PostCommitEffectError(
        PostCommitResult(value=None, business_committed=True),
        (RuntimeError("provider failed"),),
    )
    response = asyncio.run(
        localized_unhandled_exception_handler(
            SimpleNamespace(
                query_params={},
                headers={"accept-language": "zh-CN"},
            ),
            error,
        )
    )

    assert json.loads(response.body)["message"] == "提交后的相关处理未完成，系统将自动重试"
