"""站点类型化边界、canonical 依赖与文件布局门禁。"""

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
APP_ROOT = PROJECT_ROOT / "app"
SITE_APPLICATION_PACKAGE = APP_ROOT / "application" / "site"
SITE_CONTRACT_PATH = SITE_APPLICATION_PACKAGE / "contract.py"
CANONICAL_CONSUMER_PATHS = (
    APP_ROOT / "agent",
    APP_ROOT / "api",
    APP_ROOT / "application",
    APP_ROOT / "chain",
    APP_ROOT / "modules",
    APP_ROOT / "monitor",
    APP_ROOT / "startup",
    APP_ROOT / "workflow",
    APP_ROOT / "scheduler.py",
)


def _python_paths(root: Path) -> list[Path]:
    """返回 canonical 消费边界内的 Python 源文件。"""
    if root.is_file():
        return [root]
    return sorted(root.rglob("*.py"))


def _annotation_contains_any(annotation: ast.expr | None) -> bool:
    """判断跨层类型注解是否仍以 Any 隐藏数据合同。"""
    return annotation is not None and any(
        isinstance(node, ast.Name) and node.id == "Any"
        for node in ast.walk(annotation)
    )


def _is_frozen_slotted_dataclass(node: ast.ClassDef) -> bool:
    """判断 DTO 是否声明为不可变且使用 slots 的数据类。"""
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        if not isinstance(decorator.func, ast.Name) or decorator.func.id != "dataclass":
            continue
        keywords = {
            keyword.arg: keyword.value
            for keyword in decorator.keywords
            if keyword.arg is not None
        }
        return all(
            isinstance(keywords.get(name), ast.Constant)
            and keywords[name].value is True
            for name in ("frozen", "slots")
        )
    return False


def test_site_contract_uses_frozen_dtos_and_typed_ports() -> None:
    """站点四类快照、写 DTO 和 Query/Write/Repository 必须完整类型化。"""
    tree = ast.parse(
        SITE_CONTRACT_PATH.read_text(encoding="utf-8-sig"),
        filename=str(SITE_CONTRACT_PATH),
    )
    classes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }
    data_classes = {
        "SiteSnapshot",
        "SiteUserDataSnapshot",
        "SiteIconSnapshot",
        "SiteStatisticSnapshot",
        "SiteMutation",
        "SitePriorityMutation",
        "SiteWriteResult",
    }
    port_classes = {"SiteQueryPort", "SiteWritePort", "SiteRepository"}

    assert data_classes | port_classes <= classes.keys()
    assert all(_is_frozen_slotted_dataclass(classes[name]) for name in data_classes)

    violations: list[str] = []
    for class_name in data_classes | port_classes:
        for node in ast.walk(classes[class_name]):
            if isinstance(node, ast.AnnAssign) and _annotation_contains_any(node.annotation):
                violations.append(f"{class_name}:{node.lineno}:field")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                annotations = [
                    argument.annotation
                    for argument in (
                        *node.args.posonlyargs,
                        *node.args.args,
                        *node.args.kwonlyargs,
                    )
                    if argument.arg not in {"self", "cls"}
                ]
                if node.args.vararg is not None:
                    annotations.append(node.args.vararg.annotation)
                if node.args.kwarg is not None:
                    annotations.append(node.args.kwarg.annotation)
                annotations.append(node.returns)
                if any(_annotation_contains_any(annotation) for annotation in annotations):
                    violations.append(f"{class_name}:{node.lineno}:{node.name}")

    assert violations == []


def test_site_canonical_consumers_do_not_import_raw_persistence() -> None:
    """宿主消费者不得导入 SiteOper 或站点四张表的 ORM 类型。"""
    forbidden_modules = {
        "app.db.oper.site",
        "app.db.models.site",
        "app.db.models.siteicon",
        "app.db.models.sitestatistic",
        "app.db.models.siteuserdata",
    }
    forbidden_from_imports = {
        ("app.db.oper", "SiteOper"),
        ("app.db.oper.site", "SiteOper"),
        ("app.db.models", "Site"),
        ("app.db.models", "SiteIcon"),
        ("app.db.models", "SiteStatistic"),
        ("app.db.models", "SiteUserData"),
        ("app.db.models.site", "Site"),
        ("app.db.models.siteicon", "SiteIcon"),
        ("app.db.models.sitestatistic", "SiteStatistic"),
        ("app.db.models.siteuserdata", "SiteUserData"),
    }
    violations: list[str] = []

    for root in CANONICAL_CONSUMER_PATHS:
        for path in _python_paths(root):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if (node.module, alias.name) in forbidden_from_imports:
                            violations.append(
                                f"{relative}:{node.lineno}:{node.module}.{alias.name}"
                            )
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in forbidden_modules:
                            violations.append(f"{relative}:{node.lineno}:{alias.name}")

    assert violations == []


def test_site_application_has_one_typed_contract_without_any_ports() -> None:
    """Site 子包不得保留重复 Repository 或 Any 型跨层合同。"""
    retired_classes = {
        "SiteQueryRepository",
        "SiteMutationRepository",
        "SiteHealthRepository",
    }
    violations: list[str] = []
    for path in sorted(SITE_APPLICATION_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in retired_classes:
                violations.append(f"{relative}:{node.lineno}:{node.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue
                annotations = [
                    argument.annotation
                    for argument in (
                        *node.args.posonlyargs,
                        *node.args.args,
                        *node.args.kwonlyargs,
                    )
                    if argument.arg not in {"self", "cls"}
                ]
                if node.args.vararg is not None:
                    annotations.append(node.args.vararg.annotation)
                if node.args.kwarg is not None:
                    annotations.append(node.args.kwarg.annotation)
                annotations.append(node.returns)
                if any(_annotation_contains_any(annotation) for annotation in annotations):
                    violations.append(f"{relative}:{node.lineno}:{node.name}:Any")

    assert violations == []


def test_site_adapter_public_surface_has_no_any_annotations() -> None:
    """事务与请求级 Site adapter 的公开方法必须实现类型化端口。"""
    path = APP_ROOT / "db" / "adapters" / "site.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    adapter_names = {"TransactionalSiteRepository", "SessionSiteRepository"}
    classes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name in adapter_names
    }
    violations: list[str] = []

    assert classes.keys() == adapter_names
    for class_name, class_node in classes.items():
        for node in class_node.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_"):
                continue
            annotations = [
                argument.annotation
                for argument in (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                )
                if argument.arg not in {"self", "cls"}
            ]
            if node.args.vararg is not None:
                annotations.append(node.args.vararg.annotation)
            if node.args.kwarg is not None:
                annotations.append(node.args.kwarg.annotation)
            annotations.append(node.returns)
            if any(annotation is None for annotation in annotations):
                violations.append(f"{class_name}:{node.lineno}:{node.name}:missing")
            if any(_annotation_contains_any(annotation) for annotation in annotations):
                violations.append(f"{class_name}:{node.lineno}:{node.name}:Any")

    assert violations == []


def test_chain_runtime_context_owns_typed_site_repository() -> None:
    """Chain 站点仓储必须由显式运行上下文注入，旧数据 locator 不得存在。"""
    path = APP_ROOT / "application" / "chain" / "context.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    context = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ChainRuntimeContext"
    )
    annotations = {
        node.target.id: ast.unparse(node.annotation)
        for node in context.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }

    assert annotations["site_repository"] == "SiteRepository"
    assert not (APP_ROOT / "application" / "chain" / "data.py").exists()


def test_site_files_use_single_word_owners_without_package_reexports() -> None:
    """Site contract 与 adapter 使用单词文件，应用包根不得复制宿主导出面。"""
    assert SITE_CONTRACT_PATH.is_file()
    assert (APP_ROOT / "db" / "adapters" / "site.py").is_file()
    assert not (SITE_APPLICATION_PACKAGE / "site_contract.py").exists()
    assert not (APP_ROOT / "db" / "adapters" / "site_repository.py").exists()
    assert not (APP_ROOT / "db" / "adapters" / "site_ports.py").exists()

    package_path = SITE_APPLICATION_PACKAGE / "__init__.py"
    tree = ast.parse(
        package_path.read_text(encoding="utf-8-sig"),
        filename=str(package_path),
    )
    statements = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    assert statements == []
