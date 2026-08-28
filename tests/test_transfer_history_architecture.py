"""整理历史类型化边界与适配器布局门禁。"""

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
APP_ROOT = PROJECT_ROOT / "app"
HISTORY_APPLICATION_PATH = APP_ROOT / "application" / "history.py"
QUERY_APPLICATION_PATH = APP_ROOT / "application" / "query.py"
HISTORY_ADAPTER_PACKAGE = APP_ROOT / "db" / "adapters" / "history"
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
    """判断类型注解是否仍以 Any 隐藏跨层数据合同。"""
    return annotation is not None and any(
        isinstance(node, ast.Name) and node.id == "Any"
        for node in ast.walk(annotation)
    )


def _is_frozen_slotted_dataclass(node: ast.ClassDef) -> bool:
    """判断类是否声明为不可变且使用 slots 的数据类。"""
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


def test_transfer_history_contract_is_typed_and_frozen() -> None:
    """整理历史必须使用冻结 DTO 和明确 Query/Write/Repository 端口。"""
    tree = ast.parse(
        HISTORY_APPLICATION_PATH.read_text(encoding="utf-8-sig"),
        filename=str(HISTORY_APPLICATION_PATH),
    )
    classes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }
    expected = {
        "TransferHistorySnapshot",
        "TransferHistoryWrite",
        "TransferHistoryQueryPort",
        "TransferHistoryWritePort",
        "TransferHistoryRepository",
    }

    assert expected <= classes.keys()
    assert _is_frozen_slotted_dataclass(classes["TransferHistorySnapshot"])
    assert _is_frozen_slotted_dataclass(classes["TransferHistoryWrite"])

    violations: list[str] = []
    for class_name in expected:
        class_node = classes[class_name]
        for node in ast.walk(class_node):
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


def test_history_query_port_returns_explicit_snapshots() -> None:
    """插件查询端口也必须声明冻结历史快照，不能以 object 隐藏合同。"""
    tree = ast.parse(
        QUERY_APPLICATION_PATH.read_text(encoding="utf-8-sig"),
        filename=str(QUERY_APPLICATION_PATH),
    )
    history_port = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "HistoryQueryPort"
    )
    expected = {
        "list_download_history": "QueryRows[DownloadHistorySnapshot]",
        "get_download_history": "DownloadHistorySnapshot | None",
        "list_transfer_history": "QueryRows[TransferHistorySnapshot]",
        "get_transfer_history": "TransferHistorySnapshot | None",
    }

    assert {
        node.name: ast.unparse(node.returns)
        for node in history_port.body
        if isinstance(node, ast.FunctionDef)
    } == expected


def test_transfer_history_retires_dynamic_writer_facade() -> None:
    """类型化仓储启用后不得保留动态 getter、Facade 或 add_force 合同。"""
    tree = ast.parse(
        HISTORY_APPLICATION_PATH.read_text(encoding="utf-8-sig"),
        filename=str(HISTORY_APPLICATION_PATH),
    )
    retired_classes = {
        "TransferHistoryRecord",
        "TransferHistoryWriter",
        "TransferHistoryPort",
        "AsyncTransferHistoryQueryRepository",
    }
    retired_functions = {
        "_get_transfer_history_writer",
        "get_transfer_history_port",
    }
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in retired_classes:
            violations.append(f"class:{node.name}:{node.lineno}")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in retired_functions or node.name == "add_force":
                violations.append(f"function:{node.name}:{node.lineno}")
        if isinstance(node, ast.Attribute) and node.attr == "add_force":
            violations.append(f"attribute:add_force:{node.lineno}")

    assert violations == []


def test_canonical_consumers_do_not_import_raw_transfer_history_persistence() -> None:
    """宿主消费者不得导入整理历史 ORM、Oper 或旧动态 getter。"""
    forbidden_from_imports = {
        ("app.db.models", "TransferHistory"),
        ("app.db.models.transferhistory", "TransferHistory"),
        ("app.db.oper", "TransferHistoryOper"),
        ("app.db.oper.transferhistory", "TransferHistoryOper"),
        ("app.application.history", "TransferHistoryPort"),
        ("app.application.history", "get_transfer_history_port"),
    }
    forbidden_modules = {
        "app.db.models.transferhistory",
        "app.db.oper.transferhistory",
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
                elif isinstance(node, ast.Attribute) and node.attr == "add_force":
                    violations.append(f"{relative}:{node.lineno}:add_force")
                elif isinstance(node, ast.Name) and node.id == "get_transfer_history_port":
                    violations.append(
                        f"{relative}:{node.lineno}:get_transfer_history_port"
                    )
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = (
                        node.targets
                        if isinstance(node, ast.Assign)
                        else [node.target]
                    )
                    value = node.value
                    if (
                        any(
                            isinstance(target, ast.Name)
                            and target.id.startswith("TransferHistory")
                            for target in targets
                        )
                        and value is not None
                        and any(
                            isinstance(child, ast.Name) and child.id == "Any"
                            for child in ast.walk(value)
                        )
                    ):
                        violations.append(f"{relative}:{node.lineno}:Any alias")

    assert violations == []


def test_add_force_is_owned_only_by_legacy_sdk_facade() -> None:
    """旧无类型替换入口只能存在于私有 Legacy facade。"""
    allowed = "app/sdk/_legacy/history.py"
    violations: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative.startswith("app/plugins/") or relative == allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "add_force":
                    violations.append(f"{relative}:{node.lineno}:definition")
            elif isinstance(node, ast.Attribute) and node.attr == "add_force":
                violations.append(f"{relative}:{node.lineno}:call")

    assert violations == []


def test_transfer_history_adapter_uses_topic_package_without_root_exports() -> None:
    """整理历史适配器必须位于 history/transfer.py，包根不得复制宿主导出面。"""
    adapter_path = HISTORY_ADAPTER_PACKAGE / "transfer.py"
    package_path = HISTORY_ADAPTER_PACKAGE / "__init__.py"

    assert adapter_path.is_file()
    assert not (APP_ROOT / "db" / "adapters" / "transferhistory.py").exists()
    assert not (APP_ROOT / "db" / "adapters" / "transfer_history.py").exists()

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
