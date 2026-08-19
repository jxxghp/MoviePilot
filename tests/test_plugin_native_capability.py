"""插件模块以 plugin_module kind 参与能力发现与运行的端到端验证。

以 app/modules/rclone 为参照样本，把「存储模块」复制成插件形态（capability.toml
声明 kind = plugin_module），验证插件能否达到内置模块同级的原生扩展能力：能力
清单被发现、entrypoint 可物化、内置模块不受影响、扩展声明根注入的容错、插件
模块在 ModuleManager 单例里进入运行态并参与能力索引，以及坏插件清单是否会
连累其余模块装载。

测试在临时目录内自建插件包并把该目录接入 app.plugins 命名空间包的搜索路径最
前面，不依赖工作区内未提交的 app/plugins/rclonestorageplugin 副本。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from app.foundation.singleton import Singleton
from app.modules._base import _StorageModuleBase
from app.modules._base.storage import storage_backend_registry
from app.modules.rclone.rclone import Rclone
from app.runtime.capabilities.errors import CapabilityManifestError
from app.runtime.capabilities.registry import CapabilityRegistry
from app.runtime.events import eventmanager
from app.runtime.extensions import module_manager as module_manager_extension
from app.runtime.extensions.host_module_adapter import (
    HOST_MODULE_KIND,
    HOST_MODULE_SELECTOR_SCHEMAS,
    PLUGIN_MODULE_KIND,
    HostModuleAdapter,
    build_host_module_registry,
)
from app.runtime.extensions.module_manager import (
    ModuleManager,
    configure_plugin_capability_roots,
)
from app.schemas.types import EventType

PLUGIN_ID = "RcloneStoragePlugin"
PLUGIN_DIR = PLUGIN_ID.lower()
PLUGIN_VERSION_DIR = "v1_0_0"
MODULE_CLASS_NAME = "RcloneStorageModule"
MODULE_ENTRYPOINT = f"app.plugins.{PLUGIN_DIR}.{PLUGIN_VERSION_DIR}:{MODULE_CLASS_NAME}"
STORAGE_IDENTITY = "rclone_plugin"
BROKEN_PLUGIN_DIR = "brokenstorageplugin"

_CAPABILITY_TOML = f"""
schema_version = 1
id = "{MODULE_CLASS_NAME}"
kind = "{PLUGIN_MODULE_KIND}"
entrypoint = "{MODULE_ENTRYPOINT}"
depends_on = []

[metadata]
name = "RcloneStorage"
subtype = "RcloneStorage"
priority = 5

[activation]
policy = "bootstrap"
watch = []
"""

# 与 app/plugins/rclonestorageplugin/v1_0_0/__init__.py 保持一致的插件源码，
# 内嵌为字符串以保证测试不依赖工作区内未提交的插件目录。
_PLUGIN_SOURCE = '''
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from app.modules._base import _StorageModuleBase
from app.modules._base.storage import StorageBase
from app.plugins import _PluginBase
from app.schemas.file import StorageUsage as _SchemaStorageUsage
from app.schemas.workflow import FileItem as _SchemaFileItem


class RclonePluginStorage(StorageBase):
    """插件版存储后端，以内存字典模拟远端文件树，不依赖外部 rclone 进程。"""

    schema = "rclone_plugin"
    transtype = {"copy": "复制"}

    def __init__(self) -> None:
        self._tree: Dict[str, Dict[str, str]] = {"/": {"type": "dir", "name": ""}}
        self._content: Dict[str, bytes] = {}

    def init_storage(self):
        pass

    @staticmethod
    def _normalize(path: Union[Path, str]) -> str:
        text = str(path or "/").replace("\\\\", "/")
        parts = [part for part in text.split("/") if part]
        return "/" + "/".join(parts) if parts else "/"

    def _make_item(self, path: str) -> Optional[_SchemaFileItem]:
        entry = self._tree.get(path)
        if entry is None:
            return None
        size = len(self._content.get(path, b"")) if entry["type"] == "file" else None
        return _SchemaFileItem(
            storage=self.schema, type=entry["type"], path=path,
            name=entry["name"], basename=entry["name"], size=size,
        )

    def check(self) -> bool:
        return True

    def list(self, fileitem: _SchemaFileItem) -> List[_SchemaFileItem]:
        if fileitem.type == "file":
            return [fileitem]
        parent = self._normalize(fileitem.path)
        prefix = parent if parent == "/" else f"{parent}/"
        items = []
        for path in sorted(self._tree):
            if path == parent or not path.startswith(prefix):
                continue
            remainder = path[len(prefix):]
            if "/" in remainder:
                continue
            items.append(self._make_item(path))
        return items

    def create_folder(self, fileitem: _SchemaFileItem, name: str) -> Optional[_SchemaFileItem]:
        path = self._normalize(f"{self._normalize(fileitem.path)}/{name}")
        self._tree[path] = {"type": "dir", "name": name}
        return self._make_item(path)

    def get_folder(self, path: Path) -> Optional[_SchemaFileItem]:
        normalized = Path(self._normalize(path))
        existing = self.get_item(normalized)
        if existing:
            return existing
        fileitem = _SchemaFileItem(storage=self.schema, type="dir", path="/")
        for part in normalized.parts[1:]:
            current_path = Path(self._normalize(Path(fileitem.path) / part))
            entry = self.get_item(current_path)
            if not entry:
                entry = self.create_folder(fileitem, part)
            if not entry:
                return None
            fileitem = entry
        return fileitem

    def get_item(self, path: Path) -> Optional[_SchemaFileItem]:
        return self._make_item(self._normalize(path))

    def delete(self, fileitem: _SchemaFileItem) -> bool:
        path = self._normalize(fileitem.path)
        if path not in self._tree:
            return False
        del self._tree[path]
        self._content.pop(path, None)
        return True

    def rename(self, fileitem: _SchemaFileItem, name: str) -> bool:
        path = self._normalize(fileitem.path)
        entry = self._tree.get(path)
        if entry is None:
            return False
        new_path = self._normalize(f"{Path(path).parent.as_posix()}/{name}")
        self._tree[new_path] = {"type": entry["type"], "name": name}
        del self._tree[path]
        if path in self._content:
            self._content[new_path] = self._content.pop(path)
        return True

    def download(self, fileitem: _SchemaFileItem, path: Path = None) -> Optional[Path]:
        source = self._normalize(fileitem.path)
        if source not in self._tree:
            return None
        local_path = self._build_download_path(fileitem, path)
        if not local_path:
            return None
        local_path.write_bytes(self._content.get(source, b""))
        return local_path

    def upload(self, fileitem: _SchemaFileItem, path: Path,
               new_name: Optional[str] = None) -> Optional[_SchemaFileItem]:
        name = new_name or path.name
        target = self._normalize(f"{self._normalize(fileitem.path)}/{name}")
        self._tree[target] = {"type": "file", "name": name}
        self._content[target] = path.read_bytes() if path.exists() else b""
        return self._make_item(target)

    def detail(self, fileitem: _SchemaFileItem) -> Optional[_SchemaFileItem]:
        return self._make_item(self._normalize(fileitem.path))

    def copy(self, fileitem: _SchemaFileItem, path: Path, new_name: str) -> bool:
        source = self._normalize(fileitem.path)
        entry = self._tree.get(source)
        if entry is None:
            return False
        target = self._normalize(f"{self._normalize(str(path))}/{new_name}")
        self._tree[target] = {"type": entry["type"], "name": new_name}
        if source in self._content:
            self._content[target] = self._content[source]
        return True

    def move(self, fileitem: _SchemaFileItem, path: Path, new_name: str) -> bool:
        if not self.copy(fileitem, path, new_name):
            return False
        return self.delete(fileitem)

    def link(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        return False

    def softlink(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        return False

    def usage(self) -> Optional[_SchemaStorageUsage]:
        return _SchemaStorageUsage(total=0.0, available=0.0)


class RcloneStorageModule(_StorageModuleBase):
    """插件承载的存储模块，声明存储后端并复用内置存储模块业务样板。"""

    storage_class = RclonePluginStorage

    @staticmethod
    def get_name() -> str:
        return "RcloneStorage"

    @staticmethod
    def get_priority() -> int:
        return 5


class RcloneStoragePlugin(_PluginBase):
    """插件主类，负责开关与配置页面；存储能力由 RcloneStorageModule 单独承载。"""

    plugin_name = "Rclone 存储扩展"
    plugin_desc = "验证插件能否提供原生级存储模块能力"
    plugin_version = "1.0.0"
    plugin_author = "MoviePilot"
    plugin_id = "RcloneStoragePlugin"
    plugin_order = 99

    def __init__(self, plugin_id: Optional[str] = None, instance_id: Optional[str] = None):
        super().__init__(plugin_id=plugin_id, instance_id=instance_id)
        self._enabled = False

    def init_plugin(self, config: dict = None):
        self._enabled = bool((config or {}).get("enabled"))

    def get_state(self) -> bool:
        return self._enabled

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        return None, {"enabled": False}

    def get_page(self) -> Optional[List[dict]]:
        return None

    def stop_service(self):
        pass
'''

_BROKEN_CAPABILITY_TOML = """
schema_version = 1
id = "BrokenStorageModule"
kind = "plugin_module"
entrypoint = "not a valid entrypoint"
depends_on = []

[metadata]
name = "BrokenStorage"

[activation]
policy = "bootstrap"
watch = []
"""

# 一个安全的合成宿主模块：bootstrap 即激活、init_module 不做任何 I/O，用于跟插件
# 模块一起验证 ModuleManager 单例的运行视图与能力索引，避免在测试里真的激活全部
# 44 个内置模块。
_SAFE_HOST_MODULE_SOURCE = """
class SafeHostModule:
    def __init__(self):
        self.started = False

    def init_module(self):
        self.started = True

    def stop(self):
        self.started = False

    def test(self):
        return True, "ok"

    def list_files(self, *args, **kwargs):
        return []

    @staticmethod
    def get_name():
        return "SafeHost"

    @staticmethod
    def get_priority():
        return 1
"""

_SAFE_HOST_CAPABILITY_TOML = """
schema_version = 1
id = "SafeHostModule"
kind = "host_module"
entrypoint = "fixture_safe_host_module:SafeHostModule"
depends_on = []

[metadata]
name = "SafeHost"
priority = 1

[activation]
policy = "bootstrap"
watch = []
"""


@pytest.fixture
def plugins_root(tmp_path: Path) -> Iterator[Path]:
    """把临时插件根目录接入 app.plugins 命名空间包搜索路径最前面。

    插到最前面而不是追加到末尾，使临时目录始终优先于工作区内可能存在的
    同名插件目录被解析到，保证测试自建的插件包不受工作区状态影响。
    """
    root = tmp_path / "app" / "plugins"
    root.mkdir(parents=True)
    package_path = importlib.import_module("app.plugins").__path__
    package_path.insert(0, str(root))
    importlib.invalidate_caches()
    yield root
    package_path.remove(str(root))
    for module_name in [name for name in sys.modules if name.startswith("app.plugins.")]:
        sys.modules.pop(module_name, None)
    importlib.invalidate_caches()


@pytest.fixture
def plugin_version_dir(plugins_root: Path) -> Path:
    """写入样本插件的版本目录：capability.toml 与插件源码。"""
    version_dir = plugins_root / PLUGIN_DIR / PLUGIN_VERSION_DIR
    version_dir.mkdir(parents=True)
    (version_dir / "__init__.py").write_text(_PLUGIN_SOURCE, encoding="utf-8")
    (version_dir / "capability.toml").write_text(_CAPABILITY_TOML, encoding="utf-8")
    importlib.invalidate_caches()
    return version_dir


@pytest.fixture
def broken_plugin_version_dir(plugins_root: Path) -> Path:
    """写入一个格式非法的插件版本目录，只含坏 capability.toml。"""
    version_dir = plugins_root / BROKEN_PLUGIN_DIR / PLUGIN_VERSION_DIR
    version_dir.mkdir(parents=True)
    (version_dir / "capability.toml").write_text(_BROKEN_CAPABILITY_TOML, encoding="utf-8")
    return version_dir


def _host_module_root() -> Path:
    """返回内置模块的物理发现根目录。"""
    return Path(importlib.import_module("app.modules").__path__[0])


@pytest.fixture(autouse=True)
def _reset_plugin_capability_roots() -> Iterator[None]:
    """每个用例结束后恢复扩展声明根提供者为默认的空实现。"""
    yield
    configure_plugin_capability_roots(None)


# 一、能力清单发现与 entrypoint 物化


def test_plugin_capability_toml_is_discovered_as_plugin_module_kind(plugin_version_dir):
    """插件版本目录下的 capability.toml 能被独立发现，kind 为 plugin_module。"""
    registry = CapabilityRegistry.discover(
        (plugin_version_dir,), kinds={PLUGIN_MODULE_KIND}, selector_schemas={},
    )
    specs = registry.list_specs()

    assert len(specs) == 1
    spec = specs[0]
    assert spec.id == MODULE_CLASS_NAME
    assert spec.kind == PLUGIN_MODULE_KIND
    assert spec.entrypoint == MODULE_ENTRYPOINT
    assert spec.metadata["subtype"] == "RcloneStorage"


def test_plugin_entrypoint_materializes_to_the_module_class(plugin_version_dir):
    """entrypoint 能通过与内置模块共用的 HostModuleAdapter 物化成真实类对象。"""
    registry = CapabilityRegistry.discover(
        (plugin_version_dir,), kinds={PLUGIN_MODULE_KIND}, selector_schemas={},
    )
    spec = registry.get_spec(MODULE_CLASS_NAME)

    implementation = HostModuleAdapter.materialize(spec)

    assert implementation.__name__ == MODULE_CLASS_NAME
    assert issubclass(implementation, _StorageModuleBase)
    assert implementation.storage_class.schema == STORAGE_IDENTITY
    # 存储标识与内置 Rclone 不同，避免在 storage_backend_registry 中互相覆盖
    assert implementation.storage_class.schema != Rclone.schema.value


# 二、内置模块不受插件根影响：直接用生产入口 build_host_module_registry


def test_build_host_module_registry_discovers_the_plugin_root_without_dropping_host_modules(
    plugin_version_dir,
):
    """生产入口 build_host_module_registry(extra_roots) 加入插件根后，宿主模块清单不变。"""
    host_only_specs = build_host_module_registry().list_specs()
    assert host_only_specs, "host_module 基线不应为空，用于跟联合发现比对"

    combined_specs = build_host_module_registry(
        extra_roots=(plugin_version_dir,)
    ).list_specs()
    combined_host_specs = [spec for spec in combined_specs if spec.kind == HOST_MODULE_KIND]
    combined_plugin_specs = [spec for spec in combined_specs if spec.kind == PLUGIN_MODULE_KIND]

    assert {spec.id for spec in combined_host_specs} == {spec.id for spec in host_only_specs}
    assert len(combined_host_specs) == len(host_only_specs)
    assert [spec.id for spec in combined_plugin_specs] == [MODULE_CLASS_NAME]


def test_build_host_module_registry_without_extra_roots_matches_host_only_discovery():
    """不传扩展根时，生产入口的结果与只扫描宿主模块包完全一致。"""
    baseline = CapabilityRegistry.discover(
        (_host_module_root(),),
        kinds={HOST_MODULE_KIND, PLUGIN_MODULE_KIND},
        selector_schemas=HOST_MODULE_SELECTOR_SCHEMAS,
    )

    assert {spec.id for spec in build_host_module_registry().list_specs()} == {
        spec.id for spec in baseline.list_specs()
    }


# 三、扩展声明根注入：configure_plugin_capability_roots 与容错


def test_configure_plugin_capability_roots_feeds_discover_plugin_capability_roots(
    plugin_version_dir,
):
    """注入的 provider 返回值经 _discover_plugin_capability_roots 原样传出。"""
    configure_plugin_capability_roots(lambda: (plugin_version_dir,))

    roots = ModuleManager._discover_plugin_capability_roots()

    assert roots == (plugin_version_dir,)


def test_discover_plugin_capability_roots_tolerates_a_throwing_provider():
    """provider 抛错时按无扩展处理，不向上抛出，不连累宿主模块装载。"""

    def _boom():
        raise RuntimeError("provider 读取失败")

    configure_plugin_capability_roots(_boom)

    assert ModuleManager._discover_plugin_capability_roots() == ()


def test_discover_plugin_capability_roots_filters_out_missing_directories(tmp_path):
    """provider 返回不存在的目录时被过滤掉，不传给 discover() 触发报错。"""
    missing = tmp_path / "not_installed"
    configure_plugin_capability_roots(lambda: (missing,))

    assert ModuleManager._discover_plugin_capability_roots() == ()


def test_discover_plugin_capability_roots_defaults_to_empty_without_configuration():
    """未调用 configure_plugin_capability_roots 时不追加任何发现根。"""
    assert ModuleManager._discover_plugin_capability_roots() == ()


# 四、运行态与能力索引：真实 ModuleManager 单例


@pytest.fixture
def real_module_manager(
    plugin_version_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[SimpleNamespace]:
    """用真实 ModuleManager 单例装载一个安全合成宿主模块与真实插件模块。

    注册表通过真实的 CapabilityRegistry.discover() 构建（同时含 host_module 与
    plugin_module 两种 kind，与生产的 build_host_module_registry 语义一致），
    只是把宿主模块的发现根换成一个安全的合成模块，避免测试里真的激活全部内置
    模块触发外部连接。ModuleManager 的其余装载、投影、能力索引逻辑保持真实。
    """
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "fixture_safe_host_module.py").write_text(
        _SAFE_HOST_MODULE_SOURCE, encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(source_root))

    safe_host_root = tmp_path / "safe_host"
    (safe_host_root / "safehost").mkdir(parents=True)
    (safe_host_root / "safehost" / "capability.toml").write_text(
        _SAFE_HOST_CAPABILITY_TOML, encoding="utf-8",
    )
    registry = CapabilityRegistry.discover(
        (safe_host_root, plugin_version_dir),
        kinds={HOST_MODULE_KIND, PLUGIN_MODULE_KIND},
        selector_schemas={},
    )
    monkeypatch.setattr(
        module_manager_extension, "build_host_module_registry", lambda *_roots: registry,
    )

    singleton_key = (ModuleManager, (), frozenset())
    previous_manager = Singleton._instances.pop(singleton_key, None)
    resolver_attr = "_EventManager__handler_instance_resolvers"
    previous_resolvers = dict(getattr(eventmanager, resolver_attr))
    subscribers_attr = "_EventManager__broadcast_subscribers"
    previous_config_changed = dict(
        getattr(eventmanager, subscribers_attr).get(EventType.ConfigChanged, {})
    )
    sys.modules.pop("fixture_safe_host_module", None)

    manager = ModuleManager()
    try:
        yield SimpleNamespace(manager=manager)
    finally:
        try:
            manager.shutdown()
        except Exception:
            pass
        Singleton._instances.pop(singleton_key, None)
        if previous_manager is not None:
            Singleton._instances[singleton_key] = previous_manager
        setattr(eventmanager, resolver_attr, previous_resolvers)
        subscribers = getattr(eventmanager, subscribers_attr)
        if previous_config_changed:
            subscribers[EventType.ConfigChanged] = dict(previous_config_changed)
        else:
            subscribers.pop(EventType.ConfigChanged, None)
        sys.modules.pop("fixture_safe_host_module", None)


def test_plugin_module_reaches_running_state_in_the_real_module_manager(real_module_manager):
    """插件模块在真实 ModuleManager 单例里进入运行态，与合成宿主模块同一个运行视图。"""
    manager = real_module_manager.manager

    host_instance = manager.get_running_module("SafeHostModule")
    plugin_instance = manager.get_running_module(MODULE_CLASS_NAME)

    assert host_instance is not None
    assert plugin_instance is not None
    assert type(plugin_instance).__name__ == MODULE_CLASS_NAME

    storage_ids = storage_backend_registry.storage_ids()
    assert STORAGE_IDENTITY in storage_ids


def test_plugin_capability_is_indexed_by_the_real_module_manager_alongside_host_module(
    real_module_manager,
):
    """插件模块声明的 list_files 能力经 ModuleManager 的能力索引与宿主模块同级可查，且可真实调用。"""
    manager = real_module_manager.manager

    providers = manager.providers_for("list_files")
    provider_types = {type(instance).__name__ for instance in providers}

    assert "SafeHostModule" in provider_types
    assert MODULE_CLASS_NAME in provider_types
    assert "list_files" in manager.get_module_capabilities(MODULE_CLASS_NAME)

    plugin_instance = manager.get_running_module(MODULE_CLASS_NAME)
    from app.schemas.workflow import FileItem

    root = FileItem(storage=STORAGE_IDENTITY, type="dir", path="/")
    plugin_instance.create_folder(root, "movies")
    listed = plugin_instance.list_files(root)
    assert listed is not None
    assert [item.name for item in listed] == ["movies"]


# 五、坏插件清单是否会连累其余模块装载


def test_build_host_module_registry_is_not_isolated_from_a_broken_plugin_root(
    broken_plugin_version_dir,
):
    """生产入口 build_host_module_registry(extra_roots) 遇到坏插件根时整体报错。

    _discover_plugin_capability_roots() 只兜底「provider 抛错」与「目录不存在」，
    对「目录存在但 capability.toml 内容非法」没有隔离：一旦某个已装插件的清单
    写错，CapabilityRegistry.discover() 会在合并宿主模块根与全部插件根的这一次
    调用里直接抛错，build_host_module_registry() 不吞掉这个错误，调用方（未来是
    ModuleManager.__init__）会连同全部宿主模块一起装载失败。这是内核开门之外
    还需要补的一层：按插件根隔离装载失败，而不是把全部插件根塞进同一次
    discover() 调用。
    """
    with pytest.raises(CapabilityManifestError):
        build_host_module_registry(extra_roots=(broken_plugin_version_dir,))


def test_per_plugin_root_isolation_keeps_the_good_plugin_loadable(
    plugin_version_dir, broken_plugin_version_dir,
):
    """按插件各自的根分别调用 discover() 时，一个插件的坏声明只影响它自己。

    这不是 build_host_module_registry 当前的行为，而是验证「按插件根单独隔离
    装载」这一策略本身用现有的 CapabilityRegistry.discover() 可行，可以作为
    后续处理坏插件清单的落地方式。
    """
    loaded: dict[str, CapabilityRegistry] = {}
    failed: dict[str, CapabilityManifestError] = {}
    for name, root in ((PLUGIN_DIR, plugin_version_dir), (BROKEN_PLUGIN_DIR, broken_plugin_version_dir)):
        try:
            loaded[name] = CapabilityRegistry.discover(
                (root,), kinds={PLUGIN_MODULE_KIND}, selector_schemas={},
            )
        except CapabilityManifestError as error:
            failed[name] = error

    assert PLUGIN_DIR in loaded
    assert loaded[PLUGIN_DIR].get_spec(MODULE_CLASS_NAME) is not None
    assert BROKEN_PLUGIN_DIR in failed
