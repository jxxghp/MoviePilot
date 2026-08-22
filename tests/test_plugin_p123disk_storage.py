"""123 云盘存储扩展的声明与契约测试。

本文件只测声明面与令牌路由：声明能不能过契约、能不能被投影取到、按令牌取到的是不是
用户指定的那个实例、畸形配置会不会被拒。真实网盘调用不在测试范围内——那需要一个真
账号和一次外网请求，测出来的是 123 云盘今天在不在线，而不是这个扩展写得对不对。

第三方包 ``p123client`` 在本仓的测试环境里并未安装，本文件因此不触发任何会建立连接的
路径；插件模块本身必须能在缺依赖时被导入，这一点由本文件的 import 顺带守住。
"""

from pathlib import Path
from typing import Iterator

import pytest

import app.plugins.p123disk as p123disk_package
from app.application.storage import StorageHelper
from app.foundation.singleton import Singleton
from app.modules._base.storage import StorageBase
from app.plugins.p123disk import STORAGE_NAME, P123Disk
from app.plugins.p123disk.config import STORAGE_CONFIG_SCHEMA
from app.plugins.p123disk.fileitem import build_file_item
from app.plugins.p123disk.storage import STORAGE_ID, P123Storage
from app.runtime.extensions.contract.config_schema import config_value_violations
from app.runtime.extensions.contract.declaration import ServiceInstanceDeclaration
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.admission.service_instance import (
    service_instance_declaration_violation,
)
from app.runtime.extensions.admission.storage import storage_backend_violation
from app.runtime.extensions import plugin_manager as plugin_manager_module
from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.extensions.registry.service_instance import service_instance_registry
from app.runtime.extensions.registry.storage import storage_backend_registry
from app.runtime.events import Event
from app.runtime.hostports.storages import storage_config_port
from app.schemas import FileURI
from app.schemas.event import ConfigChangeEventData
from app.schemas.system import StorageConf
from app.schemas.types import EventType, SystemConfigKey

# 插件在宿主里的实例键，默认实例即裸插件标识
PLUGIN_KEY = "P123Disk"

# 三个账号各自的配置，账号字段不同即可分辨取到的是哪一份
DEFAULT_ACCOUNT = {"passport": "13800000000", "password": "default-secret"}
MAIN_ACCOUNT = {"passport": "13800000001", "password": "main-secret"}
SPARE_ACCOUNT = {"passport": "13800000002", "password": "spare-secret"}


@pytest.fixture(autouse=True)
def _isolate_storage_state() -> Iterator[None]:
    """快照并复原存储后端注册表、存储配置与存储配置端口。

    端口的 provider 没有公开读取入口，直接取私有字段做快照：本用例文件要在没有完整
    组合根的情况下让后端读到自己的配置，装完必须还原，否则会影响别的用例。
    """
    original_entries = dict(storage_backend_registry._entries)
    original_builtin = dict(storage_backend_registry._builtin_entries)
    original_provider = storage_config_port._provider
    original_storages = StorageHelper().get_storagies()
    storage_config_port.register(StorageHelper)
    try:
        yield
    finally:
        StorageHelper.save_storagies(original_storages)
        storage_config_port._provider = original_provider
        storage_backend_registry._entries.clear()
        storage_backend_registry._entries.update(original_entries)
        storage_backend_registry._builtin_entries.clear()
        storage_backend_registry._builtin_entries.update(original_builtin)


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def _enabled_plugin() -> P123Disk:
    """
    构造一个已启用的插件实例

    :return: 插件实例
    """
    plugin = P123Disk()
    plugin.init_plugin({"enabled": True})
    return plugin


def _declaration() -> ServiceInstanceDeclaration:
    """
    取出插件声明的唯一一条服务实例类型

    :return: 存储类型声明
    """
    declared = _enabled_plugin().provides_service_instances()
    assert len(declared) == 1
    return declared[0]


def _start_plugin(monkeypatch, manager: PluginManager) -> str:
    """
    按已启用配置启动插件

    站点认证级别在没有完整组合根时取不到，补成 1（所有用户可见）——本插件声明的
    可见级别正是 1，取不到时插件会被判为不满足认证要求而根本不加载。

    :param monkeypatch: pytest 的猴子补丁夹具
    :param manager: 插件管理器
    :return: 插件实例键
    """
    monkeypatch.setattr(
        plugin_manager_module, "_site_auth_level_provider", lambda: 1
    )
    monkeypatch.setattr(
        manager, "_load_selective_plugins", lambda pid, installed, check: [P123Disk]
    )
    monkeypatch.setattr(manager, "get_plugin_config", lambda pid: {"enabled": True})
    manager.start(pid=PLUGIN_KEY)
    return PLUGIN_KEY


def _save_accounts(*confs: StorageConf) -> None:
    """
    用给定的实例配置覆盖 123 云盘的整族配置

    :param confs: 实例配置
    """
    StorageHelper.save_storagies(list(confs))


def _save_three_accounts() -> None:
    """按「兼容指针 + 两个具名账号」写入配置。

    宿主为每个存储类型恰好裁出一个承接裸令牌的实例，无人自称时取写入顺序上的第一份。
    因此第一份账号以裸令牌 ``p123`` 寻址，其余两份才是 ``p123@主号`` 与 ``p123@备号``。
    """
    _save_accounts(
        StorageConf(type=STORAGE_ID, name="默认号", config=DEFAULT_ACCOUNT),
        StorageConf(type=STORAGE_ID, name="主号", config=MAIN_ACCOUNT),
        StorageConf(type=STORAGE_ID, name="备号", config=SPARE_ACCOUNT),
    )


def _fan_out() -> dict:
    """
    取出类型目录里 123 云盘当前扇出的全部实例

    :return: 实例名到存储操作对象的映射
    """
    adapter = next(
        item
        for item in service_instance_registry.adapters("storage")
        if item.entry.service_type == STORAGE_ID
    )
    return adapter.get_instances()


def _incomplete_backend(missing: str) -> type:
    """
    构造一个只差指定抽象方法未落地的存储后端类

    :param missing: 故意不实现的抽象方法名
    :return: 存储后端类
    """
    members = {
        name: (lambda self, *args, **kwargs: None)
        for name in StorageBase.__abstractmethods__
        if name != missing
    }
    return type(StorageBase)("_P123StorageMissingMethod", (StorageBase,), members)


def test_declaration_passes_the_registration_contract() -> None:
    """声明本身合契约：族、类型标识、后端类与配置契约都在位。"""
    declaration = _declaration()

    assert service_instance_declaration_violation(declaration) is None
    assert (declaration.capability, declaration.type, declaration.name) == (
        "storage",
        STORAGE_ID,
        STORAGE_NAME,
    )
    assert declaration.impl is P123Storage
    assert declaration.factory is None
    assert declaration.config_schema == STORAGE_CONFIG_SCHEMA


def test_declaration_is_taken_by_the_projection() -> None:
    """启用的插件声明能被能力投影取到，字段原样保留。"""
    projection = PluginProjection({PLUGIN_KEY: _enabled_plugin()})

    declared = projection.provided_service_instances()

    assert len(declared[PLUGIN_KEY]) == 1
    assert declared[PLUGIN_KEY][0].type == STORAGE_ID


def test_disabled_plugin_declares_nothing() -> None:
    """插件停用时不产出任何声明，存储类型随之从目录里消失。"""
    plugin = P123Disk()
    plugin.init_plugin({"enabled": False})

    declared = PluginProjection({PLUGIN_KEY: plugin}).provided_service_instances()

    assert declared == {}


def test_multi_instance_is_declared_true() -> None:
    """123 云盘按账号配置多份：两个账号是两块互不相通的空间，不是同一个东西。"""
    assert _declaration().multi_instance is True


def test_backend_identity_matches_the_declared_type() -> None:
    """后端类自报的存储标识必须与声明的类型标识一致，否则实例读不到自己的配置。"""
    assert P123Storage.schema == _declaration().type


def test_backend_implements_every_required_method() -> None:
    """存储族的必填方法由 StorageBase 的抽象方法定义，后端必须全部落地。"""
    assert storage_backend_violation(P123Storage) is None


def test_declaration_is_rejected_when_a_required_method_is_missing() -> None:
    """缺一个必填方法即整条声明被拒，不留到调用时才失败。"""
    declaration = ServiceInstanceDeclaration(
        capability="storage",
        type=STORAGE_ID,
        name=STORAGE_NAME,
        impl=_incomplete_backend("usage"),
        config_schema=STORAGE_CONFIG_SCHEMA,
    )

    violation = service_instance_declaration_violation(declaration)

    assert violation is not None
    assert "usage" in violation


def test_optional_login_methods_are_absent_rather_than_stubbed() -> None:
    """本存储不提供扫码与网页授权登录，缺席即弃权，不写返回 None 的空桩。"""
    for method in ("generate_qrcode", "check_login"):
        assert method not in P123Storage.__dict__


def test_the_plugin_neither_hijacks_the_method_table_nor_claims_by_event() -> None:
    """认领由令牌路由回答：既不挂方法表，也不监听存储选择事件。"""
    assert "get_module" not in P123Disk.__dict__
    assert "provides_modules" not in P123Disk.__dict__

    package = Path(p123disk_package.__file__).parent
    code = "\n".join(
        line
        for path in package.glob("*.py")
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "eventmanager" not in code
    assert "ChainEventType" not in code
    assert "add_event_listener" not in code


def test_the_plugin_keeps_only_the_hooks_that_still_exist() -> None:
    """保留仍是抽象钩子的四个，不实现已被声明式取代或已删除的钩子。"""
    for hook in ("get_state", "get_api", "get_form", "get_page"):
        assert hook in P123Disk.__dict__
    for hook in ("get_auth_providers", "provides_auth_providers", "get_command"):
        assert hook not in P123Disk.__dict__


def test_bare_token_lands_on_the_default_instance(monkeypatch, plugin_manager) -> None:
    """裸令牌 p123 落到兼容指针所指的那一份，自称的实例胜过写入顺序。"""
    _save_accounts(
        StorageConf(type=STORAGE_ID, name="备号", config=SPARE_ACCOUNT),
        StorageConf(
            type=STORAGE_ID, name="主号", bare_token_target=True, config=MAIN_ACCOUNT
        ),
    )
    plugin_id = _start_plugin(monkeypatch, plugin_manager)
    try:
        entry = storage_backend_registry.find(STORAGE_ID)

        assert entry is not None
        assert entry.backend is P123Storage
        assert entry.owner == plugin_id

        storage = entry.create()
        assert storage.storage_token == STORAGE_ID
        assert storage.get_conf() == MAIN_ACCOUNT
    finally:
        plugin_manager.stop(plugin_id)


def test_unknown_named_instance_is_yielded_instead_of_falling_back(
    monkeypatch, plugin_manager
) -> None:
    """指名一个不存在的实例只能让出：回落默认实例等于换一个账号执行用户没选的操作。"""
    _save_accounts(
        StorageConf(
            type=STORAGE_ID, name="主号", bare_token_target=True, config=MAIN_ACCOUNT
        )
    )
    plugin_id = _start_plugin(monkeypatch, plugin_manager)
    try:
        token = FileURI.join_storage(STORAGE_ID, "不存在的账号")

        assert storage_backend_registry.find(token) is None
        assert storage_backend_registry.resolve(token) is None
    finally:
        plugin_manager.stop(plugin_id)


def test_named_tokens_route_to_their_own_instances(monkeypatch, plugin_manager) -> None:
    """p123@主号 与 p123@备号 各是各的实例，读到的也是各自那份账号，互不串用。"""
    _save_three_accounts()
    plugin_id = _start_plugin(monkeypatch, plugin_manager)
    try:
        instances = _fan_out()

        assert sorted(instances) == ["主号", "备号", "默认号"]
        assert instances["主号"] is not instances["备号"]
        assert instances["主号"].storage_token == f"{STORAGE_ID}@主号"
        assert instances["备号"].storage_token == f"{STORAGE_ID}@备号"
        assert instances["主号"].get_conf() == MAIN_ACCOUNT
        assert instances["备号"].get_conf() == SPARE_ACCOUNT
    finally:
        plugin_manager.stop(plugin_id)


def test_the_compat_pointer_instance_answers_the_bare_token(
    monkeypatch, plugin_manager
) -> None:
    """承接裸令牌的那一份以裸标识寻址，与该类型只有一份配置时完全一致。"""
    _save_three_accounts()
    plugin_id = _start_plugin(monkeypatch, plugin_manager)
    try:
        instances = _fan_out()

        assert instances["默认号"].storage_token == STORAGE_ID
        assert instances["默认号"].get_conf() == DEFAULT_ACCOUNT
    finally:
        plugin_manager.stop(plugin_id)


def test_storage_registration_is_recycled_when_the_plugin_stops(
    monkeypatch, plugin_manager
) -> None:
    """插件停用后两张表里的登记都必须被回收，不留残留。"""
    plugin_id = _start_plugin(monkeypatch, plugin_manager)
    assert storage_backend_registry.find(STORAGE_ID) is not None
    assert service_instance_registry.find("storage", STORAGE_ID) is not None

    plugin_manager.stop(plugin_id)

    assert storage_backend_registry.find(STORAGE_ID) is None
    assert service_instance_registry.find("storage", STORAGE_ID) is None


@pytest.mark.parametrize(
    "config",
    [
        {"passport": 13800000001, "password": "x"},
        {"passport": "13800000001", "cookie": "abc"},
        {"passport": "1" * 200, "password": "x"},
    ],
    ids=["passport_not_a_string", "undeclared_field", "passport_too_long"],
)
def test_config_schema_rejects_malformed_config(config: dict) -> None:
    """契约按形状拒绝：类型不符、契约里没有的字段、超出长度上限都要说明原因。"""
    assert config_value_violations(STORAGE_CONFIG_SCHEMA, config)


def test_config_schema_accepts_the_empty_and_the_complete_config() -> None:
    """账号尚未填写的实例照常成立，填全的实例同样合契约。"""
    assert config_value_violations(STORAGE_CONFIG_SCHEMA, {}) == ()
    assert config_value_violations(STORAGE_CONFIG_SCHEMA, MAIN_ACCOUNT) == ()


def test_malformed_config_drops_only_its_own_instance(monkeypatch, plugin_manager) -> None:
    """一条坏配置只让它自己的实例不产出，同类型其余账号照常可用。"""
    _save_accounts(
        StorageConf(type=STORAGE_ID, name="主号", config=MAIN_ACCOUNT),
        StorageConf(type=STORAGE_ID, name="脏配置", config={"cookie": "abc"}),
    )
    plugin_id = _start_plugin(monkeypatch, plugin_manager)
    try:
        assert sorted(_fan_out()) == ["主号"]
    finally:
        plugin_manager.stop(plugin_id)


def test_file_item_carries_the_instance_token() -> None:
    """产出的文件项按实例令牌打戳，路径因此形如 p123@主号:/媒体库/01.mkv。"""
    item = build_file_item(
        {
            "FileId": 12,
            "ParentFileId": 3,
            "FileName": "01.mkv",
            "Type": 0,
            "Size": 1024,
            "UpdateAt": "2026-01-02T03:04:05",
            "Etag": "abc",
            "S3KeyFlag": "flag",
        },
        storage_token=f"{STORAGE_ID}@主号",
        path="/媒体库/01.mkv",
    )

    assert item.storage == f"{STORAGE_ID}@主号"
    assert FileURI.split_storage(item.storage) == (STORAGE_ID, "主号")
    assert item.uri == f"{STORAGE_ID}@主号:/媒体库/01.mkv"
    assert item.type == "file"
    assert item.extension == "mkv"


def test_directory_item_path_keeps_the_trailing_slash() -> None:
    """目录项的路径带尾部斜杠，子项路径直接在其后拼接。"""
    item = build_file_item(
        {"FileId": 7, "FileName": "媒体库", "Type": 1, "UpdateAt": "2026-01-02T03:04:05"},
        storage_token=STORAGE_ID,
        path="/媒体库",
    )

    assert item.type == "dir"
    assert item.path == "/媒体库/"
    assert item.extension is None


def test_two_instances_do_not_share_the_path_cache(monkeypatch, plugin_manager) -> None:
    """路径到文件 ID 的缓存归实例私有：共用一份会让甲账号的目录 ID 被拿去访问乙账号。"""
    _save_accounts(
        StorageConf(type=STORAGE_ID, name="主号", config=MAIN_ACCOUNT),
        StorageConf(type=STORAGE_ID, name="备号", config=SPARE_ACCOUNT),
    )
    plugin_id = _start_plugin(monkeypatch, plugin_manager)
    try:
        instances = _fan_out()
        instances["主号"]._api.remember_path("/媒体库", "1001")

        assert instances["主号"]._api.path_to_id("/媒体库") == "1001"
        assert instances["备号"]._api._id_cache == {}
    finally:
        plugin_manager.stop(plugin_id)


def _registered_instances() -> list:
    """
    列出 123 云盘当前在存储后端注册表里占据的实例位

    :return: 实例名列表，未具名实例位为 None
    """
    return [entry.instance for entry in storage_backend_registry.instances(STORAGE_ID)]


def _storage_config_changed_event() -> Event:
    """
    构造一次存储实例配置变更事件

    :return: 配置变更事件
    """
    return Event(
        EventType.ConfigChanged,
        ConfigChangeEventData(key={SystemConfigKey.Storages.value}),
    )


def test_named_tokens_are_addressable_in_the_backend_registry(
    monkeypatch, plugin_manager
) -> None:
    """三份账号各占一个实例位：具名令牌在按令牌寻址的那张表里各取到自己那份账号。"""
    _save_three_accounts()
    plugin_id = _start_plugin(monkeypatch, plugin_manager)
    try:
        main = storage_backend_registry.find(f"{STORAGE_ID}@主号")
        spare = storage_backend_registry.find(f"{STORAGE_ID}@备号")

        assert (main, spare) != (None, None)
        assert (main.instance, spare.instance) == ("主号", "备号")
        assert main.owner == spare.owner == plugin_id

        main_oper = storage_backend_registry.resolve(f"{STORAGE_ID}@主号")
        spare_oper = storage_backend_registry.resolve(f"{STORAGE_ID}@备号")
        assert main_oper is not spare_oper
        assert main_oper.storage_token == f"{STORAGE_ID}@主号"
        assert spare_oper.storage_token == f"{STORAGE_ID}@备号"
        assert main_oper.get_conf() == MAIN_ACCOUNT
        assert spare_oper.get_conf() == SPARE_ACCOUNT
    finally:
        plugin_manager.stop(plugin_id)


def test_bare_token_lands_on_the_compat_pointer_among_three_accounts(
    monkeypatch, plugin_manager
) -> None:
    """三份账号并存时裸令牌落到承接者那一位，取到的是它自己的账号。"""
    _save_three_accounts()
    plugin_id = _start_plugin(monkeypatch, plugin_manager)
    try:
        entry = storage_backend_registry.find(STORAGE_ID)

        assert entry is not None
        assert (entry.instance, entry.bare_token_target) == ("默认号", True)
        oper = entry.create()
        assert oper.storage_token == STORAGE_ID
        assert oper.get_conf() == DEFAULT_ACCOUNT
    finally:
        plugin_manager.stop(plugin_id)


def test_absent_named_instance_yields_among_three_accounts(
    monkeypatch, plugin_manager
) -> None:
    """指名一个没配过的账号一律让出，绝不回落到承接裸令牌的那一份。"""
    _save_three_accounts()
    plugin_id = _start_plugin(monkeypatch, plugin_manager)
    try:
        token = FileURI.join_storage(STORAGE_ID, "不存在的账号")

        assert storage_backend_registry.find(token) is None
        assert storage_backend_registry.resolve(token) is None
    finally:
        plugin_manager.stop(plugin_id)


def test_stop_checks_ownership_per_instance(monkeypatch, plugin_manager) -> None:
    """停用按 (标识, 实例) 两级校验归属：被接管的实例位跳过，同标识其余位照常回收。"""
    _save_three_accounts()
    plugin_id = _start_plugin(monkeypatch, plugin_manager)
    storage_backend_registry.register(
        P123Storage,
        distribution=ExtensionDistribution.MARKET,
        owner="OtherOwner@default",
        storage_id=STORAGE_ID,
        instance="主号",
    )

    plugin_manager.stop(plugin_id)

    taken_over = storage_backend_registry.find(f"{STORAGE_ID}@主号")
    assert taken_over is not None
    assert taken_over.owner == "OtherOwner@default"
    assert storage_backend_registry.find(f"{STORAGE_ID}@备号") is None
    assert storage_backend_registry.find(f"{STORAGE_ID}@默认号") is None


def test_an_illegal_instance_name_drops_only_its_own_position(
    monkeypatch, plugin_manager
) -> None:
    """一份实例名不合法的配置只让它自己登记不成，同类型其余账号照常可寻址。"""
    _save_accounts(
        StorageConf(
            type=STORAGE_ID, name="主号", bare_token_target=True, config=MAIN_ACCOUNT
        ),
        StorageConf(type=STORAGE_ID, name="坏@名字", config=SPARE_ACCOUNT),
    )
    plugin_id = _start_plugin(monkeypatch, plugin_manager)
    try:
        assert _registered_instances() == ["主号"]
        assert storage_backend_registry.resolve(STORAGE_ID).get_conf() == MAIN_ACCOUNT
    finally:
        plugin_manager.stop(plugin_id)


def test_storage_config_change_resyncs_the_addressable_instances(
    monkeypatch, plugin_manager
) -> None:
    """插件运行期间增配账号后，按令牌可寻址的实例位随之重建。"""
    _save_accounts(
        StorageConf(type=STORAGE_ID, name="主号", config=MAIN_ACCOUNT)
    )
    plugin_id = _start_plugin(monkeypatch, plugin_manager)
    try:
        assert _registered_instances() == ["主号"]

        _save_three_accounts()
        plugin_manager_module._handle_storage_instance_config_changed(
            _storage_config_changed_event()
        )

        assert sorted(_registered_instances()) == ["主号", "备号", "默认号"]
        assert (
            storage_backend_registry.resolve(f"{STORAGE_ID}@备号").get_conf()
            == SPARE_ACCOUNT
        )
    finally:
        plugin_manager.stop(plugin_id)


def test_unrelated_config_change_leaves_the_registrations_alone(
    monkeypatch, plugin_manager
) -> None:
    """与存储实例配置无关的变更不触发重建，登记维持原样。"""
    _save_accounts(
        StorageConf(type=STORAGE_ID, name="主号", config=MAIN_ACCOUNT)
    )
    plugin_id = _start_plugin(monkeypatch, plugin_manager)
    try:
        _save_three_accounts()
        plugin_manager_module._handle_storage_instance_config_changed(
            Event(
                EventType.ConfigChanged,
                ConfigChangeEventData(key={SystemConfigKey.Downloaders.value}),
            )
        )

        assert _registered_instances() == ["主号"]
    finally:
        plugin_manager.stop(plugin_id)


def test_plugin_form_carries_only_the_enable_switch() -> None:
    """插件表单只管启用开关，账号落在存储实例的配置界面上。"""
    _, model = _enabled_plugin().get_form()
    conf, storage_model = _declaration().config_form

    assert set(model) == {"enabled"}
    assert set(storage_model) == {"passport", "password"}
    assert isinstance(conf, list)
