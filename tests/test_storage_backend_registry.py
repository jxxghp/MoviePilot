"""存储一级模块：发现、自筛、登记与整理编排选中结果的守护测试。"""

from pathlib import Path
from typing import List, Optional

import pytest

from app.modules._base.storage import StorageBase, _StorageModuleBase
from app.modules.alipan import AliPanModule
from app.modules.alipan.alipan import AliPan
from app.modules.alist import AlistModule
from app.modules.alist.alist import Alist
from app.modules.alistgo import AlistGoModule
from app.modules.alistgo.alistgo import AlistGo
from app.modules.medialibrary import MediaLibraryModule
from app.modules.localstorage import LocalStorageModule
from app.modules.localstorage.local import LocalStorage
from app.modules.rclone import RcloneModule
from app.modules.rclone.rclone import Rclone
from app.modules.smb import SmbModule
from app.modules.smb.smb import SMB
from app.modules.u115 import U115Module
from app.modules.u115.u115 import U115Pan
from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.extensions.registry.storage import (
    StorageBackendRegistry,
    storage_backend_identity,
    storage_backend_registry,
)
from app.runtime.hostports.storages import storage_config_port
from app.schemas.file import FileURI
from app.schemas.types import StorageSchema
from app.schemas.workflow import FileItem
from app.startup.hostport_initializer import configure_host_ports

# 内建存储的标识、承载模块与后端实现类，顺序即模块优先级顺序
BUILTIN_STORAGE_MODULES = (
    ("alipan", AliPanModule, AliPan),
    ("alist", AlistModule, Alist),
    ("alistgo", AlistGoModule, AlistGo),
    ("local", LocalStorageModule, LocalStorage),
    ("rclone", RcloneModule, Rclone),
    ("smb", SmbModule, SMB),
    ("u115", U115Module, U115Pan),
)

# 已由各存储模块承担、媒体库文件系统模块不得再实现的存储能力方法
STORAGE_CAPABILITY_METHODS = (
    "list_files",
    "any_files",
    "create_folder",
    "get_folder",
    "delete_file",
    "rename_file",
    "download_file",
    "upload_file",
    "get_file_item",
    "get_parent_item",
    "snapshot_storage",
    "storage_manage",
)


class _MemoryStorage(StorageBase):
    """标识不在 StorageSchema 内的存储后端。"""

    schema = "memfs"
    transtype = {"copy": "复制"}

    def init_storage(self):
        """无需建立任何连接。"""
        pass

    def check(self) -> bool:
        """存储始终可用。"""
        return True

    def list(self, fileitem: FileItem) -> List[FileItem]:
        """返回固定的单个子项。"""
        return [FileItem(storage=self.schema, path="/media/demo.mkv", type="file", name="demo.mkv")]

    def create_folder(self, fileitem: FileItem, name: str) -> Optional[FileItem]:
        """按名称返回新建目录项。"""
        return FileItem(storage=self.schema, path=f"{fileitem.path}/{name}", type="dir", name=name)

    def get_folder(self, path: Path) -> Optional[FileItem]:
        """返回对应路径的目录项。"""
        return FileItem(storage=self.schema, path=path.as_posix(), type="dir", name=path.name)

    def get_item(self, path: Path) -> Optional[FileItem]:
        """返回对应路径的文件项。"""
        return FileItem(storage=self.schema, path=path.as_posix(), type="file", name=path.name)

    def delete(self, fileitem: FileItem) -> bool:
        """删除始终成功。"""
        return True

    def rename(self, fileitem: FileItem, name: str) -> bool:
        """重命名始终成功。"""
        return True

    def download(self, fileitem: FileItem, path: Path = None) -> Optional[Path]:
        """不提供实际下载。"""
        return None

    def upload(self, fileitem: FileItem, path: Path, new_name: Optional[str] = None) -> Optional[FileItem]:
        """不提供实际上传。"""
        return None

    def detail(self, fileitem: FileItem) -> Optional[FileItem]:
        """原样返回文件项。"""
        return fileitem

    def copy(self, fileitem: FileItem, path: Path, new_name: str) -> bool:
        """复制始终成功。"""
        return True

    def move(self, fileitem: FileItem, path: Path, new_name: str) -> bool:
        """移动始终成功。"""
        return True

    def link(self, fileitem: FileItem, target_file: Path) -> bool:
        """不支持硬链接。"""
        return False

    def softlink(self, fileitem: FileItem, target_file: Path) -> bool:
        """不支持软链接。"""
        return False

    def usage(self):
        """不统计使用情况。"""
        return None


class _MemoryStorageModule(_StorageModuleBase):
    """承载自由标识存储后端的一级模块。"""

    storage_class = _MemoryStorage

    @staticmethod
    def get_name() -> str:
        """获取模块名称。"""
        return "内存存储"

    @staticmethod
    def get_priority() -> int:
        """获取模块优先级。"""
        return 9


@pytest.fixture
def storage_modules():
    """启动七个内建存储模块，用例结束后停止。"""
    modules = []
    for _, module_class, _ in BUILTIN_STORAGE_MODULES:
        module = module_class()
        module.init_module()
        modules.append(module)
    yield modules
    for module in modules:
        module.stop()


@pytest.fixture
def memory_module():
    """启动自由标识存储模块，用例结束后停止。"""
    module = _MemoryStorageModule()
    module.init_module()
    yield module
    module.stop()


@pytest.fixture
def restore_host_ports():
    """用例可改写端口注册，结束后恢复组合根装配。"""
    yield
    configure_host_ports()


def test_each_backend_is_an_independent_first_class_module():
    """七个存储后端各自成包，模块类与后端实现类一一对应。"""
    for storage_id, module_class, backend in BUILTIN_STORAGE_MODULES:
        assert module_class.storage_class is backend
        assert module_class.storage_id() == storage_id
        assert module_class.get_subtype() is backend.schema


def test_running_modules_register_their_backend(storage_modules):
    """七个内建存储后端的标识与相对顺序保持不变。"""
    builtin_ids = [storage_id for storage_id, _, _ in BUILTIN_STORAGE_MODULES]

    assert list(storage_backend_registry.storage_ids()) == builtin_ids


def test_selection_by_identity_matches_registered_class(storage_modules):
    """按存储标识选中的内建实现与模块承载的实现类一致。"""
    for storage_id, _, backend in BUILTIN_STORAGE_MODULES:
        assert type(storage_backend_registry.resolve(storage_id)) is backend


def test_stopped_module_unregisters_its_backend(storage_modules):
    """模块停止后其存储标识不再可选中。"""
    module = storage_modules[0]
    storage_id = module.storage_id()

    module.stop()

    assert storage_id not in storage_backend_registry.storage_ids()
    assert storage_backend_registry.resolve(storage_id) is None


@pytest.mark.parametrize("storage_id,module_class,_backend", BUILTIN_STORAGE_MODULES)
def test_capabilities_only_claim_their_own_storage(storage_id, module_class, _backend):
    """不属于本存储的请求一律返回 None，让给下一个模块。"""
    module = module_class()
    module.init_module()
    try:
        other = FileItem(storage="not-this-storage", path="/media", type="dir", name="media")

        assert module.list_files(other) is None
        assert module.any_files(other) is None
        assert module.create_folder(other, "sub") is None
        assert module.delete_file(other) is None
        assert module.rename_file(other, "new") is None
        assert module.download_file(other) is None
        assert module.upload_file(other, Path("/tmp/x")) is None
        assert module.get_parent_item(other) is None
        assert module.get_folder("not-this-storage", Path("/media")) is None
        assert module.get_file_item("not-this-storage", Path("/media")) is None
        assert module.snapshot_storage("not-this-storage", Path("/media")) is None
        assert module.storage_manage(storage="not-this-storage", action="usage") is None
        # 属于本存储的请求必须被认领，自筛不能把所有请求都让出去
        assert module.storage_manage(storage=storage_id, action="support_transtype") is not None
    finally:
        module.stop()


def test_free_identity_backend_works_as_its_own_module(memory_module):
    """未登记进 StorageSchema 的自由标识后端可被登记、发现并经能力方法工作。"""
    assert "memfs" not in {item.value for item in StorageSchema}
    assert "memfs" in storage_backend_registry.storage_ids()

    fileitem = FileItem(storage="memfs", path="/media", type="dir", name="media")

    assert memory_module.list_files(fileitem)[0].name == "demo.mkv"
    assert memory_module.get_file_item("memfs", Path("/media/demo.mkv")).name == "demo.mkv"
    assert memory_module.storage_manage(storage="memfs", action="support_transtype") == {
        "success": True,
        "data": {"transtype": {"copy": "复制"}},
    }


def test_free_identity_backend_reads_and_writes_config(restore_host_ports):
    """自由标识后端按其标识读写存储配置。"""
    written = {}
    reset = []
    storage_config_port.register(lambda: type("_Stub", (), {
        "get_storage": staticmethod(lambda storage: None),
        "set_storage": staticmethod(lambda storage, conf: written.update({storage: conf})),
        "reset_storage": staticmethod(lambda storage: reset.append(storage)),
    })())
    oper = _MemoryStorage()

    oper.set_config({"token": "x"})
    oper.reset_config()

    assert written == {"memfs": {"token": "x"}}
    assert reset == ["memfs"]


def test_identity_reads_enum_and_free_string_alike():
    """存储标识既接受枚举成员，也接受自由字符串。"""
    assert storage_backend_identity(LocalStorage) == "local"
    assert storage_backend_identity(_MemoryStorage) == "memfs"
    assert storage_backend_identity(object()) is None


def test_registry_rejects_identity_that_cannot_prefix_a_path():
    """无法作为路径前缀的存储标识不被登记。"""

    class _DriveLetterStorage(_MemoryStorage):
        """标识与 Windows 盘符无法区分的存储后端。"""

        schema = "z"

    assert storage_backend_registry.register(_DriveLetterStorage) is None
    assert "z" not in storage_backend_registry.storage_ids()


def test_transfer_still_resolves_operators_by_identity(storage_modules):
    """整理需要成对的源、目标操作对象，媒体库文件系统模块按标识直取。"""
    media_library = MediaLibraryModule()
    select = media_library._MediaLibraryModule__get_storage_oper  # noqa: SLF001

    assert type(select("local")) is LocalStorage
    assert select("local", "list") is not None
    assert select("local", "generate_qrcode_not_exists") is None
    assert select("not-a-storage") is None


def test_medialibrary_no_longer_routes_storage_capabilities():
    """存储能力方法已由各存储模块承担，媒体库文件系统模块不得再实现。"""
    for method in STORAGE_CAPABILITY_METHODS:
        assert not hasattr(MediaLibraryModule, method), method


def test_registry_diagnose_reports_distribution(storage_modules):
    """诊断信息按存储标识给出发行方式与提供方。"""
    diagnosed = {item["storage"]: item for item in storage_backend_registry.diagnose()}

    assert diagnosed["local"]["distribution"] == ExtensionDistribution.BUILTIN.value
    assert diagnosed["local"]["owner"] == "LocalStorageModule"


def test_file_uri_round_trips_free_identity_storage(memory_module):
    """自由标识存储的文件 URI 可正常解析与还原。"""
    file_uri = FileURI.from_uri("memfs:/media/anime")

    assert file_uri.storage == "memfs"
    assert file_uri.path == "/media/anime"
    assert file_uri.uri == "memfs:/media/anime"


def test_builtin_restart_does_not_evict_an_extension_override() -> None:
    """内建模块重启不会踢掉扩展对同一标识的接管。

    内建停止时按标识注销，而该标识此刻可能已被扩展接管。注销若不校验归属，
    弹出的就是接管方那条，随后内建快照又把取值还原回内建——扩展的登记在一次
    与它无关的内建重启中被静默抹掉。注销带上自身归属才能挡住这条路径。
    """
    registry = StorageBackendRegistry()

    class _BuiltinBackend:
        """内建存储后端桩。"""
        schema = "restart_probe"

    class _ExtensionBackend:
        """扩展提供的同标识存储后端桩。"""
        schema = "restart_probe"

    registry.register(_BuiltinBackend, ExtensionDistribution.BUILTIN, owner="ProbeModule")
    registry.register(_ExtensionBackend, ExtensionDistribution.MARKET, owner="ProbePlugin@default")

    assert registry.unregister("restart_probe", owner="ProbeModule") is False
    assert registry.find("restart_probe").owner == "ProbePlugin@default"

    # 接管方自己撤销后，标识按内建快照还原
    registry.unregister_owner("ProbePlugin@default")
    assert registry.find("restart_probe").owner == "ProbeModule"


class _NamedInstanceBackend:
    """按实例登记的存储后端桩。"""

    schema = "multi_probe"


class _OtherNamedInstanceBackend:
    """同标识另一实例的存储后端桩。"""

    schema = "multi_probe"


def test_same_identity_holds_several_named_instances() -> None:
    """同一存储标识下的多个具名实例并存，互不覆盖，各自按令牌取用。"""
    registry = StorageBackendRegistry()

    assert registry.register(_NamedInstanceBackend, instance="work") == "multi_probe@work"
    assert registry.register(_OtherNamedInstanceBackend, instance="home") == "multi_probe@home"

    assert registry.find("multi_probe@work").backend is _NamedInstanceBackend
    assert registry.find("multi_probe@home").backend is _OtherNamedInstanceBackend
    assert registry.storage_ids() == ("multi_probe",)
    assert set(registry.storage_tokens()) == {"multi_probe@work", "multi_probe@home"}
    assert [entry.instance for entry in registry.instances("multi_probe")] == ["home", "work"]


def test_unnamed_registration_serves_the_bare_token() -> None:
    """未具名登记占据裸令牌位，裸令牌与今天一样直接命中它。"""
    registry = StorageBackendRegistry()

    registry.register(_NamedInstanceBackend)
    registry.register(_OtherNamedInstanceBackend, instance="work")

    assert registry.find("multi_probe").backend is _NamedInstanceBackend
    assert registry.find("multi_probe").instance is None
    assert registry.bare_token_entry("multi_probe").backend is _NamedInstanceBackend
    assert registry.find("multi_probe@work").backend is _OtherNamedInstanceBackend


def test_marked_named_instance_serves_the_bare_token() -> None:
    """全为具名实例时，自称承接裸令牌的那个接住不指定实例的调用。"""
    registry = StorageBackendRegistry()

    registry.register(_NamedInstanceBackend, instance="work")
    registry.register(_OtherNamedInstanceBackend, instance="home", bare_token_target=True)

    assert registry.find("multi_probe").backend is _OtherNamedInstanceBackend
    assert type(registry.resolve("multi_probe")) is _OtherNamedInstanceBackend


def test_missing_bare_token_target_reports_candidates_instead_of_taking_the_first() -> None:
    """无人承接裸令牌时报错并列出候选，绝不按登记顺序取第一个。"""
    registry = StorageBackendRegistry()

    registry.register(_NamedInstanceBackend, instance="work")
    registry.register(_OtherNamedInstanceBackend, instance="home")

    with pytest.raises(LookupError) as excinfo:
        registry.find("multi_probe")

    message = str(excinfo.value)
    assert "没有承接裸令牌的实例" in message
    assert "home" in message and "work" in message


def test_several_self_claimed_bare_token_targets_count_as_none() -> None:
    """多个实例同时自称承接裸令牌即无法裁决，一律报错并列出候选。"""
    registry = StorageBackendRegistry()

    registry.register(_NamedInstanceBackend, instance="work", bare_token_target=True)
    registry.register(_OtherNamedInstanceBackend, instance="home", bare_token_target=True)

    with pytest.raises(LookupError) as excinfo:
        registry.resolve("multi_probe")

    assert "多个实例自称承接裸令牌" in str(excinfo.value)


def test_unregistered_identity_still_reports_absence_not_error() -> None:
    """一条登记都没有的标识仍返回 None，不改成报错。"""
    registry = StorageBackendRegistry()

    assert registry.find("multi_probe") is None
    assert registry.resolve("multi_probe") is None
    assert registry.bare_token_entry("multi_probe") is None
    assert registry.find("multi_probe@work") is None


def test_bare_token_target_stops_serving_after_it_is_unregistered() -> None:
    """承接裸令牌的实例注销后等同于无人承接，报错而不改走另一个已登记实例。"""
    registry = StorageBackendRegistry()

    registry.register(_NamedInstanceBackend, instance="work", bare_token_target=True)
    registry.register(_OtherNamedInstanceBackend, instance="home")
    registry.unregister("multi_probe@work")

    assert registry.find("multi_probe@home").backend is _OtherNamedInstanceBackend
    with pytest.raises(LookupError):
        registry.find("multi_probe")


def test_instance_takeover_checks_ownership_per_instance() -> None:
    """归属校验按实例位进行，接管某个实例不影响同标识其它实例的回收。"""
    registry = StorageBackendRegistry()

    registry.register(_NamedInstanceBackend, ExtensionDistribution.BUILTIN,
                      owner="ProbeModule", instance="work")
    registry.register(_OtherNamedInstanceBackend, ExtensionDistribution.MARKET,
                      owner="ProbePlugin@default", instance="work")
    registry.register(_NamedInstanceBackend, ExtensionDistribution.BUILTIN,
                      owner="ProbeModule", instance="home")

    # 内建按自身归属注销被接管的实例位不生效，接管方仍在
    assert registry.unregister("multi_probe@work", owner="ProbeModule") is False
    assert registry.find("multi_probe@work").owner == "ProbePlugin@default"
    # 同标识另一实例仍归内建，正常注销
    assert registry.unregister("multi_probe@home", owner="ProbeModule") is True

    # 接管方撤销后，被覆盖的内建实例位按快照还原
    registry.unregister_owner("ProbePlugin@default")
    assert registry.find("multi_probe@work").owner == "ProbeModule"


def test_unregister_owner_returns_storage_tokens() -> None:
    """按登记方回收返回的是存储令牌，具名实例可与默认实例区分。"""
    registry = StorageBackendRegistry()

    registry.register(_NamedInstanceBackend, ExtensionDistribution.MARKET,
                      owner="ProbePlugin@default")
    registry.register(_OtherNamedInstanceBackend, ExtensionDistribution.MARKET,
                      owner="ProbePlugin@default", instance="work")

    assert set(registry.unregister_owner("ProbePlugin@default")) == {
        "multi_probe", "multi_probe@work"
    }
    assert registry.storage_tokens() == ()


def test_registry_rejects_malformed_instance_name() -> None:
    """实例名不合法的登记被拒绝，不会造出无法用 URI 表达的实例位。"""
    registry = StorageBackendRegistry()

    assert registry.register(_NamedInstanceBackend, instance="wo rk") is None
    assert registry.register(_NamedInstanceBackend, instance="wo:rk") is None
    assert registry.register(_NamedInstanceBackend, storage_id="multi_probe@work") is None
    assert registry.storage_tokens() == ()


def test_registry_diagnose_reports_instances() -> None:
    """诊断信息按登记逐条给出存储令牌、实例名与裸令牌兼容指针。"""
    registry = StorageBackendRegistry()

    registry.register(_NamedInstanceBackend, instance="work", bare_token_target=True)
    diagnosed = registry.diagnose()[0]

    assert diagnosed["storage"] == "multi_probe@work"
    assert diagnosed["storage_id"] == "multi_probe"
    assert diagnosed["instance"] == "work"
    assert diagnosed["bare_token_target"] is True


def test_named_instance_uri_resolves_through_the_registry() -> None:
    """带实例名的 URI 经注册表取到该实例的后端，裸 URI 仍取承接裸令牌的那一份。"""
    registry = StorageBackendRegistry()

    registry.register(_NamedInstanceBackend)
    registry.register(_OtherNamedInstanceBackend, instance="work")

    for uri, expected in (
        ("multi_probe:/media", _NamedInstanceBackend),
        ("multi_probe@work:/media", _OtherNamedInstanceBackend),
    ):
        file_uri = FileURI.from_uri(uri)
        assert file_uri.uri == uri
        assert registry.find(file_uri.storage).backend is expected


def test_builtin_module_registers_the_default_instance(storage_modules) -> None:
    """内建存储模块登记的是各自标识的默认实例，不带实例名。"""
    for storage_id, _, _ in BUILTIN_STORAGE_MODULES:
        entry = storage_backend_registry.find(storage_id)
        assert entry.instance is None
        assert entry.storage == storage_id
