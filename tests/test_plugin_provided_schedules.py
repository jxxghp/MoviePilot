"""插件声明定时任务链路测试：契约校验、聚合归属、调度器登记与回收、旧钩子并存。"""

import json
import threading
from dataclasses import fields
from typing import Iterator, List, Optional

import pytest
from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.cron import CronTrigger

from app.foundation.singleton import Singleton
from app.runtime.deprecation import notices as notices_module
from app.runtime.deprecation import policy as deprecation_policy
from app.runtime.deprecation.notices import DeprecationNotice, DeprecationStage
from app.runtime.extensions.contract.declaration import ScheduleDeclaration
from app.runtime.extensions.projection import plugin as projection_module
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.plugin_manager import PluginManager
from app.scheduler import Scheduler


def _run() -> bool:
    """契约合规的任务实现：不接收任何参数，回显执行成功。"""
    return True


def _daily() -> ScheduleDeclaration:
    """构造一条契约合规的每日 cron 任务声明。"""
    return ScheduleDeclaration(
        job_id="sync",
        name="定时同步",
        trigger="cron",
        trigger_args={"crontab": "0 1 * * *"},
        impl=_run,
    )


@pytest.fixture(autouse=True)
def _clean_deprecation_warned() -> Iterator[None]:
    """每个用例前后都清空废弃告警去重记录，避免用例间互相掩盖。"""
    deprecation_policy.reset_warned()
    yield
    deprecation_policy.reset_warned()


@pytest.fixture(autouse=True)
def _clean_overlap_hints() -> Iterator[None]:
    """每个用例前后都清空新旧来源重叠提示的去重记录。"""
    projection_module._schedule_source_overlap_hints_seen.clear()
    yield
    projection_module._schedule_source_overlap_hints_seen.clear()


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def _set_notice_stage(monkeypatch, key: str, stage: DeprecationStage) -> None:
    """把指定废弃标识的登记替换为指定阶段的副本，其余登记原样保留。"""
    original = notices_module.NOTICES[key]
    updated = dict(notices_module.NOTICES)
    updated[key] = DeprecationNotice(
        key=original.key,
        subject=original.subject,
        stage=stage,
        since=original.since,
        remove_in=original.remove_in,
        replacement=original.replacement,
        reason=original.reason,
    )
    monkeypatch.setattr(notices_module, "NOTICES", updated)
    monkeypatch.setattr(deprecation_policy, "NOTICES", updated)


class _RecordingLogger:
    """只记录消息文本的日志端口替身，用于断言违约原因是否可读。"""

    def __init__(self) -> None:
        """初始化各级别消息记录。"""
        self.errors: List[str] = []
        self.infos: List[str] = []

    def error(self, message: str) -> None:
        """记录一条错误消息。"""
        self.errors.append(str(message))

    def info(self, message: str) -> None:
        """记录一条提示消息。"""
        self.infos.append(str(message))

    def warning(self, message: str) -> None:
        """记录一条告警消息。"""
        self.infos.append(str(message))


class _SchedulePlugin:
    """既声明新式定时任务又实现旧式钩子的插件桩，用于驱动投影与调度器。"""

    plugin_name = "定时插件"

    def __init__(
        self,
        declarations: Optional[List[ScheduleDeclaration]] = None,
        legacy: Optional[list] = None,
        enabled: bool = True,
        raise_error: bool = False,
    ):
        self._declarations = declarations
        self._legacy = legacy
        self._enabled = enabled
        self._raise_error = raise_error

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def get_name(self) -> str:
        """返回插件展示名称。"""
        return self.plugin_name

    def provides_schedules(self):
        """返回声明的定时任务，或按需抛出异常模拟插件实现出错。"""
        if self._raise_error:
            raise RuntimeError("声明定时任务时出错")
        return self._declarations

    def get_service(self):
        """返回旧式裸描述字典列表；未配置时返回 None 表示未提供。"""
        return self._legacy


# --------------------------------------------------------------------------
# 契约校验
# --------------------------------------------------------------------------


def test_projection_accepts_valid_declaration():
    """契约合规的声明应被接受，字段原样保留。"""
    plugin = _SchedulePlugin(declarations=[_daily()])
    projection = PluginProjection({"Demo": plugin})

    declared = projection.provided_schedules()

    assert len(declared["Demo"]) == 1
    assert declared["Demo"][0].job_id == "sync"


def test_projection_projects_declaration_into_scheduler_shape():
    """声明被投影成调度器直接消费的描述字典，cron 五段表达式展开为逐字段取值。"""
    projection = PluginProjection({"Demo": _SchedulePlugin(declarations=[_daily()])})

    assert projection.services() == [{
        "id": "sync",
        "name": "定时同步",
        "trigger": "cron",
        "kwargs": {
            "minute": "0",
            "hour": "1",
            "day": "*",
            "month": "*",
            "day_of_week": "*",
        },
        "func": _run,
        "func_kwargs": {},
        "pid": "Demo",
    }]


@pytest.mark.parametrize(
    "trigger, trigger_args",
    [
        ("cron", {"crontab": "0 1 * * *"}),
        ("cron", {"hour": 1, "minute": 30}),
        ("interval", {"hours": 6}),
        ("date", {"run_date": "2026-07-19 20:30:00"}),
    ],
    ids=["cron_crontab", "cron_fields", "interval", "date"],
)
def test_projection_accepts_every_supported_trigger_type(trigger, trigger_args):
    """三种实际支持的调度类型及 cron 的两种写法都应通过契约校验。"""
    declaration = ScheduleDeclaration(
        job_id="job", name="任务", trigger=trigger, trigger_args=trigger_args, impl=_run
    )
    projection = PluginProjection({"Demo": _SchedulePlugin(declarations=[declaration])})

    assert projection.provided_schedules()["Demo"] == [declaration]


def _needs_argument(required):
    """要求一个必填参数的可调用对象，用于验证调用签名契约拦截。"""
    return required


@pytest.mark.parametrize(
    "declaration, reason",
    [
        (
            ScheduleDeclaration(
                job_id="j", name="N", trigger="cron",
                trigger_args={"crontab": "99 * * * *"}, impl=_run,
            ),
            "建不出触发器",
        ),
        (
            ScheduleDeclaration(
                job_id="j", name="N", trigger="cron",
                trigger_args={"crontab": "0 1 * *"}, impl=_run,
            ),
            "五段表达式",
        ),
        (
            ScheduleDeclaration(
                job_id="j", name="N", trigger="cron",
                trigger_args={"crontab": "0 1 * * *", "hour": 3}, impl=_run,
            ),
            "互斥",
        ),
        (
            ScheduleDeclaration(
                job_id="j", name="N", trigger="cron",
                trigger_args={"minute": "banana"}, impl=_run,
            ),
            "建不出触发器",
        ),
        (
            ScheduleDeclaration(
                job_id="j", name="N", trigger="interval",
                trigger_args={"hours": "六"}, impl=_run,
            ),
            "建不出触发器",
        ),
        (
            ScheduleDeclaration(
                job_id="j", name="N", trigger="every_night",
                trigger_args={"hour": 1}, impl=_run,
            ),
            "不受支持",
        ),
        (
            ScheduleDeclaration(job_id="j", name="N", trigger="", impl=_run),
            "trigger",
        ),
        (
            ScheduleDeclaration(
                job_id="j", name="N", trigger="cron", trigger_args={}, impl=_run
            ),
            "未给出任何调度参数",
        ),
        (
            ScheduleDeclaration(
                job_id="j", name="N", trigger="cron", trigger_args="0 1 * * *", impl=_run
            ),
            "必须是映射",
        ),
        (
            ScheduleDeclaration(
                job_id="", name="N", trigger="interval",
                trigger_args={"hours": 1}, impl=_run,
            ),
            "job_id",
        ),
        (
            ScheduleDeclaration(
                job_id="my job", name="N", trigger="interval",
                trigger_args={"hours": 1}, impl=_run,
            ),
            "不合法",
        ),
        (
            ScheduleDeclaration(
                job_id="a|b", name="N", trigger="interval",
                trigger_args={"hours": 1}, impl=_run,
            ),
            "不合法",
        ),
        (
            ScheduleDeclaration(
                job_id="j", name="", trigger="interval",
                trigger_args={"hours": 1}, impl=_run,
            ),
            "name",
        ),
        (
            ScheduleDeclaration(
                job_id="j", name="N", trigger="interval", trigger_args={"hours": 1}
            ),
            "不可调用",
        ),
        (
            ScheduleDeclaration(
                job_id="j", name="N", trigger="interval",
                trigger_args={"hours": 1}, impl="not-callable",
            ),
            "不可调用",
        ),
        (
            ScheduleDeclaration(
                job_id="j", name="N", trigger="interval",
                trigger_args={"hours": 1}, impl=_needs_argument,
            ),
            "不接受声明的 kwargs",
        ),
        (
            ScheduleDeclaration(
                job_id="j", name="N", trigger="interval",
                trigger_args={"hours": 1}, impl=_run, kwargs="not-a-mapping",
            ),
            "kwargs 必须是映射",
        ),
        ({"id": "j", "trigger": "interval"}, "不是 ScheduleDeclaration 实例"),
    ],
    ids=[
        "cron_field_out_of_range",
        "crontab_wrong_field_count",
        "crontab_conflicts_with_fields",
        "cron_field_not_parseable",
        "interval_arg_not_numeric",
        "trigger_type_unsupported",
        "trigger_empty",
        "trigger_args_empty",
        "trigger_args_not_mapping",
        "job_id_empty",
        "job_id_has_space",
        "job_id_has_separator",
        "name_empty",
        "impl_missing",
        "impl_not_callable",
        "impl_rejects_declared_kwargs",
        "kwargs_not_mapping",
        "not_a_declaration",
    ],
)
def test_projection_rejects_declaration_violations(declaration, reason):
    """畸形声明必须在登记时被整条拒绝，且日志里给出可读原因。"""
    recorder = _RecordingLogger()
    projection = PluginProjection(
        {"Demo": _SchedulePlugin(declarations=[declaration])}, log=recorder
    )

    declared = projection.provided_schedules()

    assert declared["Demo"] == []
    assert reason in "\n".join(recorder.errors)


def test_projection_rejects_non_serializable_trigger_args():
    """调度参数含过不了进程边界的对象时整条拒绝：跨进程时它要原样成为握手报文。"""
    declaration = ScheduleDeclaration(
        job_id="j",
        name="N",
        trigger="cron",
        trigger_args={"trigger": CronTrigger.from_crontab("0 1 * * *")},
        impl=_run,
    )
    projection = PluginProjection({"Demo": _SchedulePlugin(declarations=[declaration])})

    assert projection.provided_schedules()["Demo"] == []


def test_projection_rejects_duplicate_job_id_within_one_instance():
    """同一实例把同一任务标识声明两次时，保留先声明的那一条、拒绝后一条。"""
    plugin = _SchedulePlugin(
        declarations=[
            _daily(),
            ScheduleDeclaration(
                job_id="sync", name="另一个同步", trigger="interval",
                trigger_args={"hours": 1}, impl=_run,
            ),
        ]
    )
    projection = PluginProjection({"Demo": plugin})

    declared = projection.provided_schedules()

    assert len(declared["Demo"]) == 1
    assert declared["Demo"][0].name == "定时同步"


def test_projection_partial_rejection_keeps_valid_siblings():
    """一条坏声明只跳过它自己，同一实例的其余任务照常登记。"""
    plugin = _SchedulePlugin(
        declarations=[
            ScheduleDeclaration(
                job_id="bad", name="坏任务", trigger="cron",
                trigger_args={"crontab": "* * * *"}, impl=_run,
            ),
            _daily(),
            ScheduleDeclaration(
                job_id="cleanup", name="清理", trigger="interval",
                trigger_args={"hours": 12}, impl=_run,
            ),
        ]
    )
    projection = PluginProjection({"Demo": plugin})

    assert [item.job_id for item in projection.provided_schedules()["Demo"]] == [
        "sync",
        "cleanup",
    ]


def test_projection_swallows_plugin_exception_without_blocking_others():
    """单个插件声明定时任务抛异常时不应影响其它插件的投影结果。"""
    projection = PluginProjection({
        "Broken": _SchedulePlugin(raise_error=True),
        "Ok": _SchedulePlugin(declarations=[_daily()]),
    })

    declared = projection.provided_schedules()

    assert "Broken" not in declared
    assert declared["Ok"][0].job_id == "sync"


def _full_sync(full: bool = False) -> bool:
    """接收声明式 kwargs 的任务实现。"""
    return full


def test_declaration_survives_json_round_trip_except_impl():
    """声明除 impl 外全是纯数据，JSON 往返后取值不变——跨进程握手的前提。"""
    declaration = ScheduleDeclaration(
        job_id="sync",
        name="定时同步",
        trigger="cron",
        trigger_args={"crontab": "0 1 * * *"},
        kwargs={"full": True},
        impl=_full_sync,
    )

    payload = {
        field.name: getattr(declaration, field.name)
        for field in fields(declaration)
        if field.name != "impl"
    }
    payload["trigger_args"] = dict(payload["trigger_args"])
    payload["kwargs"] = dict(payload["kwargs"])

    restored = json.loads(json.dumps(payload))

    assert restored == payload
    rebuilt = ScheduleDeclaration(**restored, impl=_full_sync)
    assert rebuilt == declaration
    assert PluginProjection(
        {"Demo": _SchedulePlugin(declarations=[rebuilt])}
    ).provided_schedules()["Demo"] == [rebuilt]


# --------------------------------------------------------------------------
# 新旧两条来源并存
# --------------------------------------------------------------------------


def _legacy_service(job_id: str = "sync", name: str = "旧式同步") -> dict:
    """构造一条旧式 get_service() 描述字典。"""
    return {"id": job_id, "name": name, "trigger": "interval", "func": _run}


def test_legacy_hook_still_works_and_warns_once(
    plugin_manager: PluginManager, monkeypatch
) -> None:
    """旧钩子 get_service() 必须继续工作，并在首次触达时告警一次。"""
    emitted = []
    monkeypatch.setattr(deprecation_policy.logger, "warning", emitted.append)
    plugin_manager.running_plugins["Demo"] = _SchedulePlugin(legacy=[_legacy_service()])

    services = plugin_manager.get_plugin_services("Demo")
    plugin_manager.get_plugin_services("Demo")

    assert [service["id"] for service in services] == ["sync"]
    assert services[0]["pid"] == "Demo"
    assert len(emitted) == 1
    assert "get_service" in emitted[0]


def test_declared_schedule_wins_over_legacy_with_same_job_id(
    plugin_manager: PluginManager,
) -> None:
    """同一实例的同一任务标识被新旧两条来源挂载时，以声明式登记为准。"""
    plugin_manager.running_plugins["Demo"] = _SchedulePlugin(
        declarations=[_daily()],
        legacy=[_legacy_service(), _legacy_service("legacy_only", "只有旧式")],
    )

    services = plugin_manager.get_plugin_services("Demo")

    by_id = {service["id"]: service for service in services}
    assert set(by_id) == {"sync", "legacy_only"}
    assert by_id["sync"]["name"] == "定时同步"
    assert by_id["sync"]["trigger"] == "cron"
    assert by_id["legacy_only"]["name"] == "只有旧式"


def test_legacy_hook_stops_at_disabled_stage_and_resumes_via_override(
    plugin_manager: PluginManager, monkeypatch
) -> None:
    """阶段推进到 DISABLED 时旧钩子真的停用；标识列入 DEPRECATION_ENABLED 能恢复。"""
    plugin_manager.running_plugins["Demo"] = _SchedulePlugin(legacy=[_legacy_service()])

    assert plugin_manager.get_plugin_services("Demo")

    _set_notice_stage(monkeypatch, "plugin.get_service", DeprecationStage.DISABLED)
    monkeypatch.setattr(deprecation_policy, "_enabled_keys", frozenset)
    assert plugin_manager.get_plugin_services("Demo") == []

    monkeypatch.setattr(
        deprecation_policy, "_enabled_keys", lambda: frozenset({"plugin.get_service"})
    )
    assert plugin_manager.get_plugin_services("Demo")


def test_disabled_plugin_declares_nothing(plugin_manager: PluginManager) -> None:
    """停用的插件既不产出声明式任务也不产出旧式任务。"""
    plugin_manager.running_plugins["Demo"] = _SchedulePlugin(
        declarations=[_daily()], legacy=[_legacy_service("legacy_only")], enabled=False
    )

    assert plugin_manager.get_plugin_services("Demo") == []


# --------------------------------------------------------------------------
# 调度器登记与回收
# --------------------------------------------------------------------------


class _FakeJob:
    """记录一次 add_job 调用产生的任务身份与触发器。"""

    def __init__(self, job_id: str) -> None:
        """保存任务 id。"""
        self.id = job_id


class _FakeAPScheduler:
    """只记录任务 id 与触发器参数的调度器替身，不真正调度执行。"""

    def __init__(self) -> None:
        """初始化空的任务登记表。"""
        self._job_ids: list[str] = []
        self.triggers: dict[str, tuple] = {}

    def add_job(self, func, trigger=None, *, id, **kwargs):
        """记录一次任务注册，同 id 视为覆盖登记。"""
        if id not in self._job_ids:
            self._job_ids.append(id)
        self.triggers[id] = (trigger, kwargs)

    def remove_job(self, job_id):
        """按 id 精确移除任务，未登记时抛出 JobLookupError。"""
        if job_id not in self._job_ids:
            raise JobLookupError(job_id)
        self._job_ids.remove(job_id)
        self.triggers.pop(job_id, None)

    def get_jobs(self):
        """返回当前登记任务的快照。"""
        return [_FakeJob(job_id) for job_id in self._job_ids]


def _build_scheduler() -> Scheduler:
    """构造不启动真实 APScheduler 的调度器测试对象。"""
    scheduler = object.__new__(Scheduler)
    scheduler._scheduler = _FakeAPScheduler()
    scheduler._lock = threading.RLock()
    scheduler._jobs = {}
    return scheduler


def _bind_scheduler(monkeypatch, manager: PluginManager) -> None:
    """把调度器使用的插件管理器替换为给定实例。"""
    monkeypatch.setattr("app.scheduler.plugins.PluginManager", lambda: manager)


def test_declared_schedule_reaches_scheduler_with_expanded_trigger(
    plugin_manager: PluginManager, monkeypatch
) -> None:
    """声明式任务应被登记进调度器，调度参数按调度类型原样展开交给调度器建触发器。"""
    plugin_manager.running_plugins["Demo"] = _SchedulePlugin(declarations=[_daily()])
    _bind_scheduler(monkeypatch, plugin_manager)
    scheduler = _build_scheduler()

    scheduler.update_plugin_job("Demo")

    assert set(scheduler._jobs) == {"Demo_sync"}
    trigger, trigger_kwargs = scheduler._scheduler.triggers["Demo_sync"]
    assert trigger == "cron"
    assert trigger_kwargs["hour"] == "1"
    assert trigger_kwargs["minute"] == "0"


def test_scheduler_drops_jobs_after_plugin_disabled(
    plugin_manager: PluginManager, monkeypatch
) -> None:
    """插件停用后重建登记，其任务必须从调度器里消失，不能残留继续跑。"""
    plugin = _SchedulePlugin(declarations=[_daily()], legacy=[_legacy_service("legacy")])
    plugin_manager.running_plugins["Demo"] = plugin
    _bind_scheduler(monkeypatch, plugin_manager)
    scheduler = _build_scheduler()
    scheduler.update_plugin_job("Demo")
    assert set(scheduler._jobs) == {"Demo_sync", "Demo_legacy"}

    plugin._enabled = False
    scheduler.update_plugin_job("Demo")

    assert scheduler._jobs == {}
    assert scheduler._scheduler._job_ids == []


def test_scheduler_drops_jobs_after_plugin_uninstalled(
    plugin_manager: PluginManager, monkeypatch
) -> None:
    """插件卸载后运行态消失，其任务同样不能残留在调度器里。"""
    plugin_manager.running_plugins["Demo"] = _SchedulePlugin(declarations=[_daily()])
    _bind_scheduler(monkeypatch, plugin_manager)
    scheduler = _build_scheduler()
    scheduler.update_plugin_job("Demo")

    plugin_manager.running_plugins.pop("Demo")
    scheduler.update_plugin_job("Demo")

    assert scheduler._jobs == {}
    assert scheduler._scheduler._job_ids == []


def test_sibling_instances_own_their_own_declared_jobs(
    plugin_manager: PluginManager, monkeypatch
) -> None:
    """两个分身各声明同一任务标识时按实例键分别归属，互不覆盖。"""
    plugin_manager.running_plugins["Demo"] = _SchedulePlugin(declarations=[_daily()])
    plugin_manager.running_plugins["Demo@second"] = _SchedulePlugin(
        declarations=[_daily()]
    )
    _bind_scheduler(monkeypatch, plugin_manager)
    scheduler = _build_scheduler()

    scheduler.update_plugin_job("Demo")

    assert set(scheduler._jobs) == {"Demo_sync", "Demo@second_sync"}
    assert scheduler._jobs["Demo_sync"]["pid"] == "Demo"
    assert scheduler._jobs["Demo@second_sync"]["pid"] == "Demo@second"


def test_removing_one_instance_keeps_sibling_declared_job(
    plugin_manager: PluginManager, monkeypatch
) -> None:
    """按实例键回收只摘掉该分身的任务，兄弟分身的任务照常保留。"""
    plugin_manager.running_plugins["Demo"] = _SchedulePlugin(declarations=[_daily()])
    plugin_manager.running_plugins["Demo@second"] = _SchedulePlugin(
        declarations=[_daily()]
    )
    _bind_scheduler(monkeypatch, plugin_manager)
    scheduler = _build_scheduler()
    scheduler.update_plugin_job("Demo")

    scheduler.remove_plugin_job("Demo@second")

    assert set(scheduler._jobs) == {"Demo_sync"}
    assert scheduler._scheduler._job_ids == ["Demo_sync"]


def test_bad_declaration_does_not_block_sibling_instance_jobs(
    plugin_manager: PluginManager, monkeypatch
) -> None:
    """一个分身的坏声明不影响自己的其余任务，也不影响兄弟分身的任务。"""
    plugin_manager.running_plugins["Demo"] = _SchedulePlugin(
        declarations=[
            ScheduleDeclaration(
                job_id="bad", name="坏任务", trigger="cron",
                trigger_args={"crontab": "0 99 * * *"}, impl=_run,
            ),
            _daily(),
        ]
    )
    plugin_manager.running_plugins["Demo@second"] = _SchedulePlugin(
        declarations=[
            ScheduleDeclaration(
                job_id="cleanup", name="清理", trigger="interval",
                trigger_args={"hours": 12}, impl=_run,
            )
        ]
    )
    _bind_scheduler(monkeypatch, plugin_manager)
    scheduler = _build_scheduler()

    scheduler.update_plugin_job("Demo")

    assert set(scheduler._jobs) == {"Demo_sync", "Demo@second_cleanup"}
