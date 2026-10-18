"""
历史删除用例的请求级依赖组装测试。
"""

from types import SimpleNamespace
from unittest.mock import Mock

from app.api.dependencies import history as history_dependencies
from app.schemas.types import EventType


def test_transfer_history_mutation_dependency_wires_storage_and_event(monkeypatch) -> None:
    """
    确认删除用例使用当前请求的仓储，并发布下载文件删除事件。

    :param monkeypatch: 隔离存储链和进程级事件总线
    """
    storage = SimpleNamespace(exists=Mock(return_value=True), delete_media_file=Mock(return_value=True))
    storage_factory = Mock(return_value=storage)
    event_sender = Mock()
    monkeypatch.setattr(history_dependencies, "StorageChain", storage_factory)
    monkeypatch.setattr(history_dependencies.eventmanager, "send_event", event_sender)

    repository = object()
    download_repository = object()
    execution_repository = object()
    unit_of_work = object()
    db = object()
    runtime = SimpleNamespace(
        history=SimpleNamespace(
            transfer_mutation_repository=Mock(return_value=repository),
            download_repository=Mock(return_value=download_repository),
            transfer_execution_repository=execution_repository,
        ),
        persistence=SimpleNamespace(sync_transaction=Mock(return_value=unit_of_work)),
    )

    command = history_dependencies.get_transfer_history_mutation_command(db=db, runtime=runtime)

    assert command._repository is repository
    assert command._download_repository is download_repository
    assert command._transfer_execution_repository is execution_repository
    assert command._unit_of_work is unit_of_work
    assert command._file_exists is storage.exists
    assert command._delete_media_file is storage.delete_media_file
    storage_factory.assert_called_once_with()
    runtime.history.transfer_mutation_repository.assert_called_once_with(db)
    runtime.history.download_repository.assert_called_once_with(db)
    runtime.persistence.sync_transaction.assert_called_once_with(db)

    payload = {"src": "example"}
    assert command._publish_download_file_deleted(payload) is None
    event_sender.assert_called_once_with(EventType.DownloadFileDeleted, payload)
