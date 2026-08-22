"""扩展实例标识与运行态定位的测试。"""

import pytest

from app.runtime.extensions.contract.instance import (
    DEFAULT_INSTANCE_ID,
    describe_instance_candidates,
    extension_id_of,
    instance_key,
    is_default_instance_key,
    matches_extension,
    normalize_instance_id,
    split_instance_key,
)
from app.runtime.extensions.registry.plugin import PluginRegistry


def test_default_instance_key_degrades_to_bare_extension_id() -> None:
    """默认实例的实例键与不区分实例时取值一致。"""
    assert instance_key("Emby") == "Emby"
    assert instance_key("Emby", None) == "Emby"
    assert instance_key("Emby", "") == "Emby"
    assert instance_key("Emby", DEFAULT_INSTANCE_ID) == "Emby"


def test_bare_extension_id_resolves_to_default_instance() -> None:
    """裸扩展标识反解为默认实例。"""
    assert split_instance_key("Emby") == ("Emby", DEFAULT_INSTANCE_ID)
    assert extension_id_of("Emby") == "Emby"
    assert is_default_instance_key("Emby") is True
    assert is_default_instance_key(instance_key("Emby", DEFAULT_INSTANCE_ID)) is True


def test_named_instance_key_combines_and_splits() -> None:
    """非默认实例的实例键按分隔符组合并可无损反解。"""
    key = instance_key("Emby", "livingroom")

    assert key == "Emby@livingroom"
    assert split_instance_key(key) == ("Emby", "livingroom")
    assert extension_id_of(key) == "Emby"
    assert is_default_instance_key(key) is False


def test_key_without_instance_id_is_kept_whole() -> None:
    """缺少实例标识的键整体视为扩展标识的默认实例，不截断为另一个扩展。"""
    assert split_instance_key("Emby@") == ("Emby@", DEFAULT_INSTANCE_ID)
    assert is_default_instance_key("Emby@") is True
    assert matches_extension("Emby@", "Emby") is False


@pytest.mark.parametrize(
    "instance_id",
    [
        "home@work",
        "@",
        "客厅@卧室",
    ],
)
def test_instance_id_containing_separator_is_rejected(instance_id: str) -> None:
    """含实例键分隔符的实例标识不被接受，否则实例键无法无损反解。"""
    with pytest.raises(ValueError):
        normalize_instance_id(instance_id)
    with pytest.raises(ValueError):
        instance_key("Emby", instance_id)


@pytest.mark.parametrize(
    "instance_id",
    [
        "客厅 Emby",
        "living room",
        "living/room",
        "living\\room",
        "..",
        "living.room",
        "/absolute",
        "实例",
        "a" * 200,
    ],
)
def test_service_name_is_usable_as_instance_id(instance_id: str) -> None:
    """服务配置里用户自填的名称可直接作为实例标识，并可无损反解。"""
    key = instance_key("Emby", instance_id)

    assert key == f"Emby@{instance_id}"
    assert normalize_instance_id(instance_id) == instance_id
    assert split_instance_key(key) == ("Emby", instance_id)
    assert extension_id_of(key) == "Emby"
    assert is_default_instance_key(key) is False
    assert matches_extension(key, "Emby") is True


def test_matches_extension_selects_all_instances_of_one_extension() -> None:
    """按扩展标识筛选命中该扩展的全部实例。"""
    keys = ["Emby", "Emby@livingroom", "Jellyfin@bedroom"]

    assert [key for key in keys if matches_extension(key, "Emby")] == [
        "Emby",
        "Emby@livingroom",
    ]
    assert [key for key in keys if matches_extension(key, "Emby@livingroom")] == [
        "Emby@livingroom",
    ]
    assert [key for key in keys if matches_extension(key, None)] == keys


def test_registry_instance_hits_exact_key() -> None:
    """运行态表中按实例键精确定位。"""
    registry = PluginRegistry()
    registry.running.update(
        {"Emby": "default-instance", "Emby@livingroom": "livingroom-instance"}
    )

    assert registry.instance("Emby") == "default-instance"
    assert registry.instance("Emby@livingroom") == "livingroom-instance"
    assert registry.instance("Jellyfin") is None


def test_registry_instance_does_not_substitute_for_absent_default() -> None:
    """没有默认实例时不拿其余实例顶替，哪怕只有一个在运行。"""
    registry = PluginRegistry()
    registry.running["Emby@livingroom"] = "livingroom-instance"

    assert registry.instance("Emby") is None
    assert registry.instance("Emby@livingroom") == "livingroom-instance"


def test_registry_any_instance_takes_whichever_is_running() -> None:
    """读类级属性时取任一运行实例，与调用目标裁决无关。"""
    registry = PluginRegistry()
    registry.running.update(
        {"Emby@livingroom": "livingroom-instance", "Emby@bedroom": "bedroom-instance"}
    )

    assert registry.any_instance("Emby") == "livingroom-instance"
    assert registry.any_instance("Jellyfin") is None


def test_describe_instance_candidates_marks_enabled_state() -> None:
    """候选描述按给定顺序列出名称并标注启用态。"""
    assert describe_instance_candidates(()) == "无"
    assert describe_instance_candidates(
        [("default", True), ("alt", False)]
    ) == "default（已启用）、alt（已停用）"
