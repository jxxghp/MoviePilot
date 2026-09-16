"""TransferChain 同名包的职责、公开面与兼容身份门禁。"""

import ast
import importlib
import pickle
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
CHAIN_ROOT = PROJECT_ROOT / "app" / "chain"
TRANSFER_PACKAGE = CHAIN_ROOT / "transfer"


def _class_methods(path: Path, class_name: str) -> set[str]:
    """读取指定 owner 类直接定义的方法集合。"""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.name
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_transfer_package_retires_monolith_and_keeps_thin_facade() -> None:
    """旧单体不得复活，Facade 只允许保留三个可替换 Chain 构造点。"""
    assert not (CHAIN_ROOT / "transfer.py").exists()
    assert not (CHAIN_ROOT / "_transfer.py").exists()
    assert {
        path.name for path in TRANSFER_PACKAGE.glob("*.py")
    } == {
        "__init__.py",
        "checkpoint.py",
        "contract.py",
        "execution.py",
        "facade.py",
        "filter.py",
        "format.py",
        "history.py",
        "plan.py",
        "queue.py",
        "records.py",
        "request.py",
        "retry.py",
        "scrape.py",
        "settlement.py",
        "workflow.py",
    }
    assert all(
        path.stem == "__init__" or path.stem.isalpha()
        for path in TRANSFER_PACKAGE.glob("*.py")
    )
    facade_path = TRANSFER_PACKAGE / "facade.py"
    assert len(facade_path.read_text(encoding="utf-8").splitlines()) <= 100
    assert _class_methods(facade_path, "TransferChain") == {
        "_transfer_media_chain",
        "_transfer_storage_chain",
        "_transfer_subscribe_chain",
    }


def test_transfer_package_root_exposes_only_stable_abi() -> None:
    """包根只保留 Chain 类型与现有插件使用的任务锁兼容入口。"""
    package = importlib.import_module("app.chain.transfer")
    queue_module = importlib.import_module("app.chain.transfer.queue")

    assert package.__all__ == ["TransferChain", "task_lock"]
    assert package.TransferChain.__module__ == "app.chain.transfer"
    assert package.task_lock is queue_module.task_lock
    assert pickle.loads(pickle.dumps(package.TransferChain)) is package.TransferChain
    for internal_name in (
        "JobManager",
        "TransferExecutionOwner",
        "TransferHistoryOwner",
        "TransferPlanningOwner",
        "TransferQueueOwner",
        "TransferSettlementOwner",
        "TransferWorkflowOwner",
        "_DurableTransferStepRunner",
        "_TransferOwnerHost",
    ):
        assert not hasattr(package, internal_name)


def test_transfer_chain_keeps_compatible_mro() -> None:
    """拆分后 mixin 顺序与历史方法解析顺序必须保持稳定。"""
    chain_type = importlib.import_module("app.chain.transfer").TransferChain
    owner_types = chain_type.__mro__[1:14]
    assert [base.__name__ for base in chain_type.__mro__[:15]] == [
        "TransferChain",
        "FileFilterMixin",
        "ScrapeBatchMixin",
        "EpisodeFormatMixin",
        "HistoryMatchMixin",
        "FileKeyMixin",
        "ManualHistoryMixin",
        "FailedRetryMixin",
        "TransferQueueOwner",
        "TransferPlanningOwner",
        "TransferExecutionOwner",
        "TransferSettlementOwner",
        "TransferWorkflowOwner",
        "TransferHistoryOwner",
        "ChainBase",
    ]
    assert [
        f"{owner.__name__}.{name}"
        for owner in owner_types
        for name in vars(owner)
        if name.startswith(f"_{owner.__name__}__")
    ] == []
    assert "_TransferOwnerHost" not in {
        owner.__name__ for owner in chain_type.__mro__
    }


def test_transfer_workflows_have_one_method_owner() -> None:
    """每个拆分职责的方法只能由一个 owner 定义，避免复制实现与 MRO 遮蔽。"""
    owner_classes = {
        "execution.py": "TransferExecutionOwner",
        "history.py": "TransferHistoryOwner",
        "plan.py": "TransferPlanningOwner",
        "queue.py": "TransferQueueOwner",
        "settlement.py": "TransferSettlementOwner",
        "workflow.py": "TransferWorkflowOwner",
    }
    method_owners: dict[str, list[str]] = {}
    for filename, class_name in owner_classes.items():
        for method_name in _class_methods(TRANSFER_PACKAGE / filename, class_name):
            method_owners.setdefault(method_name, []).append(filename)

    assert {
        name: owners for name, owners in method_owners.items() if len(owners) != 1
    } == {}

    chain_type = importlib.import_module("app.chain.transfer").TransferChain
    expected_modules = {
        "put_to_queue": "app.chain.transfer.queue",
        "replay_pending": "app.chain.transfer.queue",
        "process": "app.chain.transfer.queue",
        "_plan_checkpoint_and_execute": "app.chain.transfer.plan",
        "execute_legacy_transfer_command": "app.chain.transfer.plan",
        "_TransferChain__handle_transfer": "app.chain.transfer.execution",
        "_TransferChain__perform_transfer": "app.chain.transfer.execution",
        "_publish_transfer_result": "app.chain.transfer.settlement",
        "queue_failed_transfer_notification": "app.chain.transfer.settlement",
        "do_transfer": "app.chain.transfer.workflow",
        "_execute_transfer": "app.chain.transfer.workflow",
        "remote_transfer": "app.chain.transfer.history",
        "manual_transfer": "app.chain.transfer.history",
        "send_transfer_message": "app.chain.transfer.history",
    }
    assert {
        method_name: getattr(chain_type, method_name).__module__
        for method_name in expected_modules
    } == expected_modules


def test_transfer_cross_cutting_effects_have_single_owners() -> None:
    """线程锁和终态事件必须分别只由队列、结算 owner 持有。"""
    lock_definitions: list[str] = []
    event_methods: list[str] = []
    for path in sorted(TRANSFER_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in {
                        "downloader_lock",
                        "task_lock",
                    }:
                        lock_definitions.append(f"{path.name}:{target.id}")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
                "_durable_transfer_event",
                "_publish_transfer_result",
            }:
                event_methods.append(f"{path.name}:{node.name}")

    assert lock_definitions == [
        "queue.py:task_lock",
        "queue.py:downloader_lock",
    ]
    assert event_methods == [
        "settlement.py:_durable_transfer_event",
        "settlement.py:_publish_transfer_result",
    ]
