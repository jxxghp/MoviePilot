"""内建命令清单：条目形状、业务链的物化时机与清单缺失时的失败方式。

内建命令词与业务实现的绑定归组合根持有，命令中枢只按条目形状执行。本文件锁住三件事：
清单本身不物化任何业务链、业务链在首次执行该命令时才物化且此后复用、清单未装配时命令
中枢立刻报错而不是交出一张空命令表。
"""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.command as command_module
from app.command import Command, _command_callable, _resolve_builtin_commands
from app.runtime.extensions.admission.command_arbitration import BuiltinCommandArbiter
from app.startup.builtin_commands import builtin_commands

# 内建清单里由业务链提供实现的命令词
BUSINESS_COMMANDS = (
    "/sites",
    "/subscribes",
    "/downloading",
    "/redo",
    "/clear_cache",
    "/restart",
    "/version",
    "/clear_session",
    "/stop_agent",
    "/session_status",
    "/skills",
)
# 内建清单里转交定时服务执行的命令词
SCHEDULER_COMMANDS = ("/cookiecloud", "/mediaserver_sync", "/transfer")


def test_builtin_command_words_cover_both_shapes():
    """内建清单只有业务实现型与定时任务型两种条目，没有第三种形状。"""
    table = builtin_commands()

    assert set(table) == set(BUSINESS_COMMANDS) | set(SCHEDULER_COMMANDS)


@pytest.mark.parametrize("cmd", BUSINESS_COMMANDS)
def test_business_command_carries_a_resolver_instead_of_a_bound_implementation(cmd: str):
    """业务实现型条目交出解析器，构造清单时不绑定实现。"""
    entry = builtin_commands()[cmd]

    assert callable(entry["provider"])
    assert "func" not in entry


@pytest.mark.parametrize("cmd", SCHEDULER_COMMANDS)
def test_scheduler_command_carries_its_job_id_and_type(cmd: str):
    """定时任务型条目带任务标识与类型，调用方据此分辨两种形状。"""
    entry = builtin_commands()[cmd]

    assert entry["type"] == "scheduler"
    assert entry["id"] == cmd[1:]
    assert callable(entry["func"])


def test_building_the_builtin_list_materializes_no_business_chain(monkeypatch):
    """构造内建清单不得物化任何业务链，否则命令中枢一建起来就把七条链全拉起。"""
    constructed = []
    for name in ("SiteChain", "SubscribeChain", "DownloadChain", "TransferChain",
                 "SystemChain", "MessageChain", "SkillInteractionHandler", "CommandChain"):
        monkeypatch.setattr(
            "app.startup.builtin_commands." + name,
            Mock(side_effect=lambda *_, _n=name, **__: constructed.append(_n)),
        )

    builtin_commands()

    assert constructed == []


def test_business_chain_is_materialized_on_first_execution_and_reused(monkeypatch):
    """业务链在首次执行该命令时才物化，同一命令的后续执行复用同一个实例。"""
    chain = SimpleNamespace(remote_list=lambda: None)
    factory = Mock(return_value=chain)
    monkeypatch.setattr("app.startup.builtin_commands.SiteChain", factory)
    entry = builtin_commands()["/sites"]
    assert factory.call_count == 0

    first = _command_callable(entry)
    second = _command_callable(entry)

    assert factory.call_count == 1
    assert first is second is chain.remote_list


def test_scheduler_command_starts_its_job_without_arguments(monkeypatch):
    """定时任务型命令的实现不接收任何参数，只按任务标识启动定时服务。"""
    scheduler = Mock()
    monkeypatch.setattr("app.startup.builtin_commands.Scheduler", Mock(return_value=scheduler))

    _command_callable(builtin_commands()["/transfer"])()

    scheduler.start.assert_called_once_with(job_id="transfer")


def test_scheduler_command_does_not_touch_the_scheduler_until_it_runs(monkeypatch):
    """定时任务型条目在构造清单时不解析定时服务。"""
    scheduler_class = Mock()
    monkeypatch.setattr("app.startup.builtin_commands.Scheduler", scheduler_class)

    builtin_commands()

    scheduler_class.assert_not_called()


def test_command_hub_fails_loudly_when_the_builtin_list_is_not_composed(monkeypatch):
    """清单未装配时立刻报错，不能静默交出一张没有内建命令的命令表。"""
    monkeypatch.setattr(command_module, "_builtin_commands_provider", None)

    with pytest.raises(RuntimeError) as excinfo:
        _resolve_builtin_commands()

    assert "内建命令清单未注册" in str(excinfo.value)


def test_executing_a_builtin_business_command_passes_the_call_context():
    """命令中枢按解析出的实现的签名传入本次调用的上下文。"""
    received = []
    hub = object.__new__(Command)
    hub._preset_commands = {
        "/sites": {
            "provider": lambda: (
                lambda channel, userid, source: received.append((channel, userid, source))
            ),
            "description": "管理站点",
            "data": {},
        }
    }
    hub._plugin_commands = {}
    hub._declined_plugin_commands = {}
    hub._plugin_command_revision = -1
    hub._builtin_arbiter = BuiltinCommandArbiter(log=SimpleNamespace(warning=lambda _: None))
    hub._other_commands = {}
    hub._commands = {}
    hub._rlock = threading.RLock()
    hub.messagehelper = SimpleNamespace(put=lambda **_: None)

    hub.execute(cmd="/sites", userid="u1", source="s1")

    assert received == [(None, "u1", "s1")]
