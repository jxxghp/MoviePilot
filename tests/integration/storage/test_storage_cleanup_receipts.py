"""
验证存储严格查询回执与整理历史清理的未知状态和提供方回退。
"""



from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.chain.storage import StorageChain
from app.chain.transfer.records import ManualHistoryMixin
from app.modules.filemanager.module import FileManagerModule
from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher
from app.schemas.exception import StorageQueryError
from app.schemas.file import FileItem, FileItemQueryResult


@pytest.mark.parametrize("failed_lookup", [0, 1])
def test_manual_cleanup_preserves_history_when_target_state_is_unknown(tmp_path, monkeypatch, failed_lookup):
    """
    首次或删除失败后的严格查询出错时，不删除仍有目标的历史及失败计数。
    """
    path = tmp_path / "target.mkv"
    path.write_bytes(b"existing-media")
    item = FileItem(storage="local", path=str(path), type="file")
    storage = object.__new__(StorageChain)
    calls = []

    def query(strict):
        """
        在存储模块边界回放普通查询吞错和严格查询抛错的同一故障。
        """
        index = len(calls)
        calls.append(index)
        if index == failed_lookup:
            if strict:
                raise OSError("storage unavailable")
            return None
        return item

    monkeypatch.setattr(storage, "run_module", lambda *_args, **_kwargs: query(False))
    monkeypatch.setattr(storage, "run_module_strict", lambda *_args, **_kwargs: FileItemQueryResult(item=query(True)))
    monkeypatch.setattr(storage, "delete_media_file", Mock(return_value=False))
    monkeypatch.setattr("app.chain.transfer.records.StorageChain", lambda: storage)
    cleared = Mock()
    monkeypatch.setattr("app.chain.transfer.records.clear_transfer_failures", cleared)
    repository = SimpleNamespace(delete=Mock())
    history = SimpleNamespace(id=42, transfer_task_id=None, status=True, mode="copy",
                              dest_fileitem=item.model_dump(), src="/source", src_storage="local")

    success, message = ManualHistoryMixin._delete_manual_transfer_history(SimpleNamespace(), history, repository)

    assert not success
    assert message
    assert path.read_bytes() == b"existing-media"
    repository.delete.assert_not_called()
    cleared.assert_not_called()


@pytest.mark.parametrize("scenario", ["absent", "delete_success", "delete_failed"])
def test_manual_cleanup_requires_confirmed_target_cleanup(monkeypatch, scenario):
    """
    明确不存在或删除成功才清历史；删除失败且目标仍在则保留历史和失败计数。
    """
    item = FileItem(storage="local", path="/library/target.mkv", type="file")
    storage = object.__new__(StorageChain)
    monkeypatch.setattr(storage, "run_module_strict", Mock(return_value=FileItemQueryResult(item=None if scenario == "absent" else item)))
    deletion = Mock(return_value=scenario == "delete_success")
    monkeypatch.setattr(storage, "delete_media_file", deletion)
    monkeypatch.setattr("app.chain.transfer.records.StorageChain", lambda: storage)
    cleared = Mock()
    monkeypatch.setattr("app.chain.transfer.records.clear_transfer_failures", cleared)
    repository = SimpleNamespace(delete=Mock())
    history = SimpleNamespace(id=42, transfer_task_id=None, status=True, mode="copy",
                              dest_fileitem=item.model_dump(), src="/source", src_storage="local")

    success, _message = ManualHistoryMixin._delete_manual_transfer_history(SimpleNamespace(), history, repository)

    assert success is (scenario != "delete_failed")
    assert repository.delete.called is success
    assert cleared.called is success
    assert deletion.called is (scenario != "absent")


@pytest.mark.parametrize("availability", ["unknown", "uninitialized", "no-module", "no-adapter"])
def test_manual_cleanup_keeps_history_without_available_storage_provider(monkeypatch, availability):
    """
    真实模块调度和 FileManager 对未知或未初始化存储不能伪造“目标不存在”。
    """
    module = object.__new__(FileManagerModule)
    module._support_storages = [] if availability == "uninitialized" else ["local"]
    monkeypatch.setattr(module, "_FileManagerModule__get_storage_oper", lambda _storage: None)
    modules = [] if availability == "no-module" else [module]
    def running_modules(method):
        """
        返回实现本次能力的宿主模块快照。

        :param method: 正在派发的能力名称
        :return: 只包含该能力实现方的模块列表
        """
        return [provider for provider in modules if hasattr(provider, method)]

    catalog = SimpleNamespace(get_running_modules=running_modules)
    dispatcher = ModuleInvocationDispatcher(module_catalog=catalog,
        plugin_catalog=SimpleNamespace(get_plugin_modules=lambda: {}), plugin_error_handler=Mock(),
        system_error_handler=Mock(), rate_limit_handler=Mock())
    storage = object.__new__(StorageChain)
    storage._module_dispatcher = dispatcher
    monkeypatch.setattr("app.chain.transfer.records.StorageChain", lambda: storage)
    cleared = Mock()
    monkeypatch.setattr("app.chain.transfer.records.clear_transfer_failures", cleared)
    item = FileItem(storage="unknown" if availability == "unknown" else "local", path="/target", type="file")
    history = SimpleNamespace(id=42, transfer_task_id=None, status=True, mode="copy",
                              dest_fileitem=item.model_dump(), src="/source", src_storage="local")
    repository = SimpleNamespace(delete=Mock())

    success, message = ManualHistoryMixin._delete_manual_transfer_history(SimpleNamespace(), history, repository)

    assert not success
    assert message
    repository.delete.assert_not_called()
    cleared.assert_not_called()
    # 普通查询继续保留既有 nullable 合同，不把严格用途扩散到普通浏览请求。
    assert storage.get_file_item(item.storage, Path(item.path)) is None


@pytest.mark.parametrize("placement", ["plugin", "before-host", "after-host"])
@pytest.mark.parametrize("found", [False, True])
def test_strict_storage_receipt_preserves_provider_fallback(monkeypatch, placement, found):
    """
    插件或任意宿主排序的自定义存储回执都能接管，明确不存在不与不适用混淆。
    """
    module = object.__new__(FileManagerModule)
    module._support_storages = ["local"]
    monkeypatch.setattr(module, "_FileManagerModule__get_storage_oper", lambda _storage: None)
    expected = FileItem(storage="custom", path="/target") if found else None
    provider = SimpleNamespace(get_priority=lambda: 1 if placement == "before-host" else 9,
                               get_name=lambda: "custom", get_file_item_strict=lambda **_kwargs: FileItemQueryResult(item=expected))
    modules = [module] if placement == "plugin" else [module, provider]
    plugins = {("custom", "custom"): {"get_file_item_strict": provider.get_file_item_strict}} if placement == "plugin" else {}
    def running_modules(method):
        """
        返回实现本次能力的宿主模块快照。

        :param method: 正在派发的能力名称
        :return: 只包含该能力实现方的模块列表
        """
        return [entry for entry in modules if hasattr(entry, method)]

    catalog = SimpleNamespace(get_running_modules=running_modules)
    chain = object.__new__(StorageChain)
    chain._module_dispatcher = ModuleInvocationDispatcher(module_catalog=catalog,
        plugin_catalog=SimpleNamespace(get_plugin_modules=lambda: plugins), plugin_error_handler=Mock(),
        system_error_handler=Mock(), rate_limit_handler=Mock())

    assert chain.get_file_item_strict("custom", Path("/target")) == expected


def test_strict_storage_keeps_legacy_nonempty_plugin_lookup(monkeypatch):
    """
    未实现新回执的旧插件仍可返回真实文件项，空结果则必须保守拒绝删除用途。
    """
    expected = FileItem(storage="custom", path="/target")
    lookup = Mock(return_value=expected)
    catalog = SimpleNamespace(get_running_modules=lambda _method: [])
    chain = object.__new__(StorageChain)
    chain._module_dispatcher = ModuleInvocationDispatcher(module_catalog=catalog,
        plugin_catalog=SimpleNamespace(get_plugin_modules=lambda: {("legacy", "legacy"): {"get_file_item": lookup}}),
        plugin_error_handler=Mock(), system_error_handler=Mock(), rate_limit_handler=Mock())
    assert chain.get_file_item_strict("custom", Path("/target")) == expected
    lookup.return_value = None
    with pytest.raises(StorageQueryError, match="没有能够确认目标状态"):
        chain.get_file_item_strict("custom", Path("/target"))
