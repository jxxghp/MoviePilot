"""GitHub 单点登录插件的守护测试。

本插件是登录认证族目前唯一的实现，宿主自带的登录入口类型为零，因此它同时承担参考
实现的角色。测试盯住两类事：

- **它是不是一个合规的登录入口类型**：声明过得了通用服务实例契约、被投影取得到、
  身份绑定标识按族规则派生、抢标识的两条配置一并消失、配置契约拦得住畸形配置、
  界面与端点都不出现本族不成立的「设为默认」。
- **它是不是一条守得住的登录链路**：state 一次性且过期即废、回跳地址不出站、授权码
  不能重放、身份绑定按 ``(provider, external_id)`` 命中、未绑定且未开自动建号时
  不登录成功。
"""

import asyncio
import inspect
import json
import time
from typing import Any, Dict, Iterator, List

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.endpoints.auth import auth_providers as auth_providers_endpoint
from app.application.security.auth import consume_plugin_auth_ticket
from app.application.service_config import async_write_system_setting
from app.db.oper.user import UserOper
from app.db.oper.user_identity import UserIdentityOper
from app.sdk.extension import _PluginBase
from app.plugins.githubsso import (
    ERROR_FRAGMENT_KEY,
    SERVICE_TYPE,
    STATE_COOKIE_NAME,
    TICKET_FRAGMENT_KEY,
    GithubSso,
)
from app.plugins.githubsso.config_ui import CONFIG_SCHEMA
from app.plugins.githubsso.entry import GithubIdentity, GithubSsoEntry
from app.plugins.githubsso.oauth_state import (
    DEFAULT_RETURN_PATH,
    STATE_TTL_SECONDS,
    OAuthStateStore,
    safe_return_path,
)
from app.runtime.extensions.projection.auth_entries import list_auth_entries
from app.runtime.extensions.contract.config_schema import (
    config_schema_violation,
    config_value_violations,
)
from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.admission.service_instance import (
    service_instance_declaration_violation,
)
from app.runtime.extensions.service_config import AUTH_CAPABILITY
from app.runtime.extensions.registry.service_instance import service_instance_registry
from app.runtime.log import wrap_for_plugin_instance
from app.schemas.types import SystemConfigKey
from app.schemas.user import AuthProviderInfo

# 本插件默认实例在宿主内的实例键，同时是登记表里该类型的 owner
OWNER = "GithubSso"

# 一份可用的登录入口配置内容
GOOD_CONFIG: Dict[str, Any] = {
    "client_id": "Iv1.0123456789abcdef",
    "client_secret": "s3cr3t-client-secret",
    "redirect_uri": f"https://mp.example.com/api/v1/plugin/{OWNER}/callback",
}


def _plugin(enabled: bool = True) -> GithubSso:
    """建一个已生效配置的插件实例。

    :param enabled: 插件是否启用
    :return: 插件实例
    """
    plugin = GithubSso()
    plugin.init_plugin({"enabled": enabled})
    return plugin


def _register_type(multi_instance: bool = True) -> None:
    """把本插件声明的登录入口类型登进服务实例登记表。

    :param multi_instance: 该类型能否配多份
    :return: 无返回值
    """
    declaration = _plugin().provides_service_instances()[0]
    service_instance_registry.register(
        capability=declaration.capability,
        service_type=declaration.type,
        name=declaration.name,
        owner=OWNER,
        icon=declaration.icon,
        impl=declaration.impl,
        multi_instance=multi_instance,
        distribution=ExtensionDistribution.MARKET,
        config_form=declaration.config_form,
        config_schema=declaration.config_schema,
    )


def _save_auth_configs(configs: List[Dict[str, Any]]) -> None:
    """整族覆盖登录入口配置。

    :param configs: 整族配置列表
    :return: 无返回值
    """
    asyncio.run(async_write_system_setting(SystemConfigKey.AuthProviders, configs))


def _one_config(name: str = "GitHub", **overrides: Any) -> List[Dict[str, Any]]:
    """拼一条启用的本类型配置。

    :param name: 实例名
    :param overrides: 覆盖到配置外壳上的字段
    :return: 整族配置列表
    """
    record: Dict[str, Any] = {
        "type": SERVICE_TYPE,
        "name": name,
        "enabled": True,
        "config": dict(GOOD_CONFIG),
    }
    record.update(overrides)
    return [record]


class _StubAuthService:
    """认证应用服务桩，只回答登录页要问的那一件事。"""

    @staticmethod
    def has_passkey() -> bool:
        """返回系统是否已有通行密钥。"""
        return False


@pytest.fixture(autouse=True)
def clean_auth_family() -> Iterator[None]:
    """用例前后都清空本插件的登记与整族配置，避免互相污染。"""
    service_instance_registry.unregister_owner(OWNER)
    _save_auth_configs([])
    yield
    service_instance_registry.unregister_owner(OWNER)
    _save_auth_configs([])


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
        users.delete_by_name(name)


@pytest.fixture
def stub_github(monkeypatch) -> Dict[str, Any]:
    """把与 GitHub 的三次往返换成可控的桩。

    :return: 记录调用参数的字典，用例据此断言插件交出去的是什么
    """
    calls: Dict[str, Any] = {"codes": [], "allowed": True, "identity": None}

    def _exchange(_self, code: str) -> str:
        """记录授权码并交回访问令牌。"""
        calls["codes"].append(code)
        return f"token-for-{code}"

    def _orgs(_self, _token: str):
        """按用例设定回答组织准入。"""
        return (True, "") if calls["allowed"] else (False, "该 GitHub 账号不属于允许登录的组织")

    def _identity(_self, _token: str) -> GithubIdentity:
        """交回固定的第三方账号身份。"""
        return calls["identity"] or GithubIdentity(
            external_id="583231", login="octocat", display_name="The Octocat"
        )

    monkeypatch.setattr(GithubSsoEntry, "exchange_code", _exchange)
    monkeypatch.setattr(GithubSsoEntry, "organization_allowed", _orgs)
    monkeypatch.setattr(GithubSsoEntry, "fetch_identity", _identity)
    return calls


# ---------------------------------------------------------------------------
# 它是不是一个合规的登录入口类型
# ---------------------------------------------------------------------------


def test_declaration_passes_the_shared_service_instance_contract() -> None:
    """登录入口声明走通用服务实例契约，没有另开一条校验。"""
    declaration = _plugin().provides_service_instances()[0]

    assert declaration.capability == AUTH_CAPABILITY
    assert declaration.type == SERVICE_TYPE
    assert declaration.impl is GithubSsoEntry
    assert service_instance_declaration_violation(declaration, render_mode="vuetify") is None


def test_multi_instance_is_declared_true_for_multiple_github_deployments() -> None:
    """本类型的站点地址可配，第二份配置指的是另一个 GitHub 部署，故按多实例声明。"""
    declaration = _plugin().provides_service_instances()[0]

    assert declaration.multi_instance is True
    assert "base_url" in CONFIG_SCHEMA["properties"]


def test_removed_declarative_auth_hook_is_not_used() -> None:
    """插件不使用已随认证并族删除的 `provides_auth_providers()`。"""
    assert not hasattr(_PluginBase, "provides_auth_providers")
    assert not hasattr(GithubSso, "provides_auth_providers")


def test_deprecated_v2_hooks_are_not_reintroduced() -> None:
    """已有声明式替代的 v2 钩子一个都不自己实现，走的是声明式那条路。"""
    for hook in (
        "get_auth_providers",
        "get_command",
        "get_service",
        "get_module",
        "get_dashboard_meta",
        "get_actions",
        "get_media_source",
        "get_agent_tools",
        "get_channel_capabilities",
    ):
        assert hook not in vars(GithubSso), f"{hook} 已有声明式替代，不应由本插件实现"


def test_still_implements_the_hooks_that_have_no_declarative_replacement() -> None:
    """没有声明式替代的钩子照常实现，它们仍是插件基类上的正常扩展点。"""
    for hook in ("init_plugin", "get_state", "get_api", "get_form", "get_page"):
        assert hook in vars(GithubSso), f"{hook} 仍是合法钩子，应由本插件实现"


def test_projection_picks_up_the_configured_entry() -> None:
    """声明登记后，用户配置扇出的入口能被投影取到。"""
    _register_type()
    _save_auth_configs(_one_config())

    providers = PluginProjection({OWNER: _plugin()}).auth_providers()

    assert [provider["id"] for provider in providers] == [f"{SERVICE_TYPE}@GitHub"]
    assert providers[0]["name"] == "GitHub"
    assert providers[0]["service_type"] == SERVICE_TYPE


def test_login_page_lists_the_entry_without_leaking_credentials(monkeypatch) -> None:
    """未登录的登录页取得到入口，且拿不到配置里的 client_secret。"""
    _register_type()
    _save_auth_configs(_one_config())

    class _Manager:
        """插件管理器桩，按真实投影产出入口列表。"""

        @staticmethod
        def get_plugin_auth_providers():
            """返回插件登录入口列表。"""
            return PluginProjection({OWNER: _plugin()}).auth_providers()

    monkeypatch.setattr("app.api.endpoints.auth.PluginManager", _Manager)

    providers = auth_providers_endpoint(service=_StubAuthService())
    rendered = [AuthProviderInfo(**provider).model_dump() for provider in providers]

    assert f"{SERVICE_TYPE}@GitHub" in [item["id"] for item in rendered]
    assert GOOD_CONFIG["client_secret"] not in str(providers)
    assert GOOD_CONFIG["client_id"] not in str(providers)
    assert all("config" not in provider for provider in providers)


def test_identity_defaults_to_type_at_instance_name() -> None:
    """身份绑定标识留空时按「类型@实例名」派生。"""
    _register_type()
    _save_auth_configs(_one_config(name="工作号"))

    assert [entry.identity for entry in list_auth_entries()] == [f"{SERVICE_TYPE}@工作号"]


def test_explicit_identity_provider_wins() -> None:
    """身份绑定标识填了就用填的那个，派生只在留空时兜底。"""
    _register_type()
    _save_auth_configs(_one_config(identity_provider="plugin:GithubSso"))

    assert [entry.identity for entry in list_auth_entries()] == ["plugin:GithubSso"]


def test_two_configs_claiming_one_identity_produce_no_entry() -> None:
    """两条配置抢同一个身份绑定标识时两条都不产出入口。"""
    _register_type()
    _save_auth_configs(
        _one_config(name="甲", identity_provider="shared")
        + _one_config(name="乙", identity_provider="shared")
        + _one_config(name="丙")
    )

    assert [entry.name for entry in list_auth_entries()] == ["丙"]


def test_config_schema_is_within_the_supported_subset() -> None:
    """配置契约落在宿主受支持的 JSON Schema 子集内，且不声明宿主填入的字段。"""
    assert config_schema_violation(CONFIG_SCHEMA, reserved_property_names=("name",)) is None
    assert "name" not in CONFIG_SCHEMA["properties"]
    assert "identity_provider" not in CONFIG_SCHEMA["properties"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"client_id": "a", "client_secret": "b"},
        {**GOOD_CONFIG, "client_id": 1},
        {**GOOD_CONFIG, "redirect_uri": "javascript:alert(1)"},
        {**GOOD_CONFIG, "base_url": "not-a-url"},
        {**GOOD_CONFIG, "request_timeout": 0},
        {**GOOD_CONFIG, "allowed_organizations": "acme"},
        {**GOOD_CONFIG, "unknown_field": "x"},
    ],
)
def test_config_schema_rejects_malformed_config(payload: Dict[str, Any]) -> None:
    """畸形配置在写入与构造两处共用的契约判定上被拦下。"""
    assert config_value_violations(CONFIG_SCHEMA, payload)


def test_config_schema_accepts_a_well_formed_config() -> None:
    """合规配置不产生任何违约描述。"""
    assert config_value_violations(CONFIG_SCHEMA, GOOD_CONFIG) == ()


def test_nothing_offers_to_set_a_default_target() -> None:
    """本族不设默认调用目标，界面与入口描述都不出现「设为默认」。"""
    _register_type()
    _save_auth_configs(_one_config())
    declaration = _plugin().provides_service_instances()[0]

    assert "设为默认" not in str(declaration.config_form)
    assert "default" not in str(declaration.config_form[1])
    providers = PluginProjection({OWNER: _plugin()}).auth_providers()
    assert all("default" not in provider for provider in providers)


# ---------------------------------------------------------------------------
# 它是不是一条守得住的登录链路
# ---------------------------------------------------------------------------


def test_authorize_returns_a_github_url_carrying_a_fresh_state() -> None:
    """发起授权返回 GitHub 授权地址，其中带着服务端刚签发的 state。"""
    _register_type()
    _save_auth_configs(_one_config())
    plugin = _plugin()

    response = plugin.authorize(provider=f"{SERVICE_TYPE}@GitHub", return_to="/dashboard")
    url = json.loads(response.body)["authorize_url"]

    assert url.startswith("https://github.com/login/oauth/authorize?")
    assert "state=" in url
    assert "allow_signup=false" in url
    assert GOOD_CONFIG["client_secret"] not in url


def test_authorize_hands_the_browser_binding_out_only_as_a_cookie() -> None:
    """浏览器绑定凭据只经 Cookie 下发，不出现在授权地址里。"""
    _register_type()
    _save_auth_configs(_one_config())

    response = _plugin().authorize(provider=f"{SERVICE_TYPE}@GitHub")
    binding = _cookies_of(response)[STATE_COOKIE_NAME]
    header = next(
        value.decode()
        for key, value in response.raw_headers
        if key.lower() == b"set-cookie"
    )

    assert binding
    assert binding not in json.loads(response.body)["authorize_url"]
    assert "HttpOnly" in header
    assert "SameSite=lax" in header
    assert "Secure" in header


def test_authorize_refuses_an_unknown_entry() -> None:
    """入口标识不属于本实例名下任何一条入口时不予发起。"""
    _register_type()
    _save_auth_configs(_one_config())

    with pytest.raises(HTTPException) as failure:
        _plugin().authorize(provider="github@别人家")

    assert failure.value.status_code == 404


def test_authorize_refuses_an_entry_dropped_for_identity_ambiguity() -> None:
    """被身份歧义剔除的入口在登录页上不存在，也不能绕过登录页直接发起授权。"""
    _register_type()
    _save_auth_configs(
        _one_config(name="甲", identity_provider="shared")
        + _one_config(name="乙", identity_provider="shared")
    )

    with pytest.raises(HTTPException) as failure:
        _plugin().authorize(provider="shared")

    assert failure.value.status_code == 404


def test_authorize_refuses_a_redirect_uri_pointing_elsewhere() -> None:
    """回调地址不指向本入口的回调路由时不予发起授权。"""
    _register_type()
    _save_auth_configs(
        _one_config(config={**GOOD_CONFIG, "redirect_uri": "https://evil.example/steal"})
    )

    with pytest.raises(HTTPException) as failure:
        _plugin().authorize(provider=f"{SERVICE_TYPE}@GitHub")

    assert failure.value.status_code == 400


def test_callback_without_a_known_state_is_refused() -> None:
    """回调不带服务端签发过的 state 时整条链路中止。"""
    _register_type()
    _save_auth_configs(_one_config())

    for state in ("", "forged-state"):
        with pytest.raises(HTTPException) as failure:
            _plugin().callback(
                request=_Browser(state, {STATE_COOKIE_NAME: "x"}),
                code="whatever",
                state=state,
            )
        assert failure.value.status_code == 400


def test_callback_from_another_browser_is_refused(stub_github, user_factory) -> None:
    """一条被转发出去的回调地址在别人的浏览器上不成立：绑定凭据只在发起方手里。

    这挡的是登录 CSRF：攻击者拿自己的授权结果诱使他人点开，若只核 state，受害者会在
    毫不知情的情况下登进攻击者的账号。
    """
    user_id = user_factory("攻击者")
    UserIdentityOper().bind(user_id, f"{SERVICE_TYPE}@GitHub", "583231")
    _register_type()
    _save_auth_configs(_one_config())
    plugin = _plugin()
    attacker = _start_login(plugin, f"{SERVICE_TYPE}@GitHub")

    victim = _Browser(attacker.state, {})

    with pytest.raises(HTTPException) as failure:
        plugin.callback(request=victim, code="code-0", state=attacker.state)
    assert failure.value.status_code == 400
    assert stub_github["codes"] == []


def test_state_is_single_use(stub_github, user_factory) -> None:
    """同一枚 state 只能兑现一次，重放同一份回调地址会被挡在 state 这一关。"""
    user_id = user_factory("绑定用户")
    UserIdentityOper().bind(user_id, f"{SERVICE_TYPE}@GitHub", "583231", "The Octocat")
    _register_type()
    _save_auth_configs(_one_config())
    plugin = _plugin()
    browser = _start_login(plugin, f"{SERVICE_TYPE}@GitHub")

    first = plugin.callback(request=browser, code="code-1", state=browser.state)

    assert TICKET_FRAGMENT_KEY in first.headers["location"]
    with pytest.raises(HTTPException) as replay:
        plugin.callback(request=browser, code="code-1", state=browser.state)
    assert replay.value.status_code == 400
    assert stub_github["codes"] == ["code-1"]


def test_state_expires(monkeypatch) -> None:
    """过期的 state 取不回来，过期即废没有宽限。"""
    store = OAuthStateStore()
    state, _ = store.issue("github@甲", "/")
    expired_at = time.time() + STATE_TTL_SECONDS + 1

    monkeypatch.setattr("app.plugins.githubsso.oauth_state.time.time", lambda: expired_at)

    assert store.consume(state) is None


def test_state_is_consumed_exactly_once() -> None:
    """取回即销毁：同一枚 state 第二次取回为空。"""
    store = OAuthStateStore()
    state, binding = store.issue("github@甲", "/dashboard")
    record = store.consume(state)

    assert record.return_path == "/dashboard"
    assert record.matches_binding(binding) is True
    assert record.matches_binding("another-browser") is False
    assert store.consume(state) is None


@pytest.mark.parametrize(
    "candidate",
    [
        "//evil.example/x",
        "https://evil.example/x",
        "http://evil.example",
        "/\\evil.example",
        "javascript:alert(1)",
        "evil.example",
        "/path#frag",
        "/path\nSet-Cookie: a=b",
        "",
        None,
    ],
)
def test_return_path_outside_the_site_falls_back_to_the_root(candidate) -> None:
    """回跳地址只接受站内相对路径，其余一律回落到站点根路径。"""
    assert safe_return_path(candidate) == DEFAULT_RETURN_PATH


def test_return_path_keeps_a_plain_site_path() -> None:
    """站内相对路径原样保留。"""
    assert safe_return_path("/dashboard?tab=1") == "/dashboard?tab=1"


def test_callback_only_returns_to_the_path_captured_at_authorize_time(
    stub_github, user_factory
) -> None:
    """回跳地址取发起授权时留存的那一份，回调请求带来的取值不参与。"""
    user_id = user_factory("回跳用户")
    UserIdentityOper().bind(user_id, f"{SERVICE_TYPE}@GitHub", "583231")
    _register_type()
    _save_auth_configs(_one_config())
    plugin = _plugin()
    browser = _start_login(plugin, f"{SERVICE_TYPE}@GitHub", return_to="/dashboard")

    response = plugin.callback(request=browser, code="code-2", state=browser.state)

    assert response.headers["location"].startswith("/dashboard#")


def test_bound_identity_logs_in_as_the_bound_user(stub_github, user_factory) -> None:
    """身份绑定按 (provider, external_id) 命中已绑用户，票据落到那个用户身上。"""
    user_id = user_factory("已绑用户")
    UserIdentityOper().bind(user_id, f"{SERVICE_TYPE}@GitHub", "583231", "The Octocat")
    _register_type()
    _save_auth_configs(_one_config())
    plugin = _plugin()
    browser = _start_login(plugin, f"{SERVICE_TYPE}@GitHub")

    location = plugin.callback(request=browser, code="code-3", state=browser.state).headers["location"]
    ticket = location.split(f"#{TICKET_FRAGMENT_KEY}=", 1)[1]

    assert consume_plugin_auth_ticket(ticket)["user_id"] == user_id


def test_binding_key_is_the_numeric_account_id_not_the_login_name(
    stub_github, user_factory
) -> None:
    """绑定键取 GitHub 的数字账号标识：登录名改得掉，原名会被别人注册走。"""
    user_id = user_factory("改名用户")
    UserIdentityOper().bind(user_id, f"{SERVICE_TYPE}@GitHub", "583231")
    stub_github["identity"] = GithubIdentity(
        external_id="583231", login="renamed-octocat", display_name="Renamed"
    )
    _register_type()
    _save_auth_configs(_one_config())
    plugin = _plugin()
    browser = _start_login(plugin, f"{SERVICE_TYPE}@GitHub")

    location = plugin.callback(request=browser, code="code-4", state=browser.state).headers["location"]

    ticket = location.split(f"#{TICKET_FRAGMENT_KEY}=", 1)[1]
    assert consume_plugin_auth_ticket(ticket)["user_id"] == user_id
    assert UserIdentityOper().get_by_provider_external_id(
        f"{SERVICE_TYPE}@GitHub", "renamed-octocat"
    ) is None


def test_ticket_carries_the_entry_identity_as_provider(stub_github, user_factory) -> None:
    """签票时 provider_id 原样回传入口标识，插件不自行拼一个。"""
    user_id = user_factory("标识用户")
    UserIdentityOper().bind(user_id, "plugin:GithubSso@legacy", "583231")
    _register_type()
    _save_auth_configs(_one_config(identity_provider="plugin:GithubSso@legacy"))
    plugin = _plugin()
    browser = _start_login(plugin, "plugin:GithubSso@legacy")

    location = plugin.callback(request=browser, code="code-5", state=browser.state).headers["location"]
    ticket = location.split(f"#{TICKET_FRAGMENT_KEY}=", 1)[1]

    assert consume_plugin_auth_ticket(ticket)["provider_id"] == "plugin:GithubSso@legacy"


def test_unbound_identity_does_not_log_in(stub_github) -> None:
    """未绑定且未开自动建号时不登录成功，也不替用户建号。"""
    _register_type()
    _save_auth_configs(_one_config())
    plugin = _plugin()
    browser = _start_login(plugin, f"{SERVICE_TYPE}@GitHub")
    before = UserOper().list()

    location = plugin.callback(request=browser, code="code-6", state=browser.state).headers["location"]

    assert f"#{ERROR_FRAGMENT_KEY}=" in location
    assert TICKET_FRAGMENT_KEY not in location
    assert len(UserOper().list()) == len(before)


def test_account_outside_the_allowed_organizations_does_not_log_in(
    stub_github, user_factory
) -> None:
    """组织名单是一道准入闸，名单外的账号即便已绑定也不放行。"""
    user_id = user_factory("组织外用户")
    UserIdentityOper().bind(user_id, f"{SERVICE_TYPE}@GitHub", "583231")
    stub_github["allowed"] = False
    _register_type()
    _save_auth_configs(
        _one_config(config={**GOOD_CONFIG, "allowed_organizations": ["acme"]})
    )
    plugin = _plugin()
    browser = _start_login(plugin, f"{SERVICE_TYPE}@GitHub")

    location = plugin.callback(request=browser, code="code-7", state=browser.state).headers["location"]

    assert f"#{ERROR_FRAGMENT_KEY}=" in location
    assert TICKET_FRAGMENT_KEY not in location


def test_denied_authorization_returns_to_the_login_page(stub_github) -> None:
    """用户在 GitHub 侧拒绝授权时回跳登录页并带上原因，不落进错误页。"""
    _register_type()
    _save_auth_configs(_one_config())
    plugin = _plugin()
    browser = _start_login(plugin, f"{SERVICE_TYPE}@GitHub", return_to="/login")

    response = plugin.callback(
        request=browser, state=browser.state, error="access_denied", error_description="用户已拒绝"
    )

    assert response.headers["location"].startswith(f"/login#{ERROR_FRAGMENT_KEY}=")
    assert stub_github["codes"] == []


def test_callback_still_takes_its_request_after_the_host_wraps_it() -> None:
    """宿主为日志归属包了一层后，回调仍能被 FastAPI 按签名注入请求对象。

    浏览器绑定凭据是从请求 Cookie 上读的，注入一旦失配，这道校验在真实路由上就会
    变成一次 500，而单测里直接调方法看不出来。
    """
    _register_type()
    _save_auth_configs(_one_config())
    plugin = _plugin()
    wrapped = wrap_for_plugin_instance(plugin.callback, OWNER, "default")
    app = FastAPI()
    app.add_api_route("/callback", wrapped, methods=["GET"])
    app.add_api_route(
        "/authorize",
        wrap_for_plugin_instance(plugin.authorize, OWNER, "default"),
        methods=["GET"],
    )
    client = TestClient(app)

    started = client.get("/authorize", params={"provider": f"{SERVICE_TYPE}@GitHub"})
    refused = client.get("/callback", params={"state": "forged", "code": "x"})

    assert "request" in inspect.signature(wrapped).parameters
    assert started.status_code == 200
    assert started.json()["authorize_url"].startswith("https://github.com/")
    assert started.cookies.get(STATE_COOKIE_NAME)
    assert refused.status_code == 400


def test_login_endpoints_are_anonymous_and_the_rest_are_not_registered() -> None:
    """登录链路的两个接口匿名开放，它们发生在任何用户会话之前。"""
    apis = _plugin().get_api()

    assert {api["path"] for api in apis} == {"/authorize", "/callback"}
    assert all(api["allow_anonymous"] is True for api in apis)
    assert all(api["methods"] == ["GET"] for api in apis)


def test_entry_refuses_to_be_built_without_credentials() -> None:
    """凭据缺失或回调地址不是绝对 http 地址时构造即失败，不留到握手时才炸。"""
    with pytest.raises(ValueError):
        GithubSsoEntry(name="x", client_id="", client_secret="b", redirect_uri="https://a/b")
    with pytest.raises(ValueError):
        GithubSsoEntry(name="x", client_id="a", client_secret="b", redirect_uri="/relative")


def test_authorize_url_asks_for_read_org_only_when_the_list_is_configured() -> None:
    """不配组织名单就不索取 read:org，多要一项用户没法只同意其中一项。"""
    without = GithubSsoEntry(name="x", **GOOD_CONFIG)
    with_orgs = GithubSsoEntry(name="x", **GOOD_CONFIG, allowed_organizations=["acme"])

    assert without.scope == "read:user"
    assert with_orgs.scope == "read:user read:org"


class _Browser:
    """一次授权往返里浏览器手上的两样东西：state 与随 Cookie 收到的绑定凭据。

    同时充当回调请求桩——回调只从请求上读 Cookie。

    :param state: 授权地址里带出去的 state
    :param cookies: 浏览器当前持有的 Cookie
    """

    def __init__(self, state: str, cookies: Dict[str, str]) -> None:
        """记录本次往返的 state 与 Cookie。"""
        self.state = state
        self.cookies = cookies


def _start_login(plugin: GithubSso, identity: str, return_to: str = "/") -> _Browser:
    """走一次发起授权，取回浏览器由此持有的 state 与绑定凭据。

    :param plugin: 插件实例
    :param identity: 登录入口标识
    :param return_to: 登录成功后的回跳路径
    :return: 浏览器状态
    """
    response = plugin.authorize(provider=identity, return_to=return_to)
    url = json.loads(response.body)["authorize_url"]
    state = url.split("state=", 1)[1].split("&", 1)[0]
    return _Browser(state, _cookies_of(response))


def _cookies_of(response: Any) -> Dict[str, str]:
    """从响应的 Set-Cookie 头里解出 Cookie 名到取值的映射。

    :param response: 响应对象
    :return: Cookie 映射，被删除的 Cookie 不出现在其中
    """
    jar: Dict[str, str] = {}
    for key, value in response.raw_headers:
        if key.lower() != b"set-cookie":
            continue
        name, _, rest = value.decode().partition("=")
        item = rest.split(";", 1)[0]
        if item:
            jar[name] = item
    return jar
