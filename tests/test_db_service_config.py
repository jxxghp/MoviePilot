"""
服务实例配置表的数据库层约束与数据访问行为。

下载器 / 媒体服务器 / 消息渠道的实例配置从 systemconfig 的一坨 JSON 列表搬进独立表，
换来的正是 JSON 拿不到的那两条数据库判定：``(capability, type, name)`` 唯一，且每族至多
一个默认调用目标。这两条必须由数据库真实拦下，而不是只在应用层判断一次就当作已经拦住。

``provider`` 只记账不判据，因此它既不能进唯一约束（否则扩展换标识重装会让同名配置变成
两条），又要能按它筛出「提供方已消失」的配置。
"""
from typing import Optional

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError

from app.db.models.serviceconfig import BUILTIN_PROVIDER, ServiceConfig
from app.db.oper.serviceconfig import ServiceConfigNameConflictError, ServiceConfigOper


@pytest.fixture(autouse=True)
def _track(db):
    """把本文件涉及的表纳入用例级回收。"""
    db.watermark(ServiceConfig)


def _conf(
    capability: str,
    service_type: str,
    name: str,
    *,
    enabled: bool = False,
    config: Optional[dict] = None,
    provider: str = BUILTIN_PROVIDER,
    default: bool = False,
) -> ServiceConfig:
    """构造一条服务实例配置记录。"""
    return ServiceConfig(
        capability=capability,
        type=service_type,
        name=name,
        enabled=enabled,
        config=config,
        provider=provider,
        is_default_target=default,
    )


# --------------------------------------------------------------------------- #
# 数据库层：实例名唯一
# --------------------------------------------------------------------------- #

def test_unique_identity_blocks_duplicate_at_db_level(db):
    """同一 (capability, type, name) 写第二行时，数据库唯一约束必须直接拦下。"""
    db.add(_conf("downloader", "qbittorrent", "mp-test-dup"))

    with pytest.raises(IntegrityError):
        db.session.add(_conf("downloader", "qbittorrent", "mp-test-dup"))
        db.session.commit()
    db.session.rollback()

    assert len(ServiceConfig.list_by_type(db.session, "downloader", "qbittorrent")) == 1


def test_unique_identity_blocks_duplicate_across_providers(db):
    """
    唯一约束必须跨 provider 生效。

    provider 一旦进唯一键，插件重装换个标识后同名配置就会变成两条，用户会在设置页上
    看到两个一模一样的下载器。
    """
    db.add(_conf("downloader", "qbittorrent", "mp-test-cross", provider="OldPlugin"))

    with pytest.raises(IntegrityError):
        db.session.add(
            _conf("downloader", "qbittorrent", "mp-test-cross", provider="NewPlugin")
        )
        db.session.commit()
    db.session.rollback()

    rows = ServiceConfig.list_by_type(db.session, "downloader", "qbittorrent")
    assert [row.provider for row in rows] == ["OldPlugin"]


def test_provider_is_not_part_of_the_unique_constraint():
    """结构上钉住：唯一约束的列恰好是身份三元组，provider 不在其中。"""
    constraint = next(
        item for item in ServiceConfig.__table__.constraints
        if getattr(item, "name", None) == "ux_serviceconfig_capability_type_name"
    )
    assert [column.name for column in constraint.columns] == ["capability", "type", "name"]


def test_same_name_is_allowed_in_another_type_or_capability(db):
    """实例名只在同族同类型内唯一，换类型或换族都不冲突。"""
    db.add(
        _conf("downloader", "qbittorrent", "mp-test-same"),
        _conf("downloader", "transmission", "mp-test-same"),
        _conf("mediaserver", "qbittorrent", "mp-test-same"),
    )

    assert ServiceConfig.get_by_identity(
        db.session, "downloader", "transmission", "mp-test-same"
    ) is not None
    assert ServiceConfig.get_by_identity(
        db.session, "mediaserver", "qbittorrent", "mp-test-same"
    ) is not None


# --------------------------------------------------------------------------- #
# 数据库层：每族至多一个默认调用目标
# --------------------------------------------------------------------------- #

def test_partial_unique_index_blocks_second_default_target_in_one_capability(db):
    """同一族出现第二个默认调用目标时，条件唯一索引必须拦死。"""
    db.add(_conf("downloader", "qbittorrent", "mp-test-def-a", default=True))

    with pytest.raises(IntegrityError):
        db.session.add(_conf("downloader", "transmission", "mp-test-def-b", default=True))
        db.session.commit()
    db.session.rollback()

    assert ServiceConfig.get_default_target(db.session, "downloader").name == "mp-test-def-a"


def test_each_capability_keeps_its_own_default_target(db):
    """索引作用域是族而非全表，不同族可以各有一个默认调用目标。"""
    db.add(
        _conf("downloader", "qbittorrent", "mp-test-scope-dl", default=True),
        _conf("mediaserver", "emby", "mp-test-scope-ms", default=True),
    )

    assert ServiceConfig.get_default_target(db.session, "downloader").name == "mp-test-scope-dl"
    assert ServiceConfig.get_default_target(db.session, "mediaserver").name == "mp-test-scope-ms"


def test_partial_unique_index_covers_storage_rows_too(db):
    """存储族的默认调用目标同样由条件唯一索引判定，与三族一条规则。"""
    db.add(_conf("storage", "u115", "mp-test-storage-def-a", default=True))

    with pytest.raises(IntegrityError):
        db.session.add(_conf("storage", "alipan", "mp-test-storage-def-b", default=True))
        db.session.commit()
    db.session.rollback()

    assert ServiceConfig.get_default_target(
        db.session, "storage"
    ).name == "mp-test-storage-def-a"


def test_storage_bare_token_pointer_never_touches_the_default_target_column(db):
    """裸令牌兼容指针落宿主载荷，同类型多份自称也不占用族级那一行。"""
    db.add(
        _conf("storage", "u115", "mp-test-ptr-a"),
        _conf("storage", "u115", "mp-test-ptr-b"),
    )
    for name in ("mp-test-ptr-a", "mp-test-ptr-b"):
        ServiceConfig.update_by_identity(
            db.session, "storage", "u115", name, {"host_config": {"bare_token_target": True}}
        )

    rows = ServiceConfig.list_by_type(db.session, "storage", "u115")

    assert [row.host_config for row in rows] == [
        {"bare_token_target": True}, {"bare_token_target": True}
    ]
    assert ServiceConfig.get_default_target(db.session, "storage") is None


def test_many_non_default_rows_coexist_in_one_capability(db):
    """未置位的行不入索引，同族可以有任意多行不是默认调用目标。"""
    db.add(
        _conf("notification", "telegram", "mp-test-many-a"),
        _conf("notification", "telegram", "mp-test-many-b"),
        _conf("notification", "slack", "mp-test-many-c"),
    )

    rows = ServiceConfig.list_by_capability(db.session, "notification")
    assert [row.name for row in rows] == [
        "mp-test-many-a", "mp-test-many-b", "mp-test-many-c"
    ]
    assert ServiceConfig.get_default_target(db.session, "notification") is None


def test_default_target_defaults_to_false_for_new_rows(db):
    """新建实例默认不是调用目标——默认调用目标必须由用户显式选定。"""
    db.add(ServiceConfig(capability="downloader", type="qbittorrent", name="mp-test-new"))

    row = ServiceConfig.get_by_identity(db.session, "downloader", "qbittorrent", "mp-test-new")
    assert row.is_default_target is False
    assert row.enabled is False
    assert row.provider == BUILTIN_PROVIDER


def test_model_default_target_index_is_partial_in_both_dialects():
    """
    模型（全新安装的 create_all 路径）建出的索引在两种方言下都必须带谓词。

    本仓的测试库是 SQLite，PostgreSQL 分支只能靠编译期 DDL 证明：布尔列在 PG 下不能与
    整数比较，谓词若不分方言就会在 PG 侧建索引时失败；谓词整个丢失则退化成「每族只能有
    一行配置」，把多实例整个锁死。
    """
    index = next(
        item for item in ServiceConfig.__table__.indexes
        if item.name == "ux_serviceconfig_default_target"
    )
    ddl = sa.schema.CreateIndex(index)

    assert str(ddl.compile(dialect=sqlite.dialect())).strip() == (
        "CREATE UNIQUE INDEX ux_serviceconfig_default_target "
        "ON serviceconfig (capability) WHERE is_default_target IS 1"
    )
    assert str(ddl.compile(dialect=postgresql.dialect())).strip() == (
        "CREATE UNIQUE INDEX ux_serviceconfig_default_target "
        "ON serviceconfig (capability) WHERE is_default_target IS true"
    )


# --------------------------------------------------------------------------- #
# provider：记账而非判据
# --------------------------------------------------------------------------- #

def test_builtin_provider_is_distinguishable_from_any_extension_identifier():
    """
    内建保留值必须与任何合法扩展标识区分得开。

    扩展标识取插件主类名（Python 标识符），实例键形如 ``扩展标识@实例标识``；保留值里
    的冒号出现在 ``@`` 之前，因此任何扩展实例键都构造不出它。留空则做不到这条区分，
    「内建也可禁用」那天会退回布尔式的「是不是插件」。
    """
    from app.runtime.extensions.contract.instance import instance_key

    head = BUILTIN_PROVIDER.split("@")[0]
    assert not head.isidentifier()
    assert instance_key("Builtin") != BUILTIN_PROVIDER
    assert instance_key("Builtin", "builtin") != BUILTIN_PROVIDER


def test_list_by_provider_filters_rows(db):
    """按提供方能筛出其名下的全部配置，不限族。"""
    db.add(
        _conf("downloader", "custom", "mp-test-p1", provider="PluginA"),
        _conf("mediaserver", "custom", "mp-test-p2", provider="PluginA"),
        _conf("downloader", "custom", "mp-test-p3", provider="PluginB"),
    )

    rows = ServiceConfig.list_by_provider(db.session, "PluginA")
    assert {row.name for row in rows} == {"mp-test-p1", "mp-test-p2"}


def test_list_with_absent_provider_reports_only_vanished_providers(db):
    """
    提供方不在场的配置能被一次筛出，内建保留值恒视为在场。

    这是加 provider 列的直接目的：把「没有这个类型」翻译成「该类型由扩展 X 提供，
    X 当前未启用」。
    """
    db.add(
        _conf("downloader", "qbittorrent", "mp-test-absent-builtin"),
        _conf("downloader", "custom", "mp-test-absent-live", provider="LivePlugin"),
        _conf("downloader", "custom", "mp-test-absent-gone", provider="GonePlugin"),
    )

    rows = ServiceConfig.list_with_absent_provider(db.session, ["LivePlugin"])
    assert [row.name for row in rows] == ["mp-test-absent-gone"]


def test_absent_provider_does_not_affect_the_row_itself(db):
    """
    提供方失效只影响提示文案，不影响配置本身。

    配置与类型的连接键是 ``(capability, type)``，provider 不参与定位，因此按身份三元组
    仍能原样取到这一行——这与第三方身份绑定表相反，那里 provider 是唯一键的一部分。
    """
    db.add(_conf("downloader", "custom", "mp-test-orphan", enabled=True, provider="GonePlugin"))

    orphaned = ServiceConfig.list_with_absent_provider(db.session, [])
    assert [row.name for row in orphaned] == ["mp-test-orphan"]

    row = ServiceConfig.get_by_identity(db.session, "downloader", "custom", "mp-test-orphan")
    assert row.enabled is True


# --------------------------------------------------------------------------- #
# 数据访问层：友好错误、精确读写、默认调用目标
# --------------------------------------------------------------------------- #

def test_oper_add_rejects_duplicate_name_with_readable_error(db):
    """预检查命中冲突时抛领域异常，且提示里带得出用户改得动的信息。"""
    oper = ServiceConfigOper(db=db.session)
    oper.add("downloader", "qbittorrent", "mp-test-oper-dup")

    with pytest.raises(ServiceConfigNameConflictError) as raised:
        oper.add("downloader", "qbittorrent", "mp-test-oper-dup")

    message = str(raised.value)
    assert "mp-test-oper-dup" in message
    assert "qbittorrent" in message


def test_oper_add_translates_db_level_conflict_into_readable_error(db, monkeypatch):
    """
    竞态下预检查漏过冲突（另一请求刚好在两次查询之间写入），唯一约束兜底拦截时同样
    要给出领域异常，而不是把 IntegrityError 泄露给调用方。
    """
    oper = ServiceConfigOper(db=db.session)
    oper.add("downloader", "qbittorrent", "mp-test-oper-race")
    monkeypatch.setattr(oper, "get", lambda *_args, **_kwargs: None)

    with pytest.raises(ServiceConfigNameConflictError):
        oper.add("downloader", "qbittorrent", "mp-test-oper-race")
    db.session.rollback()


def test_oper_add_returns_a_readable_row(db):
    """新增后返回的行可安全读取，且不会顺手成为默认调用目标。"""
    oper = ServiceConfigOper(db=db.session)

    row = oper.add(
        "downloader", "qbittorrent", "mp-test-oper-add",
        config={"host": "127.0.0.1"}, enabled=True, provider="PluginA",
    )

    assert row.config == {"host": "127.0.0.1"}
    assert row.enabled is True
    assert row.provider == "PluginA"
    assert row.is_default_target is False


def test_oper_get_and_list_read_back_what_was_written(db):
    """按族列出与按身份三元组精确取，读回的是写进去的那些行。"""
    oper = ServiceConfigOper(db=db.session)
    oper.add("notification", "telegram", "mp-test-read-a")
    oper.add("notification", "slack", "mp-test-read-b")

    assert [row.name for row in oper.list_by_capability("notification")] == [
        "mp-test-read-a", "mp-test-read-b"
    ]
    assert oper.get("notification", "slack", "mp-test-read-b").type == "slack"
    assert oper.get("notification", "slack", "mp-test-missing") is None


def test_oper_update_writes_only_whitelisted_fields(db):
    """
    通用更新只改白名单内的列。

    默认调用目标不在白名单里：它的「清旧再置新」必须走专用入口，否则并发下两行同时
    为真的窗口就回来了。
    """
    oper = ServiceConfigOper(db=db.session)
    oper.add("downloader", "qbittorrent", "mp-test-upd", config={"host": "old"})

    assert oper.update(
        "downloader", "qbittorrent", "mp-test-upd",
        {"config": {"host": "new"}, "enabled": True, "is_default_target": True},
    ) is True

    row = oper.get("downloader", "qbittorrent", "mp-test-upd")
    assert row.config == {"host": "new"}
    assert row.enabled is True
    assert row.is_default_target is False


def test_oper_update_returns_false_when_nothing_matches(db):
    """目标不存在或没有可写列时返回 False，不静默当作成功。"""
    oper = ServiceConfigOper(db=db.session)
    oper.add("downloader", "qbittorrent", "mp-test-upd-none")

    assert oper.update("downloader", "qbittorrent", "mp-test-absent", {"enabled": True}) is False
    assert oper.update("downloader", "qbittorrent", "mp-test-upd-none", {"capability": "x"}) is False


def test_oper_update_rejects_rename_onto_an_existing_name(db):
    """改名撞上同族同类型下的既有实例名时，给出领域异常而不是 IntegrityError。"""
    oper = ServiceConfigOper(db=db.session)
    oper.add("downloader", "qbittorrent", "mp-test-rename-a")
    oper.add("downloader", "qbittorrent", "mp-test-rename-b")

    with pytest.raises(ServiceConfigNameConflictError):
        oper.update("downloader", "qbittorrent", "mp-test-rename-a", {"name": "mp-test-rename-b"})

    assert oper.get("downloader", "qbittorrent", "mp-test-rename-a") is not None


def test_oper_update_can_rename_to_a_free_name(db):
    """名字没被占用时改名照常生效。"""
    oper = ServiceConfigOper(db=db.session)
    oper.add("downloader", "qbittorrent", "mp-test-rename-src")

    assert oper.update(
        "downloader", "qbittorrent", "mp-test-rename-src", {"name": "mp-test-rename-dst"}
    ) is True
    assert oper.get("downloader", "qbittorrent", "mp-test-rename-dst") is not None


def test_oper_set_default_target_moves_the_flag_within_the_capability(db):
    """改选默认调用目标时清旧置新一次完成，同族始终至多一行为真。"""
    oper = ServiceConfigOper(db=db.session)
    oper.add("downloader", "qbittorrent", "mp-test-dt-a")
    oper.add("downloader", "transmission", "mp-test-dt-b")

    assert oper.set_default_target("downloader", "qbittorrent", "mp-test-dt-a") is True
    assert oper.get_default_target("downloader").name == "mp-test-dt-a"

    assert oper.set_default_target("downloader", "transmission", "mp-test-dt-b") is True
    assert oper.get_default_target("downloader").name == "mp-test-dt-b"
    assert sum(
        1 for row in oper.list_by_capability("downloader") if row.is_default_target
    ) == 1


def test_oper_set_default_target_keeps_the_old_one_when_target_is_absent(db):
    """
    目标实例不存在时不动原有置位。

    先清后置一旦在目标缺席时执行到一半，该族就会从「有默认调用目标」变成「没有」，
    调用方却只看到一个失败返回值。
    """
    oper = ServiceConfigOper(db=db.session)
    oper.add("mediaserver", "emby", "mp-test-dt-keep")
    oper.set_default_target("mediaserver", "emby", "mp-test-dt-keep")

    assert oper.set_default_target("mediaserver", "emby", "mp-test-dt-gone") is False
    assert oper.get_default_target("mediaserver").name == "mp-test-dt-keep"


def test_oper_clear_default_target_leaves_the_capability_without_one(db):
    """清除置位后该族不再有默认调用目标，配置行本身不受影响。"""
    oper = ServiceConfigOper(db=db.session)
    oper.add("mediaserver", "jellyfin", "mp-test-dt-clear")
    oper.set_default_target("mediaserver", "jellyfin", "mp-test-dt-clear")

    assert oper.clear_default_target("mediaserver") == 1
    assert oper.get_default_target("mediaserver") is None
    assert oper.get("mediaserver", "jellyfin", "mp-test-dt-clear") is not None


def test_oper_delete_removes_only_the_named_instance(db):
    """删除按身份三元组定位，不波及同类型的其它实例。"""
    oper = ServiceConfigOper(db=db.session)
    oper.add("downloader", "qbittorrent", "mp-test-del-a")
    oper.add("downloader", "qbittorrent", "mp-test-del-b")

    assert oper.delete("downloader", "qbittorrent", "mp-test-del-a") is True
    assert oper.delete("downloader", "qbittorrent", "mp-test-del-a") is False
    assert oper.get("downloader", "qbittorrent", "mp-test-del-b") is not None


def test_capabilities_read_from_the_table_not_from_systemconfig(db):
    """
    三族实例配置的事实源是本表：表里的行读得到，systemconfig 的同名键不再参与。

    systemconfig 上那三个键只停写不删，留作回退用的历史快照。快照仍在，因此这条断言
    必须同时证明「表里的行读得出来」和「快照里的条目读不出来」，只验前者的话，读路径
    退回快照时用例照样绿。
    """
    from app.runtime.extensions.service_config import (
        configure_service_config_reader,
        service_capability_configs,
    )
    from app.schemas.types import SystemConfigKey

    db.add(_conf("downloader", "qbittorrent", "mp-test-table-reader", enabled=True))

    stale = [{"name": "mp-test-systemconfig", "type": "qbittorrent", "enabled": True}]
    previous = configure_service_config_reader(
        lambda key: stale if key == SystemConfigKey.Downloaders else None
    )
    try:
        configs = service_capability_configs("downloader")
    finally:
        configure_service_config_reader(previous)

    assert [conf.name for conf in configs] == ["mp-test-table-reader"]


def test_host_consumed_fields_survive_the_round_trip(db):
    """
    宿主消费的实例级字段进 ``host_config``，读出来仍在配置模型顶层。

    这些字段（路径映射、场景开关、同步媒体库与同步间隔）由宿主而不是类型实现读取，
    因此不能混进 ``config``——声明了 ``additionalProperties: false`` 的类型会把它们判为
    违约；也不能各建一列，否则宿主每加一个实例级字段就要改一次表结构。
    """
    from app.runtime.extensions.service_config import service_capability_configs

    db.session.add(
        ServiceConfig(
            capability="mediaserver",
            type="emby",
            name="mp-test-host-fields",
            enabled=True,
            config={"host": "h"},
            host_config={"sync_libraries": ["1", "2"], "sync_interval": 6},
        )
    )
    db.session.commit()

    conf = next(
        item for item in service_capability_configs("mediaserver")
        if item.name == "mp-test-host-fields"
    )
    assert conf.sync_libraries == ["1", "2"]
    assert conf.sync_interval == 6
    assert conf.config == {"host": "h"}


def test_host_payload_cannot_hijack_the_row_identity(db):
    """``host_config`` 是用户可写的 JSON，混进身份键时不得顶掉行本身的身份。"""
    from app.runtime.extensions.service_config import service_capability_configs

    db.session.add(
        ServiceConfig(
            capability="notification",
            type="telegram",
            name="mp-test-identity",
            enabled=True,
            host_config={"name": "冒名", "type": "slack", "enabled": False},
        )
    )
    db.session.commit()

    conf = next(
        item for item in service_capability_configs("notification")
        if item.type == "telegram"
    )
    assert conf.name == "mp-test-identity"
    assert conf.enabled is True


def test_oper_writes_one_row_without_reading_the_whole_family(db):
    """
    改一条配置不再需要把整族读出来改回去。

    这正是从 systemconfig 的 JSON 大对象搬出来要换的东西：读改写竞态下，另一条并发
    写入不会被整列表回写覆盖掉。
    """
    oper = ServiceConfigOper(db=db.session)
    oper.add("downloader", "qbittorrent", "mp-test-rmw-a", config={"host": "a"})
    oper.add("downloader", "qbittorrent", "mp-test-rmw-b", config={"host": "b"})

    oper.update("downloader", "qbittorrent", "mp-test-rmw-a", {"config": {"host": "a2"}})

    assert oper.get("downloader", "qbittorrent", "mp-test-rmw-a").config == {"host": "a2"}
    assert oper.get("downloader", "qbittorrent", "mp-test-rmw-b").config == {"host": "b"}
