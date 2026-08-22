"""插件数据表实例维度的默认值语义。

查询类方法与删除类方法的默认实例范围刻意不对称：查询默认只看当前（默认）实例，
删除默认跨该插件全部实例。这里直接对着真实数据库断言这条不对称，而不是断言
调用了什么方法——不对称写反了不会报错，只会在卸载插件后留下残留数据，或者在
查询时意外看到别的实例的数据。
"""

import asyncio

import pytest

from app.db.models.plugindata import PluginData
from app.db.oper.plugindata import PluginDataOper
from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID


@pytest.fixture(autouse=True)
def _track(db):
    """把插件数据表纳入用例级回收。"""
    db.watermark(PluginData)


# --------------------------------------------------------------------------- #
# 模型层：查询默认单实例
# --------------------------------------------------------------------------- #

def test_get_plugin_data_defaults_to_default_instance_only(db):
    """不传实例标识时，查询只能看到默认实例的行，看不到分身实例的行。"""
    db.add(
        PluginData(plugin_id="ScopePluginA", instance_id=DEFAULT_INSTANCE_ID, key="k1", value=1),
        PluginData(plugin_id="ScopePluginA", instance_id="clone-a", key="k1", value=2),
    )

    rows = PluginData.get_plugin_data(db.session, "ScopePluginA")

    assert {row.instance_id for row in rows} == {DEFAULT_INSTANCE_ID}
    assert asyncio.run(
        PluginData.async_get_plugin_data(plugin_id="ScopePluginA")
    )[0].instance_id == DEFAULT_INSTANCE_ID


def test_get_plugin_data_by_key_scopes_to_requested_instance(db):
    """按键查询必须按实例区分，不能把另一个实例同名键的值串读回来。"""
    db.add(
        PluginData(plugin_id="ScopePluginB", instance_id=DEFAULT_INSTANCE_ID, key="shared", value="default-value"),
        PluginData(plugin_id="ScopePluginB", instance_id="clone-a", key="shared", value="clone-value"),
    )

    assert PluginData.get_plugin_data_by_key(db.session, "ScopePluginB", "shared").value == "default-value"
    assert PluginData.get_plugin_data_by_key(
        db.session, "ScopePluginB", "shared", instance_id="clone-a"
    ).value == "clone-value"


# --------------------------------------------------------------------------- #
# 模型层：删除默认跨实例
# --------------------------------------------------------------------------- #

def test_del_plugin_data_by_key_defaults_to_all_instances(db):
    """不传实例标识时，按键删除必须清掉该插件全部实例下的这个键。"""
    db.add(
        PluginData(plugin_id="ScopePluginC", instance_id=DEFAULT_INSTANCE_ID, key="drop", value=1),
        PluginData(plugin_id="ScopePluginC", instance_id="clone-a", key="drop", value=2),
        PluginData(plugin_id="ScopePluginC", instance_id="clone-b", key="keep", value=3),
    )

    PluginData.del_plugin_data_by_key(db.session, "ScopePluginC", "drop")

    remaining = PluginData.get_plugin_data(db.session, "ScopePluginC", instance_id="clone-b")
    assert {row.key for row in remaining} == {"keep"}
    assert PluginData.get_plugin_data_by_key(db.session, "ScopePluginC", "drop") is None
    assert PluginData.get_plugin_data_by_key(
        db.session, "ScopePluginC", "drop", instance_id="clone-a"
    ) is None


def test_del_plugin_data_by_key_with_explicit_instance_only_clears_that_instance(db):
    """显式传入实例标识时，按键删除只清那一个实例，其余实例保留。"""
    db.add(
        PluginData(plugin_id="ScopePluginD", instance_id=DEFAULT_INSTANCE_ID, key="k", value=1),
        PluginData(plugin_id="ScopePluginD", instance_id="clone-a", key="k", value=2),
    )

    PluginData.del_plugin_data_by_key(db.session, "ScopePluginD", "k", instance_id="clone-a")

    assert PluginData.get_plugin_data_by_key(db.session, "ScopePluginD", "k") is not None
    assert PluginData.get_plugin_data_by_key(
        db.session, "ScopePluginD", "k", instance_id="clone-a"
    ) is None


def test_del_plugin_data_defaults_to_wiping_every_instance(db):
    """
    不传实例标识时，整插件删除必须清空该插件在全部实例下的数据。

    这正是卸载插件的语义：不该在卸载后留下某个分身实例的残留数据。
    """
    db.add(
        PluginData(plugin_id="ScopePluginE", instance_id=DEFAULT_INSTANCE_ID, key="k1", value=1),
        PluginData(plugin_id="ScopePluginE", instance_id="clone-a", key="k2", value=2),
        PluginData(plugin_id="ScopePluginF", instance_id=DEFAULT_INSTANCE_ID, key="k1", value=3),
    )

    PluginData.del_plugin_data(db.session, "ScopePluginE")

    assert PluginData.get_plugin_data(db.session, "ScopePluginE", instance_id="clone-a") == []
    assert PluginData.get_plugin_data(db.session, "ScopePluginE") == []
    assert len(PluginData.get_plugin_data(db.session, "ScopePluginF")) == 1


# --------------------------------------------------------------------------- #
# Oper 层：与模型层同一套默认值语义
# --------------------------------------------------------------------------- #

def test_oper_save_and_get_default_to_default_instance(db):
    """Oper 层保存/查询默认只作用于默认实例。"""
    oper = PluginDataOper(db=db.session)
    oper.save("ScopePluginG", "k", "default-value")
    oper.save("ScopePluginG", "k", "clone-value", instance_id="clone-a")

    assert oper.get_data("ScopePluginG", "k") == "default-value"
    assert oper.get_data("ScopePluginG", "k", instance_id="clone-a") == "clone-value"
    assert len(oper.get_data_all("ScopePluginG")) == 1


def test_oper_del_data_defaults_to_cross_instance(db):
    """Oper 层删除默认跨实例，指定实例时只清那一个实例。"""
    oper = PluginDataOper(db=db.session)
    oper.save("ScopePluginH", "k", "default-value")
    oper.save("ScopePluginH", "k", "clone-value", instance_id="clone-a")

    oper.del_data("ScopePluginH", "k")

    assert oper.get_data("ScopePluginH", "k") is None
    assert oper.get_data("ScopePluginH", "k", instance_id="clone-a") is None


def test_oper_async_save_and_get_default_to_default_instance(db):
    """异步存取路径遵循与同步路径一致的默认实例语义。"""
    oper = PluginDataOper(db=db.session)

    async def _run():
        await oper.async_save("ScopePluginI", "k", "default-value")
        await oper.async_save("ScopePluginI", "k", "clone-value", instance_id="clone-a")
        return (
            await oper.async_get_data("ScopePluginI", "k"),
            await oper.async_get_data("ScopePluginI", "k", instance_id="clone-a"),
        )

    default_value, clone_value = asyncio.run(_run())
    assert default_value == "default-value"
    assert clone_value == "clone-value"
