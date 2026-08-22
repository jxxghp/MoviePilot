"""存储模块按实例扇出后端对象的守护测试。

存储路径可以携带实例名（``u115@work:/media``），存储模块因此按实例各持有一个后端
对象：裸令牌走兼容指针所指的那一份，具名令牌走对应实例，本模块没有该实例时让出而不回落。
"""

from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from app.modules._base.storage import StorageInstanceSpec, _StorageModuleBase
from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.extensions.registry.storage import storage_backend_registry
from app.schemas.workflow import FileItem
from tests.test_storage_backend_registry import BUILTIN_STORAGE_MODULES

# 探针存储的标识，不与任何内建或插件存储重名
PROBE_STORAGE_ID = "probefs"


class _ProbeStorage:
    """记录自身实例归属与调用次数的存储后端桩。"""

    schema = PROBE_STORAGE_ID
    storage_instance: Optional[str] = None

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.calls: List[str] = []

    def support_transtype(self) -> dict:
        """记录一次调用并回报本对象服务的实例。

        :return: 以本对象实例归属为内容的整理方式表
        """
        self.calls.append("support_transtype")
        return {"instance": self.storage_instance or "裸令牌"}


class _TakeoverStorage(_ProbeStorage):
    """插件接管同一存储标识时提供的后端桩。"""


class _ProbeStorageModule(_StorageModuleBase):
    """按注入的多份实例配置扇出后端对象的存储模块。"""

    storage_class = _ProbeStorage

    def __init__(self, configs: Tuple[Tuple[Optional[str], bool, bool], ...] = ()) -> None:
        """记录注入的实例配置。

        :param configs: ``(实例名, 是否承接裸令牌, 该实例配置是否构造失败)`` 序列，
            为空时沿用基类的单一裸令牌位
        """
        super().__init__()
        self._injected = tuple(configs)

    @staticmethod
    def get_name() -> str:
        """获取模块名称。"""
        return "探针存储"

    @staticmethod
    def get_priority() -> int:
        """获取模块优先级。"""
        return 9

    def _instance_specs(self) -> Tuple[StorageInstanceSpec, ...]:
        """按注入的配置产出实例描述。

        :return: 存储实例描述元组
        """
        if not self._injected:
            return super()._instance_specs()
        return tuple(
            StorageInstanceSpec(instance=name, bare_token_target=bare_token_target)
            for name, bare_token_target, _ in self._injected
        )

    def _create_storage(self, instance: Optional[str]):
        """构造指定实例的后端对象，标记为坏配置的实例构造失败。

        :param instance: 实例名，None 表示裸令牌位
        :return: 该实例的存储操作对象
        :raises RuntimeError: 该实例的注入配置被标记为构造失败
        """
        broken = {name for name, _, is_broken in self._injected if is_broken}
        if instance in broken:
            raise RuntimeError(f"实例 {instance} 的配置连不上")
        return super()._create_storage(instance)


@pytest.fixture(autouse=True)
def clean_probe_registrations():
    """用例结束后清掉探针存储在全局登记表里的残留。"""
    yield
    while True:
        remaining = [
            entry for entry in storage_backend_registry.entries()
            if entry.storage_id == PROBE_STORAGE_ID
        ]
        if not remaining:
            break
        for entry in remaining:
            storage_backend_registry.unregister(entry.storage)


def _probe_module(configs: Tuple[Tuple[Optional[str], bool, bool], ...]) -> _ProbeStorageModule:
    """建立并启动一个按给定配置扇出的探针存储模块。

    :param configs: ``(实例名, 是否承接裸令牌, 该实例配置是否构造失败)`` 序列
    :return: 已完成初始化的存储模块
    """
    module = _ProbeStorageModule(configs)
    module.init_module()
    return module


@pytest.mark.parametrize("storage_id,module_class,backend", BUILTIN_STORAGE_MODULES)
def test_builtin_module_holds_only_the_default_instance(storage_id, module_class, backend):
    """内建存储模块只持有裸令牌位，裸令牌逐条命中它。"""
    module = module_class()
    module.init_module()
    try:
        assert list(module._storages) == [None]  # noqa: SLF001

        default_storage = module._claim(storage_id)  # noqa: SLF001

        assert type(default_storage) is backend
        assert default_storage.storage_instance is None
        assert storage_backend_registry.find(storage_id).instance is None
        assert module.storage_manage(storage=storage_id, action="support_transtype") is not None
    finally:
        module.stop()


@pytest.mark.parametrize("storage_id,module_class,_backend", BUILTIN_STORAGE_MODULES)
def test_named_token_is_yielded_instead_of_falling_back(storage_id, module_class, _backend):
    """本模块没有该具名实例时让出，具名令牌的操作绝不落到别的实例上。"""
    module = module_class()
    module.init_module()
    named = f"{storage_id}@work"
    try:
        assert module._claim(named) is None  # noqa: SLF001

        fileitem = FileItem(storage=named, path="/media", type="dir", name="media")

        assert module.list_files(fileitem) is None
        assert module.any_files(fileitem) is None
        assert module.create_folder(fileitem, "sub") is None
        assert module.delete_file(fileitem) is None
        assert module.rename_file(fileitem, "new") is None
        assert module.download_file(fileitem) is None
        assert module.upload_file(fileitem, Path("/tmp/x")) is None
        assert module.get_parent_item(fileitem) is None
        assert module.get_folder(named, Path("/media")) is None
        assert module.get_file_item(named, Path("/media")) is None
        assert module.snapshot_storage(named, Path("/media")) is None
        assert module.storage_manage(storage=named, action="usage") is None
        assert module.storage_manage(storage=named, action="save_config", conf={}) is None
    finally:
        module.stop()


@pytest.mark.parametrize("storage_id,module_class,_backend", BUILTIN_STORAGE_MODULES)
def test_malformed_token_is_claimed_by_no_module(storage_id, module_class, _backend):
    """畸形令牌不与任何存储类型相等，任何模块都不认领。"""
    module = module_class()
    module.init_module()
    malformed_tokens = (
        None, "", f"{storage_id}@", f"{storage_id}@wo rk", f"{storage_id}@wo:rk",
        f"@{storage_id}", f"{storage_id}@work@home",
    )
    try:
        for token in malformed_tokens:
            assert module._claim(token) is None, token  # noqa: SLF001
            assert module.get_file_item(token, Path("/media")) is None, token
            assert module.storage_manage(storage=token, action="usage") is None, token
    finally:
        module.stop()


def test_injected_configs_fan_out_one_backend_per_instance():
    """注入多份实例配置即扇出多个后端对象，各自登记为独立实例位。"""
    module = _probe_module((("work", False, False), ("home", False, False)))
    try:
        assert set(module._storages) == {"work", "home"}  # noqa: SLF001

        work = module._claim(f"{PROBE_STORAGE_ID}@work")  # noqa: SLF001
        home = module._claim(f"{PROBE_STORAGE_ID}@home")  # noqa: SLF001

        assert work is not home
        assert work.storage_instance == "work"
        assert home.storage_instance == "home"
        assert {
            entry.storage for entry in storage_backend_registry.instances(PROBE_STORAGE_ID)
        } == {f"{PROBE_STORAGE_ID}@work", f"{PROBE_STORAGE_ID}@home"}
    finally:
        module.stop()


def test_instances_do_not_share_their_backend_object():
    """同模块的两个实例互不串用：操作只记在被指定的那个实例上。"""
    module = _probe_module((("work", False, False), ("home", False, False)))
    try:
        result = module.storage_manage(
            storage=f"{PROBE_STORAGE_ID}@work", action="support_transtype"
        )

        assert result["data"] == {"transtype": {"instance": "work"}}
        assert module._claim(f"{PROBE_STORAGE_ID}@work").calls == ["support_transtype"]  # noqa: SLF001
        assert module._claim(f"{PROBE_STORAGE_ID}@home").calls == []  # noqa: SLF001
    finally:
        module.stop()


def test_unnamed_instance_serves_the_bare_token():
    """未具名实例占据裸令牌位，裸令牌命中它而不是任何具名实例。"""
    module = _probe_module(((None, False, False), ("work", False, False)))
    try:
        assert module._claim(PROBE_STORAGE_ID).storage_instance is None  # noqa: SLF001
        assert module._claim(f"{PROBE_STORAGE_ID}@work").storage_instance == "work"  # noqa: SLF001
    finally:
        module.stop()


def test_marked_named_instance_serves_the_bare_token():
    """全为具名实例时，自称承接的那个接住裸令牌。"""
    module = _probe_module((("work", False, False), ("home", True, False)))
    try:
        assert module._claim(PROBE_STORAGE_ID).storage_instance == "home"  # noqa: SLF001
    finally:
        module.stop()


@pytest.mark.parametrize("configs", [
    (("work", False, False), ("home", False, False)),
    (("work", True, False), ("home", True, False)),
])
def test_bare_token_is_yielded_when_no_default_can_be_decided(configs):
    """裁决不出兼容指针时裸令牌让出，绝不按顺序取第一个实例。"""
    module = _probe_module(configs)
    try:
        assert module._claim(PROBE_STORAGE_ID) is None  # noqa: SLF001
        assert module.storage_manage(storage=PROBE_STORAGE_ID, action="support_transtype") is None
    finally:
        module.stop()


def test_absent_named_instance_is_yielded_rather_than_served_by_the_default():
    """已有兼容指针时，指名一个不存在的实例仍然让出而不改走那一份。"""
    module = _probe_module(((None, False, False), ("work", False, False)))
    try:
        assert module._claim(PROBE_STORAGE_ID) is not None  # noqa: SLF001
        assert module._claim(f"{PROBE_STORAGE_ID}@archive") is None  # noqa: SLF001
        assert module.storage_manage(
            storage=f"{PROBE_STORAGE_ID}@archive", action="support_transtype"
        ) is None
    finally:
        module.stop()


def test_one_broken_instance_does_not_take_down_the_others():
    """一个实例构造失败只跳过它自己，同模块其余实例照常建立并登记。"""
    module = _probe_module(
        (("work", False, True), ("home", False, False), (None, False, False))
    )
    try:
        assert set(module._storages) == {"home", None}  # noqa: SLF001
        assert module._claim(f"{PROBE_STORAGE_ID}@work") is None  # noqa: SLF001
        assert module._claim(f"{PROBE_STORAGE_ID}@home").storage_instance == "home"  # noqa: SLF001
        assert module._claim(PROBE_STORAGE_ID).storage_instance is None  # noqa: SLF001
        assert {
            entry.instance for entry in storage_backend_registry.instances(PROBE_STORAGE_ID)
        } == {"home", None}
    finally:
        module.stop()


def test_registry_rejected_instance_is_not_held_either():
    """登记被拒的实例不予持有，模块持有的实例与登记表可取用的实例一致。"""
    module = _probe_module((("wo rk", False, False), ("home", False, False)))
    try:
        assert set(module._storages) == {"home"}  # noqa: SLF001
        assert [
            entry.instance for entry in storage_backend_registry.instances(PROBE_STORAGE_ID)
        ] == ["home"]
    finally:
        module.stop()


def test_stop_checks_ownership_per_instance():
    """停止时的归属校验按 (标识, 实例) 粒度进行，被接管的实例位不被踢掉。"""
    module = _probe_module((("work", False, False), ("home", False, False)))
    storage_backend_registry.register(
        _TakeoverStorage,
        ExtensionDistribution.MARKET,
        owner="ProbePlugin@default",
        instance="work",
    )

    module.stop()

    assert storage_backend_registry.find(f"{PROBE_STORAGE_ID}@work").owner == "ProbePlugin@default"
    assert storage_backend_registry.find(f"{PROBE_STORAGE_ID}@home") is None


def test_stop_only_touches_the_instances_this_module_holds():
    """停止只回收本模块持有的实例位，同标识下别人的实例位不受牵连。"""
    module = _probe_module((("home", False, False),))
    storage_backend_registry.register(
        _TakeoverStorage,
        ExtensionDistribution.MARKET,
        owner="ProbePlugin@default",
        instance="work",
    )

    module.stop()

    assert storage_backend_registry.find(f"{PROBE_STORAGE_ID}@work").backend is _TakeoverStorage
    assert storage_backend_registry.find(f"{PROBE_STORAGE_ID}@home") is None
