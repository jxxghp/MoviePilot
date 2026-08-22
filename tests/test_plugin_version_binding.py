"""插件实例版本绑定的行为契约测试。

覆盖每个实例按自己绑定的版本加载、跟随开关在默认实例升级后触发实例级切换、
切换失败保持已生效版本并以旧版本回退重启、绑定的版本目录缺失时的回落，
以及安装第二个版本时的依赖交集预检与多版本准入判据。
"""

from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterator, List, Optional, Tuple

import pytest

from app.adapters.system.plugin.dependency import (
    find_version_dependency_conflicts,
    is_unsatisfiable,
)
from app.adapters.system.plugin.package import PluginPackageManager
from app.foundation.singleton import Singleton
from app.runtime.extensions import plugin_manager as plugin_manager_module
from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID
from app.runtime.extensions.lifecycle.layout import (
    plugin_version_dir_name,
    plugin_version_dirs,
    plugin_version_from_dir_name,
    read_plugin_versions_manifest,
    register_plugin_version,
    write_plugin_versions_manifest,
)
from app.runtime.extensions.lifecycle.storage import (
    PluginStorage,
    configure_plugin_storage,
    get_plugin_storage,
)
from app.runtime.extensions.plugin_manager import PluginManager
from app.startup.plugins_initializer import (
    plugin_multi_version_blockers,
    plugin_version_coexistence_rejection,
)

# 插件标识必须与插件主类名一致，源码目录名取其小写
PLUGIN_ID = "VersionedPlugin"
PLUGIN_DIR = PLUGIN_ID.lower()
SECOND_INSTANCE = "second"
SECOND_KEY = f"{PLUGIN_ID}@{SECOND_INSTANCE}"

_PLUGIN_SOURCE = '''
class {class_name}:
    plugin_name = "{class_name}"
    plugin_version = "{version}"

    def __init__(self):
        self.config = {{}}

    def init_plugin(self, config=None):
        {body}
        self.config = config or {{}}

    def get_state(self):
        return bool(self.config.get("enable"))

    def get_name(self):
        return self.plugin_name

    def stop_service(self):
        pass
'''


class _RecordingLogger:
    """按等级留存日志文本的日志替身。"""

    def __init__(self) -> None:
        """初始化各等级的日志缓冲。"""
        self.records: Dict[str, List[str]] = {
            "debug": [], "info": [], "warning": [], "warn": [], "error": []
        }

    def _record(self, level: str):
        """生成把消息写入指定等级缓冲的记录函数。

        :param level: 日志等级名
        :return: 记录函数
        """
        def write(message, *_args, **_kwargs) -> None:
            """留存一条日志文本。"""
            self.records[level].append(str(message))

        return write

    def __getattr__(self, name: str):
        """按等级名返回记录函数，未知等级按 info 处理。

        :param name: 属性名
        :return: 记录函数
        """
        if name.startswith("_"):
            raise AttributeError(name)
        return self._record(name if name in self.records else "info")

    def text(self, level: str) -> str:
        """把某个等级的全部日志拼成一段文本。

        :param level: 日志等级名
        :return: 该等级的日志文本
        """
        return "\n".join(self.records.get(level, []))


def _write_version(
    plugins_root: Path,
    version: str,
    *,
    plugin_dir: str = PLUGIN_DIR,
    class_name: str = PLUGIN_ID,
    body: str = "pass",
    register: bool = False,
    extra_files: Optional[Dict[str, str]] = None,
) -> Path:
    """在版本化布局下写入一个插件版本的源码。

    :param plugins_root: 插件根目录
    :param version: 版本号
    :param plugin_dir: 插件源码目录名
    :param class_name: 插件主类名
    :param body: init_plugin 首行插入的语句
    :param register: 是否把该版本登记为元信息中的当前版本
    :param extra_files: 版本目录下额外写入的文件，键为相对路径
    :return: 版本目录
    """
    plugin_root = plugins_root / plugin_dir
    dir_name = plugin_version_dir_name(version)
    version_dir = plugin_root / dir_name
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "__init__.py").write_text(
        _PLUGIN_SOURCE.format(class_name=class_name, version=version, body=body),
        encoding="utf-8",
    )
    for name, content in (extra_files or {}).items():
        target = version_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if register:
        register_plugin_version(plugin_root, version, dir_name, source="test")
    # 新版本目录要立刻可被 import，绕开查找器对目录列表的缓存
    importlib.invalidate_caches()
    return version_dir


@pytest.fixture
def plugins_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """把插件根目录指向临时目录，并保证测试后不残留插件模块。

    :param tmp_path: 用例级临时目录
    :param monkeypatch: 用例级补丁器
    :return: 临时插件根目录
    """
    monkeypatch.setattr(
        plugin_manager_module,
        "settings",
        SimpleNamespace(
            ROOT_PATH=tmp_path, DEBUG=False, DEV=False, PLUGIN_AUTO_RELOAD=False
        ),
    )
    root = tmp_path / "app" / "plugins"
    root.mkdir(parents=True)
    plugins_package = importlib.import_module("app.plugins")
    original_path = plugins_package.__path__
    plugins_package.__path__ = [*original_path, str(root)]
    importlib.invalidate_caches()
    yield root
    plugins_package.__path__ = original_path
    for module_name in [name for name in sys.modules if name.startswith("app.plugins.")]:
        sys.modules.pop(module_name, None)
    importlib.invalidate_caches()


@pytest.fixture
def version_store() -> Iterator[Dict[Tuple[str, str], Tuple[Optional[str], bool]]]:
    """把实例版本绑定接到内存字典，并在用例结束后复原全局钩子。

    :return: `(插件ID, 实例标识)` 到 `(已生效版本, 跟随开关)` 的映射
    """
    saved = (
        plugin_manager_module._plugin_instance_version_read,
        plugin_manager_module._plugin_instance_version_write,
        plugin_manager_module._plugin_instance_follow_write,
    )
    store: Dict[Tuple[str, str], Tuple[Optional[str], bool]] = {}

    def read_binding(plugin_id: str, instance_id: str):
        """读取内存中的实例版本绑定。"""
        return store.get((plugin_id, instance_id))

    def write_version(plugin_id: str, instance_id: str, version: str) -> None:
        """写入实例已生效的版本。"""
        _current, follow = store.get((plugin_id, instance_id), (None, True))
        store[(plugin_id, instance_id)] = (version, follow)

    def write_follow(plugin_id: str, instance_id: str, follow: bool) -> None:
        """写入实例的版本跟随开关。"""
        version, _current = store.get((plugin_id, instance_id), (None, True))
        store[(plugin_id, instance_id)] = (version, bool(follow))

    plugin_manager_module._configure_plugin_instance_version_binding(
        read_binding=read_binding,
        write_version=write_version,
        write_follow_default=write_follow,
    )
    yield store
    (
        plugin_manager_module._plugin_instance_version_read,
        plugin_manager_module._plugin_instance_version_write,
        plugin_manager_module._plugin_instance_follow_write,
    ) = saved


@pytest.fixture
def switch_notices() -> Iterator[List[Tuple[str, str]]]:
    """把版本切换失败的系统消息接到内存列表。

    :return: 收到的 `(标题, 正文)` 列表
    """
    saved = (
        plugin_manager_module._plugin_version_switch_notifier,
        plugin_manager_module._plugin_multi_version_probe,
    )
    notices: List[Tuple[str, str]] = []
    plugin_manager_module._configure_plugin_version_switch_notifier(
        lambda title, text: notices.append((title, text))
    )
    plugin_manager_module._configure_plugin_multi_version_probe(plugin_multi_version_blockers)
    yield notices
    (
        plugin_manager_module._plugin_version_switch_notifier,
        plugin_manager_module._plugin_multi_version_probe,
    ) = saved


@pytest.fixture
def instance_ids() -> List[str]:
    """给出该插件已登记的实例清单，用例可就地改写。

    :return: 实例标识列表
    """
    return [DEFAULT_INSTANCE_ID]


@pytest.fixture
def plugin_manager(
    plugins_root: Path, instance_ids: List[str], version_store, switch_notices
) -> Iterator[PluginManager]:
    """构造隔离的插件管理器，接好实例清单与数据库生命周期空钩子。

    :return: 可直接驱动版本绑定的插件管理器
    """
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    saved_storage = get_plugin_storage()
    saved_ports = (
        plugin_manager_module._plugin_database_ensure,
        plugin_manager_module._plugin_database_release,
        plugin_manager_module._plugin_database_destroy,
    )
    configure_plugin_storage(PluginStorage(
        read_config=lambda plugin_id, instance_id=None: {},
        list_instances=lambda plugin_id: list(instance_ids),
    ))
    plugin_manager_module._plugin_database_ensure = lambda _pid, _iid: None
    plugin_manager_module._plugin_database_release = lambda _pid: None
    plugin_manager_module._plugin_database_destroy = lambda _pid, _iid: None
    manager = PluginManager()
    yield manager
    manager.stop()
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    configure_plugin_storage(saved_storage)
    (
        plugin_manager_module._plugin_database_ensure,
        plugin_manager_module._plugin_database_release,
        plugin_manager_module._plugin_database_destroy,
    ) = saved_ports


@pytest.fixture
def package_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PluginPackageManager:
    """按启动组合根同款接线构造插件包管理器，运行目录与事务目录都在临时目录下。

    :return: 未接任何下载实现的包管理器，由用例注入市场下载替身
    """
    monkeypatch.setattr(
        "app.adapters.system.plugin.package.settings",
        SimpleNamespace(ROOT_PATH=tmp_path, TEMP_PATH=tmp_path / "temp"),
    )
    return PluginPackageManager(
        helper=SimpleNamespace(),
        version_dirs=plugin_version_dirs,
        coexistence_checker=plugin_version_coexistence_rejection,
        version_name_resolver=plugin_version_from_dir_name,
    )


def _market_helper(plugins_root: Path, files: Dict[str, str]) -> SimpleNamespace:
    """构造复刻市场安装行为的下载替身：整体清空插件目录后写入新内容。

    :param plugins_root: 插件根目录
    :param files: 新版本内容，键为相对插件目录的路径
    :return: 带 install 方法的下载替身
    """
    def install(pid: str, repo_url: str, **_kwargs) -> Tuple[bool, str]:
        """清空插件目录并写入本次下载的源码。"""
        plugin_dir = plugins_root / pid.lower()
        shutil.rmtree(plugin_dir, ignore_errors=True)
        plugin_dir.mkdir(parents=True)
        for name, content in files.items():
            target = plugin_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return True, "安装成功"

    return SimpleNamespace(install=install)


# 一、按实例解析版本


@pytest.mark.parametrize("instance_ids", [[DEFAULT_INSTANCE_ID, SECOND_INSTANCE]])
def test_sibling_instances_load_their_own_bound_versions(
    plugins_root, plugin_manager, version_store
):
    """两个实例绑不同版本时各自加载到正确的类对象，互不干扰。"""
    _write_version(plugins_root, "1.2.0")
    _write_version(plugins_root, "2.0.0", register=True)
    version_store[(PLUGIN_ID, SECOND_INSTANCE)] = ("1.2.0", False)

    plugin_manager.start(pid=PLUGIN_ID)

    default_instance = plugin_manager.running_plugins[PLUGIN_ID]
    second_instance = plugin_manager.running_plugins[SECOND_KEY]
    assert type(default_instance).plugin_version == "2.0.0"
    assert type(second_instance).plugin_version == "1.2.0"
    assert type(default_instance) is not type(second_instance)
    # 成功启动后才登记已生效版本；未变化的实例不重复写入
    assert version_store[(PLUGIN_ID, DEFAULT_INSTANCE_ID)] == ("2.0.0", True)
    assert version_store[(PLUGIN_ID, SECOND_INSTANCE)] == ("1.2.0", False)


def test_never_started_instance_takes_the_current_version(
    plugins_root, plugin_manager, version_store
):
    """从未成功启动过的实例按插件当前版本启动，并登记该版本。"""
    _write_version(plugins_root, "1.0.0", register=False)
    _write_version(plugins_root, "3.1.0", register=True)

    plugin_manager.start(pid=PLUGIN_ID)

    assert type(plugin_manager.running_plugins[PLUGIN_ID]).plugin_version == "3.1.0"
    assert version_store[(PLUGIN_ID, DEFAULT_INSTANCE_ID)] == ("3.1.0", True)


@pytest.mark.parametrize("instance_ids", [[DEFAULT_INSTANCE_ID, SECOND_INSTANCE]])
def test_missing_bound_version_dir_falls_back_and_logs(
    plugins_root, plugin_manager, version_store, monkeypatch
):
    """绑定的版本目录不存在时告警回落到当前版本，并如实登记回落后的版本。"""
    _write_version(plugins_root, "1.0.0", register=True)
    version_store[(PLUGIN_ID, SECOND_INSTANCE)] = ("9.9.9", False)
    recorder = _RecordingLogger()
    monkeypatch.setattr(plugin_manager_module, "logger", recorder)

    plugin_manager.start(pid=PLUGIN_ID)

    assert type(plugin_manager.running_plugins[SECOND_KEY]).plugin_version == "1.0.0"
    assert version_store[(PLUGIN_ID, SECOND_INSTANCE)] == ("1.0.0", False)
    warning = recorder.text("warning")
    assert "9.9.9" in warning and "版本目录不存在" in warning and "1.0.0" in warning
    # 兄弟实例照常按当前版本启动
    assert PLUGIN_ID in plugin_manager.running_plugins


# 二、跟随开关的切换


@pytest.mark.parametrize("instance_ids", [[DEFAULT_INSTANCE_ID, SECOND_INSTANCE]])
def test_following_instance_switches_after_default_upgrade(
    plugins_root, plugin_manager, version_store
):
    """默认实例升级成功后，跟随开关为真的实例被停止再启动并切到新版本。"""
    _write_version(plugins_root, "1.0.0", register=True)
    plugin_manager.start(pid=PLUGIN_ID)
    assert version_store[(PLUGIN_ID, SECOND_INSTANCE)] == ("1.0.0", True)
    stale_instance = plugin_manager.running_plugins[SECOND_KEY]

    # 装入新版本并把默认实例升级过去
    _write_version(plugins_root, "2.0.0", register=True)
    plugin_manager.stop(PLUGIN_ID, DEFAULT_INSTANCE_ID)
    plugin_manager.start(PLUGIN_ID, DEFAULT_INSTANCE_ID)
    assert version_store[(PLUGIN_ID, DEFAULT_INSTANCE_ID)] == ("2.0.0", True)

    switched = plugin_manager.restart_version_following_instances(PLUGIN_ID)

    assert switched == [SECOND_INSTANCE]
    assert version_store[(PLUGIN_ID, SECOND_INSTANCE)] == ("2.0.0", True)
    new_instance = plugin_manager.running_plugins[SECOND_KEY]
    assert new_instance is not stale_instance
    assert type(new_instance).plugin_version == "2.0.0"


@pytest.mark.parametrize("instance_ids", [[DEFAULT_INSTANCE_ID, SECOND_INSTANCE]])
def test_pinned_instance_is_not_dragged_by_the_default_upgrade(
    plugins_root, plugin_manager, version_store
):
    """跟随开关为假的实例不随默认实例升级切换。"""
    _write_version(plugins_root, "1.0.0", register=True)
    _write_version(plugins_root, "2.0.0", register=False)
    version_store[(PLUGIN_ID, SECOND_INSTANCE)] = ("1.0.0", False)
    version_store[(PLUGIN_ID, DEFAULT_INSTANCE_ID)] = ("2.0.0", True)
    plugin_manager.start(pid=PLUGIN_ID)

    assert plugin_manager.restart_version_following_instances(PLUGIN_ID) == []
    assert version_store[(PLUGIN_ID, SECOND_INSTANCE)] == ("1.0.0", False)


# 三、切换失败的回退


@pytest.mark.parametrize("instance_ids", [[DEFAULT_INSTANCE_ID, SECOND_INSTANCE]])
def test_failed_switch_keeps_version_and_restarts_the_old_one(
    plugins_root, plugin_manager, version_store, switch_notices
):
    """切换失败时已生效版本保持旧值、以旧版本重启，兄弟实例不受影响。"""
    _write_version(plugins_root, "1.0.0", register=True)
    _write_version(plugins_root, "2.0.0", body='raise RuntimeError("boom")')
    plugin_manager.start(pid=PLUGIN_ID)
    default_instance = plugin_manager.running_plugins[PLUGIN_ID]

    binding = plugin_manager.set_plugin_instance_version(
        PLUGIN_ID, SECOND_INSTANCE, version="2.0.0", follow_default_version=False
    )

    assert binding["plugin_version"] == "1.0.0"
    assert version_store[(PLUGIN_ID, SECOND_INSTANCE)] == ("1.0.0", False)
    assert type(plugin_manager.running_plugins[SECOND_KEY]).plugin_version == "1.0.0"
    assert switch_notices and "2.0.0" in switch_notices[0][1]
    # 兄弟实例的运行对象原样保留
    assert plugin_manager.running_plugins[PLUGIN_ID] is default_instance


@pytest.mark.parametrize("instance_ids", [[DEFAULT_INSTANCE_ID, SECOND_INSTANCE]])
def test_successful_switch_records_the_new_version(
    plugins_root, plugin_manager, version_store, switch_notices
):
    """切换成功后写入新的已生效版本，不发系统消息。"""
    _write_version(plugins_root, "1.0.0", register=True)
    _write_version(plugins_root, "2.0.0")
    plugin_manager.start(pid=PLUGIN_ID)

    binding = plugin_manager.set_plugin_instance_version(
        PLUGIN_ID, SECOND_INSTANCE, version="2.0.0", follow_default_version=False
    )

    assert binding["plugin_version"] == "2.0.0"
    assert binding["follow_default_version"] is False
    assert type(plugin_manager.running_plugins[SECOND_KEY]).plugin_version == "2.0.0"
    assert switch_notices == []


def test_set_version_rejects_versions_that_are_not_installed(plugins_root, plugin_manager):
    """绑定到未安装的版本时被拒绝。"""
    _write_version(plugins_root, "1.0.0", register=True)
    plugin_manager.start(pid=PLUGIN_ID)

    with pytest.raises(ValueError, match="未安装版本"):
        plugin_manager.set_plugin_instance_version(
            PLUGIN_ID, DEFAULT_INSTANCE_ID, version="8.8.8", follow_default_version=False
        )


def test_set_version_rejects_unknown_plugin_and_instance(plugins_root, plugin_manager):
    """插件或实例不存在时抛出 LookupError。"""
    _write_version(plugins_root, "1.0.0", register=True)
    plugin_manager.start(pid=PLUGIN_ID)

    with pytest.raises(LookupError):
        plugin_manager.set_plugin_instance_version("_NoSuchPlugin", DEFAULT_INSTANCE_ID)
    with pytest.raises(LookupError):
        plugin_manager.set_plugin_instance_version(PLUGIN_ID, "ghost")


def test_list_plugin_versions_reports_installed_and_bindings(
    plugins_root, plugin_manager, version_store
):
    """版本总览列出磁盘上的已装版本与各实例绑定情况。"""
    _write_version(plugins_root, "1.0.0", register=False)
    _write_version(plugins_root, "2.0.0", register=True)
    plugin_manager.start(pid=PLUGIN_ID)

    overview = plugin_manager.list_plugin_versions(PLUGIN_ID)

    assert overview["plugin_id"] == PLUGIN_ID
    assert overview["current_version"] == "2.0.0"
    assert [item["version"] for item in overview["installed_versions"]] == ["1.0.0", "2.0.0"]
    assert [item["is_current"] for item in overview["installed_versions"]] == [False, True]
    assert overview["instances"] == [
        {
            "instance_id": DEFAULT_INSTANCE_ID,
            "instance_key": PLUGIN_ID,
            "plugin_version": "2.0.0",
            "follow_default_version": True,
            "target_version": "2.0.0",
            "running": True,
        }
    ]


# 四、安装第二个版本时的预检


def test_second_version_with_empty_dependency_intersection_is_rejected(
    plugins_root, package_manager
):
    """两个版本对同一依赖的约束交集为空时拒绝安装，并指明冲突的包。"""
    _write_version(plugins_root, "1.0.0", extra_files={"requirements.txt": "requests>=2.30\n"})
    package_manager._helper = _market_helper(
        plugins_root,
        {
            "__init__.py": _PLUGIN_SOURCE.format(
                class_name=PLUGIN_ID, version="2.0.0", body="pass"
            ),
            "requirements.txt": "requests<2.0\n",
        },
    )

    state, message = package_manager.install(
        plugin_id=PLUGIN_ID, repo_url="", version_dir="v2_0_0"
    )

    assert state is False
    assert "requests" in message and "无法并存" in message
    assert "1.0.0" in message and "2.0.0" in message
    # 已装版本原样保留，本次下载的内容不落地
    assert (plugins_root / PLUGIN_DIR / "v1_0_0" / "__init__.py").is_file()
    assert not (plugins_root / PLUGIN_DIR / "v2_0_0").exists()


def test_compatible_dependencies_allow_the_second_version(plugins_root, package_manager):
    """两个版本的依赖约束仍有交集时正常安装，已装版本不受影响。"""
    _write_version(plugins_root, "1.0.0", extra_files={"requirements.txt": "requests>=2.30\n"})
    package_manager._helper = _market_helper(
        plugins_root,
        {
            "__init__.py": _PLUGIN_SOURCE.format(
                class_name=PLUGIN_ID, version="2.0.0", body="pass"
            ),
            "requirements.txt": "requests<3.0\n",
        },
    )

    state, _message = package_manager.install(
        plugin_id=PLUGIN_ID, repo_url="", version_dir="v2_0_0"
    )

    assert state is True
    assert (plugins_root / PLUGIN_DIR / "v1_0_0" / "__init__.py").is_file()
    assert (plugins_root / PLUGIN_DIR / "v2_0_0" / "__init__.py").is_file()


def test_self_referential_absolute_import_blocks_the_second_version(
    plugins_root, package_manager
):
    """存在自引用绝对导入的插件不允许装第二个版本。"""
    _write_version(plugins_root, "1.0.0")
    package_manager._helper = _market_helper(
        plugins_root,
        {
            "__init__.py": (
                f"from app.plugins.{PLUGIN_DIR}.helper import helper\n"
                + _PLUGIN_SOURCE.format(class_name=PLUGIN_ID, version="2.0.0", body="pass")
            ),
            "helper.py": "helper = 1\n",
        },
    )

    state, message = package_manager.install(
        plugin_id=PLUGIN_ID, repo_url="", version_dir="v2_0_0"
    )

    assert state is False
    assert "多版本并存" in message and "自引用绝对导入" in message
    assert not (plugins_root / PLUGIN_DIR / "v2_0_0").exists()


def test_shared_base_model_blocks_the_second_version(plugins_root, package_manager):
    """在宿主共享声明基类上定义模型的插件不允许装第二个版本。"""
    _write_version(plugins_root, "1.0.0")
    package_manager._helper = _market_helper(
        plugins_root,
        {
            "__init__.py": (
                "from app.db import Base\n\n\n"
                "class VersionedRecord(Base):\n"
                "    pass\n\n\n"
                + _PLUGIN_SOURCE.format(class_name=PLUGIN_ID, version="2.0.0", body="pass")
            ),
        },
    )

    state, message = package_manager.install(
        plugin_id=PLUGIN_ID, repo_url="", version_dir="v2_0_0"
    )

    assert state is False
    assert "多版本并存" in message and "VersionedRecord" in message


def test_first_version_install_skips_the_coexistence_precheck(plugins_root, package_manager):
    """插件此前没有任何版本时不做并存预检，写法问题不阻断单版本安装。"""
    package_manager._helper = _market_helper(
        plugins_root,
        {
            "__init__.py": (
                f"from app.plugins.{PLUGIN_DIR}.helper import helper\n"
                + _PLUGIN_SOURCE.format(class_name=PLUGIN_ID, version="1.0.0", body="pass")
            ),
            "helper.py": "helper = 1\n",
        },
    )

    state, _message = package_manager.install(
        plugin_id=PLUGIN_ID, repo_url="", version_dir="v1_0_0"
    )

    assert state is True
    assert (plugins_root / PLUGIN_DIR / "v1_0_0" / "__init__.py").is_file()


# 五、版本约束交集判定


@pytest.mark.parametrize(
    "specifiers, empty",
    [
        ([">=2.30", "<2.0"], True),
        (["==1.0", "==2.0"], True),
        ([">=2.0", "==1.5"], True),
        ([">1.0", "<=1.0"], True),
        ([">=1.0", "<=1.0"], False),
        ([">=2.30", "<3.0"], False),
        (["==1.0", ">=1.0"], False),
        ([">=1.0", ""], False),
        (["", ""], False),
        (["~=1.4", "<1.0"], True),
        (["!=1.0", ">=1.0"], False),
        (["==1.*", ">=1.2"], False),
    ],
)
def test_specifier_intersection_emptiness(specifiers, empty):
    """同一包多条版本约束的交集判定覆盖锁定值与上下界两条路径。"""
    assert is_unsatisfiable(specifiers) is empty


def test_conflicts_only_cover_packages_declared_by_both_versions():
    """只有两个版本都声明的包才参与交集预检。"""
    conflicts = find_version_dependency_conflicts(
        {"requests": [">=2.30"], "only_old": ["<1.0"]},
        {"requests": ["<2.0"], "only_new": [">=9"]},
    )

    assert [conflict.package for conflict in conflicts] == ["requests"]
    assert conflicts[0].existing_specifier == ">=2.30"
    assert conflicts[0].new_specifier == "<2.0"


# 六、版本回收（依赖实例绑定的判据，纯目录层面的判据见 test_plugin_version_recycle.py）


def _stamp_installed_at(plugin_root: Path, stamps: Dict[str, str]) -> None:
    """把已装版本清单里各版本的登记时间改写为指定值，消除真实时钟带来的顺序不确定性。

    :param plugin_root: 插件源码根目录
    :param stamps: 版本号到 ISO8601 时间字符串的映射
    """
    manifest = read_plugin_versions_manifest(plugin_root)
    for entry in manifest["versions"]:
        if entry["version"] in stamps:
            entry["installed_at"] = stamps[entry["version"]]
    write_plugin_versions_manifest(plugin_root, manifest["versions"], manifest["current"])


@pytest.mark.parametrize("instance_ids", [[DEFAULT_INSTANCE_ID, SECOND_INSTANCE]])
def test_follow_instance_effective_version_is_protected_even_when_stale(
    plugins_root, plugin_manager, version_store
):
    """跟随实例还没来得及切换时，它自己那一行记的旧版本仍受保护，不被回收。

    跟随开关为真只决定「期望版本」读默认实例的已生效版本，不代表该实例当下就在
    跑那个版本；引用集合必须同时纳入该实例自己的已生效版本，否则会把它正在
    实际运行的旧版本删掉。这里复现真实的「默认实例单独升级、跟随实例还没重启」
    时序（与 test_following_instance_switches_after_default_upgrade 同款）：只重启
    默认实例，SECOND 保持不动，自己那一行仍记着旧版本。登记时间显式错开，让
    保留窗口（默认最近 2 个）覆盖不到最旧的 1.0.0，这样 1.0.0 能留下就只能是
    「被实例引用」这条判据在起作用。
    """
    _write_version(plugins_root, "1.0.0", register=True)
    plugin_manager.start(pid=PLUGIN_ID)
    assert version_store[(PLUGIN_ID, SECOND_INSTANCE)] == ("1.0.0", True)

    # 装入两个新版本并只重启默认实例；SECOND 尚未跟着切换，自己那一行还记着 1.0.0
    _write_version(plugins_root, "2.0.0", register=True)
    _write_version(plugins_root, "3.0.0", register=True)
    plugin_root = plugins_root / PLUGIN_DIR
    _stamp_installed_at(
        plugin_root,
        {
            "1.0.0": "2020-01-01T00:00:00+00:00",
            "2.0.0": "2020-06-01T00:00:00+00:00",
            "3.0.0": "2021-01-01T00:00:00+00:00",
        },
    )
    plugin_manager.stop(PLUGIN_ID, DEFAULT_INSTANCE_ID)
    plugin_manager.start(PLUGIN_ID, DEFAULT_INSTANCE_ID)
    assert version_store[(PLUGIN_ID, DEFAULT_INSTANCE_ID)] == ("3.0.0", True)
    assert version_store[(PLUGIN_ID, SECOND_INSTANCE)] == ("1.0.0", True)

    results = plugin_manager.recycle_plugin_versions(PLUGIN_ID)

    outcome = results[PLUGIN_ID]
    assert "1.0.0" not in outcome["removed"]
    assert (plugin_root / "v1_0_0").is_dir()
    assert "被实例引用" in outcome["kept"]["1.0.0"]


def test_recycle_removes_unreferenced_out_of_window_version(
    plugins_root, plugin_manager, version_store
):
    """没有实例引用、超出保留窗口的旧版本经管理器统一回收接口被删除。"""
    _write_version(plugins_root, "1.0.0", register=True)
    _write_version(plugins_root, "2.0.0", register=True)
    _write_version(plugins_root, "3.0.0", register=True)
    plugin_root = plugins_root / PLUGIN_DIR
    _stamp_installed_at(
        plugin_root,
        {
            "1.0.0": "2020-01-01T00:00:00+00:00",
            "2.0.0": "2020-06-01T00:00:00+00:00",
            "3.0.0": "2021-01-01T00:00:00+00:00",
        },
    )
    plugin_manager.start(pid=PLUGIN_ID)

    results = plugin_manager.recycle_plugin_versions(PLUGIN_ID)

    assert results[PLUGIN_ID]["removed"] == ["1.0.0"]
    assert not (plugin_root / "v1_0_0").exists()
    assert (plugin_root / "v2_0_0").is_dir()
    assert (plugin_root / "v3_0_0").is_dir()


def test_recycle_unknown_plugin_raises(plugins_root, plugin_manager):
    """显式指定不存在的插件时抛出 LookupError。"""
    _write_version(plugins_root, "1.0.0", register=True)
    plugin_manager.start(pid=PLUGIN_ID)

    with pytest.raises(LookupError):
        plugin_manager.recycle_plugin_versions("_NoSuchPlugin")


def test_recycle_bulk_mode_survives_a_single_plugin_failure(
    plugins_root, plugin_manager, version_store, monkeypatch
):
    """批量回收（供启动流程调用）时单个插件出错不影响调用方，只记错误日志。"""
    _write_version(plugins_root, "1.0.0", register=True)
    plugin_manager.start(pid=PLUGIN_ID)
    recorder = _RecordingLogger()
    monkeypatch.setattr(plugin_manager_module, "logger", recorder)

    def _boom(*_args, **_kwargs):
        """模拟单个插件回收过程中抛出的意外异常。"""
        raise RuntimeError("disk gone")

    monkeypatch.setattr(plugin_manager_module, "recycle_plugin_version_directories", _boom)

    results = plugin_manager.recycle_plugin_versions()

    assert results == {}
    assert "版本回收出错" in recorder.text("error")


def _stage_three_versions_with_stale_first(plugins_root: Path, plugin_manager: PluginManager) -> Path:
    """装三个版本并把登记时间错开，使最旧的 1.0.0 落在保留窗口之外。

    :param plugins_root: 插件根目录
    :param plugin_manager: 已接线的插件管理器
    :return: 插件源码根目录
    """
    _write_version(plugins_root, "1.0.0", register=True)
    _write_version(plugins_root, "2.0.0", register=True)
    _write_version(plugins_root, "3.0.0", register=True)
    plugin_root = plugins_root / PLUGIN_DIR
    _stamp_installed_at(
        plugin_root,
        {
            "1.0.0": "2020-01-01T00:00:00+00:00",
            "2.0.0": "2020-06-01T00:00:00+00:00",
            "3.0.0": "2021-01-01T00:00:00+00:00",
        },
    )
    plugin_manager.start(pid=PLUGIN_ID)
    return plugin_root


def test_recycle_skips_when_version_bindings_cannot_be_read(
    plugins_root, plugin_manager, version_store, monkeypatch
):
    """版本绑定读不出来时整体跳过回收，不拿空引用集合去删。

    「读失败」与「确实没有实例引用」在结果上无从区分。若把读失败吞成空集合，
    一次瞬时的数据库读错就会删掉实例钉住的旧版本，而版本目录删掉即无从恢复，
    因此失效方向必须是不删。断言本该被删的 1.0.0 仍在。
    """
    plugin_root = _stage_three_versions_with_stale_first(plugins_root, plugin_manager)
    recorder = _RecordingLogger()
    monkeypatch.setattr(plugin_manager_module, "logger", recorder)

    def _boom(*_args, **_kwargs):
        """模拟版本绑定读取时的数据库异常。"""
        raise RuntimeError("database is locked")

    monkeypatch.setattr(plugin_manager_module, "_plugin_instance_version_read", _boom)

    results = plugin_manager.recycle_plugin_versions(PLUGIN_ID)

    assert results == {}
    assert (plugin_root / "v1_0_0").is_dir()
    assert "版本回收出错" in recorder.text("error")


def test_recycle_skips_when_instance_list_cannot_be_read(
    plugins_root, plugin_manager, version_store, monkeypatch
):
    """实例清单读不出来时同样跳过回收，理由与版本绑定读失败一致。"""
    plugin_root = _stage_three_versions_with_stale_first(plugins_root, plugin_manager)
    recorder = _RecordingLogger()
    monkeypatch.setattr(plugin_manager_module, "logger", recorder)

    def _boom(_plugin_id):
        """模拟实例清单读取时的数据库异常。"""
        raise RuntimeError("database is locked")

    configure_plugin_storage(PluginStorage(
        read_config=lambda plugin_id, instance_id=None: {},
        list_instances=_boom,
    ))

    results = plugin_manager.recycle_plugin_versions(PLUGIN_ID)

    assert results == {}
    assert (plugin_root / "v1_0_0").is_dir()
    assert "版本回收出错" in recorder.text("error")
