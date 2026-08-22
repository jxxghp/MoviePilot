"""服务族登记表：内建族登记、契约校验按表判定、列举顺序与登记先后无关。

服务族是登记出来的而不是写死在声明面上的枚举，因此可声明服务实例的能力标签集合由
本表回答。本文件锁死两件事：宿主自带族恰好是下载器、媒体服务器、消息通知、存储与登录认证五族，取值与
`ModuleType` 的对应成员逐字相同；以及未登记的标签仍被契约校验拒绝，拒绝信息如实列出
当前登记的族而不是一份写死的清单。

顺序判据见 docs/plugin-extension-architecture.md §7.2：候选列表要按确定规则排序，不得
依赖宿主内部的登记先后。
"""

from typing import Any, Iterator, Optional

import pytest
from fastapi import HTTPException

from app.api.endpoints.service import config_form as service_config_form_endpoint
from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.extensions.contract.declaration import ServiceInstanceDeclaration
from app.runtime.extensions.admission.service_instance import (
    service_instance_declaration_violation,
)
from app.runtime.extensions.registry.service_family import (
    ServiceFamilyRegistry,
    register_builtin_service_families,
    service_family_registry,
)
from app.schemas.types import ModuleType
from tests.test_plugin_provided_storages import _ValidPluginStorage

# 宿主自带服务族的能力标签，取值与 `ModuleType` 成员逐字相同
_BUILTIN_CAPABILITIES = ("auth", "downloader", "mediaserver", "notification", "storage")

# 可经 `ServiceInstanceDeclaration` 声明的族，四族全在其列——存储只是构造协议不同，
# 不再另设专用钩子
_DECLARABLE_CAPABILITIES = _BUILTIN_CAPABILITIES


class _DemoServiceClient:
    """契约合规的服务实例实现桩，带齐各族取用链上必须在场的方法。

    本文件按族遍历同一条声明，因此这个桩要同时满足下载器、媒体服务器与消息通知三族
    的必填集；存储族另有构造协议，由 `_ValidPluginStorage` 承担。
    """

    def __init__(self, name: Optional[str] = None, **kwargs: Any):
        """记录宿主传入的实例名。"""
        self.name = name

    def is_inactive(self) -> bool:
        """回答连接是否已断开，下载器与媒体服务器族的重连回路直调它。"""
        return False

    def reconnect(self) -> bool:
        """重建连接，下载器与媒体服务器族判定失活后直调它。"""
        return True

    def get_state(self) -> bool:
        """回答通道是否就绪，消息通知族的连通性测试直调它。"""
        return True


@pytest.fixture
def isolated_registry() -> Iterator[None]:
    """快照并复原全局登记表，避免临时登记污染其它用例。"""
    original = dict(service_family_registry._entries)
    try:
        yield
    finally:
        service_family_registry._entries.clear()
        service_family_registry._entries.update(original)


def test_builtin_families_are_registered_and_vocabulary_is_unchanged() -> None:
    """宿主自带族恰好是五族，取值与 `ModuleType` 成员一致。"""
    assert service_family_registry.capabilities() == _BUILTIN_CAPABILITIES
    assert set(_BUILTIN_CAPABILITIES) == {
        ModuleType.Auth.value,
        ModuleType.Downloader.value,
        ModuleType.MediaServer.value,
        ModuleType.Notification.value,
        ModuleType.Storage.value,
    }


def test_builtin_families_carry_display_name_and_builtin_ownership() -> None:
    """内建族登记为宿主自持：有展示名称、发行方式为内建、没有登记方实例键。"""
    entries = {entry.capability: entry for entry in service_family_registry.entries()}

    assert set(entries) == set(_BUILTIN_CAPABILITIES)
    for capability, entry in entries.items():
        assert entry.name and entry.name != capability, capability
        assert entry.distribution is ExtensionDistribution.BUILTIN, capability
        assert entry.owner is None, capability


@pytest.mark.parametrize("capability", _DECLARABLE_CAPABILITIES)
def test_declaration_is_accepted_for_every_builtin_family(capability: str) -> None:
    """可声明的内建族标签都被契约校验接受，实现按各族的构造协议给。"""
    declaration = ServiceInstanceDeclaration(
        capability=capability,
        type="demo_type",
        name="演示类型",
        impl=_ValidPluginStorage if capability == "storage" else _DemoServiceClient,
    )

    assert service_instance_declaration_violation(declaration) is None


@pytest.mark.parametrize(
    "capability",
    ["Downloaders", "downloaders", "subtitleserver", "不存在"],
)
def test_declaration_is_rejected_for_unregistered_capability(capability: str) -> None:
    """未登记的能力标签仍被契约校验拒绝，拒绝信息列出当前可声明的族。"""
    declaration = ServiceInstanceDeclaration(
        capability=capability, type="demo_type", name="演示类型", impl=_DemoServiceClient
    )

    violation = service_instance_declaration_violation(declaration)

    assert violation is not None
    assert "不是可声明服务实例的能力标签" in violation
    assert str(list(_DECLARABLE_CAPABILITIES)) in violation


def test_rejection_message_reflects_the_registry_at_call_time(
    isolated_registry: None,
) -> None:
    """新登记一族后，拒绝信息里的候选列表随之出现该族，不是一份写死的清单。"""
    service_family_registry.register(
        "subtitleserver", "字幕服务器", owner="DemoPlugin@default",
        distribution=ExtensionDistribution.MARKET,
    )
    declaration = ServiceInstanceDeclaration(
        capability="不存在", type="demo_type", name="演示类型", impl=_DemoServiceClient
    )

    violation = service_instance_declaration_violation(declaration)

    assert violation is not None
    assert "'subtitleserver'" in violation
    assert service_family_registry.capabilities() == (
        "auth", "downloader", "mediaserver", "notification", "storage", "subtitleserver",
    )


def test_registered_family_is_accepted_by_contract_and_endpoint(
    isolated_registry: None,
) -> None:
    """登记一族后，该族的声明通过契约校验，配置界面端点也不再判为请求出错。"""
    with pytest.raises(HTTPException) as exc_info:
        service_config_form_endpoint("subtitleserver", "demo_type", None)
    assert exc_info.value.status_code == 404

    service_family_registry.register(
        "subtitleserver", "字幕服务器", owner="DemoPlugin@default",
        distribution=ExtensionDistribution.MARKET,
    )
    declaration = ServiceInstanceDeclaration(
        capability="subtitleserver", type="demo_type", name="演示类型",
        impl=_DemoServiceClient,
    )

    assert service_instance_declaration_violation(declaration) is None
    assert service_config_form_endpoint("subtitleserver", "demo_type", None) == {
        "available": False, "name": None, "multi_instance": True, "conf": None,
        "model": None, "component": None, "remote": None, "config_schema": None,
    }


def test_listing_order_is_independent_of_registration_order() -> None:
    """登记先后不影响列举顺序：两张按不同顺序登记的表交出同一个结果。"""
    forward = ServiceFamilyRegistry()
    backward = ServiceFamilyRegistry()
    families = (("zeta", "最后一族"), ("alpha", "第一族"), ("mediaserver", "媒体服务器"))
    for capability, name in families:
        forward.register(capability, name)
    for capability, name in reversed(families):
        backward.register(capability, name)

    assert forward.capabilities() == backward.capabilities()
    assert forward.capabilities() == ("alpha", "mediaserver", "zeta")
    assert [entry.name for entry in forward.entries()] == [
        entry.name for entry in backward.entries()
    ]


def test_builtin_seeding_is_reproducible_on_a_fresh_registry() -> None:
    """新建的登记表按同一份内建族登记，得到与全局表相同的族集合。"""
    registry = ServiceFamilyRegistry()
    register_builtin_service_families(registry)

    assert registry.capabilities() == service_family_registry.capabilities()


def test_repeated_registration_keeps_the_latest_and_never_duplicates() -> None:
    """同一标签重复登记以最新一次为准，标签不会因重复登记出现两次。"""
    registry = ServiceFamilyRegistry()
    registry.register("subtitleserver", "字幕服务器")
    registry.register(
        "subtitleserver", "字幕服务", owner="DemoPlugin@default",
        distribution=ExtensionDistribution.MARKET,
    )

    assert registry.capabilities() == ("subtitleserver",)
    entry = registry.find("subtitleserver")
    assert entry.name == "字幕服务"
    assert entry.owner == "DemoPlugin@default"
    assert entry.distribution is ExtensionDistribution.MARKET


def test_registration_requires_a_non_empty_capability() -> None:
    """标签不是非空字符串的登记被拒，不留一条查不到也删不掉的空登记。"""
    registry = ServiceFamilyRegistry()

    assert registry.register("", "无名族") is None
    assert registry.register("   ", "无名族") is None
    assert registry.register(None, "无名族") is None
    assert registry.capabilities() == ()


def test_registration_falls_back_to_the_capability_as_display_name() -> None:
    """未给展示名称时取能力标签，登记项不会出现空名称。"""
    registry = ServiceFamilyRegistry()
    registry.register("subtitleserver")
    registry.register("mediaserver", "   ")

    assert registry.find("subtitleserver").name == "subtitleserver"
    assert registry.find("mediaserver").name == "mediaserver"


def test_unregister_owner_reclaims_only_its_own_families() -> None:
    """按登记方回收只摘走仍归属它的族，内建族与其它登记方不受影响。"""
    registry = ServiceFamilyRegistry()
    register_builtin_service_families(registry)
    registry.register("subtitleserver", "字幕服务器", owner="DemoPlugin@default")
    registry.register("bookserver", "书库服务器", owner="DemoPlugin@default")
    registry.register("photoserver", "相册服务器", owner="OtherPlugin@default")

    assert registry.unregister_owner("DemoPlugin@default") == (
        "bookserver", "subtitleserver",
    )
    assert registry.capabilities() == (
        "auth", "downloader", "mediaserver", "notification", "photoserver", "storage",
    )
    assert registry.unregister_owner("DemoPlugin@default") == ()


def test_unregister_owner_skips_families_taken_over_by_a_later_registration() -> None:
    """族被更晚的登记接管后，原登记方回收自己那一份不会连带把接管方踢掉。"""
    registry = ServiceFamilyRegistry()
    registry.register("subtitleserver", "字幕服务器", owner="DemoPlugin@default")
    registry.register("subtitleserver", "字幕服务器", owner="OtherPlugin@default")

    assert registry.unregister_owner("DemoPlugin@default") == ()
    assert registry.find("subtitleserver").owner == "OtherPlugin@default"


def test_lookup_misses_for_blank_and_non_string_capabilities() -> None:
    """标签为空或不是登记过的字符串时查不到登记，不因入参形状而抛错。"""
    registry = ServiceFamilyRegistry()
    register_builtin_service_families(registry)

    assert registry.find(None) is None
    assert registry.find(ModuleType.Downloader) is None
    assert registry.is_registered("") is False
    assert registry.is_registered(ModuleType.Downloader.value) is True
