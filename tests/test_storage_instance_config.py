"""存储配置按具名实例扇出的守护测试。

一个存储类型可配置多份具名实例，实例名与存储类型拼成存储令牌 ``local@backup``；
裸令牌 ``local`` 落到该类型的兼容指针所指的那一份。本文件盯住配置到实例这一段：
配几份就扇出几份、兼容指针的裁决、令牌化的配置读写，以及承接裸令牌的实例仍以裸标识对外。
"""

import asyncio
import inspect
from pathlib import Path

import pytest

from app.application.service_config import (
    async_write_system_setting,
    read_system_setting,
)
from app.application.storage import StorageHelper
from app.modules.localstorage import LocalStorageModule
from app.modules.localstorage.local import LocalStorage
from app.modules.u115 import U115Module
from app.runtime.extensions.projection.module_declarations import builtin_multi_instance
from app.runtime.extensions.service_config import STORAGE_CAPABILITY
from app.runtime.extensions.admission.service_config import service_config_records
from app.runtime.extensions.registry.storage import (
    create_storage_backend,
    storage_backend_registry,
)
from app.schemas.system import StorageConf
from app.schemas.types import SystemConfigKey
from tests.test_storage_backend_registry import BUILTIN_STORAGE_MODULES


def storage_config_records(confs) -> list:
    """把存储实例配置整形为服务实例配置表的行，与写入端走同一份实现。

    :param confs: 存储实例配置对象序列
    :return: 服务实例配置表的行
    """
    return service_config_records(
        STORAGE_CAPABILITY, [conf.model_dump() for conf in confs]
    )


@pytest.fixture
def storage_config():
    """提供存储配置读写服务，用例前后都把存储族配置清空。

    :return: 存储配置读写服务
    """
    helper = StorageHelper()
    helper.save_storagies([])
    yield helper
    helper.save_storagies([])


def _started(module_class):
    """建立并启动一个存储模块。

    :param module_class: 存储模块类
    :return: 已完成初始化的存储模块
    """
    module = module_class()
    module.init_module()
    return module


def test_single_config_becomes_the_bare_token_target(storage_config):
    """存量的一个类型一份配置搬成具名实例后即承接裸令牌，裸令牌照旧命中它。"""
    storage_config.save_storagies([
        {"type": "local", "name": "本地", "config": {"root": "/media"}},
    ])

    module = _started(LocalStorageModule)
    try:
        assert list(module._storages) == ["本地"]  # noqa: SLF001

        storage = module._claim("local")  # noqa: SLF001

        assert storage is not None
        assert storage.storage_instance == "本地"
        assert storage.storage_is_bare_token is True
        assert storage.get_conf() == {"root": "/media"}
    finally:
        module.stop()


def test_bare_token_target_keeps_addressing_itself_with_the_bare_identity(storage_config):
    """承接裸令牌的实例对外仍是裸存储标识：配置读写键与产出的文件项都不带实例名。"""
    storage_config.save_storagies([
        {"type": "local", "name": "本地", "config": {}},
    ])

    module = _started(LocalStorageModule)
    try:
        storage = module._claim("local")  # noqa: SLF001

        assert storage.storage_token == "local"
        assert storage.get_item(Path("/")).storage == "local"
    finally:
        module.stop()


def test_named_instance_addresses_itself_with_its_token(storage_config):
    """具名实例按自己的令牌寻址，裸令牌仍归兼容指针所指的那一份。"""
    storage_config.save_storagies([
        {"type": "u115", "name": "主号", "default": True, "config": {}},
        {"type": "u115", "name": "副号", "config": {}},
    ])

    module = _started(U115Module)
    try:
        secondary = module._claim("u115@副号")  # noqa: SLF001

        assert secondary.storage_token == "u115@副号"
        assert module._claim("u115").storage_token == "u115"  # noqa: SLF001
    finally:
        module.stop()


def test_named_instance_stamps_its_token_on_the_file_items_it_produces():
    """具名实例产出的文件项带实例名，后续删除或移动因此不会落回裸令牌那一份。"""
    backup = create_storage_backend(LocalStorage, "备份盘", False)

    assert backup.storage_token == "local@备份盘"
    assert backup.get_item(Path("/")).storage == "local@备份盘"


def test_multiple_configs_fan_out_one_backend_per_instance(storage_config):
    """配几份实例就扇出几个后端对象，各自登记为独立实例位且互不串用。"""
    storage_config.save_storagies([
        {"type": "u115", "name": "主号", "default": True, "config": {"k": "a"}},
        {"type": "u115", "name": "副号", "config": {"k": "b"}},
        {"type": "u115", "name": "归档号", "config": {"k": "c"}},
    ])

    module = _started(U115Module)
    try:
        assert set(module._storages) == {"主号", "副号", "归档号"}  # noqa: SLF001

        primary = module._claim("u115@主号")  # noqa: SLF001
        secondary = module._claim("u115@副号")  # noqa: SLF001

        assert primary is not secondary
        assert primary.get_conf() == {"k": "a"}
        assert secondary.get_conf() == {"k": "b"}
        assert {
            entry.storage for entry in storage_backend_registry.instances("u115")
        } == {"u115@主号", "u115@副号", "u115@归档号"}
    finally:
        module.stop()


def test_single_instance_storage_type_keeps_only_its_default_target(storage_config):
    """声明为单实例的存储类型只认族级默认调用目标那一份，多出来的配置忽略而不删除。"""
    storage_config.save_storagies([
        {"type": "local", "name": "主盘", "default": True, "config": {}},
        {"type": "local", "name": "备份盘", "config": {}},
    ])

    module = _started(LocalStorageModule)
    try:
        assert builtin_multi_instance(STORAGE_CAPABILITY, "local") is False
        assert list(module._storages) == ["主盘"]  # noqa: SLF001
        assert module._claim("local@备份盘") is None  # noqa: SLF001
        assert module._claim("local").storage_token == "local"  # noqa: SLF001
    finally:
        module.stop()

    assert [conf.name for conf in storage_config.list_storages("local")] == [
        "主盘", "备份盘"
    ]


def test_named_operations_land_only_on_that_instance(storage_config):
    """指名实例的操作只落在该实例上，写配置不会波及同类型其它实例。"""
    storage_config.save_storagies([
        {"type": "u115", "name": "主号", "default": True, "config": {"k": "a"}},
        {"type": "u115", "name": "副号", "config": {"k": "b"}},
    ])

    module = _started(U115Module)
    try:
        module._claim("u115@副号").set_config({"k": "b2"})  # noqa: SLF001

        assert storage_config.get_storage("u115@副号").config == {"k": "b2"}
        assert storage_config.get_storage("u115@主号").config == {"k": "a"}
        assert storage_config.get_storage("u115").config == {"k": "a"}
    finally:
        module.stop()


def test_absent_named_instance_is_still_yielded(storage_config):
    """配置里没有的实例名一律让出，绝不改走别的实例。"""
    storage_config.save_storagies([
        {"type": "local", "name": "主盘", "default": True, "config": {}},
    ])

    module = _started(LocalStorageModule)
    try:
        assert module._claim("local@不存在") is None  # noqa: SLF001
        assert module.get_file_item("local@不存在", Path("/")) is None
        assert module._claim("local") is not None  # noqa: SLF001
    finally:
        module.stop()


@pytest.mark.parametrize("marks", [(False, False), (True, True)])
def test_bare_token_is_yielded_when_the_config_decides_no_pointer(storage_config, marks):
    """配置里无兼容指针或多份自称承接时裸令牌让出，绝不按顺序取第一份。"""
    storage_config.save_storagies([
        StorageConf(type="u115", name="甲"),
        StorageConf(type="u115", name="乙"),
    ])
    _force_bare_token_marks(storage_config, {"甲": marks[0], "乙": marks[1]})

    module = _started(U115Module)
    try:
        assert module._claim("u115") is None  # noqa: SLF001
        assert module.get_file_item("u115", Path("/")) is None
        assert module._claim("u115@甲") is not None  # noqa: SLF001
    finally:
        module.stop()


def _force_bare_token_marks(helper: StorageHelper, marks: dict) -> None:
    """按实例名逐条改写裸令牌兼容指针，绕开写入端「每类型恰好一条」的兜底。

    :param helper: 存储配置读写服务
    :param marks: 实例名到兼容指针取值的映射
    :return: 无返回值
    """
    from app.application.service_config import get_configured_service_instance_configs

    records = storage_config_records(helper.get_storagies())
    for record in records:
        record["host_config"] = {"bare_token_target": marks.get(record["name"], False)}
    get_configured_service_instance_configs().save_records(STORAGE_CAPABILITY, records)


def test_one_broken_config_only_takes_down_its_own_instance(storage_config):
    """一份坏配置只影响该实例，同类型其余实例照常建立并登记。"""
    storage_config.save_storagies([
        {"type": "u115", "name": "主号", "default": True, "config": {}},
        {"type": "u115", "name": "坏 实 例", "config": {}},
        {"type": "u115", "name": "副号", "config": {}},
    ])

    module = _started(U115Module)
    try:
        assert set(module._storages) == {"主号", "副号"}  # noqa: SLF001
        assert module._claim("u115") is not None  # noqa: SLF001
        assert module._claim("u115@副号") is not None  # noqa: SLF001
    finally:
        module.stop()


def test_config_is_read_and_written_by_token(storage_config):
    """配置读写按存储令牌进行，裸令牌写下去的内容由承接它的实例读回。"""
    storage_config.set_storage("local", {"root": "/a"})

    assert storage_config.get_storage("local").config == {"root": "/a"}
    assert storage_config.get_storage("local").name == "local"

    storage_config.add_storage("local", "备份盘", {"root": "/b"})

    assert storage_config.get_storage("local@备份盘").config == {"root": "/b"}
    assert storage_config.get_storage("local").config == {"root": "/a"}

    storage_config.reset_storage("local@备份盘")

    assert storage_config.get_storage("local@备份盘").config == {}
    assert storage_config.get_storage("local").config == {"root": "/a"}


def test_bare_token_write_creates_the_bare_token_target_of_an_unconfigured_type(storage_config):
    """尚未配置的存储类型按裸令牌写入即建出承接裸令牌的那一份。"""
    LocalStorage().set_config({"root": "/media"})

    conf = storage_config.get_storage("local")

    assert conf is not None
    assert conf.bare_token_target is True
    assert conf.config == {"root": "/media"}


def test_malformed_token_writes_nothing(storage_config):
    """畸形令牌不与任何实例相等，读不到也写不进。"""
    storage_config.set_storage("local@主 盘", {"root": "/a"})

    assert storage_config.get_storagies() == []
    assert storage_config.get_storage("local@主 盘") is None


@pytest.mark.parametrize("storage_id,module_class,backend", BUILTIN_STORAGE_MODULES)
def test_unconfigured_builtin_storage_keeps_its_unnamed_default_slot(
    storage_config, storage_id, module_class, backend
):
    """一份配置都没有的内建存储仍持有未具名实例位，裸令牌与配置键都不变。"""
    module = _started(module_class)
    try:
        assert list(module._storages) == [None]  # noqa: SLF001

        storage = module._claim(storage_id)  # noqa: SLF001

        assert type(storage) is backend
        assert storage.storage_instance is None
        assert storage.storage_token == storage_id
    finally:
        module.stop()


@pytest.mark.parametrize("storage_id,module_class,backend", BUILTIN_STORAGE_MODULES)
def test_builtin_storage_fans_out_configured_instances(
    storage_config, storage_id, module_class, backend
):
    """七个内建存储逐条按配置扇出实例，份数按各自声明，承接裸令牌的实例仍以裸标识读写配置。"""
    storage_config.save_storagies([
        {"type": storage_id, "name": "主号", "default": True, "config": {"k": "a"}},
        {"type": storage_id, "name": "副号", "config": {"k": "b"}},
    ])

    multi_instance = builtin_multi_instance(STORAGE_CAPABILITY, storage_id)
    module = _started(module_class)
    try:
        expected = {"主号", "副号"} if multi_instance else {"主号"}
        assert set(module._storages) == expected  # noqa: SLF001

        primary = module._claim(storage_id)  # noqa: SLF001

        assert type(primary) is backend
        assert primary.storage_token == storage_id
        assert primary.get_conf() == {"k": "a"}

        secondary = module._claim(f"{storage_id}@副号")  # noqa: SLF001
        if not multi_instance:
            assert secondary is None
        else:
            assert secondary.storage_token == f"{storage_id}@副号"
            assert secondary.get_conf() == {"k": "b"}
    finally:
        module.stop()


def test_registry_resolved_object_knows_its_instance(storage_config):
    """按令牌从注册表取出的操作对象带着实例归属，配置不会读到别的实例上。"""
    storage_config.save_storagies([
        {"type": "u115", "name": "主号", "default": True, "config": {"k": "a"}},
        {"type": "u115", "name": "副号", "config": {"k": "b"}},
    ])

    module = _started(U115Module)
    try:
        default_oper = storage_backend_registry.resolve("u115")
        named_oper = storage_backend_registry.resolve("u115@副号")

        assert default_oper.storage_token == "u115"
        assert default_oper.get_conf() == {"k": "a"}
        assert named_oper.storage_token == "u115@副号"
        assert named_oper.get_conf() == {"k": "b"}
    finally:
        module.stop()


def test_connection_sharing_is_per_instance_not_per_storage_type(storage_config):
    """按实例复用连接的存储：同实例共用一个对象，不同实例各自一个。"""
    storage_config.save_storagies([
        {"type": "u115", "name": "主号", "default": True, "config": {}},
        {"type": "u115", "name": "副号", "config": {}},
    ])

    module = _started(U115Module)
    try:
        primary = module._claim("u115@主号")  # noqa: SLF001
        secondary = module._claim("u115@副号")  # noqa: SLF001

        assert primary is not secondary
        assert primary.session is not secondary.session
        assert storage_backend_registry.resolve("u115@副号") is secondary
        assert storage_backend_registry.resolve("u115") is primary
    finally:
        module.stop()


def test_storage_setting_key_reads_and_writes_the_same_source(storage_config):
    """按配置键收发的存储设置与按令牌收发的落在同一份配置上，不会出现两份互相矛盾的。"""
    changed = asyncio.run(async_write_system_setting(
        SystemConfigKey.Storages,
        [
            {"type": "local", "name": "主盘", "config": {"root": "/a"}},
            {"type": "local", "name": "备份盘", "config": {"root": "/b"}},
        ],
    ))

    assert changed is True
    assert storage_config.get_storage("local@备份盘").config == {"root": "/b"}

    value = read_system_setting(SystemConfigKey.Storages)

    assert [item["name"] for item in value] == ["主盘", "备份盘"]
    assert [item["bare_token_target"] for item in value] == [True, False]


@pytest.mark.parametrize("storage_id,module_class,_backend", BUILTIN_STORAGE_MODULES)
def test_builtin_storage_modules_watch_the_storage_config(
    storage_id, module_class, _backend
):
    """七个内建存储模块都盯着存储配置键，改完配置即重载扇出，不必重启。"""
    manifest = (
        Path(inspect.getfile(module_class)).parent / "capability.toml"
    ).read_text(encoding="utf-8")

    assert 'watch = ["Storages"]' in manifest, storage_id


def test_records_give_every_storage_type_exactly_one_bare_token_target():
    """整形后每个存储类型恰好一个兼容指针，裸令牌始终指得到实例。"""
    records = storage_config_records([
        StorageConf(type="alist", name="甲"),
        StorageConf(type="alist", name="乙"),
        StorageConf(type="u115", name="丙"),
        StorageConf(type="u115", name="丁", bare_token_target=True),
        StorageConf(name="没有类型"),
    ])

    pointers = {
        record["type"]: record["name"]
        for record in records
        if record["host_config"]["bare_token_target"]
    }

    assert pointers == {"alist": "甲", "u115": "丁"}
    assert [record["name"] for record in records] == ["甲", "乙", "丙", "丁"]


def test_records_fall_back_to_the_storage_type_as_the_instance_name():
    """未填实例名的配置以存储类型为实例名，同类型同名的后者覆盖前者。"""
    records = storage_config_records([
        StorageConf(type="local", config={"root": "/旧"}),
        StorageConf(type="local", name="local", config={"root": "/新"}),
    ])

    assert len(records) == 1
    assert records[0]["name"] == "local"
    assert records[0]["config"] == {"root": "/新"}
