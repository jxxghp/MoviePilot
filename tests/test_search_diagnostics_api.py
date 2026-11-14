"""
最近页搜索上下文的用户隔离与接口兼容回归。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.endpoints import search as endpoint
from app.schemas.search import SearchDiagnosticCounts, SearchDiagnostics, SearchLastContextData
from app.schemas.token import TokenPayload


@pytest.mark.asyncio
async def test_last_context_only_uses_current_authenticated_user(monkeypatch):
    """
    最近上下文必须用认证身份读取完整快照，不能读取全局参数或结果。
    """
    snapshot = SearchLastContextData(params={"keyword": "Show"}, diagnostics=SearchDiagnostics(
        search_id="search-a", scope="page", stage_counts=SearchDiagnosticCounts(),
    ))
    load = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(endpoint, "SearchChain", lambda: SimpleNamespace(async_last_search_context=load))
    response = await endpoint.search_latest_context(TokenPayload(sub=41))
    load.assert_awaited_once_with(41)
    assert response.data is snapshot
    assert response.data.diagnostics.search_id == "search-a"


@pytest.mark.asyncio
async def test_last_context_without_user_has_no_unowned_results(monkeypatch):
    """
    缺少用户身份时返回空快照，不泄露无法归属的旧全局缓存。
    """
    factory = AsyncMock()
    monkeypatch.setattr(endpoint, "SearchChain", factory)
    response = await endpoint.search_latest_context(TokenPayload())
    factory.assert_not_called()
    assert response.data.model_dump(mode="json") == {"params": {}, "results": [], "diagnostics": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("by_media", [False, True])
async def test_manual_stream_passes_user_identity_from_dependency(monkeypatch, by_media):
    """
    用户编号来自已验证依赖，客户端参数不能替代诊断快照的归属身份。
    """
    calls = []

    async def page_events(*, params):
        """
        记录内部请求参数，模拟无结果的安全页完成事件。
        """
        calls.append(params)
        yield {"type": "done", "stage": "done"}

    async def stream_events(_request, source):
        """
        直接消费来源，验证入口组装而不启动真实 HTTP 服务。
        """
        async for event in source:
            yield str(event)

    monkeypatch.setattr(endpoint, "SearchChain", lambda: SimpleNamespace(search_page_events=page_events))
    monkeypatch.setattr(endpoint, "_stream_search_events", stream_events)
    principal = TokenPayload(sub=42)
    if by_media:
        monkeypatch.setattr(endpoint, "_resolve_media_search_params", AsyncMock(return_value=({"media_id": "1"}, "")))
        response = await endpoint.search_by_id_stream(
            request=None, media_id="1", media_source="themoviedb", manual_paging=True, _=principal,
        )
    else:
        response = await endpoint.search_by_title_stream(request=None, keyword="Show", manual_paging=True, _=principal)
    assert [item async for item in response.body_iterator]
    assert len(calls) == 1 and calls[0]["user_id"] == 42


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["title", "media", "subtitle-title", "subtitle-media"])
@pytest.mark.parametrize("empty", [False, True])
async def test_ordinary_search_publishes_owned_context_without_old_diagnostics(monkeypatch, kind, empty):
    """
    普通搜索的成功与空结果均替换所属用户的完整快照，不保留上次页诊断。
    """
    from app.domain.context import Context, SubtitleInfo, TorrentInfo

    result = [] if empty else ([SubtitleInfo(title="字幕", site=1, enclosure="https://example.test/subtitle")]
                              if kind.startswith("subtitle") else [Context(torrent_info=TorrentInfo(title="Show"))])
    search = AsyncMock(return_value=result)
    save = AsyncMock()
    chain = SimpleNamespace(async_search_by_title=search, async_search_by_id=search,
                            async_search_subtitles_by_title=search, async_save_search_context=save)
    monkeypatch.setattr(endpoint, "SearchChain", lambda: chain)
    monkeypatch.setattr(endpoint, "_resolve_media_search_params", AsyncMock(return_value=(
        {"media_source": "themoviedb", "media_id": "1"}, "")))
    principal = TokenPayload(sub=51)
    if kind == "title":
        response = await endpoint.search_by_title(keyword="Show", _=principal)
    elif kind == "media":
        response = await endpoint.search_by_id(media_id="1", media_source="themoviedb", _=principal)
    elif kind == "subtitle-title":
        response = await endpoint.search_subtitle_by_title(keyword="Show", _=principal)
    else:
        async def subtitle_source(**_params):
            """
            提供未签名字幕搜索协程，保留入口真实 await 顺序。
            """
            return search(), ""
        monkeypatch.setattr(endpoint, "_build_subtitle_search_source", subtitle_source)
        response = await endpoint.search_subtitle_by_id(media_id="1", media_source="themoviedb", _=principal)
    user_id, snapshot = save.await_args.args
    assert user_id == 51 and snapshot.diagnostics is None
    assert len(snapshot.results) == (0 if empty else 1)
    assert snapshot.params["result_type"] == ("subtitle" if kind.startswith("subtitle") else "torrent")
    assert response.success is not empty


@pytest.mark.asyncio
async def test_legacy_stream_publishes_only_final_owned_context(monkeypatch):
    """
    旧流的预览和来源阶段完成不发布快照，最终完成发布无诊断记录。
    """
    save = AsyncMock()
    monkeypatch.setattr(endpoint, "_save_user_search_context", save)

    async def source():
        """
        用离线事件模拟 provider 完成、过滤预览和最终结果。
        """
        yield {"type": "done", "stage": "searching", "items": []}
        yield {"type": "replace", "items": [{"torrent_info": {"title": "Show"}}]}
        yield {"type": "done", "stage": "done", "items": []}

    stream = endpoint._iter_user_search_events(source(), 52, params={"keyword": "Show"})
    assert (await anext(stream))["stage"] == "searching"
    await anext(stream)
    save.assert_not_awaited()
    assert (await anext(stream))["stage"] == "done"
    save.assert_awaited_once_with(52, params={"keyword": "Show"}, results=[])
    await stream.aclose()


@pytest.mark.asyncio
async def test_legacy_stream_error_and_close_keep_previous_snapshot(monkeypatch):
    """
    旧流发生异常或取消预览时不覆盖用户上次完整快照。
    """
    save = AsyncMock()
    monkeypatch.setattr(endpoint, "_save_user_search_context", save)

    async def source():
        """
        预览之后模拟解析失败，既无真实网络也无完成事件。
        """
        yield {"type": "append", "items": []}
        raise ValueError("mock failure")

    stream = endpoint._iter_user_search_events(source(), 52, params={"keyword": "Show"})
    await anext(stream)
    await stream.aclose()
    save.assert_not_awaited()
    with pytest.raises(ValueError, match="mock failure"):
        _ = [event async for event in endpoint._iter_user_search_events(source(), 52, params={"keyword": "Show"})]
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_search_failure_does_not_publish_context(monkeypatch):
    """
    普通搜索调用抛错时保留旧快照，不发布空成功记录。
    """
    save = AsyncMock()
    monkeypatch.setattr(endpoint, "SearchChain", lambda: SimpleNamespace(
        async_search_by_title=AsyncMock(side_effect=ValueError("mock failure")), async_save_search_context=save,
    ))
    with pytest.raises(ValueError, match="mock failure"):
        await endpoint.search_by_title(keyword="Show", _=TokenPayload(sub=51))
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_last_subtitle_context_signs_again_without_modifying_saved_snapshot(monkeypatch):
    """
    最近字幕每次读取都重新签名，持久快照不保存临时签名且不被读取过程修改。
    """
    from app.schemas.context import SubtitleInfo

    snapshot = SearchLastContextData(params={"keyword": "Show", "result_type": "subtitle"}, results=[
        SubtitleInfo(title="字幕", site=1, enclosure="https://example.test/subtitle"),
    ])
    monkeypatch.setattr(endpoint, "SearchChain", lambda: SimpleNamespace(async_last_search_context=AsyncMock(return_value=snapshot)))
    signed = []

    def sign(url, **params):
        """
        记录签名用途，确保读取时才生成访问链接。
        """
        signed.append((url, params))
        return "https://example.test/signed"

    monkeypatch.setattr(endpoint.SecurityUtils, "sign_url", sign)
    response = await endpoint.search_latest_context(TokenPayload(sub=1))
    assert response.data.results[0].enclosure == "https://example.test/signed"
    assert snapshot.results[0].enclosure == "https://example.test/subtitle"
    assert signed[0][1]["purpose"] == endpoint.SecurityUtils.subtitle_download_purpose(1)


@pytest.mark.asyncio
async def test_real_search_chain_context_roundtrip_isolates_users_and_clears_diagnostics(monkeypatch):
    """
    经真实门面、缓存 owner 与状态服务读写，用户快照保持独立且普通搜索清除旧诊断。
    """
    from app.chain.search.facade import SearchChain
    from app.domain.context import Context, TorrentInfo

    chain = SearchChain()
    stored = {}

    async def save(value, key):
        """
        用独立内存端口替代文件缓存，不执行外部 I/O。
        """
        stored[key] = value

    async def load(key):
        """
        只返回请求用户对应的完整记录。
        """
        return stored.get(key)

    monkeypatch.setattr(chain, "async_save_cache", save)
    monkeypatch.setattr(chain, "async_load_cache", load)
    monkeypatch.setattr(endpoint, "SearchChain", lambda: chain)
    snapshot = SearchLastContextData(params={"keyword": "First"}, diagnostics=SearchDiagnostics(
        search_id="first-search", scope="page", stage_counts=SearchDiagnosticCounts(),
    ))
    await chain.async_save_search_context(61, snapshot)
    first = await endpoint.search_latest_context(TokenPayload(sub=61))
    missing = await endpoint.search_latest_context(TokenPayload(sub=62))
    assert first.data.params == {"keyword": "First"}
    assert first.data.diagnostics.search_id == "first-search"
    assert missing.data.model_dump() == {"params": {}, "results": [], "diagnostics": None}

    await chain.async_save_search_context(62, SearchLastContextData(params={"keyword": "Second"}))
    monkeypatch.setattr(chain, "async_search_by_title", AsyncMock(return_value=[
        Context(torrent_info=TorrentInfo(title="Updated result")),
    ]))
    await endpoint.search_by_title(keyword="Updated", _=TokenPayload(sub=61))
    updated = await endpoint.search_latest_context(TokenPayload(sub=61))
    second = await endpoint.search_latest_context(TokenPayload(sub=62))
    assert updated.data.params["keyword"] == "Updated"
    assert updated.data.diagnostics is None
    assert updated.data.results[0].torrent_info.title == "Updated result"
    assert second.data.params == {"keyword": "Second"}
    assert second.data.results == []
