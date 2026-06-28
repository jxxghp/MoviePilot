# -*- coding: utf-8 -*-
"""订阅自定义识别词快照用例：下载时保存完整识别词，整理时快照优先、实时反查兜底。

回归场景：订阅做季+集组合偏移（如 S04E05→S01E71），下载阶段生效但整理阶段因实时反查订阅
返回空而静默回退全局识别词、丢失偏移。修复后整理优先复用下载时保存的快照。
"""
from types import SimpleNamespace

import app.chain.transfer as transfer_module
from app.chain.download import DownloadChain
from app.chain.transfer import TransferChain


def _fake_history(custom_words=None, note=None):
    """构造仅含测试所需字段的下载历史替身。"""
    return SimpleNamespace(custom_words=custom_words, note=note)


def test_transfer_prefers_snapshot_over_live_lookup(monkeypatch):
    """整理时存在下载快照，应直接使用快照且不触发实时反查订阅。"""
    called = {"lookup": False}

    class _GuardSubscribeChain:
        def get_subscribe_by_source(self, source):
            # 一旦走到实时反查即视为失败：快照存在时不应触发
            called["lookup"] = True
            return SimpleNamespace(custom_words="不应使用\n实时反查")

    monkeypatch.setattr(transfer_module, "SubscribeChain", _GuardSubscribeChain)

    history = _fake_history(
        custom_words="S04 => S01\n第 <> 集 >> EP+66",
        note={"source": "Subscribe|{...}"},
    )
    result = TransferChain._get_subscribe_custom_words(history)

    assert result == ["S04 => S01", "第 <> 集 >> EP+66"]
    assert called["lookup"] is False


def test_transfer_falls_back_to_live_lookup_without_snapshot(monkeypatch):
    """整理时无快照（历史旧记录），应按下载来源实时反查订阅取识别词。"""

    class _FakeSubscribeChain:
        def get_subscribe_by_source(self, source):
            assert source == "Subscribe|{...}"
            return SimpleNamespace(custom_words="A => B")

    monkeypatch.setattr(transfer_module, "SubscribeChain", _FakeSubscribeChain)

    history = _fake_history(custom_words=None, note={"source": "Subscribe|{...}"})
    result = TransferChain._get_subscribe_custom_words(history)

    assert result == ["A => B"]


def test_transfer_returns_none_when_unavailable(monkeypatch):
    """无下载记录、note 非字典、或来源反查不到订阅时返回 None（回退全局识别词）。"""

    class _NoneSubscribeChain:
        def get_subscribe_by_source(self, source):
            return None

    monkeypatch.setattr(transfer_module, "SubscribeChain", _NoneSubscribeChain)

    # 无下载记录
    assert TransferChain._get_subscribe_custom_words(None) is None
    # 无快照且 note 非字典：不应触发实时反查
    assert TransferChain._get_subscribe_custom_words(_fake_history(note="不是字典")) is None
    # 无快照、来源可解析但反查不到订阅
    assert (
        TransferChain._get_subscribe_custom_words(
            _fake_history(note={"source": "Subscribe|{}"})
        )
        is None
    )


def test_download_snapshots_full_subscribe_custom_words(monkeypatch):
    """下载时应反查订阅并返回完整 custom_words 文本作为整理快照。"""
    import app.chain.subscribe as subscribe_module

    class _FakeSubscribeChain:
        def get_subscribe_by_source(self, source):
            assert source == "Subscribe|{...}"
            return SimpleNamespace(custom_words="S04 => S01\n第 <> 集 >> EP+66")

    # download.py 内为延迟导入，需 patch 源模块属性
    monkeypatch.setattr(subscribe_module, "SubscribeChain", _FakeSubscribeChain)

    snapshot = DownloadChain._DownloadChain__get_source_custom_words("Subscribe|{...}")
    assert snapshot == "S04 => S01\n第 <> 集 >> EP+66"


def test_download_snapshot_none_for_non_subscribe_source():
    """非订阅来源（source 为空）不保存快照，且不触发任何订阅反查。"""
    assert DownloadChain._DownloadChain__get_source_custom_words(None) is None
    assert DownloadChain._DownloadChain__get_source_custom_words("") is None
