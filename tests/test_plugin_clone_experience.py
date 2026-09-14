"""分身创建体验测试：自动分配后缀、按 ID 恢复已停用的分身与提交前的后缀校验。

这些行为大多只在 ``build_plugin_runtime`` 装配出来的整体上成立：分身服务自己只认注入
进来的端口，「哪些 ID 算被占用」是组合根的决定，因而占用判据必须在这一层验证，而不是
在用例里另接一套判据自证。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from pydantic import ValidationError

from app.runtime.extensions.plugin.manager import PluginManager
from app.runtime.extensions.plugin.runtime import (
    PluginRuntime,
    PluginRuntimeEnvironment,
    build_plugin_runtime,
)
from app.runtime.extensions.plugin.storage import (
    PluginInstanceDirectory,
    PluginStorage,
)
from app.schemas.plugin import PluginCloneRequest, PluginInstance, PluginRuntimeStatus
from app.schemas.types import SystemConfigKey


class DemoPlugin:
    """充当源插件本体的最小插件类，只需要能被注册表登记。"""


def _logger() -> SimpleNamespace:
    """提供运行时构造所需的最小日志对象。"""
    return SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )


class _World:
    """一套全内存的插件世界，暴露用例要断言的每一份真实状态。"""

    def __init__(self) -> None:
        """建立空的实例表、配置表与运行态记录。"""
        self.rows: dict[str, PluginInstance] = {}
        self.configs: dict[str, dict] = {}
        self.installed: list[str] = []
        self.plugins_root = Path("/nonexistent-plugins-root")
        self.reloaded: list[str] = []
        self.removed: list[str] = []
        self.status = PluginRuntimeStatus.ACTIVE
        self.probe_delay = 0.0
        self.save_failure: Optional[Exception] = None
        self.on_remove: Optional[Callable[[], None]] = None
        self.runtime: Optional[PluginRuntime] = None

    def clone(self, **kwargs: Any) -> tuple[bool, str]:
        """按默认展示信息发起一次分身创建。"""
        assert self.runtime is not None
        params: dict[str, Any] = {"name": "", "description": ""}
        params.update(kwargs)
        return self.runtime.clone.clone(**params)

    def restorable(self) -> list[dict[str, Any]]:
        """调用管理器上的可恢复清单投影。

        ``get_restorable_plugin_instances`` 是一段纯投影，只用到实例表与配置表两个
        属性；用一个替身 self 调用它，既验证真实投影逻辑，又不必把 PluginManager
        这个单例连同它的文件监控与线程池一起启动起来。
        """
        assert self.runtime is not None
        stand_in = SimpleNamespace(
            _plugin_instance_store=self.runtime.instances,
            _plugin_config_store=self.runtime.configs,
        )
        return PluginManager.get_restorable_plugin_instances(stand_in, "DemoPlugin")


def _build_world(
    *,
    rows: Optional[dict[str, PluginInstance]] = None,
    configs: Optional[dict[str, dict]] = None,
    installed: Optional[list[str]] = None,
    plugins_root: Optional[Path] = None,
) -> _World:
    """按给定实例行、配置、安装清单与插件包目录装配一个全内存运行时。"""
    world = _World()
    world.rows.update(rows or {})
    world.configs.update(configs or {})
    world.installed.extend(installed or [])
    if plugins_root is not None:
        world.plugins_root = plugins_root

    values: dict = {SystemConfigKey.UserInstalledPlugins: world.installed}

    def _read_row(instance_id: str) -> Optional[PluginInstance]:
        """读取实例行，可按需放慢以拉开并发创建的竞争窗口。"""
        if world.probe_delay:
            time.sleep(world.probe_delay)
        return world.rows.get(instance_id)

    def _set_enabled(instance_id: str, is_enabled: bool) -> bool:
        """就地翻转启用位，行不存在时报告未写入。"""
        record = world.rows.get(instance_id)
        if record is None:
            return False
        world.rows[instance_id] = record.model_copy(update={"is_enabled": is_enabled})
        return True

    def _save_row(instance: PluginInstance) -> None:
        """写入实例行；预置的故障只对下一次写入生效。

        一次性是为了并发用例：同一个世界里只让先到的那个请求落库失败，后到的请求
        必须能正常写进去，才谈得上验证前者的清理有没有越权删掉后者的行。
        """
        failure, world.save_failure = world.save_failure, None
        if failure is not None:
            raise failure
        world.rows[instance.instance_id] = instance

    def _remove_runtime(instance_id: str) -> None:
        """摘掉运行态，并在回滚的第一步给用例一个确定的交错点。"""
        world.removed.append(instance_id)
        if world.on_remove is not None:
            world.on_remove()

    storage = PluginStorage(
        read=values.get,
        write=values.__setitem__,
        read_config=world.configs.get,
        write_config=lambda instance_id, config: world.configs.__setitem__(
            instance_id,
            config,
        ),
        delete_config=lambda instance_id: world.configs.pop(instance_id, None) is not None,
    )
    directory = PluginInstanceDirectory(
        get=_read_row,
        list_all=lambda: list(world.rows.values()),
        list_by_source=lambda source_plugin_id: [
            record
            for record in world.rows.values()
            if record.source_plugin_id == source_plugin_id
        ],
        save=_save_row,
        delete=lambda instance_id: world.rows.pop(instance_id, None) is not None,
        list_enabled=lambda: [
            record for record in world.rows.values() if record.is_enabled
        ],
        set_enabled=_set_enabled,
    )

    def _reload(plugin_id: str) -> PluginRuntimeStatus:
        """记录一次定向重载并回报当前世界设定的结果。"""
        world.reloaded.append(plugin_id)
        return world.status

    environment = PluginRuntimeEnvironment(
        plugins_root=world.plugins_root,
        storage=lambda: storage,
        instance_directory=lambda: directory,
        system=lambda: SimpleNamespace(),
        database=lambda: SimpleNamespace(),
        catalog_factory=lambda _mapper: SimpleNamespace(),
        import_preparer=lambda **_kwargs: None,
        import_scanner=lambda **_kwargs: None,
        auth_level=lambda: 0,
        remote_entry=lambda _plugin_id, _page: "",
        development=lambda: False,
        logger=_logger(),
        set_default_target=lambda _plugin_id, _instance_id: True,
        clear_default_target=lambda _plugin_id: None,
    )
    runtime = build_plugin_runtime(
        SimpleNamespace(
            reload_plugin=_reload,
            remove_plugin=_remove_runtime,
            get_plugin_remote_entry=lambda _plugin_id, _page: "",
            _run_file_watcher=lambda: None,
            get_plugins_from_market=lambda *_args, **_kwargs: None,
            async_get_plugins_from_market=lambda *_args, **_kwargs: None,
        ),
        environment,
        tool_build_max_attempts=1,
    )
    # 源插件本体必须在类注册表里：分身共享它的源码，它缺席就谈不上给它建分身
    runtime.registry.classes["DemoPlugin"] = DemoPlugin
    world.runtime = runtime
    return world


def _disabled_clone(instance_id: str, **fields: Any) -> PluginInstance:
    """构造一行已停用的分身，模拟「停用后设置仍留存」的状态。"""
    payload: dict[str, Any] = {
        "instance_id": instance_id,
        "source_plugin_id": "DemoPlugin",
        "is_enabled": False,
    }
    payload.update(fields)
    return PluginInstance(**payload)


# --------------------------------------------------------------------------- #
# 自动分配后缀
# --------------------------------------------------------------------------- #


def test_auto_allocated_suffix_starts_right_after_the_host_instance():
    """不填后缀时从 2 起算：本体在用户眼里就是第 1 个实例，分身接着往下排。"""
    world = _build_world()

    success, instance_id = world.clone(plugin_id="DemoPlugin")

    assert success is True
    assert instance_id == "DemoPlugin2"
    assert world.rows["DemoPlugin2"].is_enabled is True


def test_auto_allocated_suffix_skips_a_disabled_clone_row():
    """已停用的分身同样占号，自动分配不得挑中它。

    重用它的 ID 是「恢复」而不是新建：用户点的是「新建一个」，却拿到上一个分身留下
    的业务参数，等于凭空继承了一份他没打算要的配置。
    """
    world = _build_world(
        rows={"DemoPlugin2": _disabled_clone("DemoPlugin2")},
        configs={"DemoPlugin2": {"token": "旧的"}},
    )

    success, instance_id = world.clone(plugin_id="DemoPlugin")

    assert success is True
    assert instance_id == "DemoPlugin3"
    # 旧行原样留着，没有被这次新建顶掉
    assert world.rows["DemoPlugin2"].is_enabled is False
    assert world.configs["DemoPlugin2"] == {"token": "旧的"}


def test_auto_allocated_suffix_skips_an_enabled_clone_row():
    """在册的分身占号，自动分配跳过它继续往下找。"""
    world = _build_world(
        rows={
            "DemoPlugin2": _disabled_clone("DemoPlugin2", is_enabled=True),
            "DemoPlugin3": _disabled_clone("DemoPlugin3", is_enabled=True),
        },
    )

    success, instance_id = world.clone(plugin_id="DemoPlugin")

    assert success is True
    assert instance_id == "DemoPlugin4"


def test_auto_allocated_suffix_skips_an_installed_physical_plugin():
    """装过但此刻未装载的物理插件同样占号。

    它不在类注册表里，只看运行态会把 ``DemoPlugin2`` 判成空位，新分身于是顶着一个
    真实插件的身份建出来，那个插件下次装载时两者撞在同一个 ID 上。
    """
    world = _build_world(installed=["DemoPlugin", "DemoPlugin2"])

    success, instance_id = world.clone(plugin_id="DemoPlugin")

    assert success is True
    assert instance_id == "DemoPlugin3"


def test_auto_allocated_suffix_skips_a_plugin_package_present_on_disk(tmp_path):
    """磁盘上留着同名插件包时不得占用该号，否则那个插件以后再也装不回来。

    卸载不删源码，包目录会一直留着；它既不在类注册表里，也不在安装清单里，只看这两
    处会把它判成空位。分身占了这个号之后，那个插件重装时实例行的归属列对不上，写入
    会被持久化层直接拒绝。
    """
    (tmp_path / "demoplugin2").mkdir()
    world = _build_world(plugins_root=tmp_path)

    success, instance_id = world.clone(plugin_id="DemoPlugin")

    assert success is True
    assert instance_id == "DemoPlugin3"


def test_auto_allocated_suffix_skips_a_currently_loaded_plugin_class():
    """运行期已登记同名类时不得占用该号。"""
    world = _build_world()
    world.runtime.registry.classes["DemoPlugin2"] = DemoPlugin

    success, instance_id = world.clone(plugin_id="DemoPlugin")

    assert success is True
    assert instance_id == "DemoPlugin3"


def test_auto_allocation_keeps_looking_past_a_long_run_of_taken_numbers():
    """已登记分身多到超过固定探测窗口时，仍要挑出真正最小的可用序号。

    探测次数若定死成一个常数，分身数量越过它之后每次自动分配都报「后缀已耗尽」，
    可实际上下一个号就空着；用户此时只能改为手填一个号，而手填走的又是同一套判据。
    """
    taken = {
        f"DemoPlugin{index}": _disabled_clone(f"DemoPlugin{index}", is_enabled=True)
        for index in range(2, 1002)
    }
    world = _build_world(rows=taken)

    success, instance_id = world.clone(plugin_id="DemoPlugin")

    assert (success, instance_id) == (True, "DemoPlugin1002")


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_suffix_falls_back_to_automatic_allocation(blank):
    """后缀留空的几种写法都走自动分配，而不是拼出一个等于源插件 ID 的实例。"""
    world = _build_world()

    success, instance_id = world.clone(plugin_id="DemoPlugin", suffix=blank)

    assert success is True
    assert instance_id == "DemoPlugin2"


# --------------------------------------------------------------------------- #
# 判存：同一个占用判据挡住显式后缀
# --------------------------------------------------------------------------- #


def test_explicit_suffix_colliding_with_an_enabled_clone_is_rejected():
    """显式后缀撞上在册分身时拒绝，且不得覆盖它的描述符。"""
    world = _build_world(
        rows={"DemoPlugin2": _disabled_clone("DemoPlugin2", is_enabled=True, plugin_name="在跑的")},
    )

    success, message = world.clone(plugin_id="DemoPlugin", suffix="2", name="新的")

    assert success is False
    assert "已存在" in message
    assert world.rows["DemoPlugin2"].plugin_name == "在跑的"


def test_explicit_suffix_colliding_with_another_sources_clone_is_rejected():
    """归属对不上的停用行不算可恢复：它是别的源插件的分身，不能被这里改嫁。"""
    world = _build_world(
        rows={
            "DemoPlugin2": PluginInstance(
                instance_id="DemoPlugin2",
                source_plugin_id="OtherPlugin",
                is_enabled=False,
            ),
        },
    )

    success, message = world.clone(plugin_id="DemoPlugin", suffix="2")

    assert success is False
    assert "已存在" in message
    assert world.rows["DemoPlugin2"].source_plugin_id == "OtherPlugin"


def test_a_clone_cannot_be_used_as_a_clone_source():
    """分身不能再生分身：它共享源插件的源码，自己并不是一份源码。"""
    world = _build_world(rows={"DemoPlugin2": _disabled_clone("DemoPlugin2", is_enabled=True)})
    world.runtime.registry.classes["DemoPlugin2"] = DemoPlugin

    success, message = world.clone(plugin_id="DemoPlugin2", suffix="3")

    assert success is False
    assert "不能作为分身来源" in message
    assert "DemoPlugin" in message


def test_repeating_the_same_create_is_rejected_rather_than_overwriting():
    """同一个后缀重复提交两次，第二次确定地被判成已存在。"""
    world = _build_world()

    first = world.clone(plugin_id="DemoPlugin", suffix="Work", name="第一次")
    second = world.clone(plugin_id="DemoPlugin", suffix="Work", name="第二次")

    assert first == (True, "DemoPluginwork")
    assert second[0] is False
    assert "已存在" in second[1]
    assert world.rows["DemoPluginwork"].plugin_name == "第一次"


# --------------------------------------------------------------------------- #
# 按 ID 恢复已停用的分身
# --------------------------------------------------------------------------- #


def test_restoring_a_disabled_clone_brings_back_its_config_and_display_info():
    """按 ID 恢复就是把那一行重新置为启用，配置与展示信息原样回来。

    停用从不删行，业务参数一直挂在上面；恢复因而不能是「新建一行再按源插件模板铺一份
    配置」，那会把用户特意留着的东西盖掉。
    """
    world = _build_world(
        rows={
            "DemoPlugin2": _disabled_clone(
                "DemoPlugin2",
                plugin_name="夜间任务",
                plugin_desc="只在夜里跑",
                plugin_icon="night.png",
            ),
        },
        configs={
            "DemoPlugin": {"token": "源插件的"},
            "DemoPlugin2": {"token": "分身自己的", "cron": "0 3 * * *"},
        },
    )

    success, instance_id = world.clone(plugin_id="DemoPlugin", suffix="2")

    assert (success, instance_id) == (True, "DemoPlugin2")
    restored = world.rows["DemoPlugin2"]
    assert restored.is_enabled is True
    assert restored.plugin_name == "夜间任务"
    assert restored.plugin_desc == "只在夜里跑"
    assert restored.plugin_icon == "night.png"
    # 配置没有被源插件模板顶掉，这正是用户要拿回来的东西
    assert world.configs["DemoPlugin2"] == {"token": "分身自己的", "cron": "0 3 * * *"}
    assert world.reloaded == ["DemoPlugin2"]


def test_restoring_accepts_a_new_display_name_while_keeping_the_config():
    """恢复时可以顺手改名，改的只是展示信息，业务参数仍然沿用。"""
    world = _build_world(
        rows={"DemoPlugin2": _disabled_clone("DemoPlugin2", plugin_name="旧名字")},
        configs={"DemoPlugin2": {"token": "留着"}},
    )

    success, _instance_id = world.clone(
        plugin_id="DemoPlugin",
        suffix="2",
        name="新名字",
    )

    assert success is True
    assert world.rows["DemoPlugin2"].plugin_name == "新名字"
    assert world.configs["DemoPlugin2"] == {"token": "留着"}


def test_restore_previous_false_rebuilds_the_config_from_the_source_template():
    """显式要求全新时按源插件模板重建配置，并保持业务开关关闭待用户配置。"""
    world = _build_world(
        rows={"DemoPlugin2": _disabled_clone("DemoPlugin2", plugin_name="旧名字")},
        configs={
            "DemoPlugin": {"enable": True, "token": "源插件的"},
            "DemoPlugin2": {"token": "该被丢弃"},
        },
    )

    success, _instance_id = world.clone(
        plugin_id="DemoPlugin",
        suffix="2",
        name="全新",
        restore_previous=False,
    )

    assert success is True
    assert world.configs["DemoPlugin2"] == {
        "enable": False,
        "enabled": False,
        "token": "源插件的",
    }
    assert world.rows["DemoPlugin2"].plugin_name == "全新"


@pytest.mark.parametrize("template", [None, {}])
def test_restore_previous_false_clears_the_old_config_when_the_source_has_none(template):
    """源插件没有配置或配置为空时，按模板重建同样要把旧配置清掉。

    两种情形下模板里都没有可继承的业务参数；若只是跳过写入，用户明确要求的「重建一份
    全新配置」会变成「原样沿用旧配置」，那些旧参数在重载后继续生效，且毫无提示。
    """
    configs: dict[str, dict] = {"DemoPlugin2": {"token": "该被丢弃"}}
    if template is not None:
        configs["DemoPlugin"] = template
    world = _build_world(
        rows={"DemoPlugin2": _disabled_clone("DemoPlugin2")},
        configs=configs,
    )

    success, _instance_id = world.clone(
        plugin_id="DemoPlugin",
        suffix="2",
        restore_previous=False,
    )

    assert success is True
    assert "DemoPlugin2" not in world.configs


def _world_with_a_disabled_clone_and_a_foreign_occupant(kind: str, tmp_path: Path) -> _World:
    """建出「停用分身行还在，同一个 ID 又被某个真实插件占住」的世界。

    停用从不删行，那一行可以在表里躺很久；这段时间里同名的真实插件完全可能出现——
    磁盘上被放进一个同名插件包、安装清单里多出一条、或者它已经装载进类注册表。
    """
    rows = {"DemoPlugin2": _disabled_clone("DemoPlugin2", plugin_name="夜间任务")}
    configs = {"DemoPlugin2": {"token": "必须留着"}}
    if kind == "disk-package":
        (tmp_path / "demoplugin2").mkdir()
        return _build_world(rows=rows, configs=configs, plugins_root=tmp_path)
    if kind == "installed-entry":
        return _build_world(
            rows=rows,
            configs=configs,
            installed=["DemoPlugin", "DemoPlugin2"],
        )
    world = _build_world(rows=rows, configs=configs)
    world.runtime.registry.classes["DemoPlugin2"] = DemoPlugin
    return world


@pytest.mark.parametrize("kind", ["disk-package", "installed-entry", "loaded-class"])
def test_restoring_still_refuses_an_id_a_real_plugin_already_holds(kind, tmp_path):
    """恢复只豁免待恢复的那一行自身，另外三类占用者仍然要挡。

    豁免写成「只要停用行在就全放行」的话，恢复会绕过全部判存：该 ID 上压着的真实
    插件身份被分身顶掉，那个插件此后既装不回来，实例行的归属列也对不上。
    """
    world = _world_with_a_disabled_clone_and_a_foreign_occupant(kind, tmp_path)

    success, message = world.clone(plugin_id="DemoPlugin", suffix="2")

    assert success is False
    assert "占用" in message
    # 被拒绝的恢复不得改动那一行，也不得把它拉起来跑
    assert world.rows["DemoPlugin2"].is_enabled is False
    assert world.configs["DemoPlugin2"] == {"token": "必须留着"}
    assert world.reloaded == []


def test_failed_restore_only_puts_the_row_back_to_disabled():
    """恢复失败只把启用位退回停用，不得连同用户留存的配置一起毁掉。

    那一行是用户特意留着的，不是本次创建的产物；一次加载失败就删掉它，等于让用户为
    一个可重试的故障付出丢数据的代价。
    """
    world = _build_world(
        rows={"DemoPlugin2": _disabled_clone("DemoPlugin2", plugin_name="夜间任务")},
        configs={"DemoPlugin2": {"token": "必须留着"}},
    )
    world.status = PluginRuntimeStatus.LOAD_FAILED

    success, message = world.clone(plugin_id="DemoPlugin", suffix="2")

    assert success is False
    assert "加载失败" in message
    assert world.rows["DemoPlugin2"].is_enabled is False
    assert world.rows["DemoPlugin2"].plugin_name == "夜间任务"
    assert world.configs["DemoPlugin2"] == {"token": "必须留着"}
    assert world.removed == ["DemoPlugin2"]


def test_failed_fresh_creation_still_removes_the_row_it_created():
    """新建失败仍按老规矩连行带配置抹掉：它整个是本次的产物，留着只是垃圾。"""
    world = _build_world(configs={"DemoPlugin": {"token": "源插件的"}})
    world.status = PluginRuntimeStatus.LOAD_FAILED

    success, message = world.clone(plugin_id="DemoPlugin", suffix="Work")

    assert success is False
    assert "加载失败" in message
    assert "DemoPluginwork" not in world.rows
    assert "DemoPluginwork" not in world.configs


def test_a_failed_row_write_is_reported_and_cleaned_up_like_a_failed_load():
    """实例行落库失败按失败回执返回并清理，不把异常抛给调用方。

    落库是本次创建写出的第一样东西，它与备配置、首次加载同属一次创建；把它留在异常
    边界之外，写到一半失败时既没有回滚、也没有 ``(False, 原因)``，只有一个异常冒到
    上层，而半写的实例行仍留在表里。
    """
    world = _build_world(configs={"DemoPlugin": {"token": "源插件的"}})
    world.save_failure = RuntimeError("实例表不可写")

    success, message = world.clone(plugin_id="DemoPlugin", suffix="Work")

    assert success is False
    assert "实例表不可写" in message
    assert world.rows == {}
    assert world.configs == {"DemoPlugin": {"token": "源插件的"}}
    assert world.reloaded == []
    assert world.removed == ["DemoPluginwork"]


def test_a_failed_row_write_while_restoring_keeps_the_stored_settings():
    """恢复途中落库失败同样只把启用位退回停用，用户留存的配置不受牵连。"""
    world = _build_world(
        rows={"DemoPlugin2": _disabled_clone("DemoPlugin2", plugin_name="夜间任务")},
        configs={"DemoPlugin2": {"token": "必须留着"}},
    )
    world.save_failure = RuntimeError("实例表不可写")

    success, message = world.clone(plugin_id="DemoPlugin", suffix="2")

    assert success is False
    assert "实例表不可写" in message
    assert world.rows["DemoPlugin2"].is_enabled is False
    assert world.rows["DemoPlugin2"].plugin_name == "夜间任务"
    assert world.configs["DemoPlugin2"] == {"token": "必须留着"}


def test_a_failed_row_write_never_rolls_back_another_requests_instance():
    """落库失败的清理只许清掉自己这次的预留，不得抹掉另一个请求刚建好的同 ID 实例。

    清理一旦跑在占位锁之外，判据就只剩「ID 相同」：先到的请求落库失败、锁随异常释放，
    后到的同后缀请求立刻把这个 ID 占下来并成功落库，随后前者按同一个 ID 执行回滚，
    删掉的是后者的实例行、配置与运行态。
    """
    world = _build_world(configs={"DemoPlugin": {"token": "源插件的"}})
    # 只让先到的请求落库失败，后到的请求必须能正常建成
    world.save_failure = RuntimeError("实例表不可写")
    rollback_started = threading.Event()
    later_request_done = threading.Event()
    outcomes: dict[str, tuple[bool, str]] = {}

    def _hold_until_the_later_request_lands() -> None:
        """卡在先到请求的回滚第一步，直到后到的请求把自己的行写完。

        交错由事件定序，不靠抢跑：清理若在锁外，后者此刻必然能落库，这一等确定等得到；
        清理留在锁内，后者进不来，这一等走超时返回，两种实现下的顺序都是确定的。
        """
        rollback_started.set()
        later_request_done.wait(timeout=1)

    world.on_remove = _hold_until_the_later_request_lands

    def _first() -> None:
        """先到的请求：落库失败，随后执行清理。"""
        outcomes["first"] = world.clone(plugin_id="DemoPlugin", suffix="2")

    def _later() -> None:
        """后到的请求：同一个后缀，落库成功。"""
        outcomes["later"] = world.clone(plugin_id="DemoPlugin", suffix="2")
        later_request_done.set()

    first = threading.Thread(target=_first)
    first.start()
    assert rollback_started.wait(timeout=10)
    later = threading.Thread(target=_later)
    later.start()
    for thread in (later, first):
        thread.join(timeout=20)

    assert outcomes["first"][0] is False
    assert "实例表不可写" in outcomes["first"][1]
    assert outcomes["later"] == (True, "DemoPlugin2")
    # 后到请求建出来的这一行不是前者的产物，前者的清理无权碰它
    assert "DemoPlugin2" in world.rows
    assert world.rows["DemoPlugin2"].is_enabled is True
    assert world.configs["DemoPlugin2"] == {
        "enable": False,
        "enabled": False,
        "token": "源插件的",
    }


# --------------------------------------------------------------------------- #
# 校验在提交前拦截
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "suffix",
    ["a-b", "带中文", "work!", "with space", "x" * 21],
)
def test_illegal_suffix_is_rejected_by_the_request_schema(suffix):
    """非法后缀在请求解析阶段就被拦下，根本到不了会写库的那一层。"""
    with pytest.raises(ValidationError):
        PluginCloneRequest(suffix=suffix)


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_suffix_is_normalized_to_automatic_allocation_by_the_schema(blank):
    """留空的三种写法在请求层归一为 None，不会撞上格式校验。"""
    assert PluginCloneRequest(suffix=blank).suffix is None


def test_request_schema_trims_and_keeps_a_legal_suffix():
    """合法后缀去掉首尾空白后原样保留，大小写留给运行时归一。"""
    assert PluginCloneRequest(suffix="  Work  ").suffix == "Work"


def test_restore_previous_defaults_to_reusing_the_stored_settings():
    """默认沿用留存设置：静默丢弃用户数据不能是默认行为。"""
    assert PluginCloneRequest().restore_previous is True


def test_an_overlong_composed_instance_id_is_rejected_before_any_write():
    """拼出来的实例 ID 超长时在落库之前就被拒绝，不留下半个实例。

    非法 ID 若等到持久化层才报错，意味着已经写出一行、还要靠回滚去擦掉它；把判定
    提到构造阶段，失败路径上根本没有东西需要清理。
    """
    long_id = "D" + "e" * 119
    world = _build_world()
    world.runtime.registry.classes[long_id] = DemoPlugin

    success, message = world.clone(plugin_id=long_id, suffix="x" * 20)

    assert success is False
    assert "不合法" in message
    assert world.rows == {}
    assert world.configs == {}
    assert world.removed == []


# --------------------------------------------------------------------------- #
# 并发创建
# --------------------------------------------------------------------------- #


def test_concurrent_auto_allocation_never_hands_out_the_same_instance_id():
    """两个并发的自动分配各自拿到一个号，不会落到同一行上。

    「挑一个没被占用的 ID」与「把它占下来」之间存在窗口；用例把占用探测放慢来把窗口
    拉开，没有占位互斥时两个线程会选出同一个 ID，后写的顶掉先写的。
    """
    world = _build_world()
    world.probe_delay = 0.005
    barrier = threading.Barrier(2)
    results: list[tuple[bool, str]] = []
    lock = threading.Lock()

    def _create() -> None:
        """两个线程在同一时刻发起创建。"""
        barrier.wait(timeout=5)
        outcome = world.clone(plugin_id="DemoPlugin")
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=_create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(success for success, _ in results)
    assert sorted(instance_id for _, instance_id in results) == [
        "DemoPlugin2",
        "DemoPlugin3",
    ]
    assert sorted(world.rows) == ["DemoPlugin2", "DemoPlugin3"]


# --------------------------------------------------------------------------- #
# 可恢复清单
# --------------------------------------------------------------------------- #


def test_restorable_listing_only_reports_disabled_clones():
    """在册的分身与本体那一行都不在恢复清单里。

    活着的分身配置正被使用，摆进恢复选择器只会让人误以为能把它再创建一遍；本体不是
    分身，也没有「恢复成分身」这回事。
    """
    world = _build_world(
        rows={
            "DemoPlugin": PluginInstance(
                instance_id="DemoPlugin",
                source_plugin_id="DemoPlugin",
                is_enabled=False,
            ),
            "DemoPlugin2": _disabled_clone("DemoPlugin2", is_enabled=True),
            "DemoPlugin3": _disabled_clone("DemoPlugin3"),
        },
    )

    assert [item["instance_id"] for item in world.restorable()] == ["DemoPlugin3"]


def test_restorable_listing_reports_suffix_display_info_and_stored_config():
    """清单给出后缀、展示信息与是否留有业务参数，供恢复选择器直接渲染。"""
    world = _build_world(
        rows={
            "DemoPluginwork": _disabled_clone(
                "DemoPluginwork",
                plugin_name="工作实例",
                plugin_desc="独立配置",
            ),
            "DemoPlugin9": _disabled_clone("DemoPlugin9"),
        },
        configs={"DemoPluginwork": {"token": "留着"}},
    )

    listing = {item["instance_id"]: item for item in world.restorable()}

    assert listing["DemoPluginwork"] == {
        "instance_id": "DemoPluginwork",
        "suffix": "work",
        "plugin_name": "工作实例",
        "plugin_desc": "独立配置",
        "has_config": True,
    }
    # 停用的实例不在类注册表里，「有没有配置」不能走要求插件在册的读取口去问
    assert listing["DemoPlugin9"]["has_config"] is False
