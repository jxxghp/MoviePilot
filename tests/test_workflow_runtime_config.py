from datetime import datetime
from types import SimpleNamespace

from app.application.configuration import ChainRuntimeConfig
from app.schemas import ActionContext
from app.schemas.context import MediaInfo
from app.schemas.types import MediaType
from app.workflow.actions.add_subscribe import AddSubscribeAction
from app.workflow.actions.fetch_rss import FetchRssAction
from app.workflow.actions.scan_file import ScanFileAction
from app.workflow.actions import add_subscribe as add_subscribe_module
from app.workflow.actions import fetch_rss as fetch_rss_module
from app.workflow.actions import scan_file as scan_file_module


def _runtime_config(**overrides):
    """构造仅覆盖测试关注字段的 Chain 配置快照。"""
    values = {
        "media_extensions": (".mkv",),
        "subtitle_extensions": (".srt",),
        "audio_extensions": (".flac",),
        "superuser": "snapshot-admin",
        "proxy": {"https": "http://snapshot-proxy:7890"},
    }
    values.update(overrides)
    return ChainRuntimeConfig(**values)


def test_fetch_rss_reads_proxy_from_chain_snapshot(monkeypatch):
    """RSS 动作应使用一次 Chain 快照中的代理，而不是全局 settings。"""
    captured = {}

    class FakeRssHelper:
        """记录 RSS 请求参数的测试替身。"""

        def parse(self, **kwargs):
            captured.update(kwargs)
            return [{
                "title": "Example",
                "enclosure": "https://example.com/example.torrent",
                "link": "https://example.com/details",
                "size": 1,
                "pubdate": datetime(2026, 1, 1),
            }]

    monkeypatch.setattr(fetch_rss_module, "RssHelper", FakeRssHelper)
    monkeypatch.setattr(
        fetch_rss_module,
        "get_chain_runtime_config_snapshot",
        lambda: _runtime_config(),
    )
    monkeypatch.setattr(fetch_rss_module.global_vars, "is_workflow_stopped", lambda _: False)

    FetchRssAction("rss").execute(
        workflow_id=1,
        params={"url": "https://example.com/rss.xml", "proxy": True},
        context=ActionContext(),
    )

    assert captured["proxy"] == {"https": "http://snapshot-proxy:7890"}


def test_scan_file_filters_extensions_from_chain_snapshot(monkeypatch):
    """扫描动作应按快照后缀集合筛选媒体文件。"""

    class FakeStorageChain:
        """返回固定文件列表的存储链测试替身。"""

        def get_file_item(self, storage, directory):
            return SimpleNamespace(storage=storage, path=str(directory))

        def list_files(self, fileitem, recursion=True):
            return [
                SimpleNamespace(extension="mkv"),
                SimpleNamespace(extension="txt"),
                SimpleNamespace(extension="srt"),
            ]

    monkeypatch.setattr(scan_file_module, "StorageChain", FakeStorageChain)
    monkeypatch.setattr(
        scan_file_module,
        "get_chain_runtime_config_snapshot",
        lambda: _runtime_config(),
    )
    monkeypatch.setattr(scan_file_module.global_vars, "is_workflow_stopped", lambda _: False)

    context = ScanFileAction("scan").execute(
        workflow_id=1,
        params={"storage": "local", "directory": "/library"},
        context=ActionContext(),
    )

    assert [item.extension for item in context.fileitems] == ["mkv", "srt"]


def test_add_subscribe_uses_superuser_from_chain_snapshot(monkeypatch):
    """添加订阅动作应将快照中的超级管理员传给订阅链。"""
    captured = {}

    class FakeSubscribeChain:
        """记录订阅新增参数的测试替身。"""

        def exists(self, _mediainfo):
            return False

        def add(self, **kwargs):
            captured.update(kwargs)
            return 42, "ok"

    monkeypatch.setattr(add_subscribe_module, "SubscribeChain", FakeSubscribeChain)
    monkeypatch.setattr(
        add_subscribe_module,
        "get_chain_runtime_config_snapshot",
        lambda: _runtime_config(superuser="snapshot-owner"),
    )
    monkeypatch.setattr(add_subscribe_module.global_vars, "is_workflow_stopped", lambda _: False)
    monkeypatch.setattr(
        add_subscribe_module,
        "SubscribeOper",
        lambda: SimpleNamespace(get=lambda sid: sid),
    )

    AddSubscribeAction("subscribe").execute(
        workflow_id=1,
        params={},
        context=ActionContext(
            medias=[MediaInfo(type=MediaType.MOVIE, title="Example", year="2026")]
        ),
    )

    assert captured["username"] == "snapshot-owner"
