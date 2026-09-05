"""订阅类型化边界、canonical 依赖与文件布局门禁。"""

import ast
import importlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
APP_ROOT = PROJECT_ROOT / "app"
SUBSCRIPTION_PACKAGE = APP_ROOT / "application" / "subscription"
SUBSCRIBE_CHAIN_PACKAGE = APP_ROOT / "chain" / "subscribe"
CONTRACT_PATH = SUBSCRIPTION_PACKAGE / "contract.py"
CANONICAL_CONSUMER_PATHS = (
    APP_ROOT / "agent",
    APP_ROOT / "api",
    APP_ROOT / "application",
    APP_ROOT / "chain",
    APP_ROOT / "startup",
    APP_ROOT / "workflow",
)


def _python_paths(root: Path) -> list[Path]:
    """返回 canonical 消费边界内的 Python 源文件。"""
    return sorted(root.rglob("*.py"))


def _annotation_contains_any(annotation: ast.expr | None) -> bool:
    """判断跨层类型注解是否仍以 Any 隐藏数据合同。"""
    return annotation is not None and any(
        isinstance(node, ast.Name) and node.id == "Any" for node in ast.walk(annotation)
    )


def _is_frozen_slotted_dataclass(node: ast.ClassDef) -> bool:
    """判断 DTO 是否声明为不可变且使用 slots 的数据类。"""
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        if not isinstance(decorator.func, ast.Name) or decorator.func.id != "dataclass":
            continue
        keywords = {keyword.arg: keyword.value for keyword in decorator.keywords if keyword.arg is not None}
        return all(
            isinstance(keywords.get(name), ast.Constant) and keywords[name].value is True
            for name in ("frozen", "slots")
        )
    return False


def test_subscription_contract_uses_frozen_dtos_and_typed_ports() -> None:
    """订阅快照、写 DTO 与 Query/Write/Repository 必须完整类型化。"""
    tree = ast.parse(
        CONTRACT_PATH.read_text(encoding="utf-8-sig"),
        filename=str(CONTRACT_PATH),
    )
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    data_classes = {
        "SubscriptionSnapshot",
        "SubscriptionHistorySnapshot",
        "SubscriptionIdentity",
        "SubscriptionHistoryPatch",
        "SubscriptionPatch",
        "SubscriptionWriteResult",
    }
    port_classes = {
        "SubscriptionQueryPort",
        "SubscriptionHistoryQueryPort",
        "SubscriptionWritePort",
        "SubscriptionStagingPort",
        "SubscriptionHistoryStagingPort",
        "SubscriptionRepository",
    }

    assert data_classes | port_classes <= classes.keys()
    assert all(_is_frozen_slotted_dataclass(classes[name]) for name in data_classes)

    violations: list[str] = []
    for class_name in data_classes | port_classes:
        for node in ast.walk(classes[class_name]):
            if isinstance(node, ast.AnnAssign) and _annotation_contains_any(node.annotation):
                violations.append(f"{class_name}:{node.lineno}:field")
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
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
                violations.append(f"{class_name}:{node.lineno}:{node.name}")

    assert violations == []


def test_subscription_canonical_consumers_do_not_import_raw_persistence() -> None:
    """宿主消费者不得导入订阅 Oper 或两张订阅 ORM 表。"""
    forbidden_modules = {
        "app.db.subscribe_oper",
        "app.db.subscribehistory_oper",
        "app.db.oper.subscribe",
        "app.db.oper.subscribehistory",
        "app.db.models.subscribe",
        "app.db.models.subscribehistory",
        "app.sdk._legacy.subscribe",
    }
    forbidden_symbols = {
        ("app.db.oper", "SubscribeOper"),
        ("app.db.oper", "SubscribeHistoryOper"),
        ("app.db.oper.subscribe", "SubscribeOper"),
        ("app.db.oper.subscribehistory", "SubscribeHistoryOper"),
        ("app.db.subscribe_oper", "SubscribeOper"),
        ("app.db.subscribehistory_oper", "SubscribeHistoryOper"),
        ("app.db.models", "Subscribe"),
        ("app.db.models", "SubscribeHistory"),
        ("app.db.models.subscribe", "Subscribe"),
        ("app.db.models.subscribehistory", "SubscribeHistory"),
    }
    violations: list[str] = []

    for root in CANONICAL_CONSUMER_PATHS:
        for path in _python_paths(root):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if (node.module, alias.name) in forbidden_symbols:
                            violations.append(f"{relative}:{node.lineno}:{node.module}.{alias.name}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in forbidden_modules:
                            violations.append(f"{relative}:{node.lineno}:{alias.name}")

    assert violations == []


def test_subscription_application_has_one_contract_owner() -> None:
    """Subscription 子包不得保留与 central CRUD 重复的持久化协议。"""
    retired_classes = {
        "SubscribeSnapshot",
        "SubscriptionQueryRepository",
        "AsyncSubscriptionQueryRepository",
        "AsyncSubscriptionHistoryQueryRepository",
        "SubscriptionMutationRepository",
        "SubscriptionHistoryMutationRepository",
        "SubscriptionCompletionRepository",
        "SubscribeSearchRepository",
        "SubscribeIdentityDeletionRepository",
        "SubscribeWriter",
        "StagedSubscription",
        "SubscriptionStagingRepository",
        "SubscribeDeletionRepository",
        "SyncSubscribeDeletionRepository",
    }
    violations: list[str] = []

    for path in sorted(SUBSCRIPTION_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in retired_classes:
                violations.append(f"{relative}:{node.lineno}:{node.name}")

    assert violations == []


def test_subscription_use_case_ports_do_not_return_any() -> None:
    """合法的用例专属 Repository 也必须返回 DTO，不能返回 Any 或 ORM。"""
    violations: list[str] = []

    for path in sorted(SUBSCRIPTION_PACKAGE.glob("*.py")):
        if path == CONTRACT_PATH:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            is_protocol = any(isinstance(base, ast.Name) and base.id == "Protocol" for base in class_node.bases)
            if not is_protocol or "Repository" not in class_node.name:
                continue
            for method in class_node.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if _annotation_contains_any(method.returns):
                    violations.append(f"{relative}:{method.lineno}:{class_node.name}.{method.name}")

    assert violations == []


def test_subscription_callers_do_not_alias_snapshot_to_any() -> None:
    """订阅 Chain 不得以 Any 别名继续消费未声明的数据形状。"""
    paths = [
        APP_ROOT / "chain" / "_music.py",
        APP_ROOT / "workflow" / "actions" / "add_subscribe.py",
        APP_ROOT / "application" / "messaging" / "subscribe.py",
        *sorted(SUBSCRIBE_CHAIN_PACKAGE.glob("*.py")),
    ]
    violations: list[str] = []

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if not isinstance(value, ast.Name) or value.id != "Any":
                continue
            for target in targets:
                if isinstance(target, ast.Name) and "Subscribe" in target.id:
                    violations.append(f"{relative}:{node.lineno}:{target.id}=Any")

    assert violations == []


def test_subscribe_chain_package_has_single_responsibility_owners() -> None:
    """SubscribeChain Facade 只保留稳定入口，各职责必须由唯一 owner 实现。"""
    assert not (APP_ROOT / "chain" / "subscribe.py").exists()
    expected_files = {
        "__init__.py",
        "completion.py",
        "contract.py",
        "context.py",
        "create.py",
        "facade.py",
        "identity.py",
        "interaction.py",
        "match.py",
        "metadata.py",
        "notify.py",
        "policy.py",
        "query.py",
        "reconcile.py",
        "refresh.py",
        "search.py",
        "searchtask.py",
    }
    assert {path.name for path in SUBSCRIBE_CHAIN_PACKAGE.glob("*.py")} == expected_files

    facade_path = SUBSCRIBE_CHAIN_PACKAGE / "facade.py"
    facade_tree = ast.parse(
        facade_path.read_text(encoding="utf-8-sig"),
        filename=str(facade_path),
    )
    facade = next(
        node
        for node in facade_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubscribeChain"
    )
    facade_methods = {
        node.name
        for node in facade.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert facade_methods == {
        "_matches_music_resource",
        "_music_download_chain",
        "_music_media_chain",
        "_music_search_chain",
        "_music_site_keywords",
        "reconcile_rule_group_references",
        "remove_site",
    }

    package = importlib.import_module("app.chain.subscribe")
    contract = importlib.import_module("app.chain.subscribe.contract")
    chain_type = package.SubscribeChain
    owner_base = contract._SubscribeOwnerBase
    assert chain_type.__module__ == "app.chain.subscribe"
    assert package.__all__ == ["SubscribeChain"]
    assert owner_base is object
    assert all(base.__name__ != "_SubscribeOwnerHost" for base in chain_type.__mro__)
    assert {
        "SubscriptionSharePort",
        "build_subscribe_meta",
        "configure_subscription_share_port",
        "reset_subscription_share_port",
    }.isdisjoint(vars(package))

    method_owners: dict[str, str] = {}
    for owner_path in sorted(SUBSCRIBE_CHAIN_PACKAGE.glob("*.py")):
        owner_tree = ast.parse(
            owner_path.read_text(encoding="utf-8-sig"),
            filename=str(owner_path),
        )
        for owner in (
            node
            for node in owner_tree.body
            if isinstance(node, ast.ClassDef) and node.name.endswith("Owner")
        ):
            for method in (
                node
                for node in owner.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ):
                assert method.name not in method_owners, (
                    f"{method.name} 同时由 {method_owners[method.name]} "
                    f"和 {owner_path.name} 实现"
                )
                method_owners[method.name] = owner_path.name

    expected_owners = {
        "add": "app.chain.subscribe.create",
        "search": "app.chain.subscribe.search",
        "match": "app.chain.subscribe.match",
        "refresh": "app.chain.subscribe.refresh",
        "refresh_subscribe_progress": "app.chain.subscribe.refresh",
        "_SubscribeChain__finish_subscribe": "app.chain.subscribe.completion",
        "_SubscribeChain__build_completion_notification": "app.chain.subscribe.notify",
        "_remove_site": "app.chain.subscribe.reconcile",
        "follow": "app.chain.subscribe.query",
        "remote_delete": "app.chain.subscribe.interaction",
        "get_episode_priority": "app.chain.subscribe.policy",
    }
    assert {
        method_name: getattr(chain_type, method_name).__module__
        for method_name in expected_owners
    } == expected_owners


def test_chain_runtime_context_owns_typed_subscription_repository() -> None:
    """Chain 订阅仓储必须由显式运行上下文注入，旧数据 locator 不得存在。"""
    context_path = APP_ROOT / "application" / "chain" / "context.py"
    tree = ast.parse(
        context_path.read_text(encoding="utf-8-sig"),
        filename=str(context_path),
    )
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

    assert annotations["subscription_repository"] == "SubscriptionRepository"
    assert annotations["subscription_mutation_scope"] == "SubscriptionMutationScope"
    assert annotations["sync_subscription_mutation_scope"] == "SyncSubscriptionMutationScope"
    assert annotations["subscription_delete_scope"] == "DeleteSubscribeScope"
    assert annotations["sync_subscription_delete_scope"] == "SyncDeleteSubscribeScope"
    assert annotations["subscription_completion_scope"] == "CompletionScope"
    assert not (APP_ROOT / "application" / "chain" / "data.py").exists()


def test_subscription_adapters_implement_typed_public_surface() -> None:
    """短事务与请求级订阅 adapter 的公开方法不得泄漏 Any。"""
    path = APP_ROOT / "db" / "adapters" / "subscription.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    adapter_names = {
        "TransactionalSubscriptionHistoryRepository",
        "TransactionalSubscriptionRepository",
        "SessionSubscriptionRepository",
        "SessionSubscriptionHistoryRepository",
    }
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in adapter_names}
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


def test_subscription_files_have_single_word_owners_without_reexports() -> None:
    """Subscription 能力留在同名包，文件使用单词且包根不重复导出。"""
    assert CONTRACT_PATH.is_file()
    assert (APP_ROOT / "db" / "adapters" / "subscription.py").is_file()
    assert all(path.stem == "__init__" or path.stem.isalpha() for path in SUBSCRIPTION_PACKAGE.glob("*.py"))
    assert not list((APP_ROOT / "application").glob("subscription_*.py"))
    assert not list((APP_ROOT / "db" / "adapters").glob("subscription_*.py"))

    package_path = SUBSCRIPTION_PACKAGE / "__init__.py"
    tree = ast.parse(
        package_path.read_text(encoding="utf-8-sig"),
        filename=str(package_path),
    )
    statements = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        )
    ]
    assert statements == []
