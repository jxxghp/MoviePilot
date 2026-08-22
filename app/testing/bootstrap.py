"""测试引导共享实现（主程序与插件仓同源）。

主程序 ``tests/conftest.py`` 与各插件仓的极薄 shim（``tests/_bootstrap.py``，仅负责把
后端定位并加入 ``sys.path``）都委托到这里，使「隔离 CONFIG_DIR / 建表 / 注入插件目录 /
按目录打 v1·v2·v3 marker / 退出清理」等引导逻辑只在主程序维护一处，所有消费方行为与修复一致。
其中 :func:`isolate_config_dir` 为主程序与插件仓共用，``prepare_v1/v2/v3_backend`` 与
:func:`mark_plugin_generation` 为插件仓专用。

本模块只依赖标准库，``import`` 期不触发 ``app.*``：调用方可安全地「先 import 本模块、
再隔离 CONFIG_DIR」，不破坏「隔离必须早于首个 ``import app.runtime.config``」这一硬约束。
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

# 本进程隔离出的临时 CONFIG_DIR，兼作幂等标记
_isolated_config_dir: Optional[str] = None


class _SitesHelperStub:
    """
    测试环境站点资源垫片，用于动态站点资源仓缺失时提供基础只读接口。
    """

    auth_level = 0
    auth_version = ""
    indexer_version = ""

    def get_indexers(self) -> list:
        """返回空站点索引列表。"""
        return []

    async def async_get_indexers(self) -> list:
        """异步返回空站点索引列表。"""
        return []

    def get_indexer(self, *_args: Any, **_kwargs: Any) -> Optional[dict]:
        """返回空站点索引详情。"""
        return None

    async def async_get_indexer(self, *_args: Any, **_kwargs: Any) -> Optional[dict]:
        """异步返回空站点索引详情。"""
        return None

    def get_authsites(self) -> list:
        """返回空认证站点列表。"""
        return []

    def get_indexsites(self) -> list:
        """返回空索引站点列表。"""
        return []

    def check(self, *_args: Any, **_kwargs: Any) -> tuple[bool, str]:
        """返回站点资源不可用的检查结果。"""
        return False, "站点资源未加载"

    def check_user(self, *_args: Any, **_kwargs: Any) -> tuple[bool, str]:
        """返回用户认证站点不可用的检查结果。"""
        return False, "站点资源未加载"


def isolate_config_dir() -> str:
    """把 ``CONFIG_DIR`` 指向进程私有临时目录，隔离主程序真实库与配置（幂等）。

    数据库引擎已改为惰性创建，``import app.db`` 本身不再连库；但 ``settings`` 是在
    ``import app.runtime.config`` 时构造的，那一刻就把 ``CONFIG_DIR`` 读进字段并建好配置子目录，
    之后再改环境变量对 ``settings.CONFIG_PATH`` 毫无影响——引擎晚点才建，连的仍是真实 ``user.db``。
    故本函数必须早于首个牵入 ``app.runtime.config`` 的 import（``app.db`` / ``app.application.orchestration.*`` 都会牵入）。
    调用方已显式设置 ``CONFIG_DIR``（如 CI 指定隔离目录）时尊重之、不覆盖。

    :return: 实际生效的 CONFIG_DIR 绝对路径
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

    def _cleanup(path: str = tmp, rmtree=shutil.rmtree, sys_mod=sys) -> None:
        """进程退出时释放 SQLite 连接池再删临时目录。

        默认参数绑定 ``rmtree``/``path``/``sys_mod``：解释器关停期标准库模块可能已被回收为 ``None``，
        绑定后仍可安全调用。先释放已建立的 ``user.db`` 连接，规避 Windows 下
        文件锁导致 ``rmtree`` 静默失败（``ignore_errors``）、残留临时目录。

        读 ``peek_sync_engine``（有则取、无则 ``None``）而不是旧名字 ``app.db.Engine``：后者是
        惰性解析的属性，取它会**创建**引擎——只 ``import`` 过 ``app.db`` 的进程会在解释器关停时
        凭空连一次库，仅仅为了随后把它 dispose 掉。
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
    """让插件仓源码通过生产运行时的 ``app.plugins.<id>`` 命名空间导入。

    ``app.plugins`` 是命名空间包，``__path__`` 为动态重算的 ``_NamespacePath``，
    只支持追加、不支持按位置插入；改为整体赋值一份列表，插件仓源码稳定排在最前。
    """
    from importlib import import_module

    plugins_package = import_module("app.plugins")
    value = str(path)
    search_path = list(plugins_package.__path__)
    if value not in search_path:
        plugins_package.__path__ = [value, *search_path]


def ensure_sites_stub() -> None:
    """为 ``app.application.site.sites`` 补最小垫片（仅在缺失时）。

    ``app.application.site.sites`` 由独立仓库动态拉取，CI / 全新环境无该模块，而众多 ``app.application.orchestration.*`` /
    ``app.modules.*`` 在 import 期依赖它。统一补一个最小垫片，省去各测试文件各自打桩；若真实模块
    已存在（本地已拉取）则用真实模块、不覆盖，不影响真实行为。须在隔离 CONFIG_DIR 之后调用，
    以免试探性 ``import app.application.site.sites`` 牵入 ``app.runtime.config``、
    把配置路径定型到真实目录。
    """
    if "app.application.site.sites" in sys.modules:
        return
    try:
        import app.application.site.sites  # noqa: F401  本地已拉取时用真实模块
    except (ModuleNotFoundError, ImportError):
        from importlib.util import spec_from_loader
        from types import ModuleType
        stub = ModuleType("app.application.site.sites")
        stub.SitesHelper = _SitesHelperStub
        stub.__spec__ = spec_from_loader("app.application.site.sites", None)
        sys.modules["app.application.site.sites"] = stub


def ensure_optional_stub(name: str, **attrs) -> None:
    """为可选第三方依赖补占位模块（仅在缺失时），可带属性。

    用例 import 的 app 代码会牵入可选三方库（如 psutil / dateparser / Pinyin2Hanzi /
    qbittorrentapi / transmission_rpc），CI / 全新环境可能未安装。本函数在该库缺失时补一个带
    指定属性的占位，使 import 不致失败；若已真实安装则保留真实模块、不覆盖。占位为进程级常驻
    （与 import 生命周期一致、不作用域还原），是「让可选 import 不失败」的垫片——与
    :func:`stub_modules`（作用域内打桩并还原）属不同用途，故不收进 stub_modules。

    :param name: 可选依赖的顶层模块名
    :param attrs: 占位模块需暴露的属性（仅在真正创建占位时设置）
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
    """隔离 CONFIG_DIR、补 sites 垫片并建表（后端须已在 ``sys.path`` 上）。

    主程序中后端即当前包；插件仓由其 ``tests/_bootstrap.py`` shim 在 import 本模块前
    先把后端目录注入 ``sys.path``。顺序固定：先隔离 CONFIG_DIR，再补 ``app.application.site.sites`` 垫片，
    最后建表——隔离出的临时库为空，运行期查 ``systemconfig`` 等表会报 no such table，故建表；
    ``init_db`` 仅 import models + create_all，无 alembic/网络、幂等、毫秒级。
    """
    isolate_config_dir()
    ensure_sites_stub()
    from app.startup.database_initializer import init_db
    init_db()
    # 缓存装饰器在测试模块导入时即创建后端，先装配隔离配置对应的适配器。
    from app.startup.cache_initializer import configure_cache_dependencies
    configure_cache_dependencies()
    # 测试与生产使用同一组合入口，确保领域解析器获得隔离库和测试 settings。
    from app.startup.domain_initializer import configure_domain_dependencies
    configure_domain_dependencies()
    # 导入即向 application.agent 门面注册惰性 provider，不物化 Agent 实现；
    # 直接构造 SkillInteractionHandler 等门面消费方的单测依赖该注册先完成。
    import app.startup.agent_initializer  # noqa: F401
    # 扩展经端口取用目录、存储、命名、站点资源与规则配置，须先于扩展被 import 完成注入
    # （测试直接 import 扩展模块，不经过 ModuleManager，注册须在此显式补齐）。
    from app.startup.hostport_initializer import configure_host_ports
    configure_host_ports()


def prepare_v2_backend(plugins_repo: Path) -> None:
    """v2 插件单测引导：准备后端并暴露 ``<repo>/plugins.v2`` 源码。

    与 :func:`prepare_v1_backend` 互斥：v1/v2 存在同名插件包，同一进程同时加载会相互覆盖，
    须在各自独立的 pytest 会话中运行。

    :param plugins_repo: 插件仓根目录（由调用方 shim 传入）
    """
    prepare_backend()
    _expose_plugin_source(Path(plugins_repo) / "plugins.v2")


def prepare_v3_backend(plugins_repo: Path) -> None:
    """v3 插件单测引导：准备后端并暴露 ``<repo>/plugins.v3`` 源码。

    v3 插件与旧代插件可能存在同名包，必须在独立 pytest 会话中加载，避免 Python 模块
    缓存把其它代际实现复用到当前测试进程。

    :param plugins_repo: 插件仓根目录（由调用方 shim 传入）
    """
    prepare_backend()
    _expose_plugin_source(Path(plugins_repo) / "plugins.v3")


def prepare_v1_backend(plugins_repo: Path) -> None:
    """v1 插件单测引导：准备后端并暴露 ``<repo>/plugins`` 源码（与 v2 互斥）。

    :param plugins_repo: 插件仓根目录（由调用方 shim 传入）
    """
    prepare_backend()
    _expose_plugin_source(Path(plugins_repo) / "plugins")


def mark_plugin_generation(items, pytest_module) -> None:
    """按用例所在目录自动给其打 ``v1`` / ``v2`` / ``v3`` marker，供按代筛选与分会话运行。

    优先读取 pytest 7+ 的 ``item.path``，旧版 pytest 缺失该属性时回退到 ``item.fspath``。用
    「不带前导斜杠」的子串匹配，兼容相对路径与绝对路径两种
    运行方式：以 ``pytest tests/v2`` 等相对路径运行时收集路径可能不含前导斜杠。
    ``pytest`` 模块由各仓 conftest 传入，避免本模块在非测试态强依赖 pytest。

    :param items: pytest 收集到的用例集合
    :param pytest_module: 调用方传入的 ``pytest`` 模块对象
    """
    for item in items:
        item_path = getattr(item, "path", None)
        path = str(item_path if item_path is not None else item.fspath).replace("\\", "/")
        if "tests/v3/" in path:
            item.add_marker(pytest_module.mark.v3)
        elif "tests/v2/" in path:
            item.add_marker(pytest_module.mark.v2)
        elif "tests/v1/" in path:
            item.add_marker(pytest_module.mark.v1)
