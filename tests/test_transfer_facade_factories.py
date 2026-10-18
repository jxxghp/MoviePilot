"""
整理链门面的可替换构造点测试。
"""

from unittest.mock import Mock

from app.chain.subscribe import facade as subscribe_facade
from app.chain.transfer import facade as transfer_facade
from app.chain.transfer import filter as transfer_filter
from app.chain.transfer import records as transfer_records


def test_transfer_facade_uses_injected_chain_factories(monkeypatch) -> None:
    """
    媒体、存储和订阅构造点使用各自职责模块中的工厂。

    :param monkeypatch: 恢复三种构造点的模块级依赖
    """
    media = object()
    storage = object()
    subscribe = object()
    media_factory = Mock(return_value=media)
    storage_factory = Mock(return_value=storage)
    subscribe_factory = Mock(return_value=subscribe)
    monkeypatch.setattr(transfer_filter, "MediaChain", media_factory)
    monkeypatch.setattr(transfer_records, "StorageChain", storage_factory)
    monkeypatch.setattr(subscribe_facade, "SubscribeChain", subscribe_factory)

    assert transfer_facade.TransferChain._transfer_media_chain() is media
    assert transfer_facade.TransferChain._transfer_storage_chain() is storage
    assert transfer_facade.TransferChain._transfer_subscribe_chain() is subscribe
    media_factory.assert_called_once_with()
    storage_factory.assert_called_once_with()
    subscribe_factory.assert_called_once_with()


def test_transfer_facade_falls_back_to_local_chain_factories(monkeypatch) -> None:
    """
    职责模块未提供工厂时，使用门面已有的本地构造点。

    :param monkeypatch: 临时撤下职责模块的构造器并注入本地替身
    """
    media = object()
    storage = object()
    media_factory = Mock(return_value=media)
    storage_factory = Mock(return_value=storage)
    monkeypatch.setattr(transfer_filter, "MediaChain", None)
    monkeypatch.setattr(transfer_records, "StorageChain", None)
    monkeypatch.setattr(transfer_facade, "MediaChain", media_factory)
    monkeypatch.setattr(transfer_facade, "StorageChain", storage_factory)

    assert transfer_facade.TransferChain._transfer_media_chain() is media
    assert transfer_facade.TransferChain._transfer_storage_chain() is storage
    media_factory.assert_called_once_with()
    storage_factory.assert_called_once_with()
