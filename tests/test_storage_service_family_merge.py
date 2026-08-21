"""存储并入服务实例族后的等价与边界守护测试。

存储的实例配置与下载器、媒体服务器、消息渠道并成一族：一张表、一套整形、一套筛选与
默认裁决。本文件盯住三件事：并族前后同一份配置数据整形结果逐条等价；两种默认标记粒度
各自生效且互不干扰；存储类型仍走自己的声明钩子，服务实例声明不能顶替它。
"""

import asyncio

import pytest

from app.application.service_config import (
    async_write_system_setting,
    get_configured_service_instance_configs,
    read_system_setting,
)
from app.application.storage import StorageHelper
from app.modules._base.storage import StorageBase
from app.modules.alipan.alipan import AliPan
from app.modules.alist.alist import Alist
from app.modules.alistgo.alistgo import AlistGo
from app.modules.localstorage.local import LocalStorage
from app.modules.rclone.rclone import Rclone
from app.modules.smb.smb import SMB
from app.modules.u115.u115 import U115Pan
from app.plugins import _PluginBase
from app.runtime.deprecation.notices import NOTICES
from app.runtime.extensions.declaration import (
    ServiceInstanceDeclaration,
    StorageDeclaration,
)
from app.runtime.extensions.plugin.service_instance_capabilities import (
    service_instance_declaration_violation,
)
from app.runtime.extensions.plugin.storage_capabilities import (
    storage_declaration_violation,
)
from app.runtime.extensions.service_config import (
    DefaultTargetScope,
    STORAGE_CAPABILITY,
    select_instance_configs,
    service_default_scope,
)
from app.runtime.extensions.service_config_validation import (
    service_config_records,
    service_config_write_violation,
)
from app.runtime.extensions.service_family_registry import service_family_registry
from app.runtime.extensions.service_instance_registry import service_instance_registry
from app.schemas.system import DownloaderConf, StorageConf
from app.schemas.types import ModuleType, SystemConfigKey

# 并族前存储配置的专用整形规则，逐字取自并族前的 storage_config_records，用作对拍基准
_LEGACY_DEFAULT_FIELD = "is_default"


def _legacy_storage_records(confs) -> list:
    """并族前存储专用的整形实现，作为逐条对拍的参照。

    :param confs: 存储实例配置对象序列
    :return: 并族前写入服务实例配置表的行
    """
    records = {}
    for conf in confs:
        storage_id = (conf.type or "").strip()
        if not storage_id:
            continue
        name = (conf.name or "").strip() or storage_id
        records[(storage_id, name)] = {
            "type": storage_id,
            "name": name,
            "enabled": True,
            "config": conf.config or {},
            "host_config": {_LEGACY_DEFAULT_FIELD: bool(conf.is_default)},
            "is_default_target": False,
        }
    for storage_id in dict.fromkeys(key[0] for key in records):
        siblings = [record for key, record in records.items() if key[0] == storage_id]
        marked = [
            record for record in siblings
            if record["host_config"][_LEGACY_DEFAULT_FIELD]
        ]
        chosen = marked[0] if marked else siblings[0]
        for record in siblings:
            record["host_config"] = {_LEGACY_DEFAULT_FIELD: record is chosen}
    return list(records.values())


# 对拍用的配置数据集，覆盖无名条目、无类型条目、同名覆盖、无默认与多默认
_EQUIVALENCE_CASES = (
    ("单份配置", [StorageConf(type="u115", name="主号", config={"k": "a"})]),
    ("未填实例名", [StorageConf(type="u115", config={"k": "a"})]),
    ("缺类型", [StorageConf(name="没有类型", config={"k": "a"})]),
    ("同类型同名覆盖", [
        StorageConf(type="u115", config={"k": "旧"}),
        StorageConf(type="u115", name="u115", config={"k": "新"}),
    ]),
    ("一份都没有自称默认", [
        StorageConf(type="u115", name="甲"),
        StorageConf(type="u115", name="乙"),
    ]),
    ("多份自称默认", [
        StorageConf(type="u115", name="甲", is_default=True),
        StorageConf(type="u115", name="乙", is_default=True),
    ]),
    ("跨类型各有默认", [
        StorageConf(type="u115", name="甲"),
        StorageConf(type="alist", name="乙", is_default=True),
        StorageConf(type="alist", name="丙"),
    ]),
    ("空配置", []),
)


@pytest.fixture
def storage_config():
    """提供存储配置读写服务，用例前后都把存储族配置清空。

    :return: 存储配置读写服务
    """
    helper = StorageHelper()
    helper.save_storagies([])
    yield helper
    helper.save_storagies([])


@pytest.mark.parametrize("label,confs", _EQUIVALENCE_CASES, ids=[
    case[0] for case in _EQUIVALENCE_CASES
])
def test_merged_shaping_matches_the_storage_specific_shaping(label, confs):
    """同一份配置数据经并族后的通用整形，与并族前的存储专用整形逐条等价。"""
    legacy = _legacy_storage_records(confs)
    merged = service_config_records(
        STORAGE_CAPABILITY, [conf.model_dump() for conf in confs]
    )

    assert len(merged) == len(legacy), label
    for merged_record, legacy_record in zip(merged, legacy):
        for field in ("type", "name", "enabled", "config", "host_config",
                      "is_default_target"):
            assert merged_record[field] == legacy_record[field], (label, field)


def test_storage_rows_never_occupy_the_family_default_target_column(storage_config):
    """存储的默认标记只落宿主载荷，族级默认调用目标列一行都不占。"""
    storage_config.save_storagies([
        StorageConf(type="u115", name="甲", is_default=True),
        StorageConf(type="alist", name="乙", is_default=True),
    ])

    payloads = get_configured_service_instance_configs().read(STORAGE_CAPABILITY)

    assert [item["is_default"] for item in payloads] == [True, True]
    assert [item["default"] for item in payloads] == [False, False]


def test_two_default_scopes_do_not_interfere(storage_config):
    """两种默认粒度各自生效：存储每类型一个，三族每族一个，互不占用对方的载体。"""
    asyncio.run(async_write_system_setting(SystemConfigKey.Downloaders, [
        {"name": "主力", "type": "qbittorrent", "enabled": True, "default": True},
        {"name": "备用", "type": "transmission", "enabled": True, "default": True},
    ]))
    storage_config.save_storagies([
        StorageConf(type="u115", name="甲", is_default=True),
        StorageConf(type="u115", name="乙", is_default=True),
        StorageConf(type="alist", name="丙"),
    ])
    try:
        downloaders = read_system_setting(SystemConfigKey.Downloaders)
        storages = read_system_setting(SystemConfigKey.Storages)

        # 族级：整族至多一条，多交的那条被裁掉
        assert [item["default"] for item in downloaders] == [True, False]
        # 类型级：每个类型恰好一条，且跨类型互不排斥
        assert {
            (item["type"], item["name"]): item["is_default"] for item in storages
        } == {
            ("u115", "甲"): True, ("u115", "乙"): False, ("alist", "丙"): True
        }
    finally:
        asyncio.run(async_write_system_setting(SystemConfigKey.Downloaders, None))


def test_family_default_target_stays_family_scoped():
    """三族的默认标记作用域仍是族，读的仍是外壳字段 default。"""
    assert service_default_scope(ModuleType.Downloader.value) is DefaultTargetScope.FAMILY
    assert service_default_scope(ModuleType.MediaServer.value) is DefaultTargetScope.FAMILY
    assert service_default_scope(ModuleType.Notification.value) is DefaultTargetScope.FAMILY
    assert service_default_scope(STORAGE_CAPABILITY) is DefaultTargetScope.TYPE

    selected = select_instance_configs(
        [
            DownloaderConf(name="主力", type="qbittorrent", enabled=True, default=True),
            DownloaderConf(name="备用", type="qbittorrent", enabled=True),
        ],
        "qbittorrent",
        capability=ModuleType.Downloader.value,
        multi_instance=False,
    )

    assert list(selected) == ["主力"]


def test_single_instance_storage_type_is_decided_by_the_type_level_default():
    """存储的单实例裁决读类型级默认标记，不读族级默认调用目标列。"""
    selected = select_instance_configs(
        [
            StorageConf(type="local", name="甲"),
            StorageConf(type="local", name="乙", is_default=True),
        ],
        "local",
        capability=STORAGE_CAPABILITY,
        multi_instance=False,
    )

    assert list(selected) == ["乙"]


def test_storage_instances_have_no_enable_switch():
    """存储族配置模型没有启用开关，配了即生效，不会被启用态筛掉。"""
    selected = select_instance_configs(
        [StorageConf(type="u115", name="甲"), StorageConf(type="u115", name="乙")],
        "u115",
        capability=STORAGE_CAPABILITY,
    )

    assert sorted(selected) == ["乙", "甲"]


@pytest.mark.parametrize("backend,multi_instance", [
    (LocalStorage, False),
    (AliPan, True),
    (Alist, True),
    (AlistGo, True),
    (Rclone, True),
    (SMB, True),
    (U115Pan, True),
])
def test_builtin_storage_types_declare_their_own_instance_count(backend, multi_instance):
    """七个内建存储各自声明能配几份：网盘与挂载多份，本地文件系统只有一份。"""
    assert backend.multi_instance is multi_instance
    assert StorageBase.multi_instance is True


def test_storage_is_a_registered_service_family():
    """存储已是登记在册的服务族，展示名称随之进入面向用户的提示。"""
    entry = service_family_registry.find(STORAGE_CAPABILITY)

    assert entry is not None
    assert entry.name == "存储"


def test_service_instance_declaration_cannot_take_over_the_storage_family():
    """服务实例声明不能声明存储类型，拒绝理由直接指向存储自己的钩子。"""
    violation = service_instance_declaration_violation(
        ServiceInstanceDeclaration(
            capability=STORAGE_CAPABILITY,
            type="demo_storage",
            name="演示存储",
            impl=dict,
        )
    )

    assert violation is not None
    assert "provides_storages()" in violation


def test_provides_storages_is_not_in_any_deprecation_flow():
    """存储钩子保留而不进废弃流程：它承载的是另一套构造协议，不是旧写法。

    废弃登记表是给插件作者看的对外契约，只登记上游已发行、社区确实在用的旧写法；
    `provides_storages()` 从未随发行版到达社区，把它写进去只会让废弃表变成本仓重构
    过程的流水账。
    """
    assert not [key for key in NOTICES if "storages" in key]
    assert hasattr(_PluginBase, "provides_storages")


def test_storage_declaration_carries_the_instance_count_and_contract():
    """存储声明自带份数与配置契约，取值不合法即拒绝登记。"""
    assert storage_declaration_violation(
        StorageDeclaration(schema="demo_storage", impl=_DemoStorage)
    ) is None
    assert storage_declaration_violation(
        StorageDeclaration(
            schema="demo_storage", impl=_DemoStorage, multi_instance="yes"
        )
    ).startswith("multi_instance")
    assert "不受支持" in storage_declaration_violation(
        StorageDeclaration(
            schema="demo_storage", impl=_DemoStorage,
            config_schema={"type": "object", "$ref": "#/x"},
        )
    )


def test_write_side_contract_applies_to_plugin_declared_storage_types(storage_config):
    """插件存储类型声明的配置契约在写入端即生效，畸形配置退回而不是落盘。"""
    service_instance_registry.register(
        capability=STORAGE_CAPABILITY,
        service_type="demo_storage",
        name="演示存储",
        owner="DemoStoragePlugin",
        factory=lambda conf: None,
        config_schema={
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
        },
    )
    try:
        violation = service_config_write_violation(STORAGE_CAPABILITY, [
            {"type": "demo_storage", "name": "甲", "config": {"token": 1}},
        ])

        assert violation is not None
        assert "演示存储" in violation
        assert service_config_write_violation(STORAGE_CAPABILITY, [
            {"type": "demo_storage", "name": "甲", "config": {"token": "t"}},
        ]) is None
    finally:
        service_instance_registry.unregister_owner("DemoStoragePlugin")


def test_storage_rows_record_the_plugin_that_provides_the_type(storage_config):
    """存储行按类型目录回填提供方，插件提供的类型不再一律记成内建。"""
    service_instance_registry.register(
        capability=STORAGE_CAPABILITY,
        service_type="demo_storage",
        name="演示存储",
        owner="DemoStoragePlugin",
        factory=lambda conf: None,
    )
    try:
        records = service_config_records(STORAGE_CAPABILITY, [
            {"type": "demo_storage", "name": "甲", "config": {}},
            {"type": "u115", "name": "乙", "config": {}},
        ])

        assert records[0]["provider"] == "DemoStoragePlugin"
        assert records[1]["provider"] is None
    finally:
        service_instance_registry.unregister_owner("DemoStoragePlugin")


class _DemoStorage(StorageBase):
    """契约合规的存储后端桩，全部抽象方法均已落地。"""

    schema = "demo_storage"

    def init_storage(self):
        """无需建立任何连接。"""

    def check(self) -> bool:
        """存储始终可用。"""
        return True

    def list(self, fileitem):
        """返回空列表。"""
        return []

    def create_folder(self, fileitem, name):
        """不提供实际创建。"""
        return None

    def get_folder(self, path):
        """不提供实际查询。"""
        return None

    def get_item(self, path):
        """不提供实际查询。"""
        return None

    def delete(self, fileitem):
        """删除始终成功。"""
        return True

    def rename(self, fileitem, name):
        """重命名始终成功。"""
        return True

    def download(self, fileitem, path=None):
        """不提供实际下载。"""
        return None

    def upload(self, fileitem, path, new_name=None):
        """不提供实际上传。"""
        return None

    def detail(self, fileitem):
        """原样返回文件项。"""
        return fileitem

    def copy(self, fileitem, path, new_name):
        """复制始终成功。"""
        return True

    def move(self, fileitem, path, new_name):
        """移动始终成功。"""
        return True

    def link(self, fileitem, target_file):
        """硬链接始终成功。"""
        return True

    def softlink(self, fileitem, target_file):
        """软链接始终成功。"""
        return True

    def usage(self):
        """不提供容量信息。"""
        return None
