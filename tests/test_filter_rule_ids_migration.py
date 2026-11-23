"""
用隔离 SQLite 验证旧过滤规则 ID 与规则组引用的原子、保真迁移。
"""

import copy
import importlib
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from app.adapters.system import rust as rust_accel
from app.application import rules
from app.application.rules import RuleParser
from app.domain.context import TorrentInfo
from app.modules.filter import FilterModule
from app.schemas.types import SystemConfigKey

_MIGRATION = "database.versions.a7383c1d9e20_filter_rule_ids"


@pytest.fixture
def migration_database(monkeypatch):
    """
    创建只含系统配置的临时数据库，并把新迁移绑定到调用方事务。
    """
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table(
        "systemconfig", metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(), nullable=False, unique=True),
        sa.Column("value", sa.JSON()),
    )
    metadata.create_all(engine)
    migration = importlib.import_module(_MIGRATION)
    try:
        with engine.begin() as connection:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            yield migration, table, connection
    finally:
        engine.dispose()


def _save(connection, table, key, value) -> None:
    connection.execute(table.insert().values(key=key.value, value=value))


def _read(connection, table, key):
    return connection.execute(sa.select(table.c.value).where(table.c.key == key.value)).scalar_one()


def test_upgrade_preserves_reported_rule_and_group_counts_and_all_other_fields(migration_database) -> None:
    """
    模拟 Issue 的 30 条规则、27 个组和 13 处引用，只改变两个 ID 及相关引用。
    """
    migration, table, connection = migration_database
    custom = [
        {"id": " Pre Candidate", "name": "优先候补组", "include": "QHstudIo|OurTV", "exclude": None},
        {"id": "M-Team", "name": "馒头", "include": "M-Team", "size_range": ">100", "extra": {"keep": True}},
        *[{"id": f"Rule{index}", "name": f"规则{index}"} for index in range(28)],
    ]
    groups = [
        {"name": f"组{index}", "media_type": "电视剧", "category": "国产剧",
         "rule_string": "1080P &  Pre Candidate > 4K" if index < 13 else "1080P",
         "extra": [index]}
        for index in range(27)
    ]
    _save(connection, table, SystemConfigKey.CustomFilterRules, custom)
    _save(connection, table, SystemConfigKey.UserFilterRuleGroups, groups)
    _save(connection, table, SystemConfigKey.SearchFilterRuleGroups, ["组0", "组26"])
    migration.upgrade()
    updated = _read(connection, table, SystemConfigKey.CustomFilterRules)
    updated_groups = _read(connection, table, SystemConfigKey.UserFilterRuleGroups)
    expected = copy.deepcopy(custom)
    expected[0]["id"], expected[1]["id"] = "PreCandidate", "MTeam"
    assert updated == expected
    expected_groups = copy.deepcopy(groups)
    for group in expected_groups[:13]:
        group["rule_string"] = "1080P &  PreCandidate > 4K"
    assert updated_groups == expected_groups
    assert _read(connection, table, SystemConfigKey.SearchFilterRuleGroups) == ["组0", "组26"]
    migration.upgrade()
    migration.downgrade()
    assert _read(connection, table, SystemConfigKey.CustomFilterRules) == expected
    assert _read(connection, table, SystemConfigKey.UserFilterRuleGroups) == expected_groups


def test_migration_avoids_builtin_custom_and_generated_id_collisions(migration_database) -> None:
    """
    清理后的 ID 冲突时追加稳定数字后缀，不覆盖已有规则或另一条迁移结果。
    """
    migration, table, connection = migration_database
    custom = [{"id": value, "name": value} for value in [
        "PreCandidate", "PreCandidate1", "Pre Candidate", "Pre-Candidate", "4-K", "中文", "123",
    ]]
    _save(connection, table, SystemConfigKey.CustomFilterRules, custom)
    _save(connection, table, SystemConfigKey.UserFilterRuleGroups, [{
        "name": "优先级", "rule_string": "Pre Candidate > Pre-Candidate > 4-K > 中文 > 123",
    }])
    migration.upgrade()
    updated = _read(connection, table, SystemConfigKey.CustomFilterRules)
    assert [rule["id"] for rule in updated] == [
        "PreCandidate", "PreCandidate1", "PreCandidate2", "PreCandidate3", "4K1", "Rule", "Rule123",
    ]
    expression = _read(connection, table, SystemConfigKey.UserFilterRuleGroups)[0]["rule_string"]
    assert expression == "PreCandidate2 > PreCandidate3 > 4K1 > Rule > Rule123"
    for level in expression.split(">"):
        assert RuleParser().parse(level).as_list()


def test_reference_rewrite_only_replaces_complete_operands(migration_database) -> None:
    """
    同名片段不能被改写，括号和否定中的完整引用则同步迁移并保留排版。
    """
    migration, table, connection = migration_database
    _save(connection, table, SystemConfigKey.CustomFilterRules, [
        {"id": "M-Team", "name": "馒头"}, {"id": "Prefix-M-Team", "name": "前缀组"},
    ])
    _save(connection, table, SystemConfigKey.UserFilterRuleGroups, [
        {"name": "完整引用", "rule_string": "!(M-Team | Prefix-M-Team) & 1080P"},
        {"name": "相似片段", "rule_string": "Unknown-M-Team > M-Teams"},
        {"name": "空草稿", "rule_string": None},
    ])
    migration.upgrade()
    assert _read(connection, table, SystemConfigKey.UserFilterRuleGroups) == [
        {"name": "完整引用", "rule_string": "!(MTeam | PrefixMTeam) & 1080P"},
        {"name": "相似片段", "rule_string": "Unknown-M-Team > M-Teams"},
        {"name": "空草稿", "rule_string": None},
    ]


@pytest.mark.parametrize("value", [None, []])
def test_empty_custom_rules_do_not_create_or_rewrite_configuration(migration_database, value) -> None:
    """
    新安装及没有自定义规则的配置保持原值，不创建额外规则组。
    """
    migration, table, connection = migration_database
    _save(connection, table, SystemConfigKey.CustomFilterRules, value)
    migration.upgrade()
    assert _read(connection, table, SystemConfigKey.CustomFilterRules) == value
    assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 1


def test_ambiguous_whitespace_ids_fail_without_partial_changes(migration_database) -> None:
    """
    去除外围空白后无法唯一定位引用时明确失败，不能猜测后覆盖用户规则。
    """
    migration, table, connection = migration_database
    custom = [{"id": " Rule", "name": "旧规则"}, {"id": "Rule", "name": "合法规则"}]
    groups = [{"name": "组", "rule_string": "Rule"}]
    _save(connection, table, SystemConfigKey.CustomFilterRules, custom)
    _save(connection, table, SystemConfigKey.UserFilterRuleGroups, groups)
    with pytest.raises(RuntimeError, match="无法唯一定位"):
        migration.upgrade()
    assert _read(connection, table, SystemConfigKey.CustomFilterRules) == custom
    assert _read(connection, table, SystemConfigKey.UserFilterRuleGroups) == groups


def test_whitespace_id_cannot_take_over_builtin_references(migration_database) -> None:
    """
    带外围空白的旧 ID 与内置 ID 相同也必须拒绝猜测，不能改写原本有效的内置引用。
    """
    migration, table, connection = migration_database
    custom = [{"id": " 4K", "name": "旧规则"}]
    _save(connection, table, SystemConfigKey.CustomFilterRules, custom)
    _save(connection, table, SystemConfigKey.UserFilterRuleGroups, [{"name": "组", "rule_string": "4K"}])
    with pytest.raises(RuntimeError, match="无法唯一定位"):
        migration.upgrade()
    assert _read(connection, table, SystemConfigKey.CustomFilterRules) == custom


@pytest.mark.parametrize("accelerated", [False, True])
def test_migrated_ids_restore_filtering_without_changing_rule_content(migration_database, monkeypatch, accelerated) -> None:
    """
    迁移后相同规则内容在真实 Python/Rust 过滤入口重新生效，而不是跳过坏规则。
    """
    migration, table, connection = migration_database
    _save(connection, table, SystemConfigKey.CustomFilterRules, [{"id": "M-Team", "name": "馒头", "include": "M-Team"}])
    _save(connection, table, SystemConfigKey.UserFilterRuleGroups, [{"name": "组", "rule_string": "1080P & M-Team"}])
    configuration = SimpleNamespace(get=lambda key: _read(connection, table, key))
    monkeypatch.setattr(rules, "get_configured_system_config", lambda: configuration)
    monkeypatch.setattr("app.modules.filter.rust_accel.is_enabled", lambda: accelerated)
    module = FilterModule()
    module.init_module()
    keep = TorrentInfo(title="Show S01E01 1080p M-Team", description="")
    drop = TorrentInfo(title="Show S01E01 1080p OtherGroup", description="")
    with pytest.raises(ValueError, match="无效的规则 ID"):
        module.filter_torrents(["组"], [keep, drop])
    migration.upgrade()
    module.init_module()
    assert module.filter_torrents(["组"], [keep, drop]) == [keep]
    assert keep.pri_order == 100
    assert module.rule_set["MTeam"]["include"] == "M-Team"
    native = rust_accel.filter_torrents(
        groups=[{"name": "组", "rule_string": "1080P & MTeam"}],
        torrent_list=[keep, drop], rule_set=module.rule_set,
    )
    if accelerated:
        assert native is not None
        assert native[0] == [(0, 100)]
    else:
        assert native is None


def test_data_migration_belongs_to_single_startup_lineage() -> None:
    """
    新迁移连接现有 head，启动更新能够自动发现它，不依赖用户调用改名接口。
    """
    config = Config()
    config.set_main_option("script_location", "database")
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert "a7383c1d9e20" in {item.revision for item in script.walk_revisions()}
    assert script.get_revision("a7383c1d9e20").down_revision == "b849e613a72f"


def test_failure_between_config_writes_rolls_back_both_rows(monkeypatch) -> None:
    """
    两个配置更新之间的数据库错误回滚整个迁移，不留下只改 ID 而未改引用的状态。
    """
    migration = importlib.import_module(_MIGRATION)
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table("systemconfig", metadata, sa.Column("key", sa.String(), primary_key=True),
                     sa.Column("value", sa.JSON()))
    custom = [{"id": "M-Team", "name": "馒头"}]
    groups = [{"name": "组", "rule_string": "M-Team"}]
    metadata.create_all(engine)
    with engine.begin() as connection:
        _save(connection, table, SystemConfigKey.CustomFilterRules, custom)
        _save(connection, table, SystemConfigKey.UserFilterRuleGroups, groups)
    writes = []

    def fail_second_write(_connection, _cursor, statement, _parameters, _context, _executemany):
        """
        在第二个配置写入前注入故障，验证第一个写入也没有独立提交。
        """
        if statement.startswith("UPDATE"):
            writes.append(statement)
            if len(writes) == 2:
                raise RuntimeError("模拟第二次配置写入失败")

    sa.event.listen(engine, "before_cursor_execute", fail_second_write)
    try:
        with pytest.raises(RuntimeError, match="第二次配置写入失败"):
            with engine.begin() as connection:
                monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
                migration.upgrade()
        with engine.connect() as connection:
            assert _read(connection, table, SystemConfigKey.CustomFilterRules) == custom
            assert _read(connection, table, SystemConfigKey.UserFilterRuleGroups) == groups
    finally:
        sa.event.remove(engine, "before_cursor_execute", fail_second_write)
        engine.dispose()
