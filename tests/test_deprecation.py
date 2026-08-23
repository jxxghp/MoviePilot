"""渐进式废弃登记的阶段行为，以及与旧 Facade 命中观测的联动测试。"""

import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Tuple

import pytest

from app.runtime.config import settings
from app.runtime.deprecation import notices as notices_module
from app.runtime.deprecation import policy
from app.runtime.deprecation.notices import NOTICES, DeprecationNotice, DeprecationStage
from app.runtime.deprecation.policy import (
    DeprecatedFeatureError,
    all_notices,
    deprecated,
    enforce_facade,
    guard,
    is_active,
    warn,
)
from app.runtime.observability import (
    MetricSpec,
    configure_observation,
    observe_compat_facade,
)


@dataclass
class _RecordingLogger:
    """只记录 warning 文本的日志替身。"""

    messages: List[str] = field(default_factory=list)

    def warning(self, message: str) -> None:
        """记录一条告警文本。"""
        self.messages.append(message)


@dataclass
class _RecordingObservationPort:
    """保存已经通过标签合同校验的指标写入。"""

    records: List[Tuple[str, Dict[str, str]]] = field(default_factory=list)

    def record(self, spec: MetricSpec, value: float, labels: Mapping[str, str]) -> None:
        """追加一条不可变测试快照。"""
        self.records.append((spec.name, dict(labels)))


@pytest.fixture(autouse=True)
def _reset_warned():
    """避免告警去重记录在用例间泄漏。"""
    policy.reset_warned()
    yield
    policy.reset_warned()


@pytest.fixture
def warnings_log(monkeypatch) -> _RecordingLogger:
    """把废弃告警接到可断言的日志替身上。"""
    recorder = _RecordingLogger()
    monkeypatch.setattr(policy, "logger", recorder)
    return recorder


@pytest.fixture
def observation_port():
    """安装可断言的观测端口，用例结束恢复 no-op。"""
    port = _RecordingObservationPort()
    configure_observation(port)
    yield port
    configure_observation(None)


@pytest.fixture
def registry(monkeypatch):
    """
    用可控登记表替换全局登记表，避免用例依赖真实登记内容。

    :return: ``register(stage, key) -> DeprecationNotice``
    """
    table: Dict[str, DeprecationNotice] = {}
    monkeypatch.setattr(notices_module, "NOTICES", table)

    def register(stage: DeprecationStage, key: str = "demo.legacy") -> DeprecationNotice:
        """
        登记一条指定阶段的废弃通告
        :param stage: 所处阶段
        :param key: 废弃标识
        :return: 登记
        """
        notice = DeprecationNotice(
            key=key,
            subject="示例旧入口",
            stage=stage,
            since="v3.0.0",
            replacement="示例新入口",
            reason="仅用于测试",
        )
        table[key] = notice
        return notice

    return register


def test_silent_stage_stays_active_and_quiet(registry, warnings_log) -> None:
    """仅登记阶段不改变行为，也不产生任何告警。"""
    registry(DeprecationStage.SILENT)

    assert is_active("demo.legacy") is True
    guard("demo.legacy")
    warn("demo.legacy")

    assert warnings_log.messages == []


def test_warn_stage_logs_once_per_context(registry, warnings_log) -> None:
    """预警阶段功能照常，同一来源只留一次痕迹，不同来源各留一次。"""
    registry(DeprecationStage.WARN)

    assert is_active("demo.legacy") is True
    warn("demo.legacy", context="PluginA")
    warn("demo.legacy", context="PluginA")
    warn("demo.legacy", context="PluginB")

    assert len(warnings_log.messages) == 2
    assert "示例旧入口 自 v3.0.0 起进入废弃流程" in warnings_log.messages[0]
    assert "移除版本待定" in warnings_log.messages[0]
    assert "触发来源：PluginA" in warnings_log.messages[0]
    assert "请改用：示例新入口" in warnings_log.messages[0]
    assert "触发来源：PluginB" in warnings_log.messages[1]


def test_disabled_stage_blocks_until_explicitly_enabled(registry, monkeypatch) -> None:
    """停用阶段默认抛错，仅在标识写进 DEPRECATION_ENABLED 后恢复。"""
    registry(DeprecationStage.DISABLED)
    monkeypatch.setattr(settings, "DEPRECATION_ENABLED", None)

    assert is_active("demo.legacy") is False
    with pytest.raises(DeprecatedFeatureError, match="已默认停用"):
        guard("demo.legacy")

    monkeypatch.setattr(settings, "DEPRECATION_ENABLED", "other.key, demo.legacy")

    assert is_active("demo.legacy") is True
    guard("demo.legacy")


def test_removed_stage_cannot_be_restored(registry, monkeypatch) -> None:
    """移除阶段无视开关，一律抛错并说明无法恢复。"""
    registry(DeprecationStage.REMOVED)
    monkeypatch.setattr(settings, "DEPRECATION_ENABLED", "demo.legacy")

    assert is_active("demo.legacy") is False
    with pytest.raises(DeprecatedFeatureError, match="已彻底移除"):
        guard("demo.legacy")


def test_unregistered_key_is_rejected_by_lookups(registry) -> None:
    """未登记标识在查询入口上直接报错，避免登记表与调用点写法漂移。"""
    registry(DeprecationStage.WARN)

    with pytest.raises(KeyError, match="未登记的废弃标识"):
        is_active("demo.missing")
    with pytest.raises(KeyError, match="未登记的废弃标识"):
        warn("demo.missing")
    with pytest.raises(KeyError, match="未登记的废弃标识"):
        guard("demo.missing")


def test_unregistered_key_passes_through_enforcement(registry, warnings_log) -> None:
    """未纳入废弃流程的触达点原样放行，既不报错也不留痕。"""
    registry(DeprecationStage.DISABLED)

    policy.enforce("demo.missing")
    enforce_facade("OtherFacade", "any_method")

    assert warnings_log.messages == []


def test_deprecated_decorator_keeps_call_and_metadata(registry, warnings_log) -> None:
    """预警阶段的装饰器留痕后照常执行，并保留原函数元数据。"""
    registry(DeprecationStage.WARN)

    @deprecated("demo.legacy")
    def legacy_call(value: int) -> int:
        """返回入参本身。"""
        return value

    assert legacy_call(3) == 3
    assert legacy_call(4) == 4
    assert legacy_call.__name__ == "legacy_call"
    assert len(warnings_log.messages) == 1


def test_deprecated_decorator_blocks_disabled_stage(registry, monkeypatch) -> None:
    """停用阶段的装饰器直接拦截，不再执行原实现。"""
    registry(DeprecationStage.DISABLED)
    monkeypatch.setattr(settings, "DEPRECATION_ENABLED", None)
    calls: List[int] = []

    @deprecated("demo.legacy")
    def legacy_call() -> None:
        """记录一次实际执行。"""
        calls.append(1)

    with pytest.raises(DeprecatedFeatureError):
        legacy_call()

    assert calls == []


def test_compat_facade_hit_is_recorded_before_stage_is_applied(
    registry, monkeypatch, observation_port
) -> None:
    """停用阶段仍先记账再拦截，命中数不会因为收口而丢失。"""
    registry(DeprecationStage.DISABLED, key="DemoFacade.legacy_call")
    monkeypatch.setattr(settings, "DEPRECATION_ENABLED", None)

    @observe_compat_facade("DemoFacade")
    class DemoFacade:
        """被观测的旧 Facade 替身。"""

        @staticmethod
        def legacy_call() -> str:
            """返回固定结果。"""
            return "called"

    with pytest.raises(DeprecatedFeatureError):
        DemoFacade.legacy_call()

    assert observation_port.records == [
        (
            "compat.facade.hit",
            {
                "facade": "DemoFacade",
                "operation": "legacy_call",
                "visibility": "public",
                "abi_source": "legacy_facade",
            },
        )
    ]


def test_facade_notice_covers_every_operation(registry, warnings_log) -> None:
    """整个 Facade 的登记覆盖其所有方法，且每个方法各留一次痕迹。"""
    registry(DeprecationStage.WARN, key="DemoFacade")

    @observe_compat_facade("DemoFacade")
    class DemoFacade:
        """被观测的旧 Facade 替身。"""

        @staticmethod
        def first() -> None:
            """空实现。"""

        @staticmethod
        def second() -> None:
            """空实现。"""

    DemoFacade.first()
    DemoFacade.first()
    DemoFacade.second()

    assert len(warnings_log.messages) == 2
    assert "触发来源：DemoFacade.first" in warnings_log.messages[0]
    assert "触发来源：DemoFacade.second" in warnings_log.messages[1]


def test_method_notice_takes_precedence_over_facade_notice(registry, warnings_log) -> None:
    """同时登记 Facade 与其单个方法时，按方法级登记处置。"""
    registry(DeprecationStage.WARN, key="DemoFacade")
    registry(DeprecationStage.REMOVED, key="DemoFacade.legacy_call")

    @observe_compat_facade("DemoFacade")
    class DemoFacade:
        """被观测的旧 Facade 替身。"""

        @staticmethod
        def legacy_call() -> None:
            """空实现。"""

        @staticmethod
        def other_call() -> None:
            """空实现。"""

    with pytest.raises(DeprecatedFeatureError, match="已彻底移除"):
        DemoFacade.legacy_call()
    DemoFacade.other_call()

    assert "触发来源：DemoFacade.other_call" in warnings_log.messages[0]


def test_async_facade_method_applies_stage(registry, monkeypatch, observation_port) -> None:
    """异步旧 Facade 方法同样先记账再按阶段拦截。"""
    registry(DeprecationStage.DISABLED, key="DemoFacade.async_call")
    monkeypatch.setattr(settings, "DEPRECATION_ENABLED", None)

    @observe_compat_facade("DemoFacade")
    class DemoFacade:
        """被观测的旧 Facade 替身。"""

        @staticmethod
        async def async_call() -> str:
            """返回固定结果。"""
            return "called"

    with pytest.raises(DeprecatedFeatureError):
        asyncio.run(DemoFacade.async_call())

    assert observation_port.records[0][1]["operation"] == "async_call"


def test_registered_notices_are_self_consistent() -> None:
    """真实登记表的键与登记本身一致，且每条都给出替代方案与原因。"""
    assert NOTICES

    for key, notice in NOTICES.items():
        assert key == notice.key
        assert notice.since
        assert notice.replacement
        assert notice.reason
        assert isinstance(notice.stage, DeprecationStage)

    assert all_notices() == tuple(NOTICES[key] for key in sorted(NOTICES))
