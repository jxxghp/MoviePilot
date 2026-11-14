"""
反射工具公开注解参与运行时签名校验的兼容性回归。
"""

import pytest

from app.foundation.reflection import ObjectUtils


@pytest.mark.parametrize("value, expected", [(lambda: None, True), (len, True), (1, False)])
def test_argument_counter_preserves_runtime_callable_check(value, expected):
    """
    参数计数器补齐静态类型后，仍能作为签名校验的目标函数。
    """
    assert ObjectUtils.check_signature(ObjectUtils.arguments, value) is expected


def test_signature_checker_preserves_function_annotation():
    """
    签名校验器仍保留函数对象注解，同时继续支持检查内建可调用对象。
    """
    assert ObjectUtils.check_signature(ObjectUtils.check_signature, lambda: None, 1)
    assert not ObjectUtils.check_signature(ObjectUtils.check_signature, len, 1)
    assert ObjectUtils.check_signature(len, [])
