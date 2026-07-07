import base64
from unittest.mock import MagicMock

import app.api.endpoints.search as search_endpoint


INFO_HASH = "0123456789abcdef0123456789abcdef01234567"
MAGNET = f"magnet:?xt=urn:btih:{INFO_HASH}&dn=demo"


class _FakeQbittorrentModule:
    """
    为搜索探测测试提供可控的 qBittorrent 模块。
    """

    def __init__(self, server):
        self.server = server
        self.init_module = MagicMock()

    def get_instance(self, downloader):
        self.downloader = downloader
        return self.server


def test_extract_btih_supports_hex_and_base32():
    """
    磁力探测应兼容 hex 与 base32 两种 btih 写法。
    """
    base32_hash = base64.b32encode(bytes.fromhex(INFO_HASH)).decode("ascii")

    assert search_endpoint._extract_btih(MAGNET) == INFO_HASH
    assert (
        search_endpoint._extract_btih(f"magnet:?xt=urn:btih:{base32_hash}")
        == INFO_HASH
    )
    assert search_endpoint._extract_btih("https://example.com/demo.torrent") is None


def test_sanitize_probe_magnet_keeps_only_btih():
    """
    探测添加到下载器前应移除 tracker/webseed 等外部连接参数。
    """
    unsafe_magnet = (
        f"{MAGNET}&tr=http%3A%2F%2Ftracker.invalid%2Fannounce"
        "&ws=http%3A%2F%2Fwebseed.invalid%2Ffile"
    )

    assert (
        search_endpoint._sanitize_probe_magnet(unsafe_magnet)
        == f"magnet:?xt=urn:btih:{INFO_HASH}"
    )
    assert search_endpoint._sanitize_probe_magnet("magnet:?dn=demo") is None


def test_parse_probe_timeout_uses_safe_bounds():
    """
    探测超时时间应被限制在较小区间内，避免接口长期占用后端线程。
    """
    assert search_endpoint._parse_probe_timeout(None) == 30
    assert search_endpoint._parse_probe_timeout("2") == 5
    assert search_endpoint._parse_probe_timeout("90") == 60
    assert search_endpoint._parse_probe_timeout("bad") == 30


def test_build_probe_payload_uses_real_size_and_best_seeder_count():
    """
    探测结果应使用 qB 的真实大小，并优先取连接/汇报做种数中的较大值。
    """
    data = search_endpoint._build_probe_payload(
        {
            "hash": INFO_HASH,
            "name": "demo",
            "state": "metaDL",
            "total_size": 1,
            "size": 4096,
            "num_seeds": 2,
            "num_complete": 8,
            "num_leechs": 3,
            "num_incomplete": 4,
            "availability": "1.5",
        },
        existing=False,
        timed_out=False,
    )

    assert data["size"] == 4096
    assert data["seeders"] == 8
    assert data["peers"] == 4
    assert data["has_metadata"]
    assert data["availability"] == 1.5


def test_probe_existing_magnet_skips_without_leaking_status(monkeypatch):
    """
    磁力任务已存在时不应返回下载器中的任务详情。
    """
    server = MagicMock()
    server.get_torrents.return_value = (
        [
            {
                "hash": INFO_HASH,
                "name": "existing",
                "total_size": 2048,
                "num_seeds": 3,
                "num_complete": 5,
            }
        ],
        False,
    )
    module = _FakeQbittorrentModule(server)
    monkeypatch.setattr(search_endpoint, "QbittorrentModule", lambda: module)

    result = search_endpoint._probe_magnet(MAGNET, downloader="qb", timeout=5)

    assert not result["success"]
    assert "data" not in result
    server.add_torrent.assert_not_called()
    server.delete_torrents.assert_not_called()


def test_probe_new_magnet_adds_tagged_task_and_cleans_files(monkeypatch):
    """
    新磁力探测应添加带唯一标签的临时任务，拿到 metadata 后删除任务和文件。
    """
    server = MagicMock()
    first_hash_query = True

    def get_torrents(ids=None, tags=None):
        nonlocal first_hash_query
        if ids == INFO_HASH and first_hash_query:
            first_hash_query = False
            return ([], False)
        probe_tag = server.add_torrent.call_args.kwargs["tag"][0]
        return (
            [
                {
                    "hash": INFO_HASH,
                    "name": "new",
                    "total_size": 8192,
                    "tags": f"MOVIEPILOT,{probe_tag}",
                    "category": search_endpoint._PROBE_CATEGORY,
                    "num_seeds": 1,
                    "num_complete": 2,
                }
            ],
            False,
        )

    server.get_torrents.side_effect = get_torrents
    server.add_torrent.return_value = (True, [INFO_HASH])
    server.delete_torrents.return_value = True
    module = _FakeQbittorrentModule(server)
    monkeypatch.setattr(search_endpoint, "QbittorrentModule", lambda: module)

    result = search_endpoint._probe_magnet(MAGNET, downloader=None, timeout=5)

    assert result["success"]
    assert result["message"] == "探测完成"
    assert result["data"]["cleanup"] is True
    assert result["data"]["existing"] is False
    assert result["data"]["size"] == 8192

    add_kwargs = server.add_torrent.call_args.kwargs
    assert add_kwargs["content"] == f"magnet:?xt=urn:btih:{INFO_HASH}"
    assert add_kwargs["is_paused"] is False
    assert add_kwargs["dl_limit"] == search_endpoint._PROBE_SPEED_LIMIT
    assert add_kwargs["up_limit"] == search_endpoint._PROBE_SPEED_LIMIT
    assert add_kwargs["stop_condition"] == "MetadataReceived"
    assert add_kwargs["category"] == search_endpoint._PROBE_CATEGORY
    assert add_kwargs["tag"][0].startswith(f"{search_endpoint._PROBE_TAG_PREFIX}_")
    server.delete_torrents.assert_called_once_with(delete_file=False, ids=INFO_HASH)


def test_probe_rejects_magnet_without_btih(monkeypatch):
    """
    无法识别 info hash 时不应把任务交给下载器，避免误清理已有任务。
    """
    module = MagicMock()
    monkeypatch.setattr(search_endpoint, "QbittorrentModule", lambda: module)

    result = search_endpoint._probe_magnet("magnet:?dn=demo", downloader=None, timeout=5)

    assert not result["success"]
    assert result["message"] == "无法解析 magnet 链接中的 btih"
    module.init_module.assert_not_called()


def test_probe_rejects_non_string_magnet(monkeypatch):
    """
    非字符串输入应返回受控失败，不应触发下载器初始化。
    """
    module = MagicMock()
    monkeypatch.setattr(search_endpoint, "QbittorrentModule", lambda: module)

    result = search_endpoint._probe_magnet(123, downloader=None, timeout=5)

    assert not result["success"]
    assert result["message"] == "只支持 magnet 链接探测"
    module.init_module.assert_not_called()


def test_probe_timeout_returns_partial_tracker_state_and_cleans(monkeypatch):
    """
    超时时即使没有 metadata，也应返回 tracker 健康度并清理临时任务。
    """
    server = MagicMock()
    first_hash_query = True

    def get_torrents(ids=None, tags=None):
        nonlocal first_hash_query
        if ids == INFO_HASH and first_hash_query:
            first_hash_query = False
            return ([], False)
        probe_tag = server.add_torrent.call_args.kwargs["tag"][0]
        return (
            [
                {
                    "hash": INFO_HASH,
                    "name": "pending",
                    "state": "metaDL",
                    "total_size": -1,
                    "size": 0,
                    "tags": f"MOVIEPILOT,{probe_tag}",
                    "category": search_endpoint._PROBE_CATEGORY,
                    "num_seeds": 0,
                    "num_complete": 6,
                    "num_leechs": 1,
                    "num_incomplete": 2,
                }
            ],
            False,
        )

    server.get_torrents.side_effect = get_torrents
    server.add_torrent.return_value = (True, [INFO_HASH])
    server.delete_torrents.return_value = True
    module = _FakeQbittorrentModule(server)
    monkeypatch.setattr(search_endpoint, "QbittorrentModule", lambda: module)
    monkeypatch.setattr(search_endpoint.time, "sleep", lambda _seconds: None)
    monotonic_values = iter([0, 1, 6])
    monkeypatch.setattr(
        search_endpoint.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    result = search_endpoint._probe_magnet(MAGNET, downloader=None, timeout=5)

    assert result["success"]
    assert not result["data"]["has_metadata"]
    assert result["data"]["timed_out"]
    assert result["data"]["seeders"] == 6
    assert result["data"]["cleanup"] is True
    server.delete_torrents.assert_called_once_with(delete_file=False, ids=INFO_HASH)


def test_probe_add_without_hash_finds_tagged_task_and_cleans(monkeypatch):
    """
    添加成功但接口未返回 hash 时，应按唯一标签查找并清理临时任务。
    """
    server = MagicMock()

    def get_torrents(ids=None, tags=None):
        if ids == INFO_HASH:
            return (
                [
                    {
                        "hash": INFO_HASH,
                        "name": "new",
                        "total_size": 4096,
                        "tags": "MOVIEPILOT,MP_PROBE_test",
                        "category": search_endpoint._PROBE_CATEGORY,
                    }
                ],
                False,
            )
        if tags:
            return (
                [
                    {
                        "hash": INFO_HASH,
                        "name": "new",
                        "total_size": 4096,
                        "tags": f"MOVIEPILOT,{tags}",
                        "category": search_endpoint._PROBE_CATEGORY,
                    }
                ],
                False,
            )
        return ([], False)

    first_hash_query = True

    def get_torrents_with_precheck(ids=None, tags=None):
        nonlocal first_hash_query
        if ids == INFO_HASH and first_hash_query:
            first_hash_query = False
            return ([], False)
        return get_torrents(ids=ids, tags=tags)

    server.get_torrents.side_effect = get_torrents_with_precheck
    server.add_torrent.return_value = (True, [])
    server.delete_torrents.return_value = True
    module = _FakeQbittorrentModule(server)
    monkeypatch.setattr(search_endpoint, "QbittorrentModule", lambda: module)
    monkeypatch.setattr(search_endpoint.time, "sleep", lambda _seconds: None)

    result = search_endpoint._probe_magnet(MAGNET, downloader=None, timeout=5)

    assert result["success"]
    assert result["data"]["cleanup"] is True
    server.get_torrent_id_by_tag.assert_not_called()
    server.delete_torrents.assert_called_once_with(delete_file=False, ids=INFO_HASH)


def test_probe_cleanup_skips_task_without_unique_probe_tag(monkeypatch):
    """
    清理前如果唯一探测标签不存在，不应删除下载器中的任务。
    """
    server = MagicMock()
    server.get_torrents.side_effect = [
        ([], False),
        (
            [
                {
                    "hash": INFO_HASH,
                    "name": "new",
                    "total_size": 4096,
                    "tags": "MP_PROBE_test",
                }
            ],
            False,
        ),
        (
            [
                {
                    "hash": INFO_HASH,
                    "name": "new",
                    "total_size": 4096,
                    "tags": "MOVIEPILOT",
                }
            ],
            False,
        ),
        ([], False),
    ]
    server.add_torrent.return_value = (True, [INFO_HASH])
    module = _FakeQbittorrentModule(server)
    monkeypatch.setattr(search_endpoint, "QbittorrentModule", lambda: module)

    result = search_endpoint._probe_magnet(MAGNET, downloader=None, timeout=5)

    assert result["success"]
    assert result["data"]["cleanup"] is False
    server.delete_torrents.assert_not_called()


def test_probe_cleanup_skips_task_without_probe_category(monkeypatch):
    """
    即使任务带有本次唯一标签，缺少探测专用分类时也不应删除。
    """
    server = MagicMock()
    first_hash_query = True

    def get_torrents(ids=None, tags=None):
        nonlocal first_hash_query
        if ids == INFO_HASH and first_hash_query:
            first_hash_query = False
            return ([], False)
        probe_tag = server.add_torrent.call_args.kwargs["tag"][0]
        return (
            [
                {
                    "hash": INFO_HASH,
                    "name": "new",
                    "total_size": 4096,
                    "tags": f"MOVIEPILOT,{probe_tag}",
                }
            ],
            False,
        )

    server.get_torrents.side_effect = get_torrents
    server.add_torrent.return_value = (True, [INFO_HASH])
    module = _FakeQbittorrentModule(server)
    monkeypatch.setattr(search_endpoint, "QbittorrentModule", lambda: module)

    result = search_endpoint._probe_magnet(MAGNET, downloader=None, timeout=5)

    assert result["success"]
    assert result["data"]["cleanup"] is False
    server.delete_torrents.assert_not_called()
