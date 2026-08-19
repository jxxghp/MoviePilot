"""处理链模块分层约束测试。"""

import ast
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATION_ROOT = PROJECT_ROOT / "app" / "application" / "orchestration"
LEGACY_MUSIC_SCAN_ROOTS = (
    PROJECT_ROOT / "app",
    PROJECT_ROOT / "scripts",
)
CHAIN_RECOGNIZE_SCAN_ROOTS = (
    PROJECT_ROOT / "app",
    PROJECT_ROOT / "scripts",
)
CHAIN_RECOGNIZE_METHODS = {"recognize_media", "async_recognize_media"}
MUSIC_SOURCE_CHAIN_FILES = (
    "acoustid.py",
    "douban.py",
    "listenbrainz.py",
    "lrclib.py",
    "musicbrainz.py",
    "theaudiodb.py",
)


def _imported_modules(path: Path) -> set[str]:
    """解析源码中的导入模块，包含函数内部的延迟导入。"""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def _inherited_recognize_calls(path: Path) -> list[tuple[int, str]]:
    """查找业务链通过 self 隐式调用媒体识别入口的位置。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if (
                isinstance(owner, ast.Name)
                and owner.id == "self"
                and node.func.attr in {"recognize_media", "async_recognize_media"}
        ):
            calls.append((node.lineno, node.func.attr))
    return calls


def test_chain_base_does_not_import_concrete_chains() -> None:
    """基础链不得反向导入任何具体处理链。"""
    imports = _imported_modules(ORCHESTRATION_ROOT / "__init__.py")

    # 下划线前缀的内部模块（_messaging/_recognition 等）是 ChainBase 的
    # 功能域 mixin，ports 是按业务域划分的能力端口客户端，context 是 ChainBase
    # 自身的依赖注入契约，data 是持久化端口注册表，四者都不是具体处理链，允许导入
    prefix = "app.application.orchestration."
    allowed_roots = {"ports", "context", "data"}
    assert not {
        module
        for module in imports
        if module.startswith(prefix)
        and not module.removeprefix(prefix).startswith("_")
        and module.removeprefix(prefix).partition(".")[0] not in allowed_roots
    }


def test_legacy_music_chain_is_removed() -> None:
    """聚合全部音乐职责的旧 MusicChain 文件和导入不得重新出现。"""
    legacy_module = "app.application.orchestration.music"
    assert not (ORCHESTRATION_ROOT / "music.py").exists()
    violations = {
        str(path.relative_to(PROJECT_ROOT)): sorted(
            module for module in _imported_modules(path) if module == legacy_module
        )
        for root in LEGACY_MUSIC_SCAN_ROOTS
        for path in root.rglob("*.py")
        if "plugins" not in path.parts  # 插件目录由插件仓自治，跳过
        if legacy_module in _imported_modules(path)
    }
    assert not violations


def test_music_source_chains_do_not_depend_on_public_orchestration_chains() -> None:
    """音乐数据源链不得反向依赖识别、刮削、搜索或推荐编排链。"""
    forbidden = {
        "app.application.orchestration.media",
        "app.application.orchestration.recommend",
        "app.application.orchestration.scraping",
        "app.application.orchestration.search",
    }
    violations = {
        filename: sorted(imported)
        for filename in MUSIC_SOURCE_CHAIN_FILES
        if (
            imported := _imported_modules(
                ORCHESTRATION_ROOT / filename
            ).intersection(forbidden)
        )
    }
    assert not violations


def test_media_chain_excludes_scraping_and_music_exploration_methods() -> None:
    """MediaChain 只保留公共识别与详情路由，不得重新承接刮削或音乐探索职责。"""
    path = ORCHESTRATION_ROOT / "media.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    method_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    forbidden_methods = {
        "async_get_doubaninfo_by_bangumiid",
        "async_get_doubaninfo_by_tmdbid",
        "async_get_tmdbinfo_by_bangumiid",
        "async_get_tmdbinfo_by_doubanid",
        "scrape_metadata",
        "scrape_metadata_event",
        "scrape_music_metadata",
        "get_doubaninfo_by_bangumiid",
        "get_doubaninfo_by_tmdbid",
        "get_music_lyrics",
        "get_tmdbinfo_by_bangumiid",
        "get_tmdbinfo_by_doubanid",
        "async_get_music_lyrics",
        "music_chart",
        "async_music_chart",
        "music_discover",
        "async_music_discover",
        "async_music_fresh_releases",
    }

    assert not method_names.intersection(forbidden_methods)
    assert "app.application.orchestration.scraping" not in _imported_modules(path)


def test_only_media_chain_owns_recognition_entry_points() -> None:
    """识别入口归媒体编排链所有，其余处理链不得经继承隐式调用。

    识别能力由基类下发给全部处理链，任何一条链都能写出 self.recognize_media(...)
    而不留下任何显式依赖痕迹。此处按「除真实归属者外全扫」判定，不维护白名单。
    """
    violations = {
        str(path.relative_to(ORCHESTRATION_ROOT)): calls
        for path in sorted(ORCHESTRATION_ROOT.rglob("*.py"))
        if path.name != "media.py"
        if (calls := _inherited_recognize_calls(path))
    }

    assert not violations


def _chain_constructor_class(call: ast.Call) -> Optional[str]:
    """从形如 XxxChain(...) 的构造调用中取出链类名，非 Chain 命名返回 None。"""
    func = call.func
    if isinstance(func, ast.Name) and func.id.endswith("Chain"):
        return func.id
    return None


def _non_media_chain_recognize_calls(path: Path) -> list[tuple[int, str]]:
    """
    查找对非 MediaChain 处理链发起媒体识别调用的位置。

    覆盖直接构造（XxxChain().recognize_media(...)）和局部变量
    （chain = XxxChain(); chain.recognize_media(...)）两种写法；变量到链类名的
    映射按整个文件收集，不区分函数作用域。
    """
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    chain_vars: dict[str, str] = {}
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
        ):
            class_name = _chain_constructor_class(node.value)
            if class_name:
                chain_vars[node.targets[0].id] = class_name
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in CHAIN_RECOGNIZE_METHODS:
            continue
        owner = node.func.value
        class_name = None
        if isinstance(owner, ast.Call):
            class_name = _chain_constructor_class(owner)
        elif isinstance(owner, ast.Name):
            class_name = chain_vars.get(owner.id)
        if class_name and class_name != "MediaChain":
            violations.append((node.lineno, f"{class_name}().{node.func.attr}"))
    return violations


def test_host_recognize_calls_only_use_media_chain() -> None:
    """宿主任意位置发起媒体识别都必须经 MediaChain，不得绕道其它处理链。"""
    violations = {
        str(path.relative_to(PROJECT_ROOT)): calls
        for root in CHAIN_RECOGNIZE_SCAN_ROOTS
        for path in root.rglob("*.py")
        if "plugins" not in path.parts  # 插件目录由插件仓自治，跳过
        if (calls := _non_media_chain_recognize_calls(path))
    }

    assert not violations
