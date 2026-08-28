from datetime import datetime
from types import SimpleNamespace

from app.application.configuration import ChainRuntimeConfig
from app.schemas.context import MediaInfo
from app.schemas.types import MediaType
from app.schemas.workflow import ActionContext
from app.workflow.actions import add_subscribe as add_subscribe_module
from app.workflow.actions import fetch_medias as fetch_medias_module
from app.workflow.actions import fetch_rss as fetch_rss_module
from app.workflow.actions import scan_file as scan_file_module
from app.workflow.actions import send_message as send_message_module
from app.workflow.actions.add_subscribe import AddSubscribeAction
from app.workflow.actions.fetch_medias import FetchMediasAction
from app.workflow.actions.fetch_rss import FetchRssAction
from app.workflow.actions.scan_file import ScanFileAction
from app.workflow.actions.send_message import SendMessageAction


def _runtime_config(**overrides):
    """构造仅覆盖测试关注字段的 Chain 配置快照。"""
    values = {
        "media_extensions": (".mkv",),
        "subtitle_extensions": (".srt",),
        "audio_extensions": (".flac",),
        "superuser": "snapshot-admin",
        "proxy": {"https": "http://snapshot-proxy:7890"},
        "api_port": 18080,
        "api_token": "snapshot-api-token",
        "workflow_url": "https://example.com/#/workflow",
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
    monkeypatch.setattr(fetch_rss_module.runtime_stop_state, "is_workflow_stopped", lambda _: False)

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
    monkeypatch.setattr(scan_file_module.runtime_stop_state, "is_workflow_stopped", lambda _: False)

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

        subscription_repository = SimpleNamespace(get=lambda _sid: SimpleNamespace(to_dict=lambda: {
            "id": 42,
            "name": "Example",
            "type": MediaType.MOVIE.value,
        }))

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
    monkeypatch.setattr(add_subscribe_module.runtime_stop_state, "is_workflow_stopped", lambda _: False)
    AddSubscribeAction("subscribe").execute(
        workflow_id=1,
        params={},
        context=ActionContext(
            medias=[MediaInfo(type=MediaType.MOVIE, title="Example", year="2026")]
        ),
    )

    assert captured["username"] == "snapshot-owner"


def test_fetch_medias_internal_api_uses_chain_snapshot(monkeypatch):
    """获取媒体动作构造内部 API 地址时应读取 Chain 快照。"""
    captured = {}

    class FakeRequest:
        """记录内部 API 请求地址的测试替身。"""

        def __init__(self, **_kwargs):
            pass

        def post_res(self, url):
            captured["url"] = url
            return SimpleNamespace(json=lambda: [])

    monkeypatch.setattr(fetch_medias_module, "RequestUtils", FakeRequest)
    monkeypatch.setattr(
        fetch_medias_module,
        "get_chain_runtime_config_snapshot",
        lambda: _runtime_config(),
    )
    monkeypatch.setattr(fetch_medias_module.runtime_stop_state, "is_workflow_stopped", lambda _: False)

    action = FetchMediasAction("medias")
    action._FetchMediasAction__inner_sources = [
        {"api_path": "recommend/custom", "name": "自定义", "func": None}
    ]
    action.execute(
        workflow_id=1,
        params={"source_type": "ranking", "sources": ["recommend/custom"]},
        context=ActionContext(),
    )

    assert captured["url"] == "http://127.0.0.1:18080/api/v1/recommend/custom?token=snapshot-api-token"


def test_send_message_uses_workflow_url_from_chain_snapshot(monkeypatch):
    """工作流消息链接应来自 Chain 快照，而不是全局部署设置。"""
    captured = []

    class FakeActionChain:
        """记录发送消息载荷的测试替身。"""

        def post_message(self, message):
            captured.append(message)

    monkeypatch.setattr(send_message_module, "ActionChain", FakeActionChain)
    monkeypatch.setattr(
        send_message_module,
        "get_chain_runtime_config_snapshot",
        lambda: _runtime_config(),
    )

    context = ActionContext()
    context.progress = 100
    context.execute_history = [SimpleNamespace(action="测试", message="完成")]
    SendMessageAction("message").execute(
        workflow_id=1,
        params={"client": ["telegram"], "userid": "u1"},
        context=context,
    )

    assert captured[0].link == "https://example.com/#/workflow"
