"""存储并入服务实例族后的等价与边界守护测试。

存储与下载器、媒体服务器、消息渠道并成一族：一张表、一套整形、一套筛选、一套默认
调用目标裁决，声明也共用同一条 `provides_service_instances()` 钩子。本文件盯住四件事：
并族前后同一份配置数据整形结果逐条等价；族级默认调用目标对存储确实生效且与三族同规格；
裸令牌兼容指针与族级默认互不干扰；存储类型的声明与构造协议在合并后的声明面上仍然成立。

**族级默认与兼容指针回答的不是同一个问题。** 族级默认回答「调用没指定存储时用哪个」，
整族至多一个，落 ``serviceconfig.is_default_target`` 专列；兼容指针回答「存量路径
``u115:/media`` 没写实例名时落到哪一份」，每个存储类型各一个，落宿主载荷。一个实例可以
同时是两者，也可以只是其中之一。
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.application.downloader import DownloaderHelper
from app.application.mediaserver import MediaServerHelper
from app.application.notification import NotificationHelper
from app.application.service_config import (
    async_write_system_setting,
    get_configured_service_instance_configs,
    read_system_setting,
)
from app.application.storage import StorageHelper
from app.application.storage_config import select_storage_config
from app.modules._base.storage import StorageBase
from app.sdk.extension import _PluginBase
from app.runtime.deprecation.notices import NOTICES
from app.runtime.extensions.contract.declaration import ServiceInstanceDeclaration
from app.runtime.extensions.projection.module_declarations import builtin_multi_instance
from app.runtime.extensions.admission.service_instance import (
    service_instance_declaration_violation,
)
from app.runtime.extensions.service_config import (
    STORAGE_CAPABILITY,
    select_instance_configs,
    service_bare_token_field,
)
from app.runtime.extensions.admission.service_config import (
    service_config_records,
    service_config_write_violation,
)
from app.runtime.extensions.registry.service_family import service_family_registry
from app.runtime.extensions.registry.service_instance import service_instance_registry
from app.runtime.extensions.service_registry import ServiceBaseHelper
from app.schemas.system import DownloaderConf, StorageConf
from app.schemas.types import ModuleType, SystemConfigKey

# 并族前存储配置的专用整形规则，逐字取自并族前的 storage_config_records，用作对拍基准；
# 那时这个标记还叫 is_default，本文件按它今天的名字写，取值口径与当初逐字节相同
_BARE_TOKEN_FIELD = "bare_token_target"


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
            "host_config": {_BARE_TOKEN_FIELD: bool(conf.bare_token_target)},
            "is_default_target": False,
        }
    for storage_id in dict.fromkeys(key[0] for key in records):
        siblings = [record for key, record in records.items() if key[0] == storage_id]
        marked = [
            record for record in siblings
            if record["host_config"][_BARE_TOKEN_FIELD]
        ]
        chosen = marked[0] if marked else siblings[0]
        for record in siblings:
            record["host_config"] = {_BARE_TOKEN_FIELD: record is chosen}
    return list(records.values())


# 对拍用的配置数据集，覆盖无名条目、无类型条目、同名覆盖、无兼容指针与多份自称
_EQUIVALENCE_CASES = (
    ("单份配置", [StorageConf(type="u115", name="主号", config={"k": "a"})]),
    ("未填实例名", [StorageConf(type="u115", config={"k": "a"})]),
    ("缺类型", [StorageConf(name="没有类型", config={"k": "a"})]),
    ("同类型同名覆盖", [
        StorageConf(type="u115", config={"k": "旧"}),
        StorageConf(type="u115", name="u115", config={"k": "新"}),
    ]),
    ("一份都没有自称承接", [
        StorageConf(type="u115", name="甲"),
        StorageConf(type="u115", name="乙"),
    ]),
    ("多份自称承接", [
        StorageConf(type="u115", name="甲", bare_token_target=True),
        StorageConf(type="u115", name="乙", bare_token_target=True),
    ]),
    ("跨类型各有兼容指针", [
        StorageConf(type="u115", name="甲"),
        StorageConf(type="alist", name="乙", bare_token_target=True),
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


def test_storage_family_default_lands_on_the_family_column(storage_config):
    """存储的默认调用目标与三族同规格：落 is_default_target 专列，整族至多一条。"""
    storage_config.save_storagies([
        StorageConf(type="u115", name="甲", default=True),
        StorageConf(type="alist", name="乙", default=True),
    ])

    payloads = get_configured_service_instance_configs().read(STORAGE_CAPABILITY)

    assert [item["default"] for item in payloads] == [True, False]


def test_bare_token_pointer_is_not_the_family_default(storage_config):
    """兼容指针只回答地址补全：每类型各一个，且不占用族级默认调用目标列。"""
    storage_config.save_storagies([
        StorageConf(type="u115", name="甲", bare_token_target=True),
        StorageConf(type="alist", name="乙", bare_token_target=True),
    ])

    payloads = get_configured_service_instance_configs().read(STORAGE_CAPABILITY)

    assert [item["bare_token_target"] for item in payloads] == [True, True]
    assert [item["default"] for item in payloads] == [False, False]


def test_one_instance_can_be_both_the_family_default_and_the_bare_token_target(
    storage_config
):
    """同一实例可以同时是族级默认与所在类型的兼容指针，两个标记各自落各自的位置。"""
    storage_config.save_storagies([
        StorageConf(type="u115", name="甲", default=True, bare_token_target=True),
        StorageConf(type="u115", name="乙"),
    ])

    payloads = get_configured_service_instance_configs().read(STORAGE_CAPABILITY)
    marks = {
        item["name"]: (item["default"], item["bare_token_target"])
        for item in payloads
    }

    assert marks == {"甲": (True, True), "乙": (False, False)}


def test_the_two_marks_can_land_on_different_instances(storage_config):
    """族级默认与兼容指针可以落在不同实例上：一个回答用哪个，一个回答裸令牌指谁。"""
    storage_config.save_storagies([
        StorageConf(type="u115", name="甲", bare_token_target=True),
        StorageConf(type="u115", name="乙", default=True),
        StorageConf(type="alist", name="丙"),
    ])

    payloads = get_configured_service_instance_configs().read(STORAGE_CAPABILITY)
    marks = {
        item["name"]: (item["default"], item["bare_token_target"])
        for item in payloads
    }

    assert marks == {"甲": (False, True), "乙": (True, False), "丙": (False, True)}


def test_two_families_do_not_interfere(storage_config):
    """族级默认在两族各自成立、互不排斥；存储的兼容指针不参与族级那一条。"""
    asyncio.run(async_write_system_setting(SystemConfigKey.Downloaders, [
        {"name": "主力", "type": "qbittorrent", "enabled": True, "default": True},
        {"name": "备用", "type": "transmission", "enabled": True, "default": True},
    ]))
    storage_config.save_storagies([
        StorageConf(type="u115", name="甲", default=True),
        StorageConf(type="u115", name="乙", default=True),
        StorageConf(type="alist", name="丙"),
    ])
    try:
        downloaders = read_system_setting(SystemConfigKey.Downloaders)
        storages = read_system_setting(SystemConfigKey.Storages)

        # 族级：两族各自裁出至多一条，多交的那条被裁掉
        assert [item["default"] for item in downloaders] == [True, False]
        assert [item["default"] for item in storages] == [True, False, False]
        # 类型级：兼容指针每个类型恰好一条，跨类型互不排斥
        assert {
            (item["type"], item["name"]): item["bare_token_target"]
            for item in storages
        } == {
            ("u115", "甲"): True, ("u115", "乙"): False, ("alist", "丙"): True
        }
    finally:
        asyncio.run(async_write_system_setting(SystemConfigKey.Downloaders, None))


def test_only_storage_carries_a_bare_token_pointer():
    """只有存储族有裸令牌兼容指针：其余三族的调用地址不会出现「写了类型没写实例」。"""
    assert service_bare_token_field(STORAGE_CAPABILITY) == "bare_token_target"
    assert service_bare_token_field(ModuleType.Downloader.value) is None
    assert service_bare_token_field(ModuleType.MediaServer.value) is None
    assert service_bare_token_field(ModuleType.Notification.value) is None


@pytest.mark.parametrize("capability,confs,expected", [
    (
        ModuleType.Downloader.value,
        [
            DownloaderConf(name="主力", type="qbittorrent", enabled=True, default=True),
            DownloaderConf(name="备用", type="qbittorrent", enabled=True),
        ],
        ["主力"],
    ),
    (
        STORAGE_CAPABILITY,
        [
            StorageConf(type="local", name="甲"),
            StorageConf(type="local", name="乙", default=True),
        ],
        ["乙"],
    ),
], ids=["下载器", "存储"])
def test_single_instance_ruling_is_the_same_for_storage_and_the_three_families(
    capability, confs, expected
):
    """单实例类型的裁决四族同一条：读族级默认调用目标，不读别的标记。"""
    selected = select_instance_configs(
        confs,
        confs[0].type,
        capability=capability,
        multi_instance=False,
    )

    assert list(selected) == expected


def test_single_instance_storage_without_a_family_default_stops_the_type():
    """裁决不出族级默认时单实例存储类型整体停摆并报错，绝不取第一个。"""
    with pytest.raises(LookupError) as excinfo:
        select_instance_configs(
            [
                StorageConf(type="local", name="甲", bare_token_target=True),
                StorageConf(type="local", name="乙"),
            ],
            "local",
            capability=STORAGE_CAPABILITY,
            multi_instance=False,
        )

    assert "甲" in str(excinfo.value) and "乙" in str(excinfo.value)


def test_bare_token_resolution_reads_the_pointer_not_the_family_default():
    """裸令牌只认兼容指针：族级默认是同类型另一份时，裸令牌仍落到兼容指针那一份。"""
    confs = [
        StorageConf(type="u115", name="甲", bare_token_target=True),
        StorageConf(type="u115", name="乙", default=True),
    ]

    assert select_storage_config(confs, None).name == "甲"
    assert select_storage_config(confs, "乙").name == "乙"


@pytest.mark.parametrize("confs", [
    [StorageConf(type="u115", name="甲"), StorageConf(type="u115", name="乙")],
    [
        StorageConf(type="u115", name="甲", bare_token_target=True),
        StorageConf(type="u115", name="乙", bare_token_target=True),
    ],
], ids=["无人自称", "多份自称"])
def test_bare_token_yields_instead_of_taking_the_first(confs):
    """无兼容指针或多份自称时裸令牌让出，绝不按顺序取第一份。"""
    assert select_storage_config(confs, None) is None


def test_storage_instances_have_no_enable_switch():
    """存储族配置模型没有启用开关，配了即生效，不会被启用态筛掉。"""
    selected = select_instance_configs(
        [StorageConf(type="u115", name="甲"), StorageConf(type="u115", name="乙")],
        "u115",
        capability=STORAGE_CAPABILITY,
    )

    assert sorted(selected) == ["乙", "甲"]


@pytest.mark.parametrize("storage_id,multi_instance", [
    ("local", False),
    ("alipan", True),
    ("alist", True),
    ("alistgo", True),
    ("rclone", True),
    ("smb", True),
    ("u115", True),
])
def test_builtin_storage_types_declare_their_own_instance_count(storage_id, multi_instance):
    """七个内建存储各自在清单里声明能配几份：网盘与挂载多份，本地文件系统只有一份。"""
    assert builtin_multi_instance(STORAGE_CAPABILITY, storage_id) is multi_instance
    assert not hasattr(StorageBase, "multi_instance")


def test_storage_is_a_registered_service_family():
    """存储已是登记在册的服务族，展示名称随之进入面向用户的提示。"""
    entry = service_family_registry.find(STORAGE_CAPABILITY)

    assert entry is not None
    assert entry.name == "存储"


def test_service_instance_declaration_accepts_the_storage_family():
    """存储类型经服务实例声明登记，不写工厂即合契约——宿主提供默认工厂。"""
    assert service_instance_declaration_violation(
        ServiceInstanceDeclaration(
            capability=STORAGE_CAPABILITY,
            type="demo_storage",
            name="演示存储",
            impl=_DemoStorage,
        )
    ) is None


def test_storage_family_rejects_an_impl_that_is_not_a_storage_backend():
    """存储族的 impl 按继承判定：不是存储基类子类的实现整条声明被拒。"""
    violation = service_instance_declaration_violation(
        ServiceInstanceDeclaration(
            capability=STORAGE_CAPABILITY,
            type="demo_storage",
            name="演示存储",
            impl=dict,
        )
    )

    assert violation is not None
    assert "StorageBase" in violation


def test_storage_family_accepts_a_declared_factory_alongside_the_backend():
    """存储族里 factory 是可选项：给了就走它，impl 仍要用来回答令牌指的实体是谁。"""
    assert service_instance_declaration_violation(
        ServiceInstanceDeclaration(
            capability=STORAGE_CAPABILITY,
            type="demo_storage",
            name="演示存储",
            impl=_DemoStorage,
            factory=lambda conf: None,
        )
    ) is None
    assert service_instance_declaration_violation(
        ServiceInstanceDeclaration(
            capability=STORAGE_CAPABILITY,
            type="demo_storage",
            name="演示存储",
            impl=_DemoStorage,
            factory=lambda: None,
        )
    ) is not None


def test_the_storage_hook_is_gone_and_leaves_no_deprecation_entry():
    """存储专用钩子随并族删除，且不进废弃流程。

    废弃登记表是给插件作者看的对外契约，只登记上游已发行、社区确实在用的旧写法；
    `provides_storages()` 只存在于本重构线，从未随任何发行版到达社区，删除不会让已
    发布插件失效，把它写进废弃表只会让废弃表变成本仓重构过程的流水账。
    """
    assert not hasattr(_PluginBase, "provides_storages")
    assert hasattr(_PluginBase, "provides_service_instances")
    assert not [key for key in NOTICES if "storages" in key]


def test_storage_declaration_carries_the_instance_count_and_contract():
    """存储类型声明自带份数与配置契约，取值不合法即拒绝登记。"""
    assert service_instance_declaration_violation(
        ServiceInstanceDeclaration(
            capability=STORAGE_CAPABILITY,
            type="demo_storage",
            name="演示存储",
            impl=_DemoStorage,
            multi_instance="yes",
        )
    ).startswith("multi_instance")
    assert "不受支持" in service_instance_declaration_violation(
        ServiceInstanceDeclaration(
            capability=STORAGE_CAPABILITY,
            type="demo_storage",
            name="演示存储",
            impl=_DemoStorage,
            config_schema={"type": "object", "$ref": "#/x"},
        )
    )


def test_storage_config_schema_has_no_reserved_property_name():
    """存储的构造不经关键字展开，因此契约可以声明名为 name 的字段。"""
    assert service_instance_declaration_violation(
        ServiceInstanceDeclaration(
            capability=STORAGE_CAPABILITY,
            type="demo_storage",
            name="演示存储",
            impl=_DemoStorage,
            config_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        )
    ) is None


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


def _probe_instance(conf) -> SimpleNamespace:
    """
    按单条实例配置构造一个可分辨的存储实例替身

    :param conf: 单条存储实例配置
    :return: 带实例名与配置内容的替身对象
    """
    return SimpleNamespace(name=conf.name, config=dict(conf.config or {}))


@pytest.fixture
def probe_storage_type(storage_config):
    """在服务实例类型目录里登记一个探针存储类型，用例结束后回收。

    :return: 探针存储的类型标识
    """
    owner = "StorageDiscoveryProbePlugin"
    service_instance_registry.register(
        capability=STORAGE_CAPABILITY,
        service_type="probe_storage",
        name="探针存储",
        owner=owner,
        factory=_probe_instance,
    )
    try:
        yield "probe_storage"
    finally:
        service_instance_registry.unregister_owner(owner)


def test_four_families_share_one_service_discovery_base():
    """四族共用同一个服务帮助基类，存储不再自带一套取服务实现。"""
    for helper in (DownloaderHelper, MediaServerHelper, NotificationHelper, StorageHelper):
        assert issubclass(helper, ServiceBaseHelper)


def test_storage_services_come_from_the_shared_discovery(storage_config, probe_storage_type):
    """存储按配置扇出的实例经与三族同一条服务发现取用，一份配置对上一个具名实例。"""
    storage_config.save_storagies([
        StorageConf(type=probe_storage_type, name="主号", config={"账号": "甲"}),
        StorageConf(type=probe_storage_type, name="备号", config={"账号": "乙"}),
    ])
    helper = StorageHelper()

    services = helper.get_services(type_filter=probe_storage_type)

    assert sorted(services) == ["主号", "备号"]
    assert services["主号"].type == probe_storage_type
    assert services["主号"].instance.config == {"账号": "甲"}
    assert services["备号"].instance.config == {"账号": "乙"}
    assert helper.get_service("主号").instance is services["主号"].instance


def test_storage_configs_count_as_enabled_without_an_enable_switch(
    storage_config, probe_storage_type
):
    """存储配置模型没有启用开关，配了即生效，服务发现不得据此把整族滤掉。"""
    storage_config.save_storagies([
        StorageConf(type=probe_storage_type, name="主号", config={"账号": "甲"}),
    ])

    assert "enabled" not in StorageConf.model_fields
    assert list(StorageHelper().get_configs()) == ["主号"]


def test_token_addressing_stays_independent_of_service_discovery(
    storage_config, probe_storage_type
):
    """按令牌寻址仍是另一层：令牌缺实例段时按兼容指针补全，服务发现按实例名索引，答不了地址。"""
    storage_config.save_storagies([
        StorageConf(type=probe_storage_type, name="主号", config={"账号": "甲"}),
        StorageConf(
            type=probe_storage_type, name="备号", bare_token_target=True,
            config={"账号": "乙"},
        ),
    ])
    helper = StorageHelper()

    assert helper.get_storage(f"{probe_storage_type}@主号").config == {"账号": "甲"}
    assert helper.get_storage(probe_storage_type).name == "备号"
    assert helper.get_service(probe_storage_type) is None


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
