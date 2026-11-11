"""
验证插件配置写入前对脱敏占位的识别边界。
"""

import pytest

from app.agent.policy.sanitizer import contains_redacted_placeholder


@pytest.mark.parametrize(("value", "expected"), [
    ({"cookie": "***"}, True),
    ({"nested": [{"credential": "session=***; user=demo"}]}, True),
    ({"note": "_tb_token_=***; status=ok"}, True),
    ({"endpoint": "https://***@example.invalid/path"}, True),
    ({"items": ["***"]}, True),
    ({"note": "***强调文本***"}, False),
    ({"note": "__moviepilot_redaction_probe__ ***强调文本***"}, False),
    ({"note": "value=***"}, False),
    ({"cookie": "session=synthetic-cookie"}, False),
    ({"count": 0, "enabled": False, "empty": None}, False),
])
def test_redacted_placeholder_detection_preserves_nonsecret_text(value, expected):
    """
    已遮蔽凭据不能写回，普通文本中的星号仍可作为业务内容。
    """
    assert contains_redacted_placeholder(value) is expected


def test_redacted_placeholder_detection_handles_cycles_and_shared_values():
    """
    循环结构不阻塞检查，共享对象进入敏感字段时仍须重新检查。
    """
    shared = {"value": "partly***masked"}
    cycle = {"plain": shared}
    cycle["self"] = cycle
    assert contains_redacted_placeholder(cycle) is False
    cycle["cookie"] = shared
    assert contains_redacted_placeholder(cycle) is True
