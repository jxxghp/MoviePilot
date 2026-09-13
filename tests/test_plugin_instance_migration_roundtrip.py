"""插件实例迁移链在真实 Alembic 运行器上的升降级往返验证。

同目录下按单个 revision 调 ``upgrade()`` 的用例只验证单步搬迁逻辑，绕过了
Alembic 自己的版本解析、顺序编排与 ``alembic_version`` 记账。真实事故恰恰出在
这一层：链接错的 ``down_revision``、少写的 ``downgrade``、第二次升级时重复建表，
单步用例一个都发现不了。本模块因此走 ``alembic.command``，在一个落盘的临时库上
从官方 head 一路升到链尾再降回去，验证三个迁移串起来确实可用。
"""

from __future__ import annotations

import json
from configparser import ConfigParser
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 官方 v3 的迁移链末端，也是本特性三个迁移的共同起点
OFFICIAL_HEAD = "b2d4f6a8c1e3"
CHAIN_HEAD = "487f7e681955"
CHAIN = ("281965691a20", "c4e1a7b9d2f6", CHAIN_HEAD)

_TABLE = "plugininstance"
_LEGACY_KEY = "PluginInstances"

# 一条分身描述符、两条插件配置（本体一条、分身一条），照搬运行期真实形状
_LEGACY_INSTANCES = {
    "DemoPluginWork": {
        "instance_id": "DemoPluginWork",
        "source_plugin_id": "DemoPlugin",
        "plugin_name": "工作实例",
        "plugin_desc": "分身描述",
        "plugin_icon": "work.svg",
    },
}
_HOST_CONFIG = {"enabled": True, "cron": "0 1 * * *", "notify": False}
_CLONE_CONFIG = {"enabled": False, "cron": "0 2 * * *", "notify": True}


def _instance_table() -> sa.TableClause:
    """按列类型声明实例表，使 JSON 配置列读出时自动解码。"""
    return sa.table(
        _TABLE,
        sa.column("instance_id", sa.String()),
        sa.column("source_plugin_id", sa.String()),
        sa.column("plugin_name", sa.String()),
        sa.column("plugin_desc", sa.String()),
        sa.column("plugin_icon", sa.String()),
        sa.column("log_level", sa.String()),
        sa.column("log_expires_at", sa.String()),
        sa.column("config_data", sa.JSON()),
    )


def _seed_legacy_rows(engine: sa.Engine) -> None:
    """写入迁移前的 systemconfig 存量：实例描述符单键与两条 plugin.* 配置键。"""
    with engine.begin() as connection:
        for key, value in (
            (_LEGACY_KEY, _LEGACY_INSTANCES),
            ("plugin.DemoPlugin", _HOST_CONFIG),
            ("plugin.DemoPluginWork", _CLONE_CONFIG),
        ):
            connection.execute(
                sa.text("INSERT INTO systemconfig (key, value) VALUES (:key, :value)"),
                {"key": key, "value": json.dumps(value)},
            )


def _systemconfig_keys(engine: sa.Engine) -> set[str]:
    """读取当前库里的全部 systemconfig 键。"""
    with engine.connect() as connection:
        return {
            row[0]
            for row in connection.execute(sa.text("SELECT key FROM systemconfig"))
        }


def _systemconfig_value(engine: sa.Engine, key: str):
    """读取一条 systemconfig 值并解码，键不存在时返回 ``None``。"""
    with engine.connect() as connection:
        row = connection.execute(
            sa.text("SELECT value FROM systemconfig WHERE key = :key"), {"key": key}
        ).first()
    if row is None:
        return None
    value = row[0]
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


def _instance_rows(engine: sa.Engine) -> dict[str, sa.Row]:
    """按实例 ID 读出实例表全部行。"""
    table = _instance_table()
    with engine.connect() as connection:
        return {
            row.instance_id: row
            for row in connection.execute(sa.select(table))
        }


def _current_revision(engine: sa.Engine) -> str | None:
    """读取库里记账的当前 revision。"""
    with engine.connect() as connection:
        if "alembic_version" not in sa.inspect(connection).get_table_names():
            return None
        return connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar()


def _table_names(engine: sa.Engine) -> set[str]:
    """读取库中已有表名。"""
    with engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


@pytest.fixture
def official_head_database(tmp_path):
    """造一个停在官方 head 的落盘 SQLite 库，并塞入迁移前的存量数据。

    官方 head 与本分支的 schema 差异只有 ``plugininstance`` 一张表，因此「按当前
    模型建表但跳过该表，再把版本记账戳到官方 head」与真实的官方库等价；从链条最
    开头逐条重放做不到，因为官方的初始化流程本身是先 ``create_all`` 再升级，早期
    迁移并不自带建表。

    :return: ``(Alembic 配置, 指向该库的 Engine)``
    """
    from app.db.base import Base
    from app.db.models import load_all_models

    load_all_models()
    database_path = tmp_path / "roundtrip.db"
    url = f"sqlite:///{database_path}"
    engine = sa.create_engine(url)
    Base.metadata.create_all(
        engine,
        tables=[
            table
            for name, table in Base.metadata.tables.items()
            if name != _TABLE
        ],
    )

    config = Config()
    # 不读 ini 文件，避免 env.py 对日志配置段的依赖，与应用启动时的构造方式一致
    config.file_config = ConfigParser(interpolation=None)
    config.set_main_option("script_location", str(PROJECT_ROOT / "database"))
    config.set_main_option("sqlalchemy.url", url)
    command.stamp(config, OFFICIAL_HEAD)

    _seed_legacy_rows(engine)
    try:
        yield config, engine
    finally:
        # 建的是整套官方表，单个库文件接近 1MB；pytest 默认还要留最近几轮 tmp_path，
        # 攒下来不小，因此释放句柄后立刻删掉，不把回收推给它
        engine.dispose()
        database_path.unlink(missing_ok=True)


def test_upgrade_from_official_head_lands_legacy_payloads_on_instance_rows(
    official_head_database,
) -> None:
    """整条链升上来后，描述符与两条配置都应落到实例表，旧配置键就地移除。"""
    config, engine = official_head_database

    command.upgrade(config, CHAIN_HEAD)

    assert _current_revision(engine) == CHAIN_HEAD
    rows = _instance_rows(engine)
    assert set(rows) == {"DemoPlugin", "DemoPluginWork"}

    clone = rows["DemoPluginWork"]
    assert clone.source_plugin_id == "DemoPlugin"
    assert clone.plugin_name == "工作实例"
    assert clone.plugin_desc == "分身描述"
    assert clone.plugin_icon == "work.svg"
    assert clone.config_data == _CLONE_CONFIG

    host = rows["DemoPlugin"]
    # 本体没有描述符条目，靠 plugin.<ID> 配置键补建，故身份两列相等
    assert host.source_plugin_id == "DemoPlugin"
    assert host.config_data == _HOST_CONFIG

    # 日志等级列由链尾迁移补出，缺省为空表示跟随全局等级
    assert clone.log_level is None and clone.log_expires_at is None
    assert host.log_level is None and host.log_expires_at is None

    keys = _systemconfig_keys(engine)
    # 同一份配置不能两处各留一份，plugin.* 旧键必须消失
    assert "plugin.DemoPlugin" not in keys
    assert "plugin.DemoPluginWork" not in keys
    # 描述符单键刻意保留作回滚依据
    assert _systemconfig_value(engine, _LEGACY_KEY) == _LEGACY_INSTANCES


def test_downgrade_back_to_official_head_restores_the_legacy_config_keys(
    official_head_database,
) -> None:
    """降回官方 head 不应报错，实例表整张移除且配置回到旧键，回退路径不丢配置。"""
    config, engine = official_head_database
    command.upgrade(config, CHAIN_HEAD)

    command.downgrade(config, OFFICIAL_HEAD)

    assert _current_revision(engine) == OFFICIAL_HEAD
    assert _TABLE not in _table_names(engine)
    assert _systemconfig_value(engine, "plugin.DemoPlugin") == _HOST_CONFIG
    assert _systemconfig_value(engine, "plugin.DemoPluginWork") == _CLONE_CONFIG
    assert _systemconfig_value(engine, _LEGACY_KEY) == _LEGACY_INSTANCES


def test_upgrade_is_idempotent_on_an_already_migrated_database(
    official_head_database,
) -> None:
    """重复升级与降级后重升都不应报错，且不会把行搬成两份。"""
    config, engine = official_head_database
    command.upgrade(config, CHAIN_HEAD)
    first_pass = _instance_rows(engine)

    # 已在链尾时再升一次：Alembic 层面是空操作，验证不会因重复执行而失败
    command.upgrade(config, CHAIN_HEAD)
    assert _current_revision(engine) == CHAIN_HEAD
    assert _instance_rows(engine).keys() == first_pass.keys()

    # 降回官方 head 后重升：三个迁移都要在已有存量的库上重跑一遍
    command.downgrade(config, OFFICIAL_HEAD)
    command.upgrade(config, CHAIN_HEAD)

    assert _current_revision(engine) == CHAIN_HEAD
    replayed = _instance_rows(engine)
    assert set(replayed) == {"DemoPlugin", "DemoPluginWork"}
    assert replayed["DemoPlugin"].config_data == _HOST_CONFIG
    assert replayed["DemoPluginWork"].config_data == _CLONE_CONFIG
    assert replayed["DemoPluginWork"].source_plugin_id == "DemoPlugin"


def test_chain_applies_one_revision_at_a_time_in_the_declared_order(
    official_head_database,
) -> None:
    """逐级升级应按声明顺序推进记账，确认三个迁移的链接方向没有接反。"""
    config, engine = official_head_database

    for revision in CHAIN:
        command.upgrade(config, revision)
        assert _current_revision(engine) == revision

    assert _TABLE in _table_names(engine)
