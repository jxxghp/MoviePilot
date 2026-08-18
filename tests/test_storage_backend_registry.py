"""存储后端注册表：自由标识后端、插件贡献与内建后端选中结果的守护测试。"""

from pathlib import Path
from typing import List, Optional

import pytest

from app.modules.filemanager import FileManagerModule
from app.modules.filemanager.storages import StorageBase
from app.modules.filemanager.storages.alipan import AliPan
from app.modules.filemanager.storages.alist import Alist
from app.modules.filemanager.storages.alistgo import AlistGo
from app.modules.filemanager.storages.local import LocalStorage
from app.modules.filemanager.storages.rclone import Rclone
from app.modules.filemanager.storages.smb import SMB
from app.modules.filemanager.storages.u115 import U115Pan
from app.runtime.extensions.contract import ExtensionDistribution
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.extensions.storage_registry import (
    storage_backend_identity,
    storage_backend_registry,
)
from app.runtime.storages import storage_config_port
from app.schemas.file import FileURI
from app.schemas.types import StorageSchema
from app.schemas.workflow import FileItem
from app.startup.hostport_initializer import configure_host_ports

# 内建存储后端的标识与实现类
BUILTIN_BACKENDS = (
    ("alipan", AliPan),
    ("alist", Alist),
    ("alistgo", AlistGo),
    ("local", LocalStorage),
    ("rclone", Rclone),
    ("smb", SMB),
    ("u115", U115Pan),
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


class _PluginStorage(_MemoryStorage):
    """由插件提供的存储后端。"""

    schema = "pluginfs"


class _LocalOverrideStorage(_MemoryStorage):
    """与内建存储同标识的存储后端。"""

    schema = "local"


class _FakeStoragePlugin:
    """声明存储后端的插件桩。"""

    plugin_name = "存储插件"

    def __init__(self, backends: List[type], enabled: bool = True):
        """记录插件提供的后端与启用状态。"""
        self._backends = backends
        self._enabled = enabled

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def provides_storages(self) -> List[type]:
        """返回插件提供的存储后端。"""
        return self._backends


@pytest.fixture
def module():
    """提供已接入存储后端注册表的文件整理模块。"""
    file_manager = FileManagerModule()
    file_manager.init_module()
    return file_manager


@pytest.fixture
def memory_storage():
    """登记自由标识存储后端，用例结束后注销。"""
    storage_backend_registry.register(_MemoryStorage)
    yield _MemoryStorage
    storage_backend_registry.unregister("memfs")


@pytest.fixture
def restore_host_ports():
    """用例可改写端口注册，结束后恢复组合根装配。"""
    yield
    configure_host_ports()


def _select(file_manager: FileManagerModule, storage: str, method: Optional[str] = None):
    """按存储标识取用模块内部的存储操作对象。"""
    return file_manager._FileManagerModule__get_storage_oper(storage, method)  # noqa: SLF001


def test_builtin_backends_keep_identities_and_order(module):
    """七个内建存储后端的标识与相对顺序保持不变。"""
    builtin_ids = [storage_id for storage_id, _ in BUILTIN_BACKENDS]
    listed = [storage_id for storage_id in module._support_storages if storage_id in builtin_ids]

    assert listed == builtin_ids


def test_builtin_backends_keep_selection_results(module):
    """按存储标识选中的内建实现与登记的实现类一致。"""
    for storage_id, backend in BUILTIN_BACKENDS:
        assert type(_select(module, storage_id)) is backend


def test_selection_still_requires_the_requested_method(module):
    """限定操作时，未提供该操作的后端不被选中。"""
    assert _select(module, "local", "list") is not None
    assert _select(module, "local", "generate_qrcode_not_exists") is None


def test_uninitialized_module_selects_nothing():
    """未初始化的模块不接入注册表，任何存储标识都选不出实现。"""
    file_manager = FileManagerModule()

    assert file_manager._support_storages == []
    assert _select(file_manager, "local") is None


def test_free_identity_backend_is_registered_and_selected(module, memory_storage):
    """未登记进 StorageSchema 的自由标识后端可被注册、发现并选中。"""
    assert "memfs" not in {item.value for item in StorageSchema}
    assert "memfs" in module._support_storages

    oper = _select(module, "memfs")

    assert type(oper) is memory_storage
    assert oper.check() is True
    assert oper.list(FileItem(storage="memfs", path="/media"))[0].name == "demo.mkv"


def test_free_identity_backend_works_through_module_capabilities(module, memory_storage):
    """自由标识后端可经模块能力方法正常工作。"""
    fileitem = FileItem(storage="memfs", path="/media", type="dir", name="media")

    assert module.list_files(fileitem)[0].name == "demo.mkv"
    assert module.get_file_item("memfs", Path("/media/demo.mkv")).name == "demo.mkv"
    assert module.storage_manage(storage="memfs", action="support_transtype") == {
        "success": True,
        "data": {"transtype": {"copy": "复制"}},
    }


def test_free_identity_backend_reads_and_writes_config(memory_storage, restore_host_ports):
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


def test_plugin_provided_backend_joins_registry(module, monkeypatch):
    """插件提供的存储后端可接入注册表并被选中。"""
    plugin_manager = PluginManager()
    monkeypatch.setattr(
        plugin_manager,
        "_running_plugins",
        {"FakeStoragePlugin": _FakeStoragePlugin([_PluginStorage])},
    )

    assert "pluginfs" in module._support_storages
    assert type(_select(module, "pluginfs")) is _PluginStorage

    entry = storage_backend_registry.find("pluginfs")

    assert entry.distribution is ExtensionDistribution.MARKET
    assert entry.owner == "FakeStoragePlugin"


def test_plugin_backend_disappears_after_plugin_stops(module, monkeypatch):
    """插件卸载后其存储后端从注册表消失。"""
    plugin_manager = PluginManager()
    monkeypatch.setattr(
        plugin_manager,
        "_running_plugins",
        {"FakeStoragePlugin": _FakeStoragePlugin([_PluginStorage])},
    )
    assert "pluginfs" in module._support_storages

    monkeypatch.setattr(plugin_manager, "_running_plugins", {})

    assert "pluginfs" not in module._support_storages
    assert _select(module, "pluginfs") is None
    assert storage_backend_registry.find("pluginfs") is None


def test_disabled_plugin_does_not_contribute_backends(module, monkeypatch):
    """未启用的插件不向注册表贡献存储后端。"""
    plugin_manager = PluginManager()
    monkeypatch.setattr(
        plugin_manager,
        "_running_plugins",
        {"FakeStoragePlugin": _FakeStoragePlugin([_PluginStorage], enabled=False)},
    )

    assert "pluginfs" not in module._support_storages


def test_plugin_backend_overrides_builtin_identity(module, monkeypatch):
    """同标识时以插件提供的存储后端为准，插件停止后回到内建实现。"""
    plugin_manager = PluginManager()
    monkeypatch.setattr(
        plugin_manager,
        "_running_plugins",
        {"FakeStoragePlugin": _FakeStoragePlugin([_LocalOverrideStorage])},
    )

    assert type(_select(module, "local")) is _LocalOverrideStorage

    monkeypatch.setattr(plugin_manager, "_running_plugins", {})

    assert type(_select(module, "local")) is LocalStorage


def test_failing_plugin_hook_does_not_break_registry(module, monkeypatch):
    """插件声明出错只记录日志，不影响内建存储后端可用。"""

    class _BrokenPlugin:
        """声明存储后端时抛错的插件桩。"""

        plugin_name = "异常插件"

        @staticmethod
        def get_state() -> bool:
            """插件处于启用状态。"""
            return True

        @staticmethod
        def provides_storages():
            """声明过程抛出异常。"""
            raise RuntimeError("声明失败")

    plugin_manager = PluginManager()
    monkeypatch.setattr(plugin_manager, "_running_plugins", {"BrokenPlugin": _BrokenPlugin()})

    assert "local" in module._support_storages
    assert type(_select(module, "local")) is LocalStorage


def test_registry_diagnose_reports_distribution(module, memory_storage):
    """诊断信息按存储标识给出发行方式与提供方。"""
    diagnosed = {item["storage"]: item for item in storage_backend_registry.diagnose()}

    assert diagnosed["local"]["distribution"] == ExtensionDistribution.BUILTIN.value
    assert diagnosed["local"]["owner"] is None
    assert diagnosed["memfs"]["distribution"] == ExtensionDistribution.BUILTIN.value


def test_file_uri_round_trips_free_identity_storage(memory_storage):
    """自由标识存储的文件 URI 可正常解析与还原。"""
    file_uri = FileURI.from_uri("memfs:/media/anime")

    assert file_uri.storage == "memfs"
    assert file_uri.path == "/media/anime"
    assert file_uri.uri == "memfs:/media/anime"
