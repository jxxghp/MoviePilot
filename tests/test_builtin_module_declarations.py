"""内建模块在清单里声明自身身份：这个类型能配几份、这个模块服务哪些媒体来源。

两项都是模块自己的事实：宿主推不出来，此前一项靠缺省推定、一项靠端点里的硬编码表。
本文件盯住声明的形状、往返、校验拒绝面，以及两个取用点——配置界面端点据此决定要不要
给出「新增第二份」的入口，服务实例扇出据此裁剪配置。

缺省即今天的行为：不写这两个字段的模块，行为与该字段出现之前完全一致。
"""

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest
import tomllib

import app.api.endpoints.media as media_endpoint
from app.api.endpoints.service import config_form as service_config_form_endpoint
from app.modules import _DownloaderBase
from app.runtime.capabilities.registry import CapabilityRegistry
from app.runtime.extensions.lifecycle import host_module_adapter
from app.runtime.extensions.projection.module_declarations import (
    build_declaration_index,
    builtin_media_sources,
    builtin_module_media_sources,
    builtin_multi_instance,
    declaration_index,
    reset_declaration_index,
)
from app.runtime.extensions.service_config import (
    configure_service_instance_config_reader,
    service_config_key,
)
from app.schemas.service import ServiceConfigForm
from app.schemas.types import MediaSource


def _write_manifest(module_root: Path, package: str, body: str) -> Path:
    """在合成模块根下写入一个一级模块包及其声明。

    :param module_root: 合成模块根目录
    :param package: 一级模块包名
    :param body: capability.toml 正文
    :return: 声明文件路径
    """
    package_dir = module_root / package
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    manifest = package_dir / "capability.toml"
    manifest.write_text(body.strip() + "\n", encoding="utf-8")
    return manifest


def _service_module_manifest(
    package: str,
    *,
    service_capability: Optional[str] = "downloader",
    subtype: Optional[str] = None,
    service_instance: Optional[str] = None,
    media_sources: Optional[str] = None,
    watch: Optional[str] = None,
    selector_type: Optional[str] = None,
) -> str:
    """拼出一个用于校验用例的最小模块声明。

    :param package: 一级模块包名
    :param service_capability: metadata.service_capability，为 None 时不声明
    :param subtype: metadata.subtype，为 None 时不声明
    :param service_instance: metadata.service_instance 表的正文，为 None 时不声明
    :param media_sources: metadata.media_sources 的取值文本，为 None 时不声明
    :param watch: activation.watch 的取值文本，为 None 时按服务族配置键推导
    :param selector_type: 声明 when_configured selector 时的 match_value
    :return: capability.toml 正文
    """
    config_key = service_config_key(service_capability) if service_capability else None
    if watch is None:
        watch = f'["{config_key.value}"]' if config_key else "[]"
    lines = [
        "schema_version = 1",
        f'id = "{package.title()}Module"',
        'kind = "host_module"',
        f'entrypoint = "app.modules.{package}:{package.title()}Module"',
        "depends_on = []",
        "",
        "[metadata]",
        f'name = "{package.title()}"',
    ]
    if service_capability:
        lines.append(f'service_capability = "{service_capability}"')
    if subtype:
        lines.append(f'subtype = "{subtype}"')
    if media_sources:
        lines.append(f"media_sources = {media_sources}")
    lines.append("priority = 1")
    if service_instance is not None:
        lines.extend(["", "[metadata.service_instance]", service_instance.strip()])
    lines.extend(["", "[activation]"])
    if selector_type:
        lines.extend([
            'policy = "when_configured"',
            f"watch = {watch}",
            "",
            "[activation.selector]",
            'kind = "system_config_item"',
            f'key = "{config_key.value}"',
            'match_field = "type"',
            f'match_value = "{selector_type}"',
            'enabled_field = "enabled"',
        ])
    else:
        lines.extend(['policy = "bootstrap"', f"watch = {watch}"])
    return "\n".join(lines)


@pytest.fixture
def module_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把清单发现根指向一个合成模块根，并在用例结束后丢弃已建索引。"""
    root = tmp_path / "modules"
    root.mkdir()
    monkeypatch.setattr(host_module_adapter, "_MODULE_ROOT", root)
    reset_declaration_index()
    try:
        yield root
    finally:
        reset_declaration_index()


@pytest.fixture
def downloader_configs() -> Iterator[List[dict]]:
    """接管服务配置读取端口，用例改写列表即改写用户配置。"""
    configs: List[dict] = []
    previous = configure_service_instance_config_reader(
        lambda capability: configs if capability == "downloader" else None
    )
    try:
        yield configs
    finally:
        configure_service_instance_config_reader(previous)


def _downloader_config(name: str, default: bool = False) -> dict:
    """构造一条 demo 类型的下载器配置。

    :param name: 实例名
    :param default: 是否为本族默认调用目标
    :return: 与持久化形状一致的配置字典
    """
    return {
        "name": name,
        "type": "demo",
        "enabled": True,
        "default": default,
        "config": {"host": "127.0.0.1"},
    }


class _DemoDownloaderModule(_DownloaderBase):
    """只用来驱动 get_configs() 的下载器模块桩，不构造任何客户端。"""

    def __init__(self) -> None:
        """绑定固定的类型标识。"""
        super().__init__()
        self._service_name = "demo"


# ---------------------------------------------------------------------------
# 真实清单：逐类型的声明取值
# ---------------------------------------------------------------------------


def test_local_storage_is_the_only_builtin_single_instance_type() -> None:
    """本地存储是内建侧唯一一个只认一份配置的类型，其余类型的第二份指的是另一个实体。"""
    index = declaration_index()

    single = sorted(
        coordinate
        for coordinate, multi_instance in index.multiplicity.items()
        if not multi_instance
    )

    assert single == [("storage", "local")]
    assert index.multiplicity[("storage", "u115")] is True
    assert index.multiplicity[("downloader", "qbittorrent")] is True
    assert index.multiplicity[("mediaserver", "emby")] is True
    assert index.multiplicity[("notification", "telegram")] is True


def test_every_builtin_service_type_declares_its_own_count() -> None:
    """四族的内建类型逐个声明能配几份，不再由缺省推定。"""
    families = {capability for capability, _type in declaration_index().multiplicity}

    assert families == {"downloader", "mediaserver", "notification", "storage"}


def test_undeclared_type_answers_nothing_and_callers_keep_today_behaviour() -> None:
    """没有内建模块承载的类型问不出结论，取用方据此按多实例处置。"""
    assert builtin_multi_instance("downloader", "not_a_builtin_type") is None
    assert builtin_multi_instance("storage", "") is None
    assert builtin_multi_instance("", "local") is None


# ---------------------------------------------------------------------------
# 真实清单：媒体来源声明
# ---------------------------------------------------------------------------


def test_one_module_can_serve_several_media_sources() -> None:
    """一个模块可以服务多个来源，豆瓣模块声明的两个来源都在场。"""
    assert builtin_module_media_sources()["DoubanModule"] == ("douban", "doubanmusic")


def test_modules_that_serve_no_media_source_declare_none() -> None:
    """不服务任何来源的模块不出现在来源声明里。"""
    declared = builtin_module_media_sources()

    assert "QbittorrentModule" not in declared
    assert "LocalStorageModule" not in declared
    assert declared["TheMovieDbModule"] == ("themoviedb",)


def test_declared_media_sources_all_appear_in_the_builtin_source_table() -> None:
    """声明服务某个来源的模块，其来源必须在来源表里有一行，否则用户选不到它。"""
    listed = {
        str(source.media_source) for source in media_endpoint._BUILTIN_MEDIA_SOURCES
    }

    assert set(builtin_media_sources()) <= listed


def test_placeholder_sources_are_declared_by_no_builtin_module() -> None:
    """内建侧没有实现的占位来源不该被任何模块认领，实现由插件补上。"""
    declared = set(builtin_media_sources())

    for placeholder in (
        MediaSource.Bilibili,
        MediaSource.MangoTV,
        MediaSource.MiguVideo,
        MediaSource.TencentVideo,
        MediaSource.Iqiyi,
    ):
        assert str(placeholder) not in declared


def test_media_source_declaration_does_not_change_the_source_list() -> None:
    """来源列表仍由来源表给出，补上声明不改变它交出的行数与顺序。"""

    class _Manager:
        """不交出任何插件来源的插件管理器替身。"""

        @staticmethod
        def get_media_sources():
            """交出空的插件来源列表。"""
            return []

    import app.application.plugin.runtime as plugin_runtime

    original = plugin_runtime.get_plugin_manager
    plugin_runtime.get_plugin_manager = lambda: _Manager()
    try:
        sources = media_endpoint._registered_media_sources()
    finally:
        plugin_runtime.get_plugin_manager = original

    assert [str(item.media_source) for item in sources] == [
        str(item.media_source) for item in media_endpoint._BUILTIN_MEDIA_SOURCES
    ]


# ---------------------------------------------------------------------------
# 索引：与登记先后无关
# ---------------------------------------------------------------------------


def test_index_is_independent_of_the_order_specs_arrive_in() -> None:
    """索引内容只由声明本身决定，打乱声明顺序得到同一份索引。"""
    specs = list(host_module_adapter.build_host_module_registry().list_specs())

    forward = build_declaration_index(specs)
    backward = build_declaration_index(list(reversed(specs)))

    assert dict(forward.multiplicity) == dict(backward.multiplicity)
    assert dict(forward.media_sources) == dict(backward.media_sources)
    assert forward.all_media_sources() == backward.all_media_sources()


def test_media_source_listing_is_sorted_not_declaration_ordered() -> None:
    """全量来源列举按标识升序，与哪个模块先被发现无关。"""
    listed = builtin_media_sources()

    assert list(listed) == sorted(listed)


# ---------------------------------------------------------------------------
# 声明形状：TOML 往返
# ---------------------------------------------------------------------------


def test_declaration_fields_round_trip_through_toml(module_root: Path) -> None:
    """新增字段是纯数据：TOML 与 JSON 都能原样往返，不含只在进程内成立的形状。"""
    body = _service_module_manifest(
        "roundtrip",
        service_capability=None,
        media_sources='["douban", "doubanmusic"]',
        service_instance=(
            'capability = "storage"\ntype = "roundtrip"\nmulti_instance = false'
        ),
        watch='["Storages"]',
    )
    manifest = _write_manifest(module_root, "roundtrip", body)

    raw = tomllib.loads(manifest.read_text(encoding="utf-8"))
    spec = host_module_adapter.build_host_module_registry().list_specs()[0]

    assert raw["metadata"]["media_sources"] == ["douban", "doubanmusic"]
    assert raw["metadata"]["service_instance"] == {
        "capability": "storage",
        "type": "roundtrip",
        "multi_instance": False,
    }
    assert json.loads(json.dumps(raw["metadata"])) == raw["metadata"]
    assert host_module_adapter.declared_media_sources(spec) == ("douban", "doubanmusic")
    assert host_module_adapter.service_instance_declaration(spec) == (
        "storage",
        "roundtrip",
        False,
    )


def test_declared_coordinate_falls_back_to_facts_already_in_the_manifest(
    module_root: Path,
) -> None:
    """能力标签与类型标识可省略，省略时取同一份清单里已写下的同一个事实。"""
    _write_manifest(
        module_root,
        "inherited",
        _service_module_manifest(
            "inherited",
            service_capability="notification",
            subtype="Inherited",
            service_instance="multi_instance = true",
            selector_type="inherited",
        ),
    )

    spec = host_module_adapter.build_host_module_registry().list_specs()[0]

    assert host_module_adapter.service_instance_declaration(spec) == (
        "notification",
        "inherited",
        True,
    )


def test_multi_instance_defaults_to_many(module_root: Path) -> None:
    """只写坐标不写份数时按多实例处置，与该字段出现之前一致。"""
    _write_manifest(
        module_root,
        "defaulted",
        _service_module_manifest(
            "defaulted",
            service_capability="downloader",
            service_instance='type = "defaulted"',
        ),
    )

    spec = host_module_adapter.build_host_module_registry().list_specs()[0]

    assert host_module_adapter.service_instance_declaration(spec) == (
        "downloader",
        "defaulted",
        True,
    )


def test_module_without_the_new_fields_declares_nothing(module_root: Path) -> None:
    """不写新字段的模块交出空声明，索引里没有它的任何一行。"""
    _write_manifest(
        module_root,
        "silent",
        _service_module_manifest("silent", service_capability="downloader"),
    )

    index = build_declaration_index(
        host_module_adapter.build_host_module_registry().list_specs()
    )

    assert dict(index.multiplicity) == {}
    assert dict(index.media_sources) == {}


# ---------------------------------------------------------------------------
# 声明形状：拒绝面
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "service_instance,match",
    [
        ('type = "x"\nmulti_instance = "yes"', "multi_instance"),
        ('type = "x"\nunknown = 1', "未知字段"),
        ('capability = "storage"\ntype = "x"', "不一致"),
        ("multi_instance = true", "type"),
    ],
    ids=["not_bool", "unknown_key", "capability_conflict", "no_type"],
)
def test_manifest_rejects_malformed_service_instance_declaration(
    module_root: Path, service_instance: str, match: str
) -> None:
    """服务实例声明形状不合规时清单构建即失败，不留到取用时才发现。"""
    _write_manifest(
        module_root,
        "broken",
        _service_module_manifest(
            "broken",
            service_capability="downloader",
            service_instance=service_instance,
        ),
    )

    with pytest.raises(ValueError, match=match):
        host_module_adapter.build_host_module_registry()


def test_manifest_rejects_service_type_conflicting_with_the_selector(
    module_root: Path,
) -> None:
    """类型标识与 selector 的 match_value 是同一个事实，写出两个取值一律拒绝。"""
    _write_manifest(
        module_root,
        "conflicting",
        _service_module_manifest(
            "conflicting",
            service_capability="notification",
            subtype="Conflicting",
            service_instance='type = "other"\nmulti_instance = true',
            selector_type="conflicting",
        ),
    )

    with pytest.raises(ValueError, match="match_value"):
        host_module_adapter.build_host_module_registry()


def test_manifest_requires_the_family_config_key_to_be_watched(
    module_root: Path,
) -> None:
    """声明了服务实例类型的模块必须监听该族配置键，配置变更才能重建实例。"""
    _write_manifest(
        module_root,
        "unwatched",
        _service_module_manifest(
            "unwatched",
            service_capability=None,
            service_instance='capability = "storage"\ntype = "unwatched"',
            watch="[]",
        ),
    )

    with pytest.raises(ValueError, match="activation.watch"):
        host_module_adapter.build_host_module_registry()


def test_manifest_rejects_two_modules_claiming_one_service_type(
    module_root: Path,
) -> None:
    """同一个服务实例类型只能由一个模块承载，否则取哪一个只能靠遍历先后决定。"""
    for package in ("firstowner", "secondowner"):
        _write_manifest(
            module_root,
            package,
            _service_module_manifest(
                package,
                service_capability="downloader",
                service_instance='type = "contested"\nmulti_instance = true',
            ),
        )

    with pytest.raises(ValueError, match="已由"):
        host_module_adapter.build_host_module_registry()


@pytest.mark.parametrize(
    "media_sources,match",
    [
        ('"douban"', "字符串数组"),
        ('["douban", "douban"]', "重复"),
        ('["not a source"]', "非法来源标识"),
        ('[""]', "非空字符串"),
    ],
    ids=["not_array", "duplicated", "invalid_identifier", "blank"],
)
def test_manifest_rejects_malformed_media_source_declaration(
    module_root: Path, media_sources: str, match: str
) -> None:
    """媒体来源声明形状不合规时清单构建即失败。"""
    _write_manifest(
        module_root,
        "badsource",
        _service_module_manifest(
            "badsource", service_capability=None, media_sources=media_sources
        ),
    )

    with pytest.raises(ValueError, match=match):
        host_module_adapter.build_host_module_registry()


def test_manifest_rejects_two_modules_serving_one_media_source(
    module_root: Path,
) -> None:
    """一个媒体来源至多由一个内建模块服务，重复认领在清单校验时即被拒。"""
    for package in ("firstsource", "secondsource"):
        _write_manifest(
            module_root,
            package,
            _service_module_manifest(
                package, service_capability=None, media_sources='["bangumi"]'
            ),
        )

    with pytest.raises(ValueError, match="已由"):
        host_module_adapter.build_host_module_registry()


# ---------------------------------------------------------------------------
# 取用一：配置界面端点决定要不要给出「新增第二份」的入口
# ---------------------------------------------------------------------------


def test_endpoint_hides_the_second_slot_for_a_single_instance_builtin_type() -> None:
    """声明为单实例的内建类型不给出新增第二份的入口。"""
    answer: Dict[str, Any] = service_config_form_endpoint("storage", "local")

    assert answer["multi_instance"] is False
    assert answer["available"] is False


@pytest.mark.parametrize(
    "capability,service_type",
    [
        ("storage", "u115"),
        ("downloader", "qbittorrent"),
        ("notification", "telegram"),
        ("mediaserver", "emby"),
        ("downloader", "not_a_builtin_type"),
    ],
)
def test_endpoint_keeps_the_second_slot_for_everything_else(
    capability: str, service_type: str
) -> None:
    """声明为多实例与压根没声明的类型都照常给出新增第二份的入口。"""
    assert service_config_form_endpoint(capability, service_type)["multi_instance"] is True


def test_single_instance_answer_survives_the_response_model() -> None:
    """response_model 不会把 False 静默丢成缺省的 True。"""
    answer = service_config_form_endpoint("storage", "local")

    assert ServiceConfigForm.model_validate(answer).multi_instance is False


# ---------------------------------------------------------------------------
# 取用二：服务实例扇出按声明裁剪配置
# ---------------------------------------------------------------------------


def test_declared_single_instance_type_keeps_only_its_default_target(
    module_root: Path, downloader_configs: List[dict]
) -> None:
    """清单声明为单实例的类型被配了多份时按默认调用目标裁出唯一那一份。"""
    _write_manifest(
        module_root,
        "demo",
        _service_module_manifest(
            "demo",
            service_capability="downloader",
            service_instance='type = "demo"\nmulti_instance = false',
        ),
    )
    downloader_configs.extend([
        _downloader_config("主机", default=True),
        _downloader_config("备机"),
    ])

    assert list(_DemoDownloaderModule().get_configs()) == ["主机"]


def test_undeclared_type_still_fans_out_every_config(
    module_root: Path, downloader_configs: List[dict]
) -> None:
    """不写该字段的类型照旧逐条扇出，行为与该字段出现之前逐字节一致。"""
    _write_manifest(
        module_root,
        "demo",
        _service_module_manifest("demo", service_capability="downloader"),
    )
    downloader_configs.extend([
        _downloader_config("主机", default=True),
        _downloader_config("备机"),
    ])

    assert sorted(_DemoDownloaderModule().get_configs()) == ["主机", "备机"]


def test_declaration_index_survives_an_unreadable_manifest_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """清单读不出来时索引为空而不是抛错，取用方按缺省处置。"""
    monkeypatch.setattr(host_module_adapter, "_MODULE_ROOT", tmp_path / "missing")
    reset_declaration_index()
    try:
        assert builtin_multi_instance("storage", "local") is None
    finally:
        reset_declaration_index()


def test_index_follows_the_manifest_root_it_describes(
    module_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """索引跟着发现根走：换了根即重建，上一份根的结论不会被当作这一份的答案。"""
    _write_manifest(
        module_root,
        "scoped",
        _service_module_manifest(
            "scoped",
            service_capability="downloader",
            service_instance='type = "scoped"\nmulti_instance = false',
        ),
    )
    assert builtin_multi_instance("downloader", "scoped") is False

    other_root = tmp_path / "other-modules"
    other_root.mkdir()
    _write_manifest(
        other_root,
        "scoped",
        _service_module_manifest("scoped", service_capability="downloader"),
    )
    monkeypatch.setattr(host_module_adapter, "_MODULE_ROOT", other_root)

    assert builtin_multi_instance("downloader", "scoped") is None


def test_registry_discovery_is_not_a_python_import(module_root: Path) -> None:
    """索引只读磁盘上的声明，不导入模块，因此模块没起来也答得出。"""
    _write_manifest(
        module_root,
        "neverimported",
        _service_module_manifest(
            "neverimported",
            service_capability="downloader",
            service_instance='type = "neverimported"\nmulti_instance = false',
        ),
    )

    assert isinstance(host_module_adapter.build_host_module_registry(), CapabilityRegistry)
    assert builtin_multi_instance("downloader", "neverimported") is False
