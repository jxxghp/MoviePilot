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
        'from importlib import import_module as load\nresolve = load\nresolve("app.modules.qqbot")',
        'import importlib as lib\nload = lib.import_module\nload("app.modules.qqbot")',
        'kind = connection.provider\nname = "new-platform"\nif kind == name: pass',
        'kind = connection.channel\nmatch kind:\n    case "new-platform": pass',
        "import app.schemas.types as T\nkind = T.NotificationChannel.Slack",
        'mapping = {"new-platform": 1}\nkey = config.get("provider")\nhandler = mapping.get(key)',
        'actor.removeprefix("GID#")',
        'config.get("DISCORD_GUILD_ID")',
        'if connection.provider == "new-platform": pass',
        "if channel == NotificationChannel.Slack: pass",
        'kind = connection.provider\nif kind == "new-platform": pass',
        'kind = connection.channel\nother = kind\nif other in {"one", "two"}: pass',
        "from app.schemas.types import NotificationChannel as Transport\nchoice = Transport\nkind = choice.Slack",
        'mapping = {"new-platform": object()}\nkey = connection.provider\nhandler = mapping[key]',
        'mapping = {"new-platform": object()}\nalias = mapping\nkey = connection.channel\nhandler = alias.get(key)',
        'def route(connection):\n    target = connection.provider\n    if target == "fixture": return 1',
        'import importlib as loader\nhandler = loader.import_module("app.modules.qqbot")',
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
