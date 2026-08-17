import builtins
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from app.runtime.compat.diagnostics import (
    configure_legacy_import_diagnostics,
    get_legacy_import_diagnostics,
    reset_legacy_import_diagnostics,
    scan_plugin_legacy_imports,
)
from app.runtime.compat.imports import install_legacy_import_hook
from app.runtime.compat.manifest import (
    MODULE_ALIASES,
    PACKAGE_ALIASES,
    PACKAGE_EXPORTS,
    SYMBOL_ALIASES,
    VIRTUAL_PACKAGES,
    ModuleAlias,
    _MESSAGE_NOTIFICATION_SYMBOL_ALIASES,
)


LEGACY_PACKAGE = "legacy_compat_test"
LEGACY_MODULE = f"{LEGACY_PACKAGE}.target"
CANONICAL_PACKAGE = "canonical_compat_test"
CANONICAL_MODULE = f"{CANONICAL_PACKAGE}.target"


@pytest.fixture
def compatibility_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """创建可计数初始化次数的临时 canonical 模块和旧路径映射。"""
    package_dir = tmp_path / CANONICAL_PACKAGE
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "target.py").write_text(
        "import builtins\n"
        "builtins._legacy_compat_test_count = "
        "getattr(builtins, '_legacy_compat_test_count', 0) + 1\n"
        "TOKEN = object()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(
        MODULE_ALIASES,
        LEGACY_MODULE,
        ModuleAlias(
            target=CANONICAL_MODULE,
            replacement="app.sdk.test",
            introduced="test",
            owner="test",
        ),
    )
    VIRTUAL_PACKAGES.add(LEGACY_PACKAGE)
    install_legacy_import_hook()
    reset_legacy_import_diagnostics()
    yield
    MODULE_ALIASES.pop(LEGACY_MODULE, None)
    VIRTUAL_PACKAGES.discard(LEGACY_PACKAGE)
    for module_name in (LEGACY_MODULE, LEGACY_PACKAGE, CANONICAL_MODULE, CANONICAL_PACKAGE):
        sys.modules.pop(module_name, None)
    if hasattr(builtins, "_legacy_compat_test_count"):
        delattr(builtins, "_legacy_compat_test_count")
    reset_legacy_import_diagnostics()


def test_legacy_import_reuses_canonical_module_identity(compatibility_modules):
    """旧路径先导入时应复用 canonical 模块且只执行一次源码。"""
    legacy = importlib.import_module(LEGACY_MODULE)
    canonical = importlib.import_module(CANONICAL_MODULE)

    assert legacy is canonical
    assert sys.modules[LEGACY_MODULE] is sys.modules[CANONICAL_MODULE]
    assert legacy.__name__ == CANONICAL_MODULE
    assert legacy.__spec__.name == CANONICAL_MODULE
    assert builtins._legacy_compat_test_count == 1


def test_canonical_first_import_keeps_same_legacy_identity(compatibility_modules):
    """canonical 路径先导入时，旧路径仍应绑定同一模块对象。"""
    canonical = importlib.import_module(CANONICAL_MODULE)
    legacy = importlib.import_module(LEGACY_MODULE)

    assert legacy is canonical
    assert legacy.TOKEN is canonical.TOKEN
    assert builtins._legacy_compat_test_count == 1


def test_virtual_package_blocks_unregistered_descendants(compatibility_modules):
    """合成旧父包不得向其他 Finder 泄漏未登记的子模块。"""
    package = importlib.import_module(LEGACY_PACKAGE)

    assert package.__path__ == []
    with pytest.raises(ModuleNotFoundError, match="未在兼容映射中登记"):
        importlib.import_module(f"{LEGACY_PACKAGE}.unknown")


def test_debug_diagnostics_warn_once_for_runtime_alias(compatibility_modules):
    """DEBUG 运行时兼容命中应输出一次包含迁移目标的警告。"""
    messages = []
    configure_legacy_import_diagnostics(enabled=True, emitter=messages.append)

    importlib.import_module(LEGACY_MODULE)
    importlib.import_module(LEGACY_MODULE)

    assert len(messages) == 1
    assert LEGACY_MODULE in messages[0]
    assert CANONICAL_MODULE in messages[0]
    assert "app.sdk.test" in messages[0]


def test_production_diagnostics_stay_silent(compatibility_modules):
    """DEBUG 关闭时兼容导入继续生效但不输出告警。"""
    messages = []
    configure_legacy_import_diagnostics(enabled=False, emitter=messages.append)

    module = importlib.import_module(LEGACY_MODULE)

    assert module is importlib.import_module(CANONICAL_MODULE)
    assert messages == []


def test_plugin_scan_reports_cached_legacy_import(compatibility_modules, tmp_path: Path):
    """模块已缓存时，插件 AST 扫描仍应报告其静态旧导入。"""
    importlib.import_module(LEGACY_MODULE)
    reset_legacy_import_diagnostics()
    messages = []
    configure_legacy_import_diagnostics(enabled=True, emitter=messages.append)
    plugin_dir = tmp_path / "sampleplugin"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text(
        f"from {LEGACY_MODULE} import TOKEN\n",
        encoding="utf-8",
    )

    scan_plugin_legacy_imports("SamplePlugin", plugin_dir)
    scan_plugin_legacy_imports("SamplePlugin", plugin_dir)

    assert len(messages) == 1
    assert "插件 sampleplugin" in messages[0]
    assert "__init__.py:1" in messages[0]
    snapshot = get_legacy_import_diagnostics()
    assert ("app.plugins.sampleplugin", LEGACY_MODULE) in snapshot["reported"]


def test_plugin_scan_accepts_utf8_bom(compatibility_modules, tmp_path: Path):
    """插件源码带 UTF-8 BOM 时仍应识别旧导入并输出迁移警告。"""
    messages = []
    configure_legacy_import_diagnostics(enabled=True, emitter=messages.append)
    plugin_dir = tmp_path / "bomplugin"
    plugin_dir.mkdir()
    source = f"from {LEGACY_MODULE} import TOKEN\n".encode("utf-8")
    (plugin_dir / "__init__.py").write_bytes(b"\xef\xbb\xbf" + source)

    scan_plugin_legacy_imports("BomPlugin", plugin_dir)

    assert len(messages) == 1
    assert "插件 bomplugin" in messages[0]
    assert LEGACY_MODULE in messages[0]


def test_manifest_aliases_reuse_real_canonical_modules():
    """正式映射表中的旧路径应在隔离进程中复用全部 canonical 模块。"""
    code = """
import importlib
from app.runtime.compat.manifest import MODULE_ALIASES
# CI 无 app.application.site.sites 二进制模块，先补垫片再校验全部映射（与 conftest 同源）。
from app.testing.bootstrap import ensure_sites_stub
ensure_sites_stub()

for legacy_name, alias in MODULE_ALIASES.items():
    try:
        canonical = importlib.import_module(alias.target)
    except ModuleNotFoundError:
        if alias.target == "app.application.site.sites":
            continue
        raise
    legacy = importlib.import_module(legacy_name)
    assert legacy is canonical, (legacy_name, alias.target)
    assert legacy.__name__ == alias.target, legacy_name
    assert legacy.__spec__.name == alias.target, legacy_name
"""
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        check=True,
    )


def test_virtual_package_exports_resolve_exact_manifest_symbols():
    """合成旧包仅公开 manifest 声明的符号，并记录 DEBUG 兼容警告。"""
    legacy_package = "app.core.meta"
    sys.modules.pop(legacy_package, None)
    messages = []
    configure_legacy_import_diagnostics(enabled=True, emitter=messages.append)

    package = importlib.import_module(legacy_package)

    assert set(package.__all__) == set(PACKAGE_EXPORTS[legacy_package])
    assert package.MetaBase is importlib.import_module(
        "app.domain.meta.metabase"
    ).MetaBase
    assert PACKAGE_ALIASES[legacy_package].replacement in messages[0]
    reset_legacy_import_diagnostics()


def test_db_refactor_legacy_modules_are_all_registered():
    """DB 分层迁移删除的旧模块必须全部有精确兼容入口。"""
    expected = {
        "app.db.agentchat_oper",
        "app.db.agenttask_oper",
        "app.db.downloadfailure_oper",
        "app.db.downloadhistory_oper",
        "app.db.init",
        "app.db.mediaserver_oper",
        "app.db.message_oper",
        "app.db.plugindata_oper",
        "app.db.site_oper",
        "app.db.subscribe_oper",
        "app.db.subscribehistory_oper",
        "app.db.systemconfig_oper",
        "app.db.transferhistory_oper",
        "app.db.transferpending_oper",
        "app.db.user_oper",
        "app.db.userconfig_oper",
        "app.db.workflow_oper",
    }

    assert expected <= set(MODULE_ALIASES)


def test_split_user_oper_facade_exports_data_and_auth_contracts():
    """旧 user_oper 同时提供 UserOper 与八个认证依赖。"""
    legacy = importlib.import_module("app.db.user_oper")
    canonical_user = importlib.import_module("app.db.oper.user")
    canonical_deps = importlib.import_module("app.api.deps")

    assert legacy.UserOper is canonical_user.UserOper
    for name in (
        "get_current_user",
        "get_current_user_async",
        "get_current_active_user",
        "get_current_active_user_async",
        "get_current_active_manage_user",
        "get_current_active_manage_user_async",
        "get_current_active_superuser",
        "get_current_active_superuser_async",
    ):
        assert getattr(legacy, name) is getattr(canonical_deps, name)


def test_legacy_utils_media_facade_combines_strategy_and_identity_symbols():
    """旧 utils.media 同时保留领域策略和迁至 schemas 的身份原语。"""
    legacy = importlib.import_module("app.utils.media")
    domain_media = importlib.import_module("app.domain.media")
    schema_media = importlib.import_module("app.schemas.media")

    assert legacy.is_music_media_source is domain_media.is_music_media_source
    assert legacy.resolve_media_identity is schema_media.resolve_media_identity
    assert legacy.build_media_key is schema_media.build_media_key
    assert legacy.MEDIA_SOURCE_ALIASES is schema_media.MEDIA_SOURCE_ALIASES


def test_physical_modules_resolve_moved_symbols_without_reverse_imports():
    """仍存在的旧物理模块应通过 Loader 叠加迁走的符号。"""
    domain_media = importlib.import_module("app.domain.media")
    schema_media = importlib.import_module("app.schemas.media")
    transfer_schema = importlib.import_module("app.schemas.transfer")
    legacy_transfer = importlib.import_module("app.sdk._legacy.transfer")
    history_schema = importlib.import_module("app.schemas.history")
    system_schema = importlib.import_module("app.schemas.system")
    tmdb_schema = importlib.import_module("app.schemas.tmdb")
    types_schema = importlib.import_module("app.schemas.types")
    agent_schema = importlib.import_module("app.schemas.agent")
    sdk_logging = importlib.import_module("app.sdk.logging")
    legacy_logging = importlib.import_module("app.log")
    runtime_logging = importlib.import_module("app.runtime.log")
    schemas_package = importlib.import_module("app.schemas")

    assert domain_media.build_media_key is schema_media.build_media_key
    assert domain_media.resolve_media_identity is schema_media.resolve_media_identity
    assert transfer_schema.TransferTask is legacy_transfer.TransferTask
    assert transfer_schema.TransferQueue is legacy_transfer.TransferQueue
    assert transfer_schema.DownloadHistory is history_schema.DownloadHistory
    assert transfer_schema.TransferDirectoryConf is system_schema.TransferDirectoryConf
    assert transfer_schema.TmdbEpisode is tmdb_schema.TmdbEpisode
    assert transfer_schema.MediaType is types_schema.MediaType
    assert agent_schema.ReplyMode is types_schema.ReplyMode
    assert sdk_logging.LoggerManager is runtime_logging.LoggerManager
    assert legacy_logging.LoggerManager is runtime_logging.LoggerManager
    assert schemas_package.TransferTask is legacy_transfer.TransferTask
    assert schemas_package.TransferQueue is legacy_transfer.TransferQueue


def test_chain_media_legacy_scraping_symbols_resolve_to_scraping_chain():
    """刮削拆分后，旧 app.chain.media 路径应能继续取用刮削公开符号。"""
    legacy_media = importlib.import_module("app.chain.media")
    canonical_scraping = importlib.import_module("app.chain.scraping")

    assert legacy_media.ScrapingChain is canonical_scraping.ScrapingChain
    assert legacy_media.ScrapingOption is canonical_scraping.ScrapingOption
    assert legacy_media.ScrapingConfig is canonical_scraping.ScrapingConfig


def test_rules_domain_legacy_modules_resolve_to_rules():
    """规则域收敛后，filter/filter_rules 旧路径应复用 rules 模块。"""
    canonical = importlib.import_module("app.application.rules")

    for legacy_name in ("app.application.filter", "app.application.filter_rules"):
        legacy = importlib.import_module(legacy_name)
        assert legacy is canonical, legacy_name
        assert legacy.RuleHelper is canonical.RuleHelper
        assert legacy.RuleParser is canonical.RuleParser


def test_debug_diagnostics_reports_moved_symbol_path():
    """DEBUG 模式应对物理模块中的旧符号路径给出一次迁移提示。"""
    messages = []
    reset_legacy_import_diagnostics()
    configure_legacy_import_diagnostics(enabled=True, emitter=messages.append)

    domain_media = importlib.import_module("app.domain.media")
    domain_media.build_media_key
    domain_media.build_media_key

    assert len(messages) == 1
    assert "app.domain.media.build_media_key" in messages[0]
    assert "app.schemas.media.build_media_key" in messages[0]
    reset_legacy_import_diagnostics()


def test_plugin_scan_reports_moved_symbol_import(tmp_path: Path):
    """插件静态扫描应识别仍存在模块中的旧符号导入。"""
    messages = []
    reset_legacy_import_diagnostics()
    configure_legacy_import_diagnostics(enabled=True, emitter=messages.append)
    plugin_dir = tmp_path / "symbolplugin"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text(
        "from app.domain.media import build_media_key\n",
        encoding="utf-8",
    )

    scan_plugin_legacy_imports("SymbolPlugin", plugin_dir)

    assert len(messages) == 1
    assert "app.domain.media.build_media_key" in messages[0]
    assert "__init__.py:1" in messages[0]
    reset_legacy_import_diagnostics()


def test_symbol_alias_manifest_covers_all_moved_public_symbols():
    """符号级映射清单应覆盖媒体身份、整理工作项、刮削拆分与消息/通知命名统一的旧入口。"""
    assert set(SYMBOL_ALIASES["app.domain.media"]) == {
        "MEDIA_SOURCE_ALIASES",
        "MEDIA_SOURCE_PREFIXES",
        "normalize_media_source",
        "parse_media_key",
        "resolve_media_identity",
        "normalize_media_identity_payload",
        "build_media_key",
    }
    assert set(SYMBOL_ALIASES["app.chain.media"]) == {
        "ScrapingChain",
        "ScrapingOption",
        "ScrapingConfig",
    }
    assert set(SYMBOL_ALIASES["app.schemas"]) == {
        "TransferTask",
        "TransferQueue",
    } | set(_MESSAGE_NOTIFICATION_SYMBOL_ALIASES)
    assert set(SYMBOL_ALIASES["app.schemas.transfer"]) == {
        "TransferTask",
        "TransferQueue",
        "DownloadHistory",
        "TransferDirectoryConf",
        "TmdbEpisode",
        "MediaType",
    }
    assert set(SYMBOL_ALIASES["app.schemas.agent"]) == {"ReplyMode"}
    assert set(SYMBOL_ALIASES["app.sdk.logging"]) == {
        "CustomFormatter",
        "LogConfigModel",
        "LogEntry",
        "LogSettings",
        "LoggerManager",
        "NonBlockingFileHandler",
        "configure_log_settings",
        "configure_log_writer",
        "log_settings",
    }
    assert set(SYMBOL_ALIASES["app.schemas.types"]) == {
        "MessageChannel",
        "NotificationType",
    }
    assert set(SYMBOL_ALIASES["app.schemas.message"]) == set(
        _MESSAGE_NOTIFICATION_SYMBOL_ALIASES
    )
