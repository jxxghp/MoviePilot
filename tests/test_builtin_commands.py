"""内建命令清单：条目形状、业务链的物化时机与清单缺失时的失败方式。

内建命令词与业务实现的绑定归组合根持有，命令中枢只按条目形状执行。本文件锁住三件事：
清单本身不物化任何业务链、业务链在首次执行该命令时才物化且此后复用、清单未装配时命令
中枢立刻报错而不是交出一张空命令表。
"""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.runtime.command as command_module
from app.runtime.command import Command, _command_callable, _resolve_builtin_commands
from app.runtime.extensions.admission.command_arbitration import BuiltinCommandArbiter
from app.runtime.extensions.projection.command import PluginCommandTable
from app.startup.bindings.builtin_commands import builtin_commands

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
            "app.startup.bindings.builtin_commands." + name,
            Mock(side_effect=lambda *_, _n=name, **__: constructed.append(_n)),
        )

    builtin_commands()

    assert constructed == []


def test_business_chain_is_materialized_on_first_execution_and_reused(monkeypatch):
    """业务链在首次执行该命令时才物化，同一命令的后续执行复用同一个实例。"""
    chain = SimpleNamespace(remote_list=lambda: None)
    factory = Mock(return_value=chain)
    monkeypatch.setattr("app.startup.bindings.builtin_commands.SiteChain", factory)
    entry = builtin_commands()["/sites"]
    assert factory.call_count == 0

    first = _command_callable(entry)
    second = _command_callable(entry)

    assert factory.call_count == 1
    assert first is second is chain.remote_list


def test_scheduler_command_starts_its_job_without_arguments(monkeypatch):
    """定时任务型命令的实现不接收任何参数，只按任务标识启动定时服务。"""
    scheduler = Mock()
    monkeypatch.setattr("app.startup.bindings.builtin_commands.Scheduler", Mock(return_value=scheduler))

    _command_callable(builtin_commands()["/transfer"])()

    scheduler.start.assert_called_once_with(job_id="transfer")


def test_scheduler_command_does_not_touch_the_scheduler_until_it_runs(monkeypatch):
    """定时任务型条目在构造清单时不解析定时服务。"""
    scheduler_class = Mock()
    monkeypatch.setattr("app.startup.bindings.builtin_commands.Scheduler", scheduler_class)

    builtin_commands()

    scheduler_class.assert_not_called()


def test_command_hub_fails_loudly_when_the_builtin_list_is_not_composed(monkeypatch):
    """清单未装配时立刻报错，不能静默交出一张没有内建命令的命令表。"""
    monkeypatch.setattr(command_module, "_builtin_commands_provider", None)

    with pytest.raises(RuntimeError) as excinfo:
        _resolve_builtin_commands()

    assert "内建命令清单未注册" in str(excinfo.value)


def test_command_hub_fails_loudly_when_the_message_gateway_is_not_composed(monkeypatch):
    """消息网关未装配时立刻报错，不能让命令回复与菜单广播静默落空。"""
    monkeypatch.setattr(command_module, "_command_messenger_provider", None)

    with pytest.raises(RuntimeError) as excinfo:
        command_module._messenger()

    assert "命令消息网关未注册" in str(excinfo.value)


def _command_hub(preset: dict) -> Command:
    """构造只挂内建命令的命令中枢测试对象。

    :param preset: 内建命令表
    :return: 命令中枢测试对象
    """
    hub = object.__new__(Command)
    hub._preset_commands = preset
    hub._plugin_table = PluginCommandTable(
        builtin_command_words=lambda: hub._preset_commands,
        event_sender=Command.send_plugin_event,
        arbiter=BuiltinCommandArbiter(log=SimpleNamespace(warning=lambda _: None)),
    )
    hub._other_commands = {}
    hub._commands = {}
    hub._rlock = threading.RLock()
    return hub


def test_executing_a_builtin_business_command_passes_the_call_context():
    """命令中枢按解析出的实现的签名传入本次调用的上下文。"""
    received = []
    hub = _command_hub({
        "/sites": {
            "provider": lambda: (
                lambda channel, userid, source: received.append((channel, userid, source))
            ),
            "description": "管理站点",
            "data": {},
        }
    })

    hub.execute(cmd="/sites", userid="u1", source="s1")

    assert received == [(None, "u1", "s1")]


def test_executing_a_scheduler_command_frames_the_run_with_progress_messages(monkeypatch):
    """定时任务型命令由命令中枢发出开始与完成提示，实现本身不接收参数。"""
    runs = []
    messenger = Mock()
    monkeypatch.setattr(command_module, "_command_messenger_provider", lambda: messenger)
    hub = _command_hub({
        "/transfer": {
            "id": "transfer",
            "type": "scheduler",
            "func": lambda: runs.append("ran"),
            "description": "下载文件整理",
        }
    })

    hub.execute(cmd="/transfer", userid="u1")

    assert runs == ["ran"]
    titles = [call.args[0].title for call in messenger.post_message.call_args_list]
    assert titles == ["开始执行 下载文件整理 ...", "下载文件整理 执行完成"]


def test_command_failure_is_reported_through_the_message_gateway(monkeypatch):
    """命令执行出错时经消息网关留下系统提示，用户在消息中心看得到。"""
    messenger = Mock()
    monkeypatch.setattr(command_module, "_command_messenger_provider", lambda: messenger)
    hub = _command_hub({
        "/restart": {
            "provider": lambda: (lambda: (_ for _ in ()).throw(RuntimeError("炸了"))),
            "description": "重启系统",
            "data": {},
        }
    })

    hub.execute(cmd="/restart", userid="u1")

    messenger.put_system_message.assert_called_once_with(
        title="执行命令 /restart 出错", message="炸了"
    )
