"""登录入口并入服务实例族后的等价与边界守护测试。

登录入口与下载器、媒体服务器、消息渠道、存储并成一族：一张表、一套整形、一套筛选、
同一条 `provides_service_instances()` 声明钩子。本文件盯住六件事：

- 并族后的入口扇出结果与并族前分身级钩子的结果逐条等价；
- 两台媒体服务器各自是独立入口，各自能绑不同身份（并族前判为分身级的那条能力）；
- ``multi_instance=False`` 的类型被配多份时按既定口径整类型停摆，其余类型不受牵连；
- 一份坏配置只影响它自己，登录入口不会整族消失；
- 登录页在未登录状态下取得到入口列表，且拿不到配置载荷里的密钥；
- 存量绑定在升级后仍能登录——身份绑定标识填成旧取值即原样命中。

**身份绑定标识是绑定唯一键的一半。** 它由插件签票时交来、宿主既不生成也不改写，
因此并族改的是入口的配置载体，不是这一列的口径；存量行一行都不用动。
"""

import asyncio
from typing import Any, Dict, Iterator, List, Optional

import pytest

from app.api.endpoints.auth import auth_providers as auth_providers_endpoint
from app.application.security.auth import (
    consume_plugin_auth_ticket,
    create_plugin_auth_ticket_for_identity,
)
from app.application.service_config import (
    async_write_system_setting,
    read_system_setting,
)
from app.db.oper.user import UserOper
from app.db.oper.user_identity import UserIdentityOper
from app.runtime.extensions.projection.auth_entries import list_auth_entries
from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.extensions.contract.declaration import ServiceInstanceDeclaration
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.admission.service_instance import (
    service_instance_declaration_violation,
)
from app.runtime.extensions.service_config import (
    AUTH_CAPABILITY,
    service_host_fields,
    service_supports_default_target,
)
from app.runtime.extensions.admission.service_config import service_config_records
from app.runtime.extensions.registry.service_family import service_family_registry
from app.runtime.extensions.registry.service_instance import service_instance_registry
from app.schemas.system import AuthProviderConf
from app.schemas.types import ModuleType, SystemConfigKey
from app.schemas.user import AuthProviderInfo


class _EmbySsoEntry:
    """登录入口类型的实现桩，构造签名满足宿主的关键字展开协议。"""

    def __init__(self, name: Optional[str] = None, **kwargs: Any):
        """记录宿主传入的实例名与配置内容。"""
        self.name = name
        self.config = kwargs


class _AuthPlugin:
    """声明登录入口类型的最小插件桩。"""

    plugin_name = "媒体服务器单点登录"
    plugin_version = "1.2.3"

    def __init__(self, declarations: Optional[List[Any]] = None, enabled: bool = True):
        self._declarations = declarations
        self._enabled = enabled

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def provides_service_instances(self):
        """返回本插件声明的服务实例类型。"""
        return self._declarations


class _VueAuthPlugin(_AuthPlugin):
    """按 vue 模式渲染的登录入口插件桩。"""

    def get_render_mode(self):
        """声明本插件按 vue 模式渲染，编译产物位于 dist/assets。"""
        return "vue", "dist/assets"


class _LegacyAuthPlugin:
    """并族前分身级旧钩子的插件桩，用作扇出结果的对拍基准。"""

    plugin_name = "客厅Emby"

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return True

    def get_auth_providers(self):
        """返回分身级登录入口描述。"""
        return [{"id": "emby_sso@客厅Emby", "name": "客厅Emby", "icon": "mdi-emby"}]


def _declaration(
    service_type: str = "emby_sso",
    name: str = "Emby 单点登录",
    icon: Optional[str] = "mdi-emby",
    multi_instance: bool = True,
) -> ServiceInstanceDeclaration:
    """构造一条合契约的登录入口类型声明。

    :param service_type: 类型标识
    :param name: 类型展示名称
    :param icon: 类型展示图标
    :param multi_instance: 该类型能否配多份
    :return: 登录入口类型声明
    """
    return ServiceInstanceDeclaration(
        capability=AUTH_CAPABILITY,
        type=service_type,
        name=name,
        icon=icon,
        multi_instance=multi_instance,
        impl=_EmbySsoEntry,
    )


def _register_type(
    owner: str = "EmbySsoPlugin",
    service_type: str = "emby_sso",
    name: str = "Emby 单点登录",
    icon: Optional[str] = "mdi-emby",
    multi_instance: bool = True,
) -> None:
    """把一个登录入口类型登进服务实例登记表。

    :param owner: 提供该类型的扩展实例键
    :param service_type: 类型标识
    :param name: 类型展示名称
    :param icon: 类型展示图标
    :param multi_instance: 该类型能否配多份
    :return: 无返回值
    """
    service_instance_registry.register(
        capability=AUTH_CAPABILITY,
        service_type=service_type,
        name=name,
        owner=owner,
        icon=icon,
        impl=_EmbySsoEntry,
        multi_instance=multi_instance,
        distribution=ExtensionDistribution.MARKET,
    )


def _save_auth_configs(configs: List[Dict[str, Any]]) -> None:
    """整族覆盖登录入口配置。

    :param configs: 整族配置列表
    :return: 无返回值
    """
    asyncio.run(async_write_system_setting(SystemConfigKey.AuthProviders, configs))


@pytest.fixture(autouse=True)
def clean_auth_family() -> Iterator[None]:
    """用例前后都清空登录认证族的登记与配置，避免互相污染。"""
    service_instance_registry.unregister_owner("EmbySsoPlugin")
    service_instance_registry.unregister_owner("SiteSsoPlugin")
    _save_auth_configs([])
    yield
    service_instance_registry.unregister_owner("EmbySsoPlugin")
    service_instance_registry.unregister_owner("SiteSsoPlugin")
    _save_auth_configs([])


# ---------------------------------------------------------------------------
# 并族的形状：族登记、配置落点、默认调用目标
# ---------------------------------------------------------------------------


def test_auth_is_a_registered_service_family_with_the_shared_vocabulary() -> None:
    """登录认证是宿主自带的服务族，能力标签与 `ModuleType` 词表一致。"""
    entry = service_family_registry.find(AUTH_CAPABILITY)

    assert AUTH_CAPABILITY == ModuleType.Auth.value
    assert entry is not None
    assert entry.name == "登录认证"
    assert entry.owner is None


def test_auth_declaration_passes_the_shared_contract() -> None:
    """登录入口类型走的是通用服务实例契约，没有另开一条校验。"""
    assert service_instance_declaration_violation(_declaration()) is None


def test_auth_instance_config_lands_in_the_service_config_table() -> None:
    """登录入口配置落服务实例配置表，读回的形状与写入一致。"""
    _save_auth_configs([
        {
            "type": "emby_sso",
            "name": "客厅Emby",
            "enabled": True,
            "config": {"host": "http://emby.local"},
        }
    ])

    stored = read_system_setting(SystemConfigKey.AuthProviders)

    assert len(stored) == 1
    assert stored[0]["type"] == "emby_sso"
    assert stored[0]["name"] == "客厅Emby"
    assert stored[0]["config"] == {"host": "http://emby.local"}


def test_identity_provider_is_the_only_host_field_of_the_family() -> None:
    """身份绑定标识是本族唯一的实例级宿主字段，随宿主载荷落库。"""
    assert service_host_fields(AUTH_CAPABILITY) == ("identity_provider",)


def test_auth_family_has_no_default_call_target() -> None:
    """本族不接受默认调用目标：登录时用户点的是具体入口，不存在未指定。"""
    assert service_supports_default_target(AUTH_CAPABILITY) is False
    assert service_supports_default_target(ModuleType.Downloader.value) is True
    assert "default" not in AuthProviderConf.model_fields


def test_default_marker_is_trimmed_off_before_it_can_hit_the_unique_index() -> None:
    """即使写入端点收到置位的默认标记，整形也一律裁成假，不去占那条条件唯一索引。"""
    records = service_config_records(AUTH_CAPABILITY, [
        {"type": "emby_sso", "name": "甲", "enabled": True, "default": True},
        {"type": "emby_sso", "name": "乙", "enabled": True, "default": True},
    ])

    assert [record["is_default_target"] for record in records] == [False, False]


def test_two_default_marked_auth_configs_can_be_saved_together() -> None:
    """两条都置位的登录入口配置能一并写入：裁剪在前，条件唯一索引拦不到它们。"""
    _save_auth_configs([
        {"type": "emby_sso", "name": "甲", "enabled": True, "default": True},
        {"type": "emby_sso", "name": "乙", "enabled": True, "default": True},
    ])

    assert len(read_system_setting(SystemConfigKey.AuthProviders)) == 2


# ---------------------------------------------------------------------------
# 入口扇出：与并族前逐条等价、两台服务器各自独立
# ---------------------------------------------------------------------------


def test_configured_entry_matches_the_pre_merge_fan_out_field_by_field() -> None:
    """一条配置扇出的入口，字段与并族前分身级钩子产出的入口逐条等价。

    对拍基准先取：登录入口类型尚未登记、也没有任何配置时，同一个实例键上只有分身级
    旧钩子那一条来源，得到的正是并族前的结果。
    """
    legacy = PluginProjection({"EmbySsoPlugin": _LegacyAuthPlugin()}).auth_providers()
    _register_type()
    _save_auth_configs([
        {"type": "emby_sso", "name": "客厅Emby", "enabled": True, "config": {}}
    ])

    merged = PluginProjection({"EmbySsoPlugin": _AuthPlugin()}).auth_providers()

    assert len(merged) == len(legacy) == 1
    for field in ("id", "type", "name", "icon", "enabled", "plugin_id", "instance_key"):
        assert merged[0][field] == legacy[0][field], field


def test_two_media_servers_are_two_independent_entries() -> None:
    """两台媒体服务器各自是独立登录入口，标识互不相同——并族前的分身级能力不得倒退。"""
    _register_type()
    _save_auth_configs([
        {"type": "emby_sso", "name": "客厅Emby", "enabled": True, "config": {}},
        {"type": "emby_sso", "name": "书房Emby", "enabled": True, "config": {}},
    ])

    providers = PluginProjection({"EmbySsoPlugin": _AuthPlugin()}).auth_providers()

    assert {provider["name"] for provider in providers} == {"客厅Emby", "书房Emby"}
    assert {provider["id"] for provider in providers} == {
        "emby_sso@客厅Emby", "emby_sso@书房Emby",
    }


def test_entries_follow_the_order_of_the_user_configuration() -> None:
    """入口次序取用户配置的先后，不取类型的登记先后。"""
    _register_type(owner="SiteSsoPlugin", service_type="site_sso", name="站点单点登录")
    _register_type()
    _save_auth_configs([
        {"type": "emby_sso", "name": "客厅Emby", "enabled": True, "config": {}},
        {"type": "site_sso", "name": "站点甲", "enabled": True, "config": {}},
        {"type": "emby_sso", "name": "书房Emby", "enabled": True, "config": {}},
    ])

    assert [entry.name for entry in list_auth_entries()] == [
        "客厅Emby", "站点甲", "书房Emby",
    ]


def test_two_media_servers_bind_different_identities_for_the_same_external_id(
    user_factory,
) -> None:
    """两台服务器上同号的账号各绑各的用户：入口标识不同即身份命名空间不同。"""
    _register_type()
    _save_auth_configs([
        {"type": "emby_sso", "name": "客厅Emby", "enabled": True, "config": {}},
        {"type": "emby_sso", "name": "书房Emby", "enabled": True, "config": {}},
    ])
    living_room, study = user_factory("客厅用户"), user_factory("书房用户")
    identities = UserIdentityOper()
    identities.bind(living_room, "emby_sso@客厅Emby", "1", "客厅账号")
    identities.bind(study, "emby_sso@书房Emby", "1", "书房账号")

    entries = {entry.name: entry.identity for entry in list_auth_entries()}
    living_ticket = create_plugin_auth_ticket_for_identity(entries["客厅Emby"], "1")
    study_ticket = create_plugin_auth_ticket_for_identity(entries["书房Emby"], "1")

    assert consume_plugin_auth_ticket(living_ticket)["user_id"] == living_room
    assert consume_plugin_auth_ticket(study_ticket)["user_id"] == study


def test_entry_disappears_when_its_type_is_not_registered() -> None:
    """类型登记随扩展启停，扩展没在跑就没有入口——点进去也没人能完成握手。"""
    _save_auth_configs([
        {"type": "emby_sso", "name": "客厅Emby", "enabled": True, "config": {}}
    ])

    assert list_auth_entries() == []


def test_disabled_config_produces_no_entry() -> None:
    """停用的配置不产出登录入口。"""
    _register_type()
    _save_auth_configs([
        {"type": "emby_sso", "name": "客厅Emby", "enabled": False, "config": {}}
    ])

    assert list_auth_entries() == []


def test_vue_mode_entry_carries_login_component_and_remote() -> None:
    """vue 模式下入口带固定的 AuthPage 组件与联邦远程入口。"""
    _register_type()
    _save_auth_configs([
        {"type": "emby_sso", "name": "客厅Emby", "enabled": True, "config": {}}
    ])
    projection = PluginProjection(
        {"EmbySsoPlugin": _VueAuthPlugin()},
        remote_entry_factory=lambda key, dist, version: f"/plugin/file/{key}/{dist}",
    )

    providers = projection.auth_providers()

    assert providers[0]["component"] == "AuthPage"
    assert providers[0]["remote"]["id"] == "EmbySsoPlugin"
    assert providers[0]["remote"]["version"] == _VueAuthPlugin.plugin_version


# ---------------------------------------------------------------------------
# 单实例类型与逐条错误隔离
# ---------------------------------------------------------------------------


def test_single_instance_type_configured_twice_stops_that_type_only() -> None:
    """单实例类型被配多份时整类型停摆，同族其它类型的入口照常产出。"""
    _register_type()
    _register_type(
        owner="SiteSsoPlugin",
        service_type="site_sso",
        name="站点单点登录",
        icon="mdi-web",
        multi_instance=False,
    )
    _save_auth_configs([
        {"type": "emby_sso", "name": "客厅Emby", "enabled": True, "config": {}},
        {"type": "site_sso", "name": "站点甲", "enabled": True, "config": {}},
        {"type": "site_sso", "name": "站点乙", "enabled": True, "config": {}},
    ])

    entries = list_auth_entries()

    assert [entry.name for entry in entries] == ["客厅Emby"]


def test_single_instance_type_configured_once_still_produces_its_entry() -> None:
    """单实例类型只配一份时照常产出入口。"""
    _register_type(
        owner="SiteSsoPlugin",
        service_type="site_sso",
        name="站点单点登录",
        multi_instance=False,
    )
    _save_auth_configs([
        {"type": "site_sso", "name": "站点甲", "enabled": True, "config": {}}
    ])

    assert [entry.name for entry in list_auth_entries()] == ["站点甲"]


def test_one_broken_config_does_not_take_the_whole_family_down() -> None:
    """一条配置的类型没人提供，只让它自己消失，其余入口仍在登录页上。"""
    _register_type()
    _save_auth_configs([
        {"type": "unknown_sso", "name": "没人提供", "enabled": True, "config": {}},
        {"type": "emby_sso", "name": "客厅Emby", "enabled": True, "config": {}},
    ])

    assert [entry.name for entry in list_auth_entries()] == ["客厅Emby"]


def test_ambiguous_identity_drops_only_the_conflicting_entries() -> None:
    """两条配置抢同一个身份绑定标识时两条都不产出，其余入口不受影响。"""
    _register_type()
    _save_auth_configs([
        {
            "type": "emby_sso", "name": "甲", "enabled": True, "config": {},
            "identity_provider": "shared",
        },
        {
            "type": "emby_sso", "name": "乙", "enabled": True, "config": {},
            "identity_provider": "shared",
        },
        {"type": "emby_sso", "name": "丙", "enabled": True, "config": {}},
    ])

    assert [entry.name for entry in list_auth_entries()] == ["丙"]


# ---------------------------------------------------------------------------
# 登录页：未登录可取、不泄露密钥
# ---------------------------------------------------------------------------


class _StubAuthService:
    """认证应用服务桩，只回答登录页要问的那一件事。"""

    @staticmethod
    def has_passkey() -> bool:
        """返回系统是否已有通行密钥。"""
        return False


def test_login_page_lists_entries_without_leaking_config_payload(monkeypatch) -> None:
    """未登录的登录页取得到入口列表，且拿不到配置载荷里的密钥。"""
    _register_type()
    _save_auth_configs([
        {
            "type": "emby_sso",
            "name": "客厅Emby",
            "enabled": True,
            "config": {"api_key": "s3cr3t", "host": "http://emby.local"},
        }
    ])

    class _Manager:
        """插件管理器桩，按真实投影产出入口列表。"""

        @staticmethod
        def get_plugin_auth_providers():
            """返回插件登录入口列表。"""
            return PluginProjection({"EmbySsoPlugin": _AuthPlugin()}).auth_providers()

    monkeypatch.setattr("app.api.endpoints.auth.PluginManager", _Manager)

    providers = auth_providers_endpoint(service=_StubAuthService())
    rendered = [AuthProviderInfo(**provider).model_dump() for provider in providers]

    assert [item["id"] for item in rendered] == ["emby_sso@客厅Emby"]
    assert "s3cr3t" not in str(providers)
    assert all("config" not in provider for provider in providers)
    assert all("config_form" not in item for item in rendered)


def test_one_broken_plugin_does_not_empty_the_login_page(monkeypatch) -> None:
    """一个插件组装入口时出错只跳过它自己，另一个插件的入口仍在登录页上。"""
    _register_type()
    _register_type(owner="SiteSsoPlugin", service_type="site_sso", name="站点单点登录")
    _save_auth_configs([
        {"type": "emby_sso", "name": "客厅Emby", "enabled": True, "config": {}},
        {"type": "site_sso", "name": "站点甲", "enabled": True, "config": {}},
    ])

    class _BrokenRenderPlugin(_AuthPlugin):
        """渲染模式钩子抛异常的插件桩。"""

        def get_render_mode(self):
            """模拟插件实现出错。"""
            raise RuntimeError("渲染模式读取失败")

    projection = PluginProjection({
        "EmbySsoPlugin": _BrokenRenderPlugin(),
        "SiteSsoPlugin": _AuthPlugin(),
    })

    providers = projection.auth_providers()

    assert [provider["id"] for provider in providers] == ["site_sso@站点甲"]


def test_login_entry_list_survives_a_config_read_failure(monkeypatch) -> None:
    """整族配置读取出错时入口列表退化为空，不向上抛断掉登录页。"""
    _register_type()
    monkeypatch.setattr(
        "app.runtime.extensions.projection.auth_entries.service_capability_configs",
        lambda capability: (_ for _ in ()).throw(RuntimeError("库连不上")),
    )

    assert list_auth_entries() == []


# ---------------------------------------------------------------------------
# 头号风险：存量绑定在升级后仍能登录
# ---------------------------------------------------------------------------


def test_legacy_binding_still_logs_in_after_the_merge(user_factory) -> None:
    """分身时代绑定的身份，在入口配置里填回旧标识后照常登录。

    存量行的 ``provider`` 是 ``plugin:<实例键>``，这个取值推不出并族后的类型与实例名，
    因此不能靠迁移改写；改由入口配置显式承接它，一行存量数据都不用动。
    """
    legacy_provider = "plugin:EmbySsoPlugin@livingroom"
    user_id = user_factory("存量用户")
    UserIdentityOper().bind(user_id, legacy_provider, "emby-uid-1", "存量账号")
    _register_type()
    _save_auth_configs([
        {
            "type": "emby_sso",
            "name": "客厅Emby",
            "enabled": True,
            "config": {},
            "identity_provider": legacy_provider,
        }
    ])

    providers = PluginProjection({"EmbySsoPlugin": _AuthPlugin()}).auth_providers()
    ticket = create_plugin_auth_ticket_for_identity(providers[0]["id"], "emby-uid-1")

    assert providers[0]["id"] == legacy_provider
    assert consume_plugin_auth_ticket(ticket)["user_id"] == user_id


def test_renaming_an_instance_can_keep_the_old_identity(user_factory) -> None:
    """改名即换入口，把改名前的派生取值填进身份绑定标识即可保住绑定。"""
    user_id = user_factory("改名用户")
    UserIdentityOper().bind(user_id, "emby_sso@客厅Emby", "emby-uid-3")
    _register_type()
    _save_auth_configs([
        {
            "type": "emby_sso",
            "name": "起居室Emby",
            "enabled": True,
            "config": {},
            "identity_provider": "emby_sso@客厅Emby",
        }
    ])

    entry = list_auth_entries()[0]
    ticket = create_plugin_auth_ticket_for_identity(entry.identity, "emby-uid-3")

    assert entry.name == "起居室Emby"
    assert consume_plugin_auth_ticket(ticket)["user_id"] == user_id


def test_binding_row_is_untouched_by_the_merge(user_factory) -> None:
    """并族不改写身份绑定行：宿主不生成也不改写 provider 列。"""
    user_id = user_factory("未承接用户")
    UserIdentityOper().bind(user_id, "plugin:EmbySsoPlugin@livingroom", "emby-uid-2")
    _register_type()
    _save_auth_configs([
        {"type": "emby_sso", "name": "客厅Emby", "enabled": True, "config": {}}
    ])

    list_auth_entries()
    row = UserIdentityOper().get_by_provider_external_id(
        "plugin:EmbySsoPlugin@livingroom", "emby-uid-2"
    )

    assert row is not None
    assert row.user_id == user_id


# ---------------------------------------------------------------------------
# 旧钩子的去留
# ---------------------------------------------------------------------------


def test_declaration_hook_for_auth_providers_is_gone() -> None:
    """`provides_auth_providers()` 整条删除，插件基类上不再有这个钩子。"""
    from app.sdk.extension import _PluginBase

    assert not hasattr(_PluginBase, "provides_auth_providers")


def test_plugin_still_declaring_the_removed_hook_is_simply_ignored() -> None:
    """插件残留着已删除的钩子时宿主原样忽略，不因此报错也不产出入口。"""

    class _StalePlugin(_AuthPlugin):
        """仍实现已删除钩子的插件桩。"""

        def provides_auth_providers(self):
            """返回旧声明，宿主不再取用。"""
            return [{"id": "oidc", "name": "OIDC 登录"}]

    providers = PluginProjection({"EmbySsoPlugin": _StalePlugin()}).auth_providers()

    assert providers == []


def test_legacy_dict_hook_still_produces_entries_alongside_configured_ones() -> None:
    """分身级旧钩子仍在废弃期内照常生效，与配置扇出的入口并存。"""
    _register_type()
    _save_auth_configs([
        {"type": "emby_sso", "name": "客厅Emby", "enabled": True, "config": {}}
    ])

    class _MixedPlugin(_AuthPlugin):
        """同时提供配置扇出入口与分身级旧钩子入口的插件桩。"""

        def get_auth_providers(self):
            """返回分身级登录入口描述。"""
            return [{"id": "legacy_provider", "name": "旧式登录"}]

    providers = PluginProjection({"EmbySsoPlugin": _MixedPlugin()}).auth_providers()

    assert {provider["id"] for provider in providers} == {
        "emby_sso@客厅Emby", "legacy_provider",
    }


@pytest.fixture
def user_factory() -> Iterator[Any]:
    """按用户名建号并返回其主键，用例结束后清理。

    :return: 建号函数
    """
    created: List[str] = []
    users = UserOper()

    def _create(name: str) -> int:
        """建一个可用于身份绑定的用户。

        :param name: 用户名
        :return: 用户主键
        """
        users.add(name=name, is_active=True, is_superuser=False, hashed_password="x")
        created.append(name)
        return users.get_by_name(name).id

    yield _create
    for name in created:
        # 删用户会级联删掉它名下的全部身份绑定，用例之间因此不会互相看见对方的绑定
        users.delete_by_name(name)
