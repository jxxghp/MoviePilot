"""
插件实例配置表的模型约束与数据访问层行为。

这张表承载「一个插件按配置扇出多个实例」与「实例各自的版本」两件事，唯一约束和
默认值任何一处出偏差，都会表现为实例互相覆盖或启动状态判断错误，而不是一个可见
的报错。这里对着真实数据库断言约束生效与查回的内容，而不是断言调用了什么。
"""
import asyncio

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.pluginconfig import PluginConfig
from app.db.oper.pluginconfig import PluginConfigOper


@pytest.fixture(autouse=True)
def _track(db):
    """把插件实例配置表纳入用例级回收。"""
    db.watermark(PluginConfig)


# --------------------------------------------------------------------------- #
# 模型约束
# --------------------------------------------------------------------------- #

def test_unique_constraint_rejects_duplicate_plugin_instance(db):
    """
    同一 (plugin_id, instance_id) 二次插入必须被唯一约束拒绝。

    约束失效意味着同一实例能并存两行配置，读取哪一行取决于查询顺序，是否启用、
    生效版本都会变得不确定。
    """
    db.add(PluginConfig(plugin_id="PluginA", instance_id="default"))

    with pytest.raises(IntegrityError):
        db.session.add(PluginConfig(plugin_id="PluginA", instance_id="default"))
        db.session.commit()
    db.session.rollback()

    assert len(PluginConfig.list_by_plugin(db.session, "PluginA")) == 1


def test_unique_constraint_allows_same_instance_id_across_plugins(db):
    """
    不同插件下同名实例标识不冲突——约束的唯一键是二元组，不是单独的实例标识。
    """
    db.add(PluginConfig(plugin_id="PluginA2", instance_id="default"),
           PluginConfig(plugin_id="PluginB2", instance_id="default"))

    assert {row.plugin_id for row in PluginConfig.list_by_plugin(db.session, "PluginA2")} \
        == {"PluginA2"}
    assert {row.plugin_id for row in PluginConfig.list_by_plugin(db.session, "PluginB2")} \
        == {"PluginB2"}


def test_defaults_apply_when_only_identity_is_supplied(db):
    """
    只提供 plugin_id 与 instance_id 时，其余列必须落到定稿的默认值。

    is_enabled 默认关闭、follow_default_version 默认跟随、plugin_version 默认为空，
    任何一个默认值改变都会让新建实例在未显式配置前就处于错误状态。
    """
    row = db.add(PluginConfig(plugin_id="PluginC", instance_id="default"))

    fetched = PluginConfig.get_by_instance(db.session, "PluginC", "default")
    assert fetched.id == row.id
    assert fetched.is_enabled is False
    assert fetched.follow_default_version is True
    assert fetched.plugin_version is None
    assert fetched.log_level is None
    assert fetched.log_expires_at is None
    assert fetched.config_data is None


# --------------------------------------------------------------------------- #
# 数据访问层：PluginConfigOper
# --------------------------------------------------------------------------- #

def test_oper_get_returns_none_when_absent(db):
    """
    按 (plugin_id, instance_id) 取单条：不存在时同步、异步都必须返回 None。
    """
    oper = PluginConfigOper(db=db.session)

    assert oper.get("PluginMissing", "default") is None
    assert asyncio.run(PluginConfigOper().async_get("PluginMissing", "default")) is None


def test_oper_get_returns_matching_row(db):
    """
    按 (plugin_id, instance_id) 取单条：命中时同步、异步必须指向同一行。
    """
    db.add(PluginConfig(plugin_id="PluginD", instance_id="default"),
           PluginConfig(plugin_id="PluginD", instance_id="alt"))
    oper = PluginConfigOper(db=db.session)

    found = oper.get("PluginD", "alt")
    assert found.instance_id == "alt"
    async_found = asyncio.run(PluginConfigOper().async_get("PluginD", "alt"))
    assert async_found.id == found.id


def test_oper_list_by_plugin_scopes_to_that_plugin(db):
    """
    按插件列全部实例：必须只返回该插件的行，不能串到其他插件。
    """
    db.add(PluginConfig(plugin_id="PluginE", instance_id="default"),
           PluginConfig(plugin_id="PluginE", instance_id="alt"),
           PluginConfig(plugin_id="PluginF", instance_id="default"))
    oper = PluginConfigOper(db=db.session)

    listed = {row.instance_id for row in oper.list_by_plugin("PluginE")}
    assert listed == {"default", "alt"}

    async_listed = {row.instance_id for row in
                    asyncio.run(PluginConfigOper().async_list_by_plugin("PluginE"))}
    assert async_listed == {"default", "alt"}


def test_oper_list_enabled_spans_plugins_and_excludes_disabled(db):
    """
    列全部启用实例：跨插件返回已启用的行，且必须排除未启用的行。
    """
    db.add(PluginConfig(plugin_id="PluginG", instance_id="default", is_enabled=True),
           PluginConfig(plugin_id="PluginG", instance_id="alt", is_enabled=False),
           PluginConfig(plugin_id="PluginH", instance_id="default", is_enabled=True))
    oper = PluginConfigOper(db=db.session)

    enabled_keys = {(row.plugin_id, row.instance_id) for row in oper.list_enabled()}
    assert ("PluginG", "default") in enabled_keys
    assert ("PluginH", "default") in enabled_keys
    assert ("PluginG", "alt") not in enabled_keys

    async_enabled_keys = {(row.plugin_id, row.instance_id) for row in
                          asyncio.run(PluginConfigOper().async_list_enabled())}
    assert ("PluginG", "alt") not in async_enabled_keys


def test_oper_upsert_creates_then_updates_same_row(db):
    """
    写入/更新：首次写入新建一行，同一实例再次写入必须更新而非新增，且刷新更新时间。
    """
    oper = PluginConfigOper(db=db.session)

    created = oper.upsert("PluginI", "default", {"is_enabled": True, "plugin_version": "1.0.0"})
    assert created.is_enabled is True
    assert created.plugin_version == "1.0.0"
    assert created.created_at is not None
    first_updated_at = created.updated_at

    updated = oper.upsert("PluginI", "default", {"plugin_version": "1.1.0"})
    assert updated.id == created.id
    assert updated.plugin_version == "1.1.0"
    assert updated.created_at == created.created_at

    assert len(PluginConfig.list_by_plugin(db.session, "PluginI")) == 1
    assert first_updated_at is not None


def test_oper_async_upsert_creates_then_updates_same_row(db):
    """
    异步写入/更新必须与同步路径遵循相同的「不存在则建，存在则改」语义。
    """
    async def _run():
        oper = PluginConfigOper()
        created = await oper.async_upsert(
            "PluginJ", "default", {"is_enabled": True, "plugin_version": "2.0.0"}
        )
        updated = await oper.async_upsert("PluginJ", "default", {"plugin_version": "2.1.0"})
        return created, updated

    created, updated = asyncio.run(_run())
    db.watermark(PluginConfig)
    assert updated.id == created.id
    assert updated.plugin_version == "2.1.0"
    assert len(PluginConfig.list_by_plugin(db.session, "PluginJ")) == 1


def test_oper_delete_instance_removes_only_that_instance(db):
    """
    按实例删除：只能删掉目标实例，同插件下的其他实例必须保留。
    """
    db.add(PluginConfig(plugin_id="PluginK", instance_id="default"),
           PluginConfig(plugin_id="PluginK", instance_id="alt"))
    oper = PluginConfigOper(db=db.session)

    assert oper.delete_instance("PluginK", "default") is True
    remaining = {row.instance_id for row in PluginConfig.list_by_plugin(db.session, "PluginK")}
    assert remaining == {"alt"}

    assert oper.delete_instance("PluginK", "missing") is False


def test_oper_async_delete_instance_removes_only_that_instance(db):
    """
    异步按实例删除必须与同步路径行为一致。
    """
    db.add(PluginConfig(plugin_id="PluginL", instance_id="default"),
           PluginConfig(plugin_id="PluginL", instance_id="alt"))

    async def _run():
        oper = PluginConfigOper()
        deleted = await oper.async_delete_instance("PluginL", "default")
        missing = await oper.async_delete_instance("PluginL", "missing")
        return deleted, missing

    deleted, missing = asyncio.run(_run())
    assert (deleted, missing) == (True, False)
    remaining = {row.instance_id for row in PluginConfig.list_by_plugin(db.session, "PluginL")}
    assert remaining == {"alt"}


def test_oper_delete_by_plugin_removes_every_instance_of_that_plugin(db):
    """
    按插件删除：必须删掉该插件的全部实例，且不影响其他插件的行。
    """
    db.add(PluginConfig(plugin_id="PluginM", instance_id="default"),
           PluginConfig(plugin_id="PluginM", instance_id="alt"),
           PluginConfig(plugin_id="PluginN", instance_id="default"))
    oper = PluginConfigOper(db=db.session)

    assert oper.delete_by_plugin("PluginM") == 2
    assert PluginConfig.list_by_plugin(db.session, "PluginM") == []
    assert len(PluginConfig.list_by_plugin(db.session, "PluginN")) == 1


def test_oper_async_delete_by_plugin_removes_every_instance_of_that_plugin(db):
    """
    异步按插件删除必须与同步路径一样清空该插件的全部实例。
    """
    db.add(PluginConfig(plugin_id="PluginO", instance_id="default"),
           PluginConfig(plugin_id="PluginO", instance_id="alt"),
           PluginConfig(plugin_id="PluginP", instance_id="default"))

    async def _run():
        oper = PluginConfigOper()
        return await oper.async_delete_by_plugin("PluginO")

    deleted_count = asyncio.run(_run())
    assert deleted_count == 2
    assert PluginConfig.list_by_plugin(db.session, "PluginO") == []
    assert len(PluginConfig.list_by_plugin(db.session, "PluginP")) == 1
