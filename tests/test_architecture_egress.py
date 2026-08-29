"""宿主 direct egress 事实与人工政策契约。"""

import json
from pathlib import Path

from scripts.architecture.baseline import collect_dependency_baseline
from scripts.architecture.egress import collect_direct_egress

PROJECT_ROOT = Path(__file__).parents[1]
DEPENDENCY_POLICY_PATH = (
    PROJECT_ROOT / "tests" / "fixtures" / "architecture" / "dependency-policy.json"
)
FROZEN_EGRESS_EDGES_BY_REASON = {
    "canonical_transport": {
        ("app.adapters.cache.redis", "redis"),
        ("app.adapters.network.browser", "cloakbrowser"),
        ("app.adapters.network.doh", "socket.getaddrinfo"),
        ("app.adapters.network.doh", "urllib.request"),
        ("app.adapters.network.http", "httpx"),
        ("app.adapters.network.http", "httpx2"),
        ("app.adapters.network.http", "requests"),
        ("app.adapters.network.ip", "socket.gethostbyname"),
        ("app.adapters.network.resolver", "socket.getaddrinfo"),
    },
    "transport_configuration": {
        ("app.adapters.network.http", "urllib3"),
    },
    "sdk_transport": {
        ("app.agent.llm.capability", "openai"),
        ("app.agent.llm.helper", "google.genai"),
        ("app.agent.llm.helper", "httpx"),
        ("app.agent.llm.helper", "langchain_anthropic"),
        ("app.agent.llm.helper", "langchain_aws"),
        ("app.agent.llm.helper", "langchain_deepseek"),
        ("app.agent.llm.helper", "langchain_google_genai"),
        ("app.agent.llm.helper", "langchain_openai"),
        ("app.agent.llm.helper", "openai"),
        ("app.agent.llm.provider", "boto3"),
        ("app.agent.llm.provider", "botocore"),
        ("app.agent.llm.provider", "google.genai"),
        ("app.agent.llm.provider", "openai"),
        ("app.agent.tools.impl.search_web", "ddgs"),
        ("app.api.endpoints.message", "pywebpush"),
        ("app.modules.discord.discord", "discord"),
        ("app.modules.feishu.feishu", "lark_oapi"),
        ("app.modules.filemanager.storages.smb", "smbclient"),
        ("app.modules.filemanager.storages.u115", "oss2"),
        ("app.modules.plex.plex", "plexapi"),
        ("app.modules.qbittorrent.qbittorrent", "qbittorrentapi"),
        ("app.modules.slack.slack", "slack_bolt"),
        ("app.modules.slack.slack", "slack_sdk"),
        ("app.modules.telegram.telegram", "telebot"),
        ("app.modules.transmission.transmission", "transmission_rpc"),
        ("app.modules.webpush", "pywebpush"),
    },
    "streaming_protocol": {
        ("app.modules.qqbot.gateway", "websocket"),
        ("app.modules.rtorrent.rtorrent", "socket.create_connection"),
        ("app.modules.rtorrent.rtorrent", "xmlrpc.client.ServerProxy"),
        ("app.modules.wechat.wechatbot", "websocket"),
    },
    "contained_vendor": {
        ("app.modules.themoviedb.tmdbv3api.tmdb", "requests"),
    },
    "local_control_plane": {
        ("app.cli", "urllib.request"),
    },
    "diagnostic_probe": {
        ("app.doctor.checks", "socket.create_connection"),
        ("app.doctor.checks", "urllib.request"),
    },
    "type_or_compat_only": {
        ("app.adapters.external.plugin.client", "httpx2"),
        ("app.adapters.external.plugin.client", "requests"),
        ("app.modules.emby.emby", "requests"),
        ("app.modules.filemanager.storages.smb", "smbprotocol"),
        ("app.modules.jellyfin.jellyfin", "requests"),
        ("app.modules.rtorrent.rtorrent", "http.client"),
        ("app.modules.telegram.compat", "urllib3"),
        ("app.modules.zspace.zspace", "requests"),
        ("app.startup.lifecycle", "urllib3"),
    },
    "daemon_control_plane": {
        ("app.runtime.state", "docker"),
    },
    "test_network_guard": {
        ("app.testing.network_guard", "socket.getaddrinfo"),
    },
}
FROZEN_EGRESS_REASON_BY_EDGE = {
    edge: reason
    for reason, edges in FROZEN_EGRESS_EDGES_BY_REASON.items()
    for edge in edges
}
FROZEN_EGRESS_FINGERPRINT_BY_EDGE = {
    ("app.adapters.cache.redis", "redis"): "49f0b28ef731b25aa772d6887febd762d03b981a1f52d6dfc8dff82f2f489f81",
    ("app.adapters.external.plugin.client", "httpx2"): "2d249b0906bc7f8fa29f569d6bcc5667d4290a551b1a06bcdf85e09ec50a793d",
    ("app.adapters.external.plugin.client", "requests"): "b5239e7eee9c23569b7fec66791e56bcc226db86a8d09ee7fc58d3ba8bc14f09",
    ("app.adapters.network.browser", "cloakbrowser"): "15c1777b14eb9147d6cab9783f67577011714f9220600dda5db5269b59726173",
    ("app.adapters.network.doh", "socket.getaddrinfo"): "4ff03419dfacc6bf582b7d4421dd5a0666a63f8ca79be2b1e625f4c8f4c96b71",
    ("app.adapters.network.doh", "urllib.request"): "6f5f5fd3da02a9e780ea5e7cc1e47bd962314a1a358f14b4ee698485f96ab52b",
    ("app.adapters.network.http", "httpx"): "c00458c868240f1adfe6278ba22bd5c4782e9bd2fcf896114c2288bf82981ee3",
    ("app.adapters.network.http", "httpx2"): "a70799e9930ff79cd28ebed92836c8108cd2d18bc668e0081f68533d84f69d0d",
    ("app.adapters.network.http", "requests"): "dffac27a2e24803dd7c50b238ba44b4f106b7b105e63a487edc12258f65c710d",
    ("app.adapters.network.http", "urllib3"): "66cbd8ec4e7552bd458db0baada30f1953e6d0493793822d23a2199567bca98a",
    ("app.adapters.network.ip", "socket.gethostbyname"): "a43e8969e2f26546fcf925b258728b309f1b9b966ff3c7b48a64273b8c82d048",
    ("app.adapters.network.resolver", "socket.getaddrinfo"): "1b1ce6763d730d7aa9c35d32a668560013c939467dcb3087357ffa53653ff306",
    ("app.agent.llm.capability", "openai"): "60160ab01b60be2c9794b9724a50b90f8875c6a10748ff9392a1e4bc7d48c334",
    ("app.agent.llm.helper", "google.genai"): "d41688ecda4c5de296fabb0d0d25c79a7b0f29f67ccdd44569dc1110e1db9b86",
    ("app.agent.llm.helper", "httpx"): "caeccd432f48d9a23948dd5b9220ddbebc57d2b2936bfa499fc7d4876e935ea8",
    ("app.agent.llm.helper", "langchain_anthropic"): "c8d2c3166c4d42d61e7ef38a25080cac0f93422f47393df6dc19b329a7f72265",
    ("app.agent.llm.helper", "langchain_aws"): "51dd02f5e2305cce4e1f6f29b4ed47edf54dde9f6557781178a6edf96bfeeb6b",
    ("app.agent.llm.helper", "langchain_deepseek"): "bcf8b1ffae0995cfc33cfe26609cef8014cd2ce036b845c2a88c0cd878b1ebc4",
    ("app.agent.llm.helper", "langchain_google_genai"): "79ae88c63908da9ea36dcfc23c4ad5345bc5d1977b80cc2e0b89292d063ed8ee",
    ("app.agent.llm.helper", "langchain_openai"): "15775bf1694780d5cf516e6cc3d3b5174ebb25e3038433132d3071e6abceba52",
    ("app.agent.llm.helper", "openai"): "dbebd5e9fa38b2a525a5a2e5a92d855ba9d05762410590bfb544e665fe38f620",
    ("app.agent.llm.provider", "boto3"): "04ca4b5ecbda1531266d5f53817dde061dcfdd3293cabf9301fc8d8e4c281a88",
    ("app.agent.llm.provider", "botocore"): "fbeaa702703665ca60233a153aade0da7e49dfb6fe31f72e5593c883fb7d3f11",
    ("app.agent.llm.provider", "google.genai"): "b1a42e9a06f7cf3bdd1cb069a73c3597ebd0433cf64da6c0a305cccb56d257b7",
    ("app.agent.llm.provider", "openai"): "ffac3b355eb640f6421299cf838cb4e24866a39afabd9dcfd8d0f340925dc1a8",
    ("app.agent.tools.impl.search_web", "ddgs"): "377e73bb3be804b825d60ad6e792344ca633de107f8c4dbb45d50a34a4dfa04b",
    ("app.api.endpoints.message", "pywebpush"): "7d83ca0dc89dfb6af1cdd7bc90d922105de94222e4e3a44bba4f53bbd97bc31b",
    ("app.cli", "urllib.request"): "71b3e5be2a7d85fdc7a207d15c0e690d7ab96b335b882a3eaec03a1cb4ec1d1b",
    ("app.doctor.checks", "socket.create_connection"): "dfb34d4710353029dcc06e0e0ce3b299bae1a7e71a5743de1d253ac26380f9b6",
    ("app.doctor.checks", "urllib.request"): "d8dd2279263ef58c3b4c59c70451533a4fc27d2ccd76960ca3aff29cd73f3354",
    ("app.modules.discord.discord", "discord"): "e1486525fedbfe574aa47d60b483426c8c48e4e27fa35dbe34f6cd2f47d37c86",
    ("app.modules.emby.emby", "requests"): "6c4c17fe170226ea1272f5859bc119c2775442cabe8ce2a9edcabaf9c37bec31",
    ("app.modules.feishu.feishu", "lark_oapi"): "99d16968a59932b5980f6d42ea2bbc8fa35ccdb1a27933beec29b4bdc093d78b",
    ("app.modules.filemanager.storages.smb", "smbclient"): "62e0585282ef206ac81b2bf9e93a58423fb058685edc0ef4ee592453bbf30376",
    ("app.modules.filemanager.storages.smb", "smbprotocol"): "f02686afd99c59820dffa7b4c2627a0be1ad62980c98a9cd33b7564697842b22",
    ("app.modules.filemanager.storages.u115", "oss2"): "f7b89c8ae6dad2603f0a9e0caaa159769aef5b3581d7e728f62445b979366eae",
    ("app.modules.jellyfin.jellyfin", "requests"): "5c46d09ca9a4bcc0bae21ca5d554ef09baa3f901c562b27c5f5ee1439c7746b5",
    ("app.modules.plex.plex", "plexapi"): "76c1334863dc6c6623ce6ad3415bec5ff92656f51c31e4d982285898f193e57e",
    ("app.modules.qbittorrent.qbittorrent", "qbittorrentapi"): "b2f5a27f0c54cf95ed42fe99848fd6c9c0641caca8bc4ff8006a411cb427066d",
    ("app.modules.qqbot.gateway", "websocket"): "6f6b7d61f3a95e620e67c450d544f0aa077f087a188d98f32e4501d94c6b37ae",
    ("app.modules.rtorrent.rtorrent", "http.client"): "6adc93b3bc479dfe81197554c7977930a60abc51714f14ef29298eb40730aaf7",
    ("app.modules.rtorrent.rtorrent", "socket.create_connection"): "878114d6b1bda091bd3b3d8aa819831d84381682feba78267592d2593295af90",
    ("app.modules.rtorrent.rtorrent", "xmlrpc.client.ServerProxy"): "2ac64b5670c930bc28bb2135cc4b73891c1cae01a74a9586f74e302878c45c85",
    ("app.modules.slack.slack", "slack_bolt"): "be5dcb032ece8d8627abeb243f98143aaf60f26751ab6e5e098813d4048419e6",
    ("app.modules.slack.slack", "slack_sdk"): "7559f31e4172ad3bbbaf161e1164ea48b997c56d05c77b49b82626298c39aa14",
    ("app.modules.telegram.compat", "urllib3"): "18862bbdb252b59573f57ea776b5d64bfb775e7739603fecd23cc8f2c38a7e0d",
    ("app.modules.telegram.telegram", "telebot"): "78f5ab18bfd67ba4fa0f3c0fc4a1a561a7b1d9e81e335fb80de86edb480b84b8",
    ("app.modules.themoviedb.tmdbv3api.tmdb", "requests"): "d605eb176a203b3f4d205c5d183469b3002426682eb9cb4e7408bb6013652484",
    ("app.modules.transmission.transmission", "transmission_rpc"): "1652e661cb17dbadb039fdc4ab73d6d06e47eb118292ba3693313e45834959cf",
    ("app.modules.webpush", "pywebpush"): "389c73b06150e3d5bcaf31f35a25178873d2ed38ef9a28354cb9bab691eeab76",
    ("app.modules.wechat.wechatbot", "websocket"): "1bae78270eadce0571e2caaa111a5c0a9065ba2da97ebb26b8a5b76d3ed5eef6",
    ("app.modules.zspace.zspace", "requests"): "9df3fd27b9696d45a72e7c8f67b5a9ad79a7371d1fe690bbaa17485bd1960d51",
    ("app.runtime.state", "docker"): "20a91ec521f7dfe6a0153dfd8ea49c4bac7f0a16b55f1c0655dfb33f54a01215",
    ("app.startup.lifecycle", "urllib3"): "cb6f0a314aeb1e2d3e76c240aa20460ac0c36d9f5c18c1a6ea3170f64dd3366b",
    ("app.testing.network_guard", "socket.getaddrinfo"): "2518de211c9ba32ccbc004cf58fe9c98837fe8ac95fe8ffb6ea82897b28d753f",
}


def _module(tmp_path: Path, name: str, content: str) -> dict[str, Path]:
    """创建供 direct egress collector 使用的独立模块。"""
    path = tmp_path / f"{name.replace('.', '_')}.py"
    path.write_text(content, encoding="utf-8")
    return {name: path}


def _entries_by_target(value: dict[str, object]) -> dict[str, dict[str, object]]:
    """把单 source 合成事实按 target 建索引。"""
    return {entry["target"]: entry for entry in value["entries"]}


def _policy_facts(groups: list[dict[str, object]]) -> list[dict[str, str]]:
    """展开按语义分组保存的人工 egress fact 引用。"""
    return [fact for group in groups for fact in group["facts"]]


def _policy_differences(
    facts: list[dict[str, object]],
    reviewed: list[dict[str, str]],
) -> tuple[list[str], list[str]]:
    """按完整事实指纹返回未审查事实和陈旧人工 policy。"""
    actual = {str(fact["fingerprint"]) for fact in facts}
    expected = {fact["fingerprint"] for fact in reviewed}
    return sorted(actual - expected), sorted(expected - actual)


def _classification_differences(
    facts: list[dict[str, object]],
    groups: list[dict[str, object]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]]]:
    """返回新增边、陈旧分类和与初始语义不符的分类。"""
    actual = {(str(fact["source"]), str(fact["target"])) for fact in facts}
    reviewed = {
        (str(fact["source"]), str(fact["target"])): str(group["reason_code"])
        for group in groups
        for fact in group["facts"]
    }
    unexpected = sorted(actual - FROZEN_EGRESS_REASON_BY_EDGE.keys())
    stale = sorted(reviewed.keys() - actual)
    misclassified = sorted(
        edge
        for edge in actual & reviewed.keys() & FROZEN_EGRESS_REASON_BY_EDGE.keys()
        if reviewed[edge] != FROZEN_EGRESS_REASON_BY_EDGE[edge]
    )
    return unexpected, stale, misclassified


def _fingerprint_differences(
    facts: list[dict[str, object]],
) -> list[tuple[str, str]]:
    """返回调用面已超过初始上界的 direct egress 边。"""
    actual = {
        (str(fact["source"]), str(fact["target"])): str(fact["fingerprint"])
        for fact in facts
    }
    return sorted(
        edge
        for edge, fingerprint in actual.items()
        if FROZEN_EGRESS_FINGERPRINT_BY_EDGE.get(edge) != fingerprint
    )


def test_egress_collector_distinguishes_bindings_and_runtime_uses(
    tmp_path: Path,
) -> None:
    """类型导入、真实调用、lazy import、re-export 与 SDK 必须可区分。"""
    modules = _module(
        tmp_path,
        "app.sample",
        '''
from typing import TYPE_CHECKING
import requests as req
from requests import Response
import httpx
from urllib import request as urlrequest
from app.adapters.network.http import requests as bridged
from google import genai

if TYPE_CHECKING:
    import cloudscraper

EXAMPLE = "requests.get('https://docstring.invalid')"

def run():
    import aiohttp
    session = req.Session()
    session.get("https://example.com")
    client = httpx.AsyncClient()
    client.stream("GET", "https://example.com")
    urlrequest.urlopen("https://example.com")
    bridged.Session()
    genai.Client()
    aiohttp.ClientSession()
    unknown.get("https://ignored.example")
''',
    )

    value = collect_direct_egress(modules)
    entries = _entries_by_target(value)

    assert set(entries) == {
        "aiohttp",
        "google.genai",
        "httpx",
        "requests",
        "urllib.request",
    }
    assert entries["requests"]["bindings"] == [
        "from:requests.Response as Response",
        "import:requests as req",
        "reexport:app.adapters.network.http.requests as bridged",
    ]
    assert entries["requests"]["uses"] == [
        "run|call:Session",
        "run|call:Session",
        "run|call:get",
    ]
    assert entries["httpx"]["uses"] == [
        "run|call:AsyncClient",
        "run|call:stream",
    ]
    assert entries["urllib.request"]["uses"] == ["run|call:urlopen"]
    assert entries["google.genai"]["kind"] == "network_sdk"
    assert entries["aiohttp"]["uses"] == ["run|call:ClientSession"]
    assert all("line" not in entry for entry in value["entries"])


def test_egress_collector_preserves_type_only_runtime_import(tmp_path: Path) -> None:
    """用于响应类型的运行期 import 必须保留，但不能伪报为外呼操作。"""
    value = collect_direct_egress(
        _module(tmp_path, "app.sample", "from requests import Response\n")
    )

    assert value["entries"] == [
        {
            "source": "app.sample",
            "target": "requests",
            "kind": "raw_transport",
            "bindings": ["from:requests.Response as Response"],
            "uses": [],
            "fingerprint": value["entries"][0]["fingerprint"],
        }
    ]


def test_egress_collector_tracks_aliases_contexts_and_protocol_operations(
    tmp_path: Path,
) -> None:
    """Client 别名、上下文实例、socket 与 XML-RPC 获取都进入稳定 uses。"""
    value = collect_direct_egress(
        _module(
            tmp_path,
            "app.sample",
            '''
import httpx
import requests
import socket
import xmlrpc.client
from urllib.parse import urlparse

def sync_call(flag):
    client_cls = requests.Session if flag else requests.Session
    client = client_cls()
    client.post("https://example.com")
    socket.create_connection(("localhost", 1))
    xmlrpc.client.ServerProxy("http://localhost")
    urlparse("https://not-egress.example")

async def async_call():
    async with httpx.AsyncClient() as client:
        await client.get("https://example.com")
''',
        )
    )
    entries = _entries_by_target(value)

    assert entries["requests"]["uses"] == [
        "sync_call|call:__call__",
        "sync_call|call:post",
    ]
    assert entries["httpx"]["uses"] == [
        "async_call|call:AsyncClient",
        "async_call|call:get",
    ]
    assert entries["socket.create_connection"]["kind"] == "protocol_operation"
    assert entries["xmlrpc.client.ServerProxy"]["kind"] == "protocol_operation"
    assert "urllib.request" not in entries


def test_egress_collector_keeps_lexical_scopes_and_merges_branches(
    tmp_path: Path,
) -> None:
    """函数 lazy import 不得泄漏，分支与 self client 必须保留可证明来源。"""
    value = collect_direct_egress(
        _module(
            tmp_path,
            "app.sample",
            '''
import requests

def first():
    import httpx as client
    client.get("https://example.com")

def second(client):
    client.get("https://ignored.example")

def branch(flag):
    if flag:
        requester = requests.request
    else:
        requester = unknown
    requester("GET", "https://example.com")

class Service:
    def __init__(self):
        self.client = requests.Session()

    def run(self):
        self.client.get("https://example.com")
''',
        )
    )
    entries = _entries_by_target(value)

    assert entries["httpx"]["uses"] == ["first|call:get"]
    assert entries["requests"]["uses"] == [
        "Service.__init__|call:Session",
        "Service.run|call:get",
        "branch|call:request",
    ]


def test_egress_collector_preserves_wildcard_chains_and_duplicate_calls(
    tmp_path: Path,
) -> None:
    """高风险 wildcard、链式 client 和重复操作都必须改变事实 identity。"""
    value = collect_direct_egress(
        _module(
            tmp_path,
            "app.sample",
            '''
from requests import *
import requests

def run():
    requests.Session().get("https://example.com/one")
    requests.get("https://example.com/two")
    requests.get("https://example.com/three")
''',
        )
    )
    entry = _entries_by_target(value)["requests"]

    assert entry["bindings"] == [
        "from:requests.* as *",
        "import:requests as requests",
    ]
    assert entry["uses"] == [
        "run|call:Session",
        "run|call:get",
        "run|call:get",
        "run|call:get",
    ]


def test_egress_collector_tracks_annotations_class_order_and_union_branches(
    tmp_path: Path,
) -> None:
    """注入参数、后置初始化与双 transport 分支都必须保留 operation。"""
    value = collect_direct_egress(
        _module(
            tmp_path,
            "app.sample",
            '''
import httpx
import requests

async def injected(client: httpx.AsyncClient):
    await client.get("https://example.com")

class Service:
    def run(self):
        self.client.post("https://example.com")

    def __init__(self):
        try:
            self.client = requests.Session()
        except Exception:
            self.client = None

def branch(flag):
    if flag:
        client = requests.Session()
    else:
        client = httpx.Client()
    client.get("https://example.com")
''',
        )
    )
    entries = _entries_by_target(value)

    assert entries["requests"]["uses"] == [
        "Service.__init__|call:Session",
        "Service.run|call:post",
        "branch|call:Session",
        "branch|call:get",
    ]
    assert entries["httpx"]["uses"] == [
        "branch|call:Client",
        "branch|call:get",
        "injected|call:get",
    ]


def test_egress_collector_tracks_dynamic_and_late_bound_modules(
    tmp_path: Path,
) -> None:
    """注册目标的字面量动态导入与后置模块别名不能绕过门禁。"""
    value = collect_direct_egress(
        _module(
            tmp_path,
            "app.sample",
            '''
import importlib
import requests

def late_bound():
    transport.post("https://example.com")

def dynamic():
    importlib.import_module("httpx").get("https://example.com")

def builtin_dynamic():
    __import__("httpx").post("https://example.com")

transport = requests
''',
        )
    )
    entries = _entries_by_target(value)

    assert entries["requests"]["uses"] == ["late_bound|call:post"]
    assert entries["httpx"]["bindings"] == [
        "dynamic:__import__(httpx)",
        "dynamic:importlib.import_module(httpx)"
    ]
    assert entries["httpx"]["uses"] == [
        "builtin_dynamic|call:dynamic-import",
        "builtin_dynamic|call:post",
        "dynamic|call:dynamic-import",
        "dynamic|call:get",
    ]


def test_egress_collector_respects_local_shadow_scopes(tmp_path: Path) -> None:
    """lambda、for、comprehension 与 except-as 局部名不得污染 import provenance。"""
    value = collect_direct_egress(
        _module(
            tmp_path,
            "app.sample",
            '''
import requests

def run():
    for requests in []:
        requests.get("https://ignored.example")
    [requests.get("https://ignored.example") for requests in []]
    (lambda requests: requests.get("https://ignored.example"))(object())
    try:
        raise RuntimeError
    except RuntimeError as requests:
        requests.get("https://ignored.example")
''',
        )
    )

    assert _entries_by_target(value)["requests"]["uses"] == []


def test_egress_collector_stops_at_response_operations(tmp_path: Path) -> None:
    """直接请求进入事实，但响应 json/text 解析不是新的 egress operation。"""
    value = collect_direct_egress(
        _module(
            tmp_path,
            "app.sample",
            '''
import requests

def run():
    requests.get("https://example.com").json()
''',
        )
    )

    assert _entries_by_target(value)["requests"]["uses"] == ["run|call:get"]


def test_egress_collector_tracks_local_wrapper_injections(tmp_path: Path) -> None:
    """本地包装函数接收的可证明 client 必须传播，未知调用不得伪造来源。"""
    value = collect_direct_egress(
        _module(
            tmp_path,
            "app.sample",
            '''
import httpx
import requests

def helper(client):
    client.get("https://example.com")

def run():
    requests_client = requests.Session()
    httpx_client = httpx.Client()
    helper(requests_client)
    helper(httpx_client)
    helper(object())
''',
        )
    )
    entries = _entries_by_target(value)

    assert entries["requests"]["uses"] == [
        "helper|call:get",
        "run|call:Session",
    ]
    assert entries["httpx"]["uses"] == [
        "helper|call:get",
        "run|call:Client",
    ]


def test_egress_collector_handles_runtime_else_and_final_module_binding(
    tmp_path: Path,
) -> None:
    """TYPE_CHECKING else 是运行期路径，函数使用模块最终绑定。"""
    value = collect_direct_egress(
        _module(
            tmp_path,
            "app.sample",
            '''
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx as transport
else:
    import requests as transport

captured = transport
transport = object()

def run():
    captured.get("https://example.com")
    transport.get("https://ignored.example")
''',
        )
    )

    assert _entries_by_target(value)["requests"]["uses"] == ["run|call:get"]


def test_egress_collector_merges_class_and_zero_iteration_paths(
    tmp_path: Path,
) -> None:
    """类 client 清空与零次循环都不得抹掉已证明的可达能力。"""
    value = collect_direct_egress(
        _module(
            tmp_path,
            "app.sample",
            '''
import requests

class Service:
    def connect(self):
        client = requests.Session()
        self.client = client

    def close(self):
        self.client = None

    def run(self):
        self.client.get("https://example.com/class")

def loop():
    client = requests.Session()
    for client in []:
        pass
    client.post("https://example.com/loop")
''',
        )
    )

    assert _entries_by_target(value)["requests"]["uses"] == [
        "Service.connect|call:Session",
        "Service.run|call:get",
        "loop|call:Session",
        "loop|call:post",
    ]


def test_egress_collector_preserves_annotated_optional_client(tmp_path: Path) -> None:
    """None 默认值不得抹掉注解 client 经实例属性传递的真实调用。"""
    value = collect_direct_egress(
        _module(
            tmp_path,
            "app.sample",
            '''
import httpx

class Service:
    def __init__(self, client: httpx.AsyncClient = None):
        self.client = client

    async def run(self):
        await self.client.get("https://example.com")
''',
        )
    )

    assert _entries_by_target(value)["httpx"]["uses"] == [
        "Service.run|call:get"
    ]


def test_current_egress_facts_are_complete_and_self_consistent() -> None:
    """当前宿主 egress identity 必须完整、可收缩、排序且统计自洽。"""
    value = collect_dependency_baseline()["direct_egress"]
    entries = value["entries"]
    application_entries = [
        entry
        for entry in entries
        if str(entry["source"]).startswith("app.application.")
    ]
    chain_entries = [
        entry
        for entry in entries
        if str(entry["source"]).startswith("app.chain.")
    ]

    assert value["count"] == len(entries)
    assert sum(value["counts_by_kind"].values()) == value["count"]
    assert set(value["counts_by_kind"]) == {
        "raw_transport",
        "network_sdk",
        "protocol_operation",
    }
    assert value["application_chain_counts"] == {
        "app.application": len(application_entries),
        "app.chain": len(chain_entries),
    }
    assert application_entries == []
    assert chain_entries == []
    assert entries == sorted(
        entries,
        key=lambda entry: (entry["source"], entry["target"], entry["kind"]),
    )
    assert all(entry["target"] != "aiohttp" for entry in entries)
    assert all(len(entry["fingerprint"]) == 64 for entry in entries)
    assert all(set(entry) == {
        "source",
        "target",
        "kind",
        "bindings",
        "uses",
        "fingerprint",
    } for entry in entries)
    assert all(
        "*" not in binding
        for entry in entries
        for binding in entry["bindings"]
    )
    assert '"line"' not in json.dumps(value)


def test_current_egress_facts_match_exact_policy() -> None:
    """现存事实必须逐条分类，且 registry/scope 不得与 collector 漂移。"""
    facts = collect_dependency_baseline()["direct_egress"]
    policy = json.loads(DEPENDENCY_POLICY_PATH.read_text(encoding="utf-8"))
    egress_policy = policy["direct_egress"]
    groups = egress_policy["groups"]
    reviewed = _policy_facts(groups)
    facts_by_fingerprint = {
        fact["fingerprint"]: fact
        for fact in facts["entries"]
    }

    assert policy["schema_version"] == 3
    assert egress_policy["scope"] == facts["scope"]
    assert egress_policy["registry"] == facts["registry"]
    assert len(reviewed) == len({fact["fingerprint"] for fact in reviewed}) == len(
        facts["entries"]
    )
    assert all(set(fact) == {"source", "target", "kind", "fingerprint"} for fact in reviewed)
    assert all("*" not in fact["source"] + fact["target"] for fact in reviewed)
    assert all(len(fact["fingerprint"]) == 64 for fact in reviewed)
    assert all(
        {
            key: facts_by_fingerprint[fact["fingerprint"]][key]
            for key in ("source", "target", "kind", "fingerprint")
        }
        == fact
        for fact in reviewed
    )
    assert _policy_differences(facts["entries"], reviewed) == ([], [])
    assert set(FROZEN_EGRESS_FINGERPRINT_BY_EDGE) == set(
        FROZEN_EGRESS_REASON_BY_EDGE
    )
    assert _fingerprint_differences(facts["entries"]) == []
    assert _classification_differences(facts["entries"], groups) == ([], [], [])


def test_egress_policy_enforces_debt_and_exception_schemas() -> None:
    """债务必须绑定清零叶，精确例外必须有 owner 与业务理由。"""
    policy = json.loads(DEPENDENCY_POLICY_PATH.read_text(encoding="utf-8"))
    groups = policy["direct_egress"]["groups"]
    debt_tracking = {
        "direct_http": "S2-L7",
        "requestutils_session_bridge": "S2-L7",
    }
    exception_reasons = {
        "canonical_transport",
        "transport_configuration",
        "sdk_transport",
        "streaming_protocol",
        "contained_vendor",
        "local_control_plane",
        "diagnostic_probe",
        "type_or_compat_only",
        "daemon_control_plane",
        "test_network_guard",
    }

    assert not {
        "direct_http",
        "requestutils_session_bridge",
    } & {group["reason_code"] for group in groups}

    for group in groups:
        if group["classification"] == "temporary_debt":
            assert set(group) == {
                "classification",
                "reason_code",
                "tracking",
                "target_state",
                "reason",
                "facts",
            }
            assert group["reason_code"] in debt_tracking
            assert group["tracking"] == debt_tracking[group["reason_code"]]
            assert group["target_state"] == "empty"
            assert str(group["reason"]).strip()
        else:
            assert group["classification"] == "approved_exception"
            assert set(group) == {
                "classification",
                "reason_code",
                "owner",
                "reason",
                "facts",
            }
            assert group["reason_code"] in exception_reasons
            assert group["owner"] == "$source"
            assert str(group["reason"]).strip()


def test_egress_policy_rejects_classification_swaps() -> None:
    """事实即使仍被覆盖，也不能在债务与例外 reason 间互换。"""
    facts = [
        {"source": "app.adapters.network.http", "target": "httpx"},
        {"source": "app.agent.llm.capability", "target": "openai"},
    ]
    swapped = [
        {
            "reason_code": "sdk_transport",
            "facts": [{"source": "app.adapters.network.http", "target": "httpx"}],
        },
        {
            "reason_code": "direct_http",
            "facts": [{"source": "app.agent.llm.capability", "target": "openai"}],
        },
    ]

    assert _classification_differences(facts, swapped) == (
        [],
        [],
        [
            ("app.adapters.network.http", "httpx"),
            ("app.agent.llm.capability", "openai"),
        ],
    )


def test_egress_policy_rejects_same_edge_surface_growth() -> None:
    """同一 source/target 的调用面变化不能靠刷新 policy 绕过。"""
    facts = [
        {
            "source": "app.adapters.network.http",
            "target": "httpx",
            "fingerprint": "f" * 64,
        }
    ]

    assert _fingerprint_differences(facts) == [
        ("app.adapters.network.http", "httpx")
    ]


def test_egress_policy_rejects_add_remove_replacement_and_stale_entries() -> None:
    """新增、事实变化和删除后未清 policy 都必须产生精确差异。"""
    original = [
        {
            "source": "app.sample",
            "target": "requests",
            "kind": "raw_transport",
            "fingerprint": "a" * 64,
        }
    ]
    reviewed = [
        {
            "source": "app.sample",
            "target": "requests",
            "kind": "raw_transport",
            "fingerprint": "a" * 64,
        }
    ]
    added = [
        *original,
        {
            "source": "app.other",
            "target": "aiohttp",
            "kind": "raw_transport",
            "fingerprint": "b" * 64,
        },
    ]
    replacement = [{**original[0], "fingerprint": "c" * 64}]

    assert _policy_differences(original, reviewed) == ([], [])
    assert _policy_differences(added, reviewed) == (["b" * 64], [])
    assert _policy_differences([], reviewed) == ([], ["a" * 64])
    assert _policy_differences(replacement, reviewed) == (
        ["c" * 64],
        ["a" * 64],
    )
    assert _policy_differences([], []) == ([], [])
