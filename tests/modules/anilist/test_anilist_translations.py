"""
验证 AniList 中文数据集的跨实例持久化、到期刷新、镜像出口及失败回退。
"""

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.anilist import anilist as anilist_client
from app.modules.anilist.anilist import AniListApi
from app.runtime import cache as runtime_cache

_TranslationEnvironment = tuple[dict[str, Any], list[float]]
_Translations = list[dict[str, Any]]


@pytest.fixture
def translation_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _TranslationEnvironment:
    """
    隔离数据集缓存、下载出口和时钟，使用真实文件后端且禁止路由至 Redis。
    """
    values = {"CACHE_PATH": tmp_path, "GITHUB_PROXY": None}
    runtime_setting = anilist_client.get_runtime_setting
    monkeypatch.setattr(anilist_client, "get_runtime_setting", lambda name: values[name] if name in values else runtime_setting(name))
    monkeypatch.setattr(runtime_cache, "_backend_type_provider", lambda: "redis")
    now = [100_000.0]
    monkeypatch.setattr(anilist_client, "time", lambda: now[0])
    return values, now


@pytest.fixture
def translations() -> _Translations:
    """
    提供含数字和字符串标识的中文数据，确保 JSON 快照不会改变标识类型。
    """
    return [
        {"id": 12, "title": "中文标题", "synonyms": ["中文别名"]},
        {"id": "12", "title": "字符串标题", "synonyms": []},
    ]


def _download(client: AniListApi, asynchronous: bool, payload: object) -> Mock:
    """
    替换指定传输的下载边界，不访问真实网络。
    """
    request = client._async_request if asynchronous else client._request
    request.get_json = AsyncMock(return_value=payload) if asynchronous else Mock(return_value=payload)
    return request.get_json


def _load(client: AniListApi, asynchronous: bool) -> dict[object, dict[str, Any]]:
    """
    运行同步或异步加载入口并取得最终标题索引。
    """
    return asyncio.run(client._async_translation_map()) if asynchronous else client._translation_map()


def _cache_path(values: dict[str, Any]) -> Path:
    """
    返回当前用例对应的持久化快照路径。
    """
    return values["CACHE_PATH"] / "anilist" / "anilist-chinese.json"


@pytest.mark.parametrize("first_async", [False, True])
@pytest.mark.parametrize("second_async", [False, True])
def test_new_client_reuses_disk_snapshot(
    translation_environment: _TranslationEnvironment, translations: _Translations,
    first_async: bool, second_async: bool,
) -> None:
    """
    首次下载落盘，重建客户端和切换同步异步入口均应复用快照而无需重新下载。
    """
    values, _ = translation_environment
    first = AniListApi()
    download = _download(first, first_async, translations)
    expected = _load(first, first_async)
    assert _load(first, first_async) == expected
    download.assert_called_once_with(first._translations_url)
    assert expected[12]["title"] == "中文标题"
    assert expected["12"]["title"] == "字符串标题"
    assert json.loads(_cache_path(values).read_bytes())["items"] == translations

    second = AniListApi()
    download = _download(second, second_async, None)
    assert _load(second, second_async) == expected
    download.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_expired_snapshot_and_memory_refresh(
    translation_environment: _TranslationEnvironment, translations: _Translations, asynchronous: bool,
) -> None:
    """
    磁盘和内存到期均应重新下载，更新后的快照能由另一个客户端立即读取。
    """
    values, now = translation_environment
    path = _cache_path(values)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"expires_at": now[0] - 1, "items": [{"id": 12, "title": "旧标题"}]}))
    client = AniListApi()
    download = _download(client, asynchronous, translations)
    assert _load(client, asynchronous)[12]["title"] == "中文标题"
    download.assert_called_once()

    now[0] += client._translations_ttl + 1
    updated = [{"id": 12, "title": "更新标题", "synonyms": []}]
    download = _download(client, asynchronous, updated)
    assert _load(client, asynchronous)[12]["title"] == "更新标题"
    download.assert_called_once()
    restored = AniListApi()
    download = _download(restored, not asynchronous, None)
    assert _load(restored, not asynchronous)[12]["title"] == "更新标题"
    download.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("payload", [None, {}, [], [{"id": []}], [{"id": 12, "title": []}]])
def test_failed_refresh_keeps_stale_snapshot_and_retries(
    translation_environment: _TranslationEnvironment, translations: _Translations,
    asynchronous: bool, payload: object,
) -> None:
    """
    下载失败或载荷无效不得覆盖旧快照；退避后仍能恢复新中文数据。
    """
    values, now = translation_environment
    path = _cache_path(values)
    path.parent.mkdir(parents=True)
    content = json.dumps({"expires_at": now[0] - 1, "items": translations}).encode()
    path.write_bytes(content)
    client = AniListApi()
    download = _download(client, asynchronous, payload)
    assert _load(client, asynchronous)[12]["title"] == "中文标题"
    assert _load(client, asynchronous)[12]["title"] == "中文标题"
    download.assert_called_once()
    assert path.read_bytes() == content

    now[0] += client._translations_retry + 1
    download = _download(client, asynchronous, [{"id": 12, "title": "恢复标题"}])
    assert _load(client, asynchronous)[12]["title"] == "恢复标题"
    download.assert_called_once()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_initial_failure_is_not_cached_forever(
    translation_environment: _TranslationEnvironment, translations: _Translations, asynchronous: bool,
) -> None:
    """
    初次下载失败时不落盘空数据，五分钟后可在同一实例内重试成功。
    """
    values, now = translation_environment
    client = AniListApi()
    download = _download(client, asynchronous, None)
    assert _load(client, asynchronous) == {}
    assert _load(client, asynchronous) == {}
    download.assert_called_once()
    assert not _cache_path(values).exists()
    now[0] += client._translations_retry + 1
    download = _download(client, asynchronous, translations)
    assert _load(client, asynchronous)[12]["title"] == "中文标题"
    download.assert_called_once()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("content", [
    b"{", b"\xff", b"[]", b"{}",
    b'{"expires_at": "invalid", "items": []}',
    b'{"expires_at": NaN, "items": []}',
    b'{"expires_at": 999999, "items": {}}',
])
def test_corrupt_cache_is_redownloaded(
    translation_environment: _TranslationEnvironment, translations: _Translations,
    asynchronous: bool, content: bytes,
) -> None:
    """
    损坏 JSON、无效编码、时间或条目结构都按缓存未命中处理。
    """
    values, _ = translation_environment
    path = _cache_path(values)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    client = AniListApi()
    download = _download(client, asynchronous, translations)
    assert _load(client, asynchronous)[12]["title"] == "中文标题"
    download.assert_called_once()
    assert json.loads(path.read_bytes())["items"] == translations


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("operation", ["get", "set", "init"])
def test_disk_errors_preserve_downloaded_titles(
    translation_environment: _TranslationEnvironment, translations: _Translations,
    asynchronous: bool, operation: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    缓存目录不可写或文件读写失败时，仍能使用成功下载的数据并在内存复用。
    """
    if operation == "init":
        monkeypatch.setattr(anilist_client, "FileCache", Mock(side_effect=PermissionError("readonly")))
    client = AniListApi()
    if operation != "init":
        cache = client._async_file_cache if asynchronous else client._file_cache
        error = AsyncMock(side_effect=OSError("disk error")) if asynchronous else Mock(side_effect=OSError("disk error"))
        monkeypatch.setattr(cache, operation, error)
    download = _download(client, asynchronous, translations)
    assert _load(client, asynchronous)[12]["title"] == "中文标题"
    assert _load(client, asynchronous)[12]["title"] == "中文标题"
    download.assert_called_once()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mirror_success", [False, True])
def test_github_mirror_precedes_proxied_raw_url(
    translation_environment: _TranslationEnvironment, translations: _Translations,
    asynchronous: bool, mirror_success: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    镜像前缀支持路径和缺失尾斜杠，镜像直连，失败后回到已配置 PROXY 的原始客户端。
    """
    values, _ = translation_environment
    values["GITHUB_PROXY"] = " https://mirror.example/prefix "
    values["PROXY"] = {"https": "socks5://127.0.0.1:1080"}
    client = AniListApi()
    raw = _download(client, asynchronous, translations)
    transport = Mock()
    payload = translations if mirror_success else {"error": "unavailable"}
    transport.get_json = AsyncMock(return_value=payload) if asynchronous else Mock(return_value=payload)
    constructor = Mock(return_value=transport)
    monkeypatch.setattr(anilist_client, "AsyncRequestUtils" if asynchronous else "RequestUtils", constructor)
    assert _load(client, asynchronous)[12]["title"] == "中文标题"
    constructor.assert_called_once_with(ua=anilist_client.get_runtime_setting("NORMAL_USER_AGENT"))
    transport.get_json.assert_called_once_with(f"https://mirror.example/prefix/{client._translations_url}")
    if mirror_success:
        raw.assert_not_called()
    else:
        raw.assert_called_once_with(client._translations_url)
    request = client._async_request if asynchronous else client._request
    assert request._proxies


@pytest.mark.parametrize("asynchronous", [False, True])
def test_failed_write_keeps_newer_memory_on_next_refresh(
    translation_environment: _TranslationEnvironment, asynchronous: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    新数据落盘失败后再遇下载失败，旧磁盘快照不得覆盖更新的内存数据。
    """
    values, now = translation_environment
    path = _cache_path(values)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"expires_at": now[0] - 1, "items": [{"id": 12, "title": "旧标题"}]}))
    client = AniListApi()
    cache = client._async_file_cache if asynchronous else client._file_cache
    error = AsyncMock(side_effect=OSError("readonly")) if asynchronous else Mock(side_effect=OSError("readonly"))
    monkeypatch.setattr(cache, "set", error)
    _download(client, asynchronous, [{"id": 12, "title": "新标题"}])
    assert _load(client, asynchronous)[12]["title"] == "新标题"
    now[0] += client._translations_ttl + 1
    _download(client, asynchronous, None)
    assert _load(client, asynchronous)[12]["title"] == "新标题"


def test_mixed_concurrent_clients_download_once(
    translation_environment: _TranslationEnvironment, translations: _Translations,
) -> None:
    """
    同步线程和多个异步客户端共用加载锁，等待者从磁盘取得同一快照。
    """
    sync_client = AniListApi()
    sync_download = _download(sync_client, False, translations)
    clients = [AniListApi() for _ in range(6)]
    downloads = [_download(client, True, translations) for client in clients]

    async def load_all() -> list[dict[object, dict[str, Any]]]:
        """
        同时发起跨线程和协程加载，覆盖两种入口的缓存竞争。
        """
        return await asyncio.wait_for(asyncio.gather(
            asyncio.to_thread(sync_client._translation_map),
            *(client._async_translation_map() for client in clients),
        ), timeout=5)

    results = asyncio.run(load_all())
    assert all(result[12]["title"] == "中文标题" for result in results)
    assert sync_download.call_count + sum(download.call_count for download in downloads) == 1


def test_cancelled_loader_releases_shared_lock(
    translation_environment: _TranslationEnvironment, translations: _Translations,
) -> None:
    """
    异步下载取消后释放共享锁，后续实例仍能重新下载并持久化。
    """
    client = AniListApi()
    successor = AniListApi()
    download = _download(successor, True, translations)

    async def cancel_and_reload() -> dict[object, dict[str, Any]]:
        """
        在下载边界取消首个持锁者，并限定后续加载的完成时间。
        """
        entered = asyncio.Event()

        async def blocked_download(_url: str) -> None:
            """
            挂起数据集下载直到调用方取消，避免依赖真实网络时序。
            """
            entered.set()
            await asyncio.Event().wait()

        client._async_request.get_json = blocked_download
        task = asyncio.create_task(client._async_translation_map())
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await asyncio.wait_for(successor._async_translation_map(), timeout=2)

    assert asyncio.run(cancel_and_reload())[12]["title"] == "中文标题"
    download.assert_awaited_once()
