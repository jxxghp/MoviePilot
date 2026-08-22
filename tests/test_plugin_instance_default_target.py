"""插件实例默认调用目标：解析规则、置位的原子性与条件唯一索引。

一个插件扇出多个实例后，「调用没指定实例」只允许两种结局：走用户选定且已启用的
默认调用目标，或者报错。这里最要紧的一条是默认调用目标被停用时必须报错而不是改走
另一个启用实例——那等于用户停用了一个实例、调用却被悄悄改道，且不留任何痕迹。

置位的唯一性同时由应用层和数据库把守，两道都要验：应用层的「置新清旧」只能保证
单个调用路径正确，两个并发事务各自置位不同实例时都会通过应用层检查，只有条件唯一
索引能拦下后提交的那一个，因此索引必须绕开 ORM 直接写库验证。
"""
import asyncio

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError

from app.db.models.pluginconfig import PluginConfig
from app.db.oper.pluginconfig import PluginConfigOper
from app.runtime.extensions.admission.instance_selection import (
    PluginInstanceTarget,
    configure_plugin_instance_targets,
    resolve_plugin_instance_key,
    select_plugin_instance_id,
)


@pytest.fixture(autouse=True)
def _track(db):
    """把插件实例配置表纳入用例级回收。"""
    db.watermark(PluginConfig)


@pytest.fixture
def instance_targets(monkeypatch):
    """把实例状态读取钩子换成用例自备的内存实现。

    :return: ``install(plugin_id, targets)``，登记某插件的实例状态
    """
    import app.runtime.extensions.admission.instance_selection as module

    registry: dict[str, list[PluginInstanceTarget]] = {}
    monkeypatch.setattr(
        module, "_instance_target_lister", lambda plugin_id: registry.get(plugin_id, [])
    )

    def install(plugin_id: str, targets: list[PluginInstanceTarget]) -> None:
        registry[plugin_id] = targets

    return install


def _target(instance_id: str, *, enabled: bool = True, default: bool = False):
    """构造一条实例状态。"""
    return PluginInstanceTarget(
        instance_id=instance_id, is_enabled=enabled, is_default_target=default
    )


# --------------------------------------------------------------------------- #
# 解析规则
# --------------------------------------------------------------------------- #

def test_explicit_instance_id_wins_over_default_target():
    """显式指定实例时直接用它，默认调用目标不参与。"""
    targets = [_target("default", default=True), _target("alt")]

    assert select_plugin_instance_id("PluginA", targets, "alt") == "alt"


def test_explicit_instance_id_is_used_even_without_any_registered_instance():
    """显式指定实例时不查实例状态，一条实例配置都没有也照样按指定的走。"""
    assert select_plugin_instance_id("PluginA", [], "alt") == "alt"


def test_enabled_default_target_is_used_when_instance_is_omitted():
    """未指定实例且默认调用目标已启用时走该目标。"""
    targets = [_target("default"), _target("alt", default=True), _target("other")]

    assert select_plugin_instance_id("PluginA", targets, None) == "alt"


def test_missing_default_target_raises_and_lists_candidates():
    """未指定实例且没有默认调用目标时报错，且必须列出可选实例名。"""
    targets = [_target("alt"), _target("default"), _target("cold", enabled=False)]

    with pytest.raises(LookupError) as excinfo:
        select_plugin_instance_id("PluginA", targets, None)

    message = str(excinfo.value)
    assert "PluginA" in message
    assert "未设置默认实例" in message
    # 只说失败不说能选什么，用户无从下手
    assert "default（已启用）" in message
    assert "alt（已启用）" in message
    assert "cold（已停用）" in message


def test_disabled_default_target_raises_instead_of_falling_back():
    """默认调用目标被停用时必须报错，不得改走其它已启用实例。

    这是本机制的核心安全判据：用户停用了默认实例，调用却静默换一个实例执行，
    等于把「这个下载器我不想用了」翻译成「换另一个下载器」，且没有任何痕迹。
    """
    targets = [
        _target("default", enabled=False, default=True),
        _target("alt"),
        _target("other"),
    ]

    with pytest.raises(LookupError) as excinfo:
        select_plugin_instance_id("PluginA", targets, None)

    message = str(excinfo.value)
    assert "默认实例 default 已停用" in message
    assert "alt（已启用）" in message
    assert "other（已启用）" in message


def test_no_instance_at_all_raises_with_empty_candidate_list():
    """一条实例配置都没有时同样报错，候选列表如实报告为空。"""
    with pytest.raises(LookupError) as excinfo:
        select_plugin_instance_id("PluginA", [], None)

    assert "可选实例：无" in str(excinfo.value)


def test_candidate_list_is_ordered_default_first_then_ascending():
    """候选列表按默认实例优先、其余升序排列，报错文案必须稳定可预期。"""
    targets = [_target("zeta"), _target("alpha"), _target("default")]

    with pytest.raises(LookupError) as excinfo:
        select_plugin_instance_id("PluginA", targets, None)

    message = str(excinfo.value)
    assert message.endswith("可选实例：default（已启用）、alpha（已启用）、zeta（已启用）")


def test_illegal_explicit_instance_id_is_rejected():
    """显式指定的实例标识含实例键分隔符时拒绝，避免解析出歧义的实例键。"""
    with pytest.raises(ValueError):
        select_plugin_instance_id("PluginA", [], "alt@x")


# --------------------------------------------------------------------------- #
# 解析结果：实例键
# --------------------------------------------------------------------------- #

def test_resolve_returns_bare_plugin_id_for_default_instance(instance_targets):
    """默认实例的实例键退化为裸插件标识。"""
    instance_targets("PluginB", [_target("default", default=True)])

    assert resolve_plugin_instance_key("PluginB") == "PluginB"


def test_resolve_returns_composed_key_for_clone_instance(instance_targets):
    """分身实例的实例键由插件标识与实例标识拼成。"""
    instance_targets("PluginB", [_target("default"), _target("alt", default=True)])

    assert resolve_plugin_instance_key("PluginB") == "PluginB@alt"
    assert resolve_plugin_instance_key("PluginB", "default") == "PluginB"


def test_resolve_without_configured_lister_raises(monkeypatch):
    """读取钩子未装配时按「没有任何实例」处理，未指定实例即报错，不得静默取到什么。"""
    import app.runtime.extensions.admission.instance_selection as module

    monkeypatch.setattr(module, "_instance_target_lister", module._no_instance_targets)

    with pytest.raises(LookupError):
        resolve_plugin_instance_key("PluginB")
    assert resolve_plugin_instance_key("PluginB", "alt") == "PluginB@alt"


# --------------------------------------------------------------------------- #
# 置位的原子性
# --------------------------------------------------------------------------- #

def test_set_default_target_clears_the_previous_one(db):
    """设置新的默认调用目标时，同插件原有的置位必须被清掉。"""
    db.add(PluginConfig(plugin_id="PluginC", instance_id="default", is_default_target=True),
           PluginConfig(plugin_id="PluginC", instance_id="alt"),
           PluginConfig(plugin_id="PluginD", instance_id="default", is_default_target=True))
    oper = PluginConfigOper(db=db.session)

    assert oper.set_default_target("PluginC", "alt") is True

    flags = {
        row.instance_id: row.is_default_target
        for row in PluginConfig.list_by_plugin(db.session, "PluginC")
    }
    assert flags == {"default": False, "alt": True}
    # 其他插件的置位不受影响
    assert PluginConfigOper(db=db.session).get_default_target("PluginD").instance_id == "default"


def test_set_default_target_leaves_previous_intact_when_target_absent(db):
    """目标实例不存在时原样返回，不得把原有的默认调用目标清成「没有」。"""
    db.add(PluginConfig(plugin_id="PluginE", instance_id="default", is_default_target=True))
    oper = PluginConfigOper(db=db.session)

    assert oper.set_default_target("PluginE", "missing") is False
    assert oper.get_default_target("PluginE").instance_id == "default"


def test_async_set_default_target_matches_sync_semantics(db):
    """异步置位必须与同步路径一致：换目标时清旧，目标缺席时不动原值。"""
    db.add(PluginConfig(plugin_id="PluginF", instance_id="default", is_default_target=True),
           PluginConfig(plugin_id="PluginF", instance_id="alt"))

    async def _run():
        oper = PluginConfigOper()
        switched = await oper.async_set_default_target("PluginF", "alt")
        absent = await oper.async_set_default_target("PluginF", "missing")
        current = await oper.async_get_default_target("PluginF")
        return switched, absent, current.instance_id

    assert asyncio.run(_run()) == (True, False, "alt")


def test_clear_default_target_leaves_plugin_without_a_default(db):
    """清除置位后该插件不再有默认调用目标，解析随即回到报错分支。"""
    db.add(PluginConfig(plugin_id="PluginG", instance_id="default", is_default_target=True))
    oper = PluginConfigOper(db=db.session)

    assert oper.clear_default_target("PluginG") == 1
    assert oper.get_default_target("PluginG") is None


# --------------------------------------------------------------------------- #
# 条件唯一索引
# --------------------------------------------------------------------------- #

def test_partial_unique_index_rejects_a_second_default_target(db):
    """同一插件写入第二个置位行必须被数据库拒绝。

    直接写库而不走 ``set_default_target``：应用层的「置新清旧」证明不了并发写入下
    的唯一性，这条断言针对的正是绕开了应用层的那条路径。
    """
    db.add(PluginConfig(plugin_id="PluginH", instance_id="default", is_default_target=True))

    with pytest.raises(IntegrityError):
        db.session.add(
            PluginConfig(plugin_id="PluginH", instance_id="alt", is_default_target=True)
        )
        db.session.commit()
    db.session.rollback()

    assert len(PluginConfig.list_by_plugin(db.session, "PluginH")) == 1


def test_partial_unique_index_allows_many_rows_without_the_flag(db):
    """未置位的行不入索引，同一插件可以有任意多行未置位。"""
    db.add(PluginConfig(plugin_id="PluginI", instance_id="default", is_default_target=True),
           PluginConfig(plugin_id="PluginI", instance_id="a"),
           PluginConfig(plugin_id="PluginI", instance_id="b"),
           PluginConfig(plugin_id="PluginI", instance_id="c"))

    rows = PluginConfig.list_by_plugin(db.session, "PluginI")
    assert len(rows) == 4
    assert sum(1 for row in rows if row.is_default_target) == 1


def test_partial_unique_index_scopes_to_one_plugin(db):
    """不同插件各自可以有一个默认调用目标，索引的作用域是插件而非全表。"""
    db.add(PluginConfig(plugin_id="PluginJ", instance_id="default", is_default_target=True),
           PluginConfig(plugin_id="PluginK", instance_id="default", is_default_target=True))

    assert PluginConfig.get_default_target(db.session, "PluginJ").plugin_id == "PluginJ"
    assert PluginConfig.get_default_target(db.session, "PluginK").plugin_id == "PluginK"


def test_model_index_is_partial_in_both_dialects():
    """模型（全新安装的 create_all 路径）建出的索引在两种方言下都必须带谓词。

    本仓的测试库是 SQLite，PostgreSQL 分支只能靠编译期 DDL 证明：布尔列在 PG 下
    不能与整数比较，谓词若不分方言就会在 PG 侧建索引时失败；谓词整个丢失则退化成
    「每个插件只能有一行配置」，把插件分身整个锁死。
    """
    index = next(
        item for item in PluginConfig.__table__.indexes
        if item.name == "ux_pluginconfig_default_target"
    )
    ddl = sa.schema.CreateIndex(index)

    assert str(ddl.compile(dialect=sqlite.dialect())).strip() == (
        "CREATE UNIQUE INDEX ux_pluginconfig_default_target "
        "ON pluginconfig (plugin_id) WHERE is_default_target IS 1"
    )
    assert str(ddl.compile(dialect=postgresql.dialect())).strip() == (
        "CREATE UNIQUE INDEX ux_pluginconfig_default_target "
        "ON pluginconfig (plugin_id) WHERE is_default_target IS true"
    )


def test_default_target_defaults_to_false_for_new_rows(db):
    """新建实例默认不是调用目标——默认调用目标必须由用户显式选定。"""
    db.add(PluginConfig(plugin_id="PluginL", instance_id="default"))

    assert PluginConfig.get_by_instance(db.session, "PluginL", "default").is_default_target is False
    assert PluginConfig.get_default_target(db.session, "PluginL") is None


# --------------------------------------------------------------------------- #
# 运行态定位与调用目标的边界
# --------------------------------------------------------------------------- #

@pytest.fixture
def call_target_manager():
    """构造隔离的插件管理器，供调用目标解析用例登记运行实例。"""
    from app.runtime.extensions.plugin_manager import PluginManager
    from app.foundation.singleton import Singleton

    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def test_call_target_hits_exact_instance_key(call_target_manager, instance_targets):
    """传实例键时精确命中，不经过默认调用目标裁决。"""
    call_target_manager._running_plugins["PluginP@alt"] = "alt-instance"

    assert call_target_manager._resolve_call_target("PluginP@alt") == "alt-instance"
    assert call_target_manager._resolve_call_target("PluginP@missing") is None


def test_call_target_without_any_running_instance_is_not_loaded(
    call_target_manager, instance_targets
):
    """一个实例都没在跑属于插件未加载，返回空而不是报「选不出目标」。"""
    instance_targets("PluginP", [_target("alt", default=True)])

    assert call_target_manager._resolve_call_target("PluginP") is None


def test_call_target_refuses_the_sole_running_instance_without_default(
    call_target_manager, instance_targets
):
    """插件只有一个实例在跑但未设默认调用目标时报错，不拿它顶替默认实例。"""
    call_target_manager._running_plugins["PluginP@alt"] = "alt-instance"
    instance_targets("PluginP", [_target("alt")])

    with pytest.raises(LookupError) as excinfo:
        call_target_manager._resolve_call_target("PluginP")
    assert "alt（已启用）" in str(excinfo.value)


def test_call_target_follows_the_designated_default(
    call_target_manager, instance_targets
):
    """设定默认调用目标后，按插件标识发起的调用落到该实例。"""
    call_target_manager._running_plugins["PluginP@alt"] = "alt-instance"
    instance_targets("PluginP", [_target("alt", default=True)])

    assert call_target_manager._resolve_call_target("PluginP") == "alt-instance"


def test_call_target_refuses_disabled_default_among_running_instances(
    call_target_manager, instance_targets
):
    """默认调用目标已停用时报错，不改走另一个在跑的实例。"""
    call_target_manager._running_plugins.update(
        {"PluginP@alt": "alt-instance", "PluginP@spare": "spare-instance"}
    )
    instance_targets(
        "PluginP", [_target("alt", enabled=False, default=True), _target("spare")]
    )

    with pytest.raises(LookupError) as excinfo:
        call_target_manager._resolve_call_target("PluginP")
    message = str(excinfo.value)
    assert "alt（已停用）" in message
    assert "spare（已启用）" in message


def test_class_level_attribute_read_tolerates_undecidable_target(
    call_target_manager, instance_targets
):
    """读类级属性取任一运行实例即可，不受默认调用目标是否可裁决影响。"""

    class _Carrier:
        plugin_name = "分身插件"

    call_target_manager._running_plugins["PluginP@alt"] = _Carrier()
    instance_targets("PluginP", [_target("alt")])

    assert call_target_manager.get_plugin_attr("PluginP", "plugin_name") == "分身插件"


def test_run_plugin_method_reports_undecidable_target(
    call_target_manager, instance_targets
):
    """按插件标识调用方法而目标裁决不出来时，错误必须冒出来而不是返回空。"""

    class _Carrier:
        def ping(self) -> str:
            return "pong"

    call_target_manager._running_plugins["PluginP@alt"] = _Carrier()
    instance_targets("PluginP", [_target("alt")])

    with pytest.raises(LookupError):
        call_target_manager.run_plugin_method("PluginP", "ping")
    assert call_target_manager.run_plugin_method("PluginP@alt", "ping") == "pong"
    assert call_target_manager.run_plugin_method("PluginQ", "ping") is None


# --------------------------------------------------------------------------- #
# 组合根装配
# --------------------------------------------------------------------------- #

def test_composition_root_lister_reports_persisted_state(db):
    """组合根注入的读取钩子必须如实映射配置行的启用态与置位。"""
    from app.startup.plugins_initializer import _list_plugin_instance_targets

    db.add(PluginConfig(plugin_id="PluginM", instance_id="default", is_enabled=False),
           PluginConfig(plugin_id="PluginM", instance_id="alt",
                        is_enabled=True, is_default_target=True),
           PluginConfig(plugin_id="PluginN", instance_id="default", is_enabled=True))

    targets = {item.instance_id: item for item in _list_plugin_instance_targets("PluginM")}
    assert set(targets) == {"default", "alt"}
    assert targets["default"].is_enabled is False
    assert targets["default"].is_default_target is False
    assert targets["alt"].is_enabled is True
    assert targets["alt"].is_default_target is True

    assert select_plugin_instance_id(
        "PluginM", _list_plugin_instance_targets("PluginM")
    ) == "alt"
