import pytest

from app.runtime.deprecation import notices as notices_module
from app.runtime.deprecation import policy as policy_module
from app.runtime.deprecation.notices import DeprecationNotice, DeprecationStage
from app.runtime.deprecation.policy import DeprecatedFeatureError


def _notice(key: str, stage: DeprecationStage) -> DeprecationNotice:
    """构造一条指定阶段的废弃登记。"""
    return DeprecationNotice(
        key=key,
        subject=f"{key}()",
        stage=stage,
        since="v3.1.0",
        remove_in="v3.3.0",
        replacement="新接口",
        reason="旧路径无契约校验",
    )


@pytest.fixture(autouse=True)
def _clean_warned():
    """每个用例前后都清空告警去重记录。"""
    policy_module.reset_warned()
    yield
    policy_module.reset_warned()


@pytest.fixture
def staged(monkeypatch):
    """把 NOTICES 替换成单条可控登记，并返回设置阶段的函数。"""

    def _apply(stage: DeprecationStage, key: str = "demo.feature"):
        table = {key: _notice(key, stage)}
        monkeypatch.setattr(notices_module, "NOTICES", table)
        monkeypatch.setattr(policy_module, "NOTICES", table)
        return key

    return _apply


def test_stage_warn_keeps_feature_active(staged):
    """阶段一：功能照常生效。"""
    key = staged(DeprecationStage.WARN)
    assert policy_module.is_active(key) is True


def test_stage_disabled_is_off_by_default(staged, monkeypatch):
    """阶段二：默认关闭。"""
    key = staged(DeprecationStage.DISABLED)
    monkeypatch.setattr(policy_module, "_enabled_keys", frozenset)
    assert policy_module.is_active(key) is False


def test_stage_disabled_can_be_restored_for_observation(staged, monkeypatch):
    """阶段二：显式列入开关后恢复生效，用于观察真实依赖方。"""
    key = staged(DeprecationStage.DISABLED)
    monkeypatch.setattr(policy_module, "_enabled_keys", lambda: frozenset({key}))
    assert policy_module.is_active(key) is True


def test_stage_removed_is_never_active(staged, monkeypatch):
    """阶段三：即使列入开关也不生效。"""
    key = staged(DeprecationStage.REMOVED)
    monkeypatch.setattr(policy_module, "_enabled_keys", lambda: frozenset({key}))
    assert policy_module.is_active(key) is False


def test_guard_only_raises_at_removed_stage(staged):
    """阶段三触达即报错，前两个阶段放行。"""
    for stage in (DeprecationStage.WARN, DeprecationStage.DISABLED):
        key = staged(stage)
        policy_module.guard(key)

    key = staged(DeprecationStage.REMOVED)
    with pytest.raises(DeprecatedFeatureError) as excinfo:
        policy_module.guard(key)
    assert "新接口" in str(excinfo.value)


def test_warn_is_emitted_once_per_context(staged, monkeypatch):
    """同一触发来源只告警一次，不同来源各告警一次。"""
    key = staged(DeprecationStage.WARN)
    emitted = []
    monkeypatch.setattr(policy_module.logger, "warning", lambda msg: emitted.append(msg))

    policy_module.warn(key, context="PluginA")
    policy_module.warn(key, context="PluginA")
    policy_module.warn(key, context="PluginB")

    assert len(emitted) == 2
    assert "PluginA" in emitted[0]
    assert "PluginB" in emitted[1]


def test_message_carries_migration_guidance(staged):
    """提示语必须给出起始版本、移除版本与替代方案。"""
    key = staged(DeprecationStage.WARN)
    message = policy_module.get_notice(key).message(context="PluginA")
    for fragment in ("v3.1.0", "v3.3.0", "新接口", "PluginA"):
        assert fragment in message


def test_disabled_message_points_at_the_switch(staged):
    """阶段二的提示语要告诉运维如何临时恢复。"""
    key = staged(DeprecationStage.DISABLED)
    assert "DEPRECATION_ENABLED" in policy_module.get_notice(key).message()


def test_decorator_runs_warns_then_skips_by_stage(staged, monkeypatch):
    """装饰器在阶段一执行本体、阶段二跳过、阶段三抛错。"""
    calls = []
    monkeypatch.setattr(policy_module.logger, "warning", lambda msg: None)

    key = staged(DeprecationStage.WARN)

    @policy_module.deprecated(key)
    def legacy() -> str:
        calls.append("ran")
        return "done"

    assert legacy() == "done"
    assert calls == ["ran"]

    staged(DeprecationStage.DISABLED, key)
    monkeypatch.setattr(policy_module, "_enabled_keys", frozenset)
    policy_module.reset_warned()
    assert legacy() is None
    assert calls == ["ran"]

    staged(DeprecationStage.REMOVED, key)
    policy_module.reset_warned()
    with pytest.raises(DeprecatedFeatureError):
        legacy()


def test_unknown_key_is_rejected():
    """未登记的标识不允许静默通过。"""
    with pytest.raises(KeyError):
        policy_module.get_notice("nope.not.registered")


def test_shipped_notices_are_self_consistent():
    """仓库内登记的每条废弃都必须信息完整且以自身 key 为索引。"""
    entries = policy_module.all_notices()
    assert entries
    for notice in entries:
        assert notices_module.NOTICES[notice.key] is notice
        assert notice.subject and notice.replacement and notice.reason
        assert notice.since.startswith("v") and notice.remove_in.startswith("v")
        assert isinstance(notice.stage, DeprecationStage)
