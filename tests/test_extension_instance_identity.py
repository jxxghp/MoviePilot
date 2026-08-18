"""扩展实例标识与运行态定位的测试。"""

import pytest

from app.runtime.extensions.instance import (
    DEFAULT_INSTANCE_ID,
    extension_id_of,
    instance_key,
    is_default_instance_key,
    matches_extension,
    normalize_instance_id,
    resolve_running_instance,
    split_instance_key,
)


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
        "living room",
        "living/room",
        "living\\room",
        "..",
        "living.room",
        "../../etc",
        "/absolute",
        "实例",
        "home@work",
    ],
)
def test_illegal_instance_id_is_rejected(instance_id: str) -> None:
    """路径分隔符、点号与分隔符等非法字符不被接受。"""
    with pytest.raises(ValueError):
        normalize_instance_id(instance_id)
    with pytest.raises(ValueError):
        instance_key("Emby", instance_id)


def test_instance_id_length_boundary() -> None:
    """实例标识最长 64 字符，超长被拒。"""
    longest = "a" * 64

    assert normalize_instance_id(longest) == longest
    assert instance_key("Emby", longest) == f"Emby@{longest}"
    with pytest.raises(ValueError):
        normalize_instance_id("a" * 65)
    with pytest.raises(ValueError):
        instance_key("Emby", "a" * 65)


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


def test_resolve_running_instance_hits_exact_key() -> None:
    """运行态表中按实例键精确定位。"""
    running = {"Emby": "default-instance", "Emby@livingroom": "livingroom-instance"}

    assert resolve_running_instance(running, "Emby") == "default-instance"
    assert resolve_running_instance(running, "Emby@livingroom") == "livingroom-instance"
    assert resolve_running_instance(running, "Jellyfin") is None


def test_resolve_running_instance_falls_back_to_sole_instance() -> None:
    """扩展标识只对应一个在运行的实例时回落到该实例。"""
    running = {"Emby@livingroom": "livingroom-instance"}

    assert resolve_running_instance(running, "Emby") == "livingroom-instance"


def test_resolve_running_instance_does_not_guess_among_several() -> None:
    """扩展标识对应多个在运行的实例时不回落。"""
    running = {
        "Emby@livingroom": "livingroom-instance",
        "Emby@bedroom": "bedroom-instance",
    }

    assert resolve_running_instance(running, "Emby") is None
