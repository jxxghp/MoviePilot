"""
执行渠道核心架构约束，并验证规则能够拒绝重新引入的协议耦合。
"""

import pytest

from scripts.architecture.channel_platforms import source_violations, violations


def test_channel_core_has_no_platform_protocol_dependencies():
    """
    平台协议只允许在模块、已验证的装配注册和固定离线迁移中实现。
    """
    assert violations() == []


@pytest.mark.parametrize(
    "source",
    [
        "from app.modules.qqbot.channel import QQPlatform",
        'actor.removeprefix("GID#")',
        'config.get("DISCORD_GUILD_ID")',
        'if connection.provider == "new-platform": pass',
        "if channel == NotificationChannel.Slack: pass",
    ],
)
def test_guard_rejects_protocol_and_concrete_platform_dispatch(source):
    """
    修改文件位置或增加新平台名称都不能绕过契约检查。
    """
    assert source_violations(source)


def test_guard_allows_documentation_and_generic_platform_contracts():
    """
    文档提到历史协议和通用能力比较不构成平台解释。
    """
    assert (
        source_violations('''"""QQ group: 是历史问题。"""
result = platform.actor(value)
if platform.provider == connection.provider:
    result = domain.authorize(result)
''')
        == []
    )
