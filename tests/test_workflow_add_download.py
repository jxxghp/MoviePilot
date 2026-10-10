"""覆盖 RSS 领域上下文通过工作流动作桥接后提交下载的契约。"""

import json
from unittest.mock import Mock

import pytest

from app.chain.workflow import _serialize_workflow_value
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.meta.metabase import MetaBase
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaType
from app.schemas.workflow import ActionContext
from app.workflow.actions import add_download, fetch_rss, filter_torrents
from app.workflow.actions.add_download import AddDownloadAction
from app.workflow.actions.fetch_rss import FetchRssAction
from app.workflow.actions.filter_torrents import FilterTorrentsAction


@pytest.mark.parametrize("match_media", [False, True])
@pytest.mark.parametrize("only_lack", [False, True])
def test_rss_filter_add_download_accepts_domain_context(monkeypatch, match_media, only_lack):
    """RSS 无论是否预先识别，都应经资源过滤及动作输入桥接正常提交下载。"""
    media = MediaInfo(title="Show", type=MediaType.TV, tmdb_id=123)
    rss_helper = Mock()
    rss_helper.parse.return_value = [{
        "title": "Show S01E03",
        "enclosure": "https://example.com/show.torrent",
        "link": "https://example.com/details/1",
        "size": 1024,
    }]
    media_chain = Mock()
    media_chain.recognize_by_meta.return_value = media
    download_chain = Mock()
    download_chain.media_exists.return_value = None
    download_chain.download_single.return_value = "hash-show"
    torrent_helper = Mock()
    torrent_helper.filter_torrent.return_value = True
    filter_chain = Mock()
    filter_chain.filter_torrents.return_value = True
    monkeypatch.setattr(fetch_rss, "RssHelper", lambda: rss_helper)
    monkeypatch.setattr(fetch_rss, "MediaChain", lambda: media_chain)
    monkeypatch.setattr(add_download, "MediaChain", lambda: media_chain)
    monkeypatch.setattr(add_download, "DownloadChain", lambda: download_chain)
    monkeypatch.setattr(filter_torrents, "TorrentHelper", lambda: torrent_helper)
    monkeypatch.setattr(filter_torrents, "ActionChain", lambda: filter_chain)
    monkeypatch.setattr(fetch_rss.runtime_stop_state, "is_workflow_stopped", lambda _wid: False)

    rss_result = FetchRssAction("rss").execute_with_inputs(
        workflow_id=1,
        params={"url": "https://example.com/rss.xml", "match_media": match_media},
        inputs={},
        runtime={},
        context=ActionContext(),
    )
    source = rss_result.outputs["torrents"][0]
    assert isinstance(source, Context)
    assert source.media_info is (media if match_media else None)

    filter_result = FilterTorrentsAction("filter").execute_with_inputs(
        workflow_id=1,
        params={},
        inputs=rss_result.outputs,
        runtime={},
        context=ActionContext(),
    )
    action = AddDownloadAction("download")
    saved_cache = []
    action.check_cache = lambda _wid, _key: False
    action.save_cache = lambda _wid, key: saved_cache.append(key)
    result = action.execute_with_inputs(
        workflow_id=1,
        params={"only_lack": only_lack, "downloader": "qb", "save_path": "/downloads", "labels": "rss"},
        inputs=filter_result.outputs,
        runtime={},
        context=ActionContext(),
    )

    assert result.success is True
    assert action.done is True
    download_chain.download_single.assert_called_once_with(
        context=source, downloader="qb", save_path="/downloads", label="rss",
    )
    assert source.media_info is media
    assert isinstance(source.meta_info, MetaBase)
    assert isinstance(source.torrent_info, TorrentInfo)
    assert media_chain.recognize_by_meta.call_count == 1
    assert download_chain.media_exists.call_count == int(only_lack)
    assert saved_cache == ["None-Show S01E03"]
    assert result.outputs["downloads"][0].download_id == "hash-show"
    assert result.context.downloads[0].downloader == "qb"


def test_add_download_reuses_complete_domain_context():
    """领域对象应原样交给下载链，保留剧集限制、识别状态及原始媒体信息。"""
    context = Context(
        meta_info=MetaInfo("Show S01E03"),
        media_info=MediaInfo(title="Show", type=MediaType.TV, tmdb_id=123),
        torrent_info=TorrentInfo(title="Show S01E03"),
        media_recognize_fail_count=2,
        allowed_episodes={3},
        selected_episodes=[3],
        resource_source="rss",
    )

    assert AddDownloadAction._to_domain_context(context) is context


@pytest.mark.parametrize("missing", ["media_info", "torrent_info"])
def test_add_download_rejects_incomplete_domain_context(missing):
    """原生领域输入仍须满足下载动作的媒体及种子信息完整性要求。"""
    context = Context(
        media_info=MediaInfo(title="Show", type=MediaType.TV),
        torrent_info=TorrentInfo(title="Show S01E03"),
    )
    setattr(context, missing, None)

    with pytest.raises(ValueError, match="缺少媒体或种子信息"):
        AddDownloadAction._to_domain_context(context)


@pytest.mark.parametrize("resume_at", ["filter", "download"])
def test_restored_node_output_can_filter_and_download(monkeypatch, resume_at):
    """持久化的节点输出字典经显式输入恢复后，仍应按领域契约过滤并提交下载。"""
    source = Context(
        meta_info=MetaInfo("Show S01E03"),
        media_info=MediaInfo(title="Show", type=MediaType.TV, tmdb_id=123),
        torrent_info=TorrentInfo(title="Show S01E03", site=1, pri_order=10),
        resource_source="rss",
        match_source="tmdb",
        candidate_recognized=True,
    )
    inputs = json.loads(json.dumps(_serialize_workflow_value({"torrents": [source]})))
    assert isinstance(inputs["torrents"][0], dict)
    restored = AddDownloadAction._to_domain_context(inputs["torrents"][0])
    assert restored.media_info.type is MediaType.TV
    assert restored.resource_source == "rss"
    filter_chain = Mock()
    filter_chain.filter_torrents.return_value = True
    torrent_helper = Mock()
    torrent_helper.filter_torrent.return_value = True
    download_chain = Mock()
    download_chain.download_single.return_value = "restored-hash"
    monkeypatch.setattr(filter_torrents, "ActionChain", lambda: filter_chain)
    monkeypatch.setattr(filter_torrents, "TorrentHelper", lambda: torrent_helper)
    monkeypatch.setattr(add_download, "DownloadChain", lambda: download_chain)
    monkeypatch.setattr(add_download.runtime_stop_state, "is_workflow_stopped", lambda _wid: False)

    if resume_at == "filter":
        filtered = FilterTorrentsAction("filter").execute_with_inputs(
            workflow_id=1, params={}, inputs=inputs, runtime={}, context=ActionContext(),
        )
        assert filtered.success
        assert filter_chain.filter_torrents.call_args.kwargs["mediainfo"].type is MediaType.TV
        inputs = _serialize_workflow_value(filtered.outputs)

    action = AddDownloadAction("download")
    monkeypatch.setattr(action, "check_cache", lambda _wid, _key: False)
    save_cache = Mock()
    monkeypatch.setattr(action, "save_cache", save_cache)
    result = action.execute_with_inputs(
        workflow_id=1, params={}, inputs=inputs, runtime={}, context=ActionContext(),
    )

    assert result.success
    downloaded = download_chain.download_single.call_args.kwargs["context"]
    assert isinstance(downloaded, Context)
    assert downloaded.media_info.type is MediaType.TV
    assert isinstance(downloaded.meta_info, MetaBase)
    assert downloaded.resource_source == "rss"
    assert downloaded.match_source == "tmdb"
    assert downloaded.candidate_recognized
    assert result.outputs["downloads"][0].download_id == "restored-hash"
    save_cache.assert_called_once_with(1, "1-Show S01E03")


@pytest.mark.parametrize("include_valid", [False, True])
def test_historical_text_resource_reports_failure_without_blocking_valid_download(monkeypatch, include_valid):
    """旧版存成文本的资源无法恢复，应提示重跑且保留失败状态，仍处理其余有效资源。"""
    source = Context(
        meta_info=MetaInfo("Show S01E03"),
        media_info=MediaInfo(title="Show", type=MediaType.TV, tmdb_id=123),
        torrent_info=TorrentInfo(title="Show S01E03"),
    )
    context = ActionContext()
    context.torrents = ["Context(meta_info=...)"] + ([source] if include_valid else [])
    download_chain = Mock()
    download_chain.download_single.return_value = "valid-hash"
    warning = Mock()
    monkeypatch.setattr(add_download, "DownloadChain", lambda: download_chain)
    monkeypatch.setattr(add_download.logger, "warning", warning)
    monkeypatch.setattr(add_download.runtime_stop_state, "is_workflow_stopped", lambda _wid: False)
    action = AddDownloadAction("download")
    monkeypatch.setattr(action, "check_cache", lambda _wid, _key: False)
    save_cache = Mock()
    monkeypatch.setattr(action, "save_cache", save_cache)

    result = action.execute_with_inputs(
        workflow_id=1, params={}, inputs={"torrents": context.torrents}, runtime={}, context=context,
    )

    assert result.success is False
    assert "重新运行" in warning.call_args.args[0]
    if include_valid:
        download_chain.download_single.assert_called_once()
        assert download_chain.download_single.call_args.kwargs["context"] is source
        assert result.outputs["downloads"][0].download_id == "valid-hash"
        save_cache.assert_called_once_with(1, "None-Show S01E03")
    else:
        download_chain.download_single.assert_not_called()
        save_cache.assert_not_called()
        assert result.context.downloads == []
