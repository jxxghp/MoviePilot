"""
主程序与插件仓共享的测试引导及进程级隔离装配。

调用方必须在导入应用配置前隔离 CONFIG_DIR。插件仓先把后端路径加入 sys.path，
再调用对应代际的引导函数；各代插件可能同名，需使用独立的 pytest 会话。
本模块导入时仅依赖标准库，数据库和应用依赖在引导函数调用时才加载。
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Callable, Iterable, Optional

if TYPE_CHECKING:
    import pytest

# 本进程隔离出的临时 CONFIG_DIR，兼作幂等标记
_isolated_config_dir: Optional[str] = None


class _SitesHelperStub:
    """
    用空结果替代测试中不应加载的动态站点资源。

    :cvar auth_level: 固定为 0，表示不提供认证资源等级
    :cvar auth_version: 空字符串，表示没有认证资源版本
    :cvar indexer_version: 空字符串，表示没有索引资源版本
    """

    auth_level = 0
    auth_version = ""
    indexer_version = ""

    def get_indexers(self) -> list[object]:
        """
        提供同步站点索引列表占位。

        :return: 固定返回空列表
        """
        return []

    async def async_get_indexers(self) -> list[object]:
        """
        提供异步站点索引列表占位。

        :return: 等待后固定返回空列表
        """
        return []

    def get_indexer(self, *_args: object, **_kwargs: object) -> Optional[dict[str, object]]:
        """
        提供同步站点详情查询占位。

        :param _args: 为兼容真实接口接收的位置参数，当前忽略
        :param _kwargs: 为兼容真实接口接收的关键字参数，当前忽略
        :return: 固定返回 None，表示没有站点详情
        """
        return None

    async def async_get_indexer(self, *_args: object, **_kwargs: object) -> Optional[dict[str, object]]:
        """
        提供异步站点详情查询占位。

        :param _args: 为兼容真实接口接收的位置参数，当前忽略
        :param _kwargs: 为兼容真实接口接收的关键字参数，当前忽略
        :return: 等待后固定返回 None，表示没有站点详情
        """
        return None

    def get_authsites(self) -> list[object]:
        """
        提供认证站点列表占位。

        :return: 固定返回空列表
        """
        return []

    def get_indexsites(self) -> list[object]:
        """
        提供索引站点列表占位。

        :return: 固定返回空列表
        """
        return []

    def check(self, *_args: object, **_kwargs: object) -> tuple[bool, str]:
        """
        声明测试占位未加载真实站点资源。

        :param _args: 兼容真实接口的位置参数，当前忽略
        :param _kwargs: 兼容真实接口的关键字参数，当前忽略
        :return: False 和站点资源未加载的提示
        """
        return False, "站点资源未加载"

    def check_user(self, *_args: object, **_kwargs: object) -> tuple[bool, str]:
        """
        声明测试占位不提供用户站点认证。

        :param _args: 兼容真实接口的位置参数，当前忽略
        :param _kwargs: 兼容真实接口的关键字参数，当前忽略
        :return: False 和站点资源未加载的提示
        """
        return False, "站点资源未加载"


def isolate_config_dir() -> str:
    """
    为测试设置进程私有配置目录，重复调用返回首次选择的目录。

    必须早于任何会导入 app.runtime.config 的操作，因为配置对象首次创建后不会随环境变量更新。
    调用方已设置 CONFIG_DIR 时直接复用，不验证或清理该目录，调用方必须确保它与真实配置隔离。
    仅对本函数新建的临时目录注册退出清理。

    :return: 实际使用的 CONFIG_DIR 文本；已有环境变量原样返回，新建目录为绝对路径
    """
    global _isolated_config_dir
    if _isolated_config_dir is not None:
        return _isolated_config_dir
    existing = os.environ.get("CONFIG_DIR")
    if existing:
        _isolated_config_dir = existing
        return existing
    tmp = tempfile.mkdtemp(prefix="mp-test-config-")
    os.environ["CONFIG_DIR"] = tmp
    _isolated_config_dir = tmp

    def _cleanup(
        path: str = tmp,
        rmtree: Callable[..., None] = shutil.rmtree,
        sys_mod: ModuleType = sys,
    ) -> None:
        """
        退出时释放已创建的数据库连接池，再尽力删除测试临时目录。

        只查看已加载引擎，避免清理时反而创建新数据库连接；释放连接池异常和目录删除错误均忽略。

        :param path: 本次创建的隔离目录，默认值在注册回调时绑定
        :param rmtree: 提前绑定的目录删除函数，避免解释器退出时模块被回收
        :param sys_mod: 提前绑定的 sys 模块，用于查找已加载的引擎模块
        """
        try:
            engine_mod = sys_mod.modules.get("app.db.engine")
            peek = getattr(engine_mod, "peek_sync_engine", None)
            engine = peek() if peek is not None else None
            if engine is not None:
                engine.dispose()
        except Exception:
            pass
        rmtree(path, ignore_errors=True)

    atexit.register(_cleanup)
    return tmp


def _expose_plugin_source(path: Path) -> None:
    """
    把插件仓源码目录加入生产插件包的搜索路径。

    仅在尚未加入时前置该目录；修改在当前进程持续有效，不在函数退出时还原。

    :param path: 当前测试代际的插件源码目录
    """
    from importlib import import_module

    plugins_package = import_module("app.plugins")
    value = str(path)
    if value not in plugins_package.__path__:
        plugins_package.__path__.insert(0, value)


def ensure_sites_stub() -> None:
    """
    在进程模块缓存中安装确定性的站点资源占位。

    必须在业务模块导入前调用；始终替换该模块，即使本机存在真实扩展。
    此修改持续到进程结束或由调用方恢复，真实资源的加载和 ABI 由专项测试验证。
    """
    from importlib.util import spec_from_loader
    from types import ModuleType

    stub = ModuleType("app.application.site.sites")
    setattr(stub, "SitesHelper", _SitesHelperStub)
    stub.__spec__ = spec_from_loader("app.application.site.sites", None)
    sys.modules["app.application.site.sites"] = stub


def ensure_optional_stub(name: str, **attrs: object) -> None:
    """
    导入可选依赖，导入失败时为当前进程安装占位模块。

    模块已在 sys.modules 中或可成功导入时不修改；捕获 ImportError 后创建占位，
    包括依赖内部导入失败的情况。占位不会自动还原，需要作用域隔离时应使用 stub_modules。

    :param name: 待导入或替换的模块全名
    :param attrs: 新占位模块需要暴露的属性
    """
    if name in sys.modules:
        return
    try:
        __import__(name)
        return
    except ImportError:
        pass
    from types import ModuleType
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


def prepare_backend() -> None:
    """
    按顺序隔离配置、替换站点资源并装配测试后端。

    先设置 CONFIG_DIR，再创建模型表和事务执行器，加载配置快照，并配置缓存、领域解析和
    类型化运行配置。服务目录返回空集合，不启动真实服务模块。此过程会修改进程级依赖配置，
    调用方应在专用测试会话中使用，或自行保存并恢复相关状态。
    """
    isolate_config_dir()
    ensure_sites_stub()
    from app.startup.initializers.database import init_db
    init_db()
    from app.db.adapters.transaction import TransactionalWriteRunner
    from app.db.session import SessionFactory, async_session_scope
    from app.db.uow import configure_transaction_runners

    transaction_runner = TransactionalWriteRunner(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )
    configure_transaction_runners(
        sync=transaction_runner.sync,
        async_=transaction_runner.async_,
    )
    from app.application.service import configure_service_directory

    # 共享引导不启动真实服务模块；需要具体服务实例的测试应在自身边界显式覆盖该目录。
    configure_service_directory(
        configs=lambda _config_key, _conf_type: [],
        modules=lambda _module_type: [],
    )
    from app.db.adapters.configuration import TransactionalUserConfigurationRepository
    from app.db.oper.systemconfig import SystemConfigOper

    with SessionFactory() as session:
        SystemConfigOper().load_snapshot(session)
    TransactionalUserConfigurationRepository(SessionFactory).load_snapshot()
    # 缓存装饰器在测试模块导入时即创建后端，先装配隔离配置对应的适配器。
    from app.startup.initializers.cache import configure_cache_dependencies
    configure_cache_dependencies()
    # 测试与生产使用同一组合入口，确保领域解析器获得隔离库和测试 settings。
    from app.startup.initializers.domain import configure_domain_dependencies
    configure_domain_dependencies()
    # 搜索、调度与 API 链路读取组合根登记的类型化配置快照；插件仓集成测试会驱动这些真实链路，
    # 故按生产 publish_configuration 的同一组构建函数登记，快照随测试 settings 实时生成。
    from app.application.configuration import RuntimeConfiguration, configure_runtime_configuration
    from app.runtime.config import settings
    from app.startup.composition.configuration import (
        build_api_runtime_config,
        build_chain_runtime_config,
        build_scheduler_runtime_config,
    )
    configure_runtime_configuration(
        RuntimeConfiguration(
            api=lambda: build_api_runtime_config(settings),
            scheduler=lambda: build_scheduler_runtime_config(settings),
            chain=lambda: build_chain_runtime_config(settings),
        )
    )


def prepare_v2_backend(plugins_repo: Path) -> None:
    """
    准备后端并向插件命名空间加入 v2 插件源码。

    不同代际可能存在同名插件，必须在独立 pytest 会话中加载。

    :param plugins_repo: 插件仓根目录；使用其 plugins.v2 子目录
    """
    prepare_backend()
    _expose_plugin_source(Path(plugins_repo) / "plugins.v2")


def prepare_v3_backend(plugins_repo: Path) -> None:
    """
    准备后端并向插件命名空间加入 v3 插件源码。

    不同代际可能存在同名插件，必须在独立 pytest 会话中加载。

    :param plugins_repo: 插件仓根目录；使用其 plugins.v3 子目录
    """
    prepare_backend()
    _expose_plugin_source(Path(plugins_repo) / "plugins.v3")


def prepare_v1_backend(plugins_repo: Path) -> None:
    """
    准备后端并向插件命名空间加入 v1 插件源码。

    不同代际可能存在同名插件，必须在独立 pytest 会话中加载。

    :param plugins_repo: 插件仓根目录；使用其 plugins 子目录
    """
    prepare_backend()
    _expose_plugin_source(Path(plugins_repo) / "plugins")


def mark_plugin_generation(items: Iterable[pytest.Item], pytest_module: ModuleType) -> None:
    """
    按用例路径添加插件代际标记，供 pytest 按代筛选。

    优先读取 item.path，旧版回退至 item.fspath；统一路径分隔符后按 tests/vN/ 子串匹配，
    因此绝对和相对路径都适用。未匹配代际目录的用例保持原样。

    :param items: 已收集的测试用例，原地追加 v1、v2 或 v3 标记
    :param pytest_module: 调用方提供的 pytest 模块，避免模块导入时依赖 pytest
    """
    markers = getattr(pytest_module, "mark")
    for item in items:
        item_path = getattr(item, "path", None)
        path = str(item_path if item_path is not None else item.fspath).replace("\\", "/")
        if "tests/v3/" in path:
            item.add_marker(markers.v3)
        elif "tests/v2/" in path:
            item.add_marker(markers.v2)
        elif "tests/v1/" in path:
            item.add_marker(markers.v1)
