"""服务实例配置端点：类型目录、逐条增删改、默认调用目标、密钥掩码与提供方缺席分类。

逐条写入取代整份列表替换，要证的第一件事是**互不覆盖**：一次写入只碰它自己那一行，
同族其它配置——包括本次请求根本没见过的那些——原样留着。整份替换做不到这一点。

密钥这条线证两头：列表下发不带明文，回填掩码不改动库里的原值，而改掩码之外的内容
照常落库。契约判定这条线证畸形配置在落盘前被退回，并且退回的是这一条、不牵连别的。

判据见 docs/plugin-extension-architecture.md §4 与 §7.2。
"""

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api.deps import get_current_active_superuser
from app.api.endpoints import service as service_endpoint
from app.api.endpoints.service import (
    PROVIDER_DISABLED,
    PROVIDER_NOT_INSTALLED,
    PROVIDER_START_FAILED,
    absent_service_providers,
    clear_service_default_target,
    create_service_config,
    delete_service_config,
    list_service_configs,
    service_families,
    service_types,
    set_service_default_target,
    update_service_config,
)
from app.api.endpoints.system import set_setting as set_setting_endpoint
from app.api.service_secrets import SECRET_MASK
from app.application.service_config import get_configured_service_instance_configs
from app.db.models.serviceconfig import BUILTIN_PROVIDER, ServiceConfig
from app.db.oper.serviceconfig import ServiceConfigOper
from app.runtime.extensions.registry.service_instance import service_instance_registry
from app.schemas.service import (
    ServiceConfigProviderIssue,
    ServiceFamilyInfo,
    ServiceInstanceConfigInfo,
    ServiceInstanceConfigPayload,
    ServiceTypeInfo,
)
from app.schemas.types import ModuleType, SystemConfigKey

DOWNLOADER = ModuleType.Downloader.value
STORAGE = ModuleType.Storage.value
AUTH = ModuleType.Auth.value

# 本文件写入的实例名统一带前缀：整族列表端点会读到同库里其它用例留下的行，
# 断言按前缀筛出自己那几条，才不会被别处的数据带偏
PREFIX = "mp-test-ep-"

# 一份带凭据的下载器配置：端口是普通字段，password 与 auth.token 是凭据
_SECRET_CONFIG = {
    "host": "127.0.0.1",
    "port": 8080,
    "password": "s3cret",
    "auth": {"token": "tok-abc", "user": "admin"},
}

# 一份声明了必填与范围的契约，用来证明写入确实过了契约判定
_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {"type": "string", "minLength": 1},
        "port": {"type": "integer", "minimum": 1, "maximum": 65535},
    },
    "required": ["host"],
    "additionalProperties": False,
}


class _DemoDownloader:
    """契约合规的下载器客户端桩。"""

    def __init__(self, name: Optional[str] = None, **kwargs: Any):
        """记录宿主传入的实例名。"""
        self.name = name


@pytest.fixture(autouse=True)
def _isolate_service_instance_registry() -> Iterator[None]:
    """快照并复原服务实例注册表，避免测试间相互污染。"""
    original = dict(service_instance_registry._adapters)
    try:
        yield
    finally:
        service_instance_registry._adapters.clear()
        service_instance_registry._adapters.update(original)


@pytest.fixture(autouse=True)
def _track(db):
    """把服务实例配置表纳入用例级回收。"""
    db.watermark(ServiceConfig)


def _payload(**fields: Any) -> ServiceInstanceConfigPayload:
    """构造写入载荷，未声明的顶层键按宿主实例级字段透传。"""
    return ServiceInstanceConfigPayload(**fields)


def _seed(
    name: str,
    *,
    capability: str = DOWNLOADER,
    service_type: str = "qbittorrent",
    config: Optional[dict] = None,
    host_config: Optional[dict] = None,
    enabled: bool = True,
    default: bool = False,
    provider: str = BUILTIN_PROVIDER,
) -> ServiceConfig:
    """直接落一行实例配置，绕开端点以便摆出用例前置状态。"""
    return ServiceConfig(
        capability=capability,
        type=service_type,
        name=f"{PREFIX}{name}",
        enabled=enabled,
        config=config or {},
        host_config=host_config,
        is_default_target=default,
        provider=provider,
    )


def _own(items: List[dict]) -> Dict[str, dict]:
    """按实例名筛出本用例写入的配置，避开同库里其它用例的数据。"""
    return {
        item["name"][len(PREFIX):]: item
        for item in items
        if item["name"].startswith(PREFIX)
    }


def _register_demo_type(
    service_type: str = "demo_downloader",
    *,
    owner: str = "DemoPlugin",
    capability: str = DOWNLOADER,
    multi_instance: bool = True,
    config_form: Any = None,
    config_schema: Any = None,
) -> None:
    """登记一个扩展声明的服务实例类型。"""
    service_instance_registry.register(
        capability=capability,
        service_type=service_type,
        name="演示下载器",
        impl=_DemoDownloader,
        owner=owner,
        multi_instance=multi_instance,
        config_form=config_form,
        config_schema=config_schema,
    )


def _dependency_of(func, parameter_name: str):
    """读取 FastAPI 函数参数上声明的依赖函数。"""
    return inspect.signature(func).parameters[parameter_name].default.dependency


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "endpoint",
    [
        service_families,
        service_types,
        list_service_configs,
        create_service_config,
        update_service_config,
        delete_service_config,
        set_service_default_target,
        clear_service_default_target,
        absent_service_providers,
    ],
)
def test_every_service_config_endpoint_requires_superuser(endpoint):
    """配置里装着凭据，读与写一律只允许管理员。"""
    assert _dependency_of(endpoint, "_") is get_current_active_superuser


# --------------------------------------------------------------------------- #
# 列表：密钥掩码
# --------------------------------------------------------------------------- #


def test_listing_never_returns_plaintext_secrets(db):
    """列表端点不得下发明文密钥，凭据一律换成掩码。"""
    db.add(_seed("secret", config=_SECRET_CONFIG))

    listed = _own(list_service_configs(DOWNLOADER, None))["secret"]

    assert listed["config"]["password"] == SECRET_MASK
    assert listed["config"]["auth"]["token"] == SECRET_MASK
    assert "s3cret" not in str(listed)
    assert "tok-abc" not in str(listed)
    # 非凭据字段原样下发，否则用户看不出自己配了什么
    assert listed["config"]["port"] == 8080
    assert listed["config"]["auth"]["user"] == "admin"
    assert set(listed["masked_fields"]) == {"config.password", "config.auth.token"}


def test_listing_keeps_unset_secret_fields_visible_as_unset(db):
    """没配过的凭据不掩码，否则「未配置」会显示成「已配置」。"""
    db.add(_seed("blank", config={"password": "", "token": None, "host": "h"}))

    listed = _own(list_service_configs(DOWNLOADER, None))["blank"]

    assert listed["config"]["password"] == ""
    assert listed["config"]["token"] is None
    assert listed["masked_fields"] == []


def test_updating_a_port_does_not_require_retyping_the_password(db):
    """回传掩码即表示凭据未改动，改一个端口号不该被迫重新输入密码。"""
    db.add(_seed("keep", config=dict(_SECRET_CONFIG)))

    update_service_config(
        DOWNLOADER,
        "qbittorrent",
        _payload(
            enabled=True,
            config={
                "host": "127.0.0.1",
                "port": 9090,
                "password": SECRET_MASK,
                "auth": {"token": SECRET_MASK, "user": "admin"},
            },
        ),
        name=f"{PREFIX}keep",
        _=None,
    )

    stored = ServiceConfigOper(db=db.session).get_row(DOWNLOADER, "qbittorrent", f"{PREFIX}keep")
    assert stored["config"]["port"] == 9090
    assert stored["config"]["password"] == "s3cret"
    assert stored["config"]["auth"]["token"] == "tok-abc"


def test_submitting_a_new_password_replaces_the_stored_one(db):
    """回传掩码之外的内容即表示用户确实改了密码，新值必须落库。"""
    db.add(_seed("rotate", config=dict(_SECRET_CONFIG)))

    update_service_config(
        DOWNLOADER,
        "qbittorrent",
        _payload(enabled=True, config={"host": "127.0.0.1", "password": "brand-new"}),
        name=f"{PREFIX}rotate",
        _=None,
    )

    stored = ServiceConfigOper(db=db.session).get_row(DOWNLOADER, "qbittorrent", f"{PREFIX}rotate")
    assert stored["config"]["password"] == "brand-new"


def test_mask_is_never_stored_as_a_password(db):
    """库里没有原值时提交掩码，掩码本身绝不能被当成密码存进去。"""
    created = create_service_config(
        DOWNLOADER,
        _payload(
            type="qbittorrent",
            name=f"{PREFIX}copied",
            enabled=True,
            config={"host": "h", "password": SECRET_MASK},
        ),
        None,
    )

    stored = ServiceConfigOper(db=db.session).get_row(DOWNLOADER, "qbittorrent", created["name"])
    assert "password" not in stored["config"]


# --------------------------------------------------------------------------- #
# 逐条增删改：各自生效且不牵连同族其它配置
# --------------------------------------------------------------------------- #


def test_create_adds_only_its_own_record(db):
    """新增一条不动同族已有的配置。"""
    db.add(_seed("kept", config={"host": "old"}))

    create_service_config(
        DOWNLOADER,
        _payload(type="transmission", name=f"{PREFIX}added", enabled=True, config={"host": "new"}),
        None,
    )

    listed = _own(list_service_configs(DOWNLOADER, None))
    assert listed["kept"]["config"]["host"] == "old"
    assert listed["added"]["type"] == "transmission"
    # 新增的实例一律不是默认调用目标，置位必须由用户显式选定
    assert listed["added"]["is_default_target"] is False


def test_update_touches_only_the_targeted_record(db):
    """更新一条不动同族其它配置的启用态与内容。"""
    db.add(
        _seed("target", config={"host": "a"}),
        _seed("sibling", service_type="transmission", config={"host": "b"}, enabled=True),
    )

    update_service_config(
        DOWNLOADER,
        "qbittorrent",
        _payload(enabled=False, config={"host": "changed"}),
        name=f"{PREFIX}target",
        _=None,
    )

    listed = _own(list_service_configs(DOWNLOADER, None))
    assert listed["target"]["config"]["host"] == "changed"
    assert listed["target"]["enabled"] is False
    assert listed["sibling"]["config"]["host"] == "b"
    assert listed["sibling"]["enabled"] is True


def test_delete_removes_only_the_targeted_record(db):
    """删除一条不牵连同族其它配置。"""
    db.add(_seed("gone", config={"host": "a"}), _seed("stay", service_type="transmission"))

    result = delete_service_config(DOWNLOADER, "qbittorrent", name=f"{PREFIX}gone", _=None)

    assert result.success is True
    listed = _own(list_service_configs(DOWNLOADER, None))
    assert "gone" not in listed
    assert "stay" in listed


def test_delete_reports_missing_record_as_404(db):
    """删除不存在的配置以 404 退回，不能悄悄当成成功。"""
    with pytest.raises(HTTPException) as exc_info:
        delete_service_config(DOWNLOADER, "qbittorrent", name=f"{PREFIX}ghost", _=None)

    assert exc_info.value.status_code == 404


def test_update_can_rename_without_disturbing_siblings(db):
    """改名走同一条更新语句，同族其它配置不受影响。"""
    db.add(_seed("before", config={"host": "a"}), _seed("other", service_type="transmission"))

    update_service_config(
        DOWNLOADER,
        "qbittorrent",
        _payload(name=f"{PREFIX}after", enabled=True, config={"host": "a"}),
        name=f"{PREFIX}before",
        _=None,
    )

    listed = _own(list_service_configs(DOWNLOADER, None))
    assert "before" not in listed
    assert listed["after"]["config"]["host"] == "a"
    assert "other" in listed


def test_update_reports_missing_record_as_404(db):
    """更新不存在的配置以 404 退回。"""
    with pytest.raises(HTTPException) as exc_info:
        update_service_config(
            DOWNLOADER, "qbittorrent", _payload(enabled=True), name=f"{PREFIX}ghost", _=None
        )

    assert exc_info.value.status_code == 404


# --------------------------------------------------------------------------- #
# 并发：单条写入不互相覆盖
# --------------------------------------------------------------------------- #


def test_concurrent_edits_of_different_records_do_not_overwrite_each_other(db):
    """两位管理员各改各的配置时，谁都不该丢掉对方的改动。

    整份列表替换下必然翻车：先打开页面的那一位提交时带着一份不含对方新配置的整份
    列表，对方那一条会被整个抹掉。逐条写入只碰自己那一行，因此两笔改动都留得住。
    """
    db.add(_seed("first", config={"host": "a"}), _seed("second", service_type="transmission",
                                                       config={"host": "b"}))
    # 甲打开页面，此刻库里只有这两条
    snapshot = _own(list_service_configs(DOWNLOADER, None))
    assert set(snapshot) == {"first", "second"}

    # 乙在甲提交之前新增了一条并改了另一条
    create_service_config(
        DOWNLOADER,
        _payload(type="rtorrent", name=f"{PREFIX}third", enabled=True, config={"host": "c"}),
        None,
    )
    update_service_config(
        DOWNLOADER,
        "transmission",
        _payload(enabled=True, config={"host": "b-changed"}),
        name=f"{PREFIX}second",
        _=None,
    )

    # 甲此时才提交自己那一条
    update_service_config(
        DOWNLOADER,
        "qbittorrent",
        _payload(enabled=True, config={"host": "a-changed"}),
        name=f"{PREFIX}first",
        _=None,
    )

    listed = _own(list_service_configs(DOWNLOADER, None))
    assert listed["first"]["config"]["host"] == "a-changed"
    assert listed["second"]["config"]["host"] == "b-changed"
    assert listed["third"]["config"]["host"] == "c"


# --------------------------------------------------------------------------- #
# 重名：唯一约束的可读错误
# --------------------------------------------------------------------------- #


def test_duplicate_name_returns_a_readable_conflict(db):
    """同族同类型下重名以 409 退回并说明该换名，不把数据库异常抛到界面。"""
    db.add(_seed("dup", config={"host": "a"}))

    with pytest.raises(HTTPException) as exc_info:
        create_service_config(
            DOWNLOADER,
            _payload(type="qbittorrent", name=f"{PREFIX}dup", enabled=True, config={"host": "b"}),
            None,
        )

    assert exc_info.value.status_code == 409
    assert "请换一个名称" in exc_info.value.detail
    assert "IntegrityError" not in exc_info.value.detail
    assert "UNIQUE" not in exc_info.value.detail


def test_rename_onto_an_existing_name_returns_a_readable_conflict(db):
    """改名撞上同族同类型下的既有配置同样以可读错误退回。"""
    db.add(_seed("one", config={"host": "a"}), _seed("two", config={"host": "b"}))

    with pytest.raises(HTTPException) as exc_info:
        update_service_config(
            DOWNLOADER,
            "qbittorrent",
            _payload(name=f"{PREFIX}two", enabled=True, config={"host": "a"}),
            name=f"{PREFIX}one",
            _=None,
        )

    assert exc_info.value.status_code == 409
    assert "请换一个名称" in exc_info.value.detail


def test_same_name_under_a_different_type_is_not_a_conflict(db):
    """唯一约束带类型这一维，换个类型的同名配置是合法的。"""
    db.add(_seed("shared", config={"host": "a"}))

    create_service_config(
        DOWNLOADER,
        _payload(type="transmission", name=f"{PREFIX}shared", enabled=True, config={"host": "b"}),
        None,
    )

    types = {item["type"] for item in list_service_configs(DOWNLOADER, None)
             if item["name"] == f"{PREFIX}shared"}
    assert types == {"qbittorrent", "transmission"}


# --------------------------------------------------------------------------- #
# 默认调用目标
# --------------------------------------------------------------------------- #


def test_set_default_target_clears_the_previous_one(db):
    """置位新目标时清掉旧的，整族至多一条为真。"""
    db.add(_seed("old", config={"host": "a"}, default=True), _seed("new", service_type="transmission"))

    result = set_service_default_target(
        DOWNLOADER, "transmission", name=f"{PREFIX}new", _=None
    )

    assert result.success is True
    listed = _own(list_service_configs(DOWNLOADER, None))
    assert listed["new"]["is_default_target"] is True
    assert listed["old"]["is_default_target"] is False


def test_set_default_target_on_missing_record_keeps_the_previous_default(db):
    """目标缺席时以 404 退回且原有置位原样保留，不能出现「旧的清了新的没设上」。"""
    db.add(_seed("holder", config={"host": "a"}, default=True))

    with pytest.raises(HTTPException) as exc_info:
        set_service_default_target(DOWNLOADER, "qbittorrent", name=f"{PREFIX}ghost", _=None)

    assert exc_info.value.status_code == 404
    assert ServiceConfigOper(db=db.session).get_default_target(DOWNLOADER).name == f"{PREFIX}holder"


def test_clear_default_target_leaves_the_family_without_one(db):
    """清除后该族不再有默认调用目标，重复清除是空操作。"""
    db.add(_seed("holder", config={"host": "a"}, default=True))

    assert clear_service_default_target(DOWNLOADER, None).success is True
    assert ServiceConfigOper(db=db.session).get_default_target(DOWNLOADER) is None
    assert clear_service_default_target(DOWNLOADER, None).success is True


def test_family_without_default_target_rejects_the_placement(db):
    """登录认证族没有默认调用目标，置位请求以 400 退回而不是写进那一列。"""
    db.add(_seed("entry", capability=AUTH, service_type="oidc"))

    with pytest.raises(HTTPException) as exc_info:
        set_service_default_target(AUTH, "oidc", name=f"{PREFIX}entry", _=None)

    assert exc_info.value.status_code == 400


def test_update_does_not_disturb_the_default_target(db):
    """改配置内容不该顺带动到默认置位，置位只由专用入口改写。"""
    db.add(_seed("holder", config={"host": "a"}, default=True))

    update_service_config(
        DOWNLOADER,
        "qbittorrent",
        _payload(enabled=True, config={"host": "b"}),
        name=f"{PREFIX}holder",
        _=None,
    )

    assert _own(list_service_configs(DOWNLOADER, None))["holder"]["is_default_target"] is True


# --------------------------------------------------------------------------- #
# 类型目录与服务族目录
# --------------------------------------------------------------------------- #


def test_type_catalog_reports_multiplicity_and_form_availability():
    """类型目录要答出能不能配多份，以及有没有专属配置界面。"""
    _register_demo_type("single_type", multi_instance=False)
    _register_demo_type("form_type", config_form=([{"component": "VTextField"}], {"host": ""}))
    _register_demo_type("plain_type", config_schema=_SCHEMA)

    catalog = {item["type"]: item for item in service_types(DOWNLOADER, None)}

    assert catalog["single_type"]["multi_instance"] is False
    assert catalog["single_type"]["config_form_available"] is False
    assert catalog["form_type"]["multi_instance"] is True
    assert catalog["form_type"]["config_form_available"] is True
    # 声明了契约但没有专属界面时仍答 False，前端据契约生成默认表单
    assert catalog["plain_type"]["config_form_available"] is False
    assert catalog["plain_type"]["config_schema"] == _SCHEMA


def test_type_catalog_does_not_leak_across_families():
    """同名类型登记在不同族下时各归各的目录，不串族。"""
    _register_demo_type("only_downloader", capability=DOWNLOADER)
    _register_demo_type("only_storage", capability=STORAGE)

    downloader_types = {item["type"] for item in service_types(DOWNLOADER, None)}
    storage_types = {item["type"] for item in service_types(STORAGE, None)}

    assert "only_downloader" in downloader_types
    assert "only_downloader" not in storage_types
    assert "only_storage" in storage_types
    assert "only_storage" not in downloader_types
    assert all(item["capability"] == STORAGE for item in service_types(STORAGE, None))


def test_type_catalog_rejects_capability_outside_service_families():
    """不支持声明服务实例的能力标签视为请求出错。"""
    with pytest.raises(HTTPException) as exc_info:
        service_types("subtitleserver", None)

    assert exc_info.value.status_code == 404


def test_family_catalog_lists_every_registered_family():
    """服务族目录必须列全宿主自带的五族。"""
    listed = {item["capability"] for item in service_families(None)}

    assert {DOWNLOADER, STORAGE, AUTH, ModuleType.MediaServer.value,
            ModuleType.Notification.value} <= listed


# --------------------------------------------------------------------------- #
# 契约判定
# --------------------------------------------------------------------------- #


def test_write_is_rejected_when_config_violates_the_declared_schema(db):
    """畸形配置在落盘前被退回并说明原因，不留到构造实例时才失败。"""
    _register_demo_type("schema_type", config_schema=_SCHEMA)

    with pytest.raises(HTTPException) as exc_info:
        create_service_config(
            DOWNLOADER,
            _payload(
                type="schema_type",
                name=f"{PREFIX}bad",
                enabled=True,
                config={"port": 70000},
            ),
            None,
        )

    assert exc_info.value.status_code == 400
    assert "host" in exc_info.value.detail
    assert ServiceConfigOper(db=db.session).get(DOWNLOADER, "schema_type", f"{PREFIX}bad") is None


def test_rejected_write_does_not_disturb_sibling_records(db):
    """一条写坏了只退这一条，同族其它配置原样留着。"""
    _register_demo_type("schema_type", config_schema=_SCHEMA)
    db.add(_seed("healthy", config={"host": "a"}))

    with pytest.raises(HTTPException):
        create_service_config(
            DOWNLOADER,
            _payload(type="schema_type", name=f"{PREFIX}bad", enabled=True, config={}),
            None,
        )

    assert _own(list_service_configs(DOWNLOADER, None))["healthy"]["config"]["host"] == "a"


def test_valid_config_against_the_declared_schema_is_accepted(db):
    """合契约的配置照常落库，并记下声明该类型的扩展。"""
    _register_demo_type("schema_type", owner="DemoPlugin@work", config_schema=_SCHEMA)

    created = create_service_config(
        DOWNLOADER,
        _payload(
            type="schema_type",
            name=f"{PREFIX}good",
            enabled=True,
            config={"host": "127.0.0.1", "port": 8080},
        ),
        None,
    )

    assert created["provider"] == "DemoPlugin@work"
    assert created["type_available"] is True
    assert created["type_name"] == "演示下载器"


def test_write_without_a_type_identifier_is_rejected(db):
    """表按「能力标签加类型加实例名」定位一行，装不下没有身份的条目。"""
    with pytest.raises(HTTPException) as exc_info:
        create_service_config(DOWNLOADER, _payload(name=f"{PREFIX}nameless", enabled=True), None)

    assert exc_info.value.status_code == 400


# --------------------------------------------------------------------------- #
# 宿主消费的实例级字段
# --------------------------------------------------------------------------- #


def test_host_fields_are_split_out_of_the_type_payload(db):
    """路径映射归宿主、host 归类型实现，两者不混在同一列里。"""
    created = create_service_config(
        DOWNLOADER,
        _payload(
            type="qbittorrent",
            name=f"{PREFIX}split",
            enabled=True,
            config={"host": "127.0.0.1"},
            path_mapping=[["/media", "/downloads"]],
            unknown_field="dropped",
        ),
        None,
    )

    assert created["config"] == {"host": "127.0.0.1"}
    assert created["host_config"]["path_mapping"] == [["/media", "/downloads"]]
    # 族配置模型之外的顶层键不入库，否则宿主载荷会变成第二个什么都往里塞的大对象
    assert "unknown_field" not in created["host_config"]


def test_storage_keeps_exactly_one_bare_token_pointer_after_delete(db):
    """删掉承接裸令牌的那一份后要重裁指针，否则该类型的存量裸路径整体失效。"""
    db.add(
        _seed("holder", capability=STORAGE, service_type="u115",
              host_config={"bare_token_target": True}),
        _seed("other", capability=STORAGE, service_type="u115",
              host_config={"bare_token_target": False}),
    )

    delete_service_config(STORAGE, "u115", name=f"{PREFIX}holder", _=None)

    marked = [
        row for row in ServiceConfigOper(db=db.session).list_rows_by_type(STORAGE, "u115")
        if (row["host_config"] or {}).get("bare_token_target")
    ]
    assert len(marked) == 1
    assert marked[0]["name"] == f"{PREFIX}other"


# --------------------------------------------------------------------------- #
# 提供方已消失：三种成因分开
# --------------------------------------------------------------------------- #


class _PluginRuntimeStub:
    """按实例键回答启用状态的插件运行时替身。"""

    def __init__(self, enabled_keys: set):
        self._enabled_keys = enabled_keys

    def get_plugin_state(self, pid: str) -> bool:
        """返回该实例键当前是否处于已启用的运行态。"""
        return pid in self._enabled_keys


def _absent_providers(installed: List[str], enabled_keys: set) -> Dict[str, dict]:
    """在给定的安装清单与运行态下取「提供方已消失」列表，按实例名索引。"""
    config_stub = SimpleNamespace(
        get=lambda key: installed if key == SystemConfigKey.UserInstalledPlugins else None
    )
    with patch("app.api.endpoints.service.get_configured_system_config",
               return_value=config_stub), \
            patch("app.api.endpoints.service.PluginManager",
                  return_value=_PluginRuntimeStub(enabled_keys)):
        return _own(absent_service_providers(None))


def test_absent_provider_separates_the_three_causes(db):
    """未安装、已安装未启用、已启用但类型没登记上，三种成因必须分得开。"""
    db.add(
        _seed("uninstalled", service_type="type_a", provider="GonePlugin"),
        _seed("disabled", service_type="type_b", provider="SleepingPlugin"),
        _seed("broken", service_type="type_c", provider="RunningPlugin"),
    )

    issues = _absent_providers(
        installed=["SleepingPlugin", "RunningPlugin"],
        enabled_keys={"RunningPlugin"},
    )

    assert issues["uninstalled"]["reason"] == PROVIDER_NOT_INSTALLED
    assert issues["disabled"]["reason"] == PROVIDER_DISABLED
    assert issues["broken"]["reason"] == PROVIDER_START_FAILED
    assert issues["broken"]["extension_id"] == "RunningPlugin"


def test_absent_provider_ignores_builtin_and_registered_types(db):
    """内建类型与当下已登记的类型都不算提供方消失。"""
    _register_demo_type("live_type", owner="RunningPlugin")
    db.add(
        _seed("builtin", service_type="qbittorrent"),
        _seed("live", service_type="live_type", provider="RunningPlugin"),
    )

    issues = _absent_providers(installed=["RunningPlugin"], enabled_keys={"RunningPlugin"})

    assert issues == {}


def test_absent_provider_reads_the_registry_not_the_provider_column(db):
    """判据是登记表当下有没有这个类型，而不是提供方在不在场。

    提供方在场、类型却没登记上，正是「用户看插件是已启用、实例却静默不存在」那一种；
    若按 provider 列判定，这一行会被整个漏掉。
    """
    db.add(_seed("silent", service_type="never_registered", provider="RunningPlugin@work"))

    issues = _absent_providers(installed=["RunningPlugin"], enabled_keys={"RunningPlugin@work"})

    assert issues["silent"]["reason"] == PROVIDER_START_FAILED
    assert issues["silent"]["provider"] == "RunningPlugin@work"
    assert issues["silent"]["extension_id"] == "RunningPlugin"


def test_listing_marks_configs_whose_type_is_gone(db):
    """列表上同样标出「这条配置当下产不出实例」，不必另开一次请求才看得见。"""
    db.add(_seed("orphan", service_type="never_registered", provider="GonePlugin"))

    listed = _own(list_service_configs(DOWNLOADER, None))["orphan"]

    assert listed["type_available"] is False
    assert listed["type_name"] is None


# --------------------------------------------------------------------------- #
# response_model 不得静默裁掉字段
# --------------------------------------------------------------------------- #


def test_response_model_keeps_every_field_including_nested_config(db):
    """嵌套配置必须原样穿过 ``response_model``，不被 FastAPI 静默裁成空对象。

    ``response_model`` 的校验/序列化等价于对返回字典执行一次 ``Model(**payload).model_dump()``；
    ``config`` 若声明成裸 dict 而不是递归的 JsonData，嵌套层会在这一步被削平。
    """
    db.add(
        _seed(
            "nested",
            config={
                "host": "h",
                "auth": {"token": "t", "scopes": ["read", "write"]},
                "rules": [{"match": {"path": "/a"}, "weight": 3}],
            },
            host_config={"path_mapping": [["/media", "/downloads"]]},
        )
    )

    payload = _own(list_service_configs(DOWNLOADER, None))["nested"]
    serialized = ServiceInstanceConfigInfo(**payload).model_dump()

    assert set(serialized) == set(payload)
    assert serialized["config"]["auth"]["scopes"] == ["read", "write"]
    assert serialized["config"]["rules"][0]["match"]["path"] == "/a"
    assert serialized["host_config"]["path_mapping"] == [["/media", "/downloads"]]


def test_nested_config_survives_the_real_response_model_serialization(db):
    """走真实 ASGI 链路取列表，嵌套配置必须原样到达客户端且不带明文密钥。

    直接调用端点函数绕过了 FastAPI 的 ``response_model`` 校验与序列化，而字段正是在
    那一步被静默裁掉的。这条用例走完整链路，同时在报文文本上确认密钥没有漏出去。
    """
    db.add(
        _seed(
            "wire",
            config={
                "host": "h",
                "password": "s3cret",
                "auth": {"token": "tok-abc", "scopes": ["read", "write"]},
            },
        )
    )

    async def _fetch() -> httpx.Response:
        """经 ASGI 传输取一次该族的配置列表。"""
        app = FastAPI()
        app.include_router(service_endpoint.router, prefix="/service")
        app.dependency_overrides[get_current_active_superuser] = lambda: SimpleNamespace(
            id=1, name="admin", is_superuser=True
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.get(f"/service/configs/{DOWNLOADER}")

    response = asyncio.run(_fetch())

    assert response.status_code == 200
    assert "s3cret" not in response.text
    assert "tok-abc" not in response.text
    body = response.json()
    listed = _own(body["data"])["wire"]
    assert listed["config"]["auth"]["scopes"] == ["read", "write"]
    assert listed["config"]["password"] == SECRET_MASK
    assert listed["config"]["auth"]["token"] == SECRET_MASK


def test_response_model_keeps_every_field_of_the_other_endpoints(db):
    """族目录、类型目录与提供方缺席三个端点的字段同样不得被裁掉。"""
    _register_demo_type("schema_type", config_schema=_SCHEMA)
    db.add(_seed("orphan", service_type="never_registered", provider="GonePlugin"))

    family = service_families(None)[0]
    assert set(ServiceFamilyInfo(**family).model_dump()) == set(family)

    catalog = next(
        item for item in service_types(DOWNLOADER, None) if item["type"] == "schema_type"
    )
    serialized_type = ServiceTypeInfo(**catalog).model_dump()
    assert set(serialized_type) == set(catalog)
    assert serialized_type["config_schema"]["properties"]["port"]["maximum"] == 65535

    issue = _absent_providers(installed=[], enabled_keys=set())["orphan"]
    assert set(ServiceConfigProviderIssue(**issue).model_dump()) == set(issue)


# --------------------------------------------------------------------------- #
# 存量写入口不得失效
# --------------------------------------------------------------------------- #


def test_whole_list_replacement_still_works(db):
    """借道系统设置的整份列表替换仍要能用，新端点不取代它。"""
    db.add(_seed("legacy", config={"host": "a"}))

    response = asyncio.run(
        set_setting_endpoint(
            SystemConfigKey.Downloaders.value,
            value=[
                {
                    "name": f"{PREFIX}legacy",
                    "type": "qbittorrent",
                    "enabled": True,
                    "config": {"host": "replaced"},
                }
            ],
            _=None,
        )
    )

    assert response.success is True
    stored = ServiceConfigOper(db=db.session).get_row(DOWNLOADER, "qbittorrent", f"{PREFIX}legacy")
    assert stored["config"]["host"] == "replaced"


def test_single_record_write_is_visible_to_the_whole_list_reader(db):
    """逐条写入之后整族读取端立刻看得到，两条入口共用同一份缓存失效。"""
    create_service_config(
        DOWNLOADER,
        _payload(type="qbittorrent", name=f"{PREFIX}fresh", enabled=True, config={"host": "n"}),
        None,
    )

    payloads = get_configured_service_instance_configs().read(DOWNLOADER)

    assert any(item["name"] == f"{PREFIX}fresh" for item in payloads)
