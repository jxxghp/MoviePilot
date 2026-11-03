"""
验证插件扩展点之后及真实提交前的黑名单门禁与历史安全快照。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.chain.download.selection as selection_module
import app.chain.download.submission as submission_module
from app.application.configuration import get_configured_system_config
from app.application.download.blacklist import TorrentBlacklistService
from app.chain.download import DownloadChain
from app.domain.blacklist import torrent_blacklist_identity
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.blacklist import BlacklistRule
from app.schemas.event import ResourceSelectionEventData
from app.schemas.types import MediaType, SystemConfigKey


def _context(title="allowed"):
    """
    构造包含完整媒体与种子事实的最小下载上下文。
    """
    return Context(meta_info=MetaInfo(title), media_info=MediaInfo(type=MediaType.MOVIE, title=title),
                   torrent_info=TorrentInfo(title=title, site=1))


def _multiple_hash_magnet():
    """
    构造长度未超元数据上限但指向三十三个不同资源的异常磁力。
    """
    return "magnet:?" + "&".join(f"xt=urn:btih:{index:040x}" for index in range(33))


@pytest.fixture
def setup(monkeypatch):
    """
    保留真实下载阶段，只替换外部媒体补全、下载器和持久化副作用。
    """
    config = get_configured_system_config()
    previous = config.get(SystemConfigKey.TorrentBlacklist)
    config.set(SystemConfigKey.TorrentBlacklist, None)
    service = TorrentBlacklistService(config)
    chain = object.__new__(DownloadChain)
    chain.download = Mock(return_value=("qb", "ab" * 20, None, None))
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()
    chain._record_download_failure = Mock()
    chain._record_music_album_track_keys = Mock()
    chain._active_download_failure_fingerprints = Mock(return_value={})
    chain._resolve_media_download_dir = Mock(return_value=("local", Path("/downloads"), None))
    content = "magnet:?xt=urn:btih:" + "ab" * 20
    chain.download_torrent = Mock(return_value=(content, None, None))
    helper = SimpleNamespace(
        get_fileinfo_from_torrent_content=Mock(return_value=("test", ["test.mkv"])),
        sort_torrents=lambda contexts: contexts,
    )
    monkeypatch.setattr(submission_module, "TorrentHelper", lambda: helper)
    monkeypatch.setattr(selection_module, "TorrentHelper", lambda: helper)
    monkeypatch.setattr(submission_module, "MediaChain",
                        lambda: SimpleNamespace(supplement_tmdb_info=lambda media, _meta: media))
    events = SimpleNamespace(send_event=Mock(return_value=None))
    monkeypatch.setattr(submission_module, "eventmanager", events)
    chain.eventmanager = events
    try:
        yield SimpleNamespace(chain=chain, service=service, events=events, content=content, config=config)
    finally:
        config.set(SystemConfigKey.TorrentBlacklist, previous)


def test_batch_filters_plugin_replacement_from_one_snapshot(setup):
    """
    插件替换批量候选后才应用限制，本轮只读取一次黑名单快照。
    """
    setup.service.add_rule(BlacklistRule(keyword="blocked"))
    allowed, blocked = _context(), _context("blocked")
    setup.events.send_event.return_value = SimpleNamespace(
        event_data=ResourceSelectionEventData(updated=True, updated_contexts=[blocked, allowed]))
    original = setup.config.get
    setup.config.get = Mock(wraps=original)
    try:
        result, _ = setup.chain._prepare_batch_download_contexts([allowed], None, "Subscribe|1")
        assert result == [allowed]
        assert sum(call.args == (SystemConfigKey.TorrentBlacklist,) for call in setup.config.get.call_args_list) == 1
    finally:
        setup.config.get = original


@pytest.mark.parametrize("replace_context", [False, True])
def test_single_rechecks_resource_after_plugin_change(setup, replace_context):
    """
    原地修改和替换整个上下文都不能绕过插件之后的门禁。
    """
    setup.service.add_rule(BlacklistRule(keyword="blocked"))
    context = _context()

    def change(_event_type, data):
        """
        模拟插件在事件边界提供另一条资源。
        """
        if replace_context:
            data.context = _context("blocked")
        else:
            data.context.torrent_info = TorrentInfo(title="blocked", site=1)
        return SimpleNamespace(event_data=data)

    setup.events.send_event.side_effect = change
    result, reason = setup.chain.download_single(context, source="Manual", return_detail=True)
    assert result is None
    assert "黑名单" in reason
    setup.chain.download_torrent.assert_not_called()
    setup.chain.download.assert_not_called()
    setup.chain._record_download_failure.assert_not_called()
    setup.chain._settle_accepted_download.assert_not_called()


@pytest.mark.parametrize("source", ["Subscribe|1", "rss", "Manual", None])
@pytest.mark.parametrize("entryway", ["fetch", "content", "file"])
def test_actual_hash_blocks_every_single_entryway(setup, tmp_path, source, entryway):
    """
    旧历史哈希在读取内容后拒绝提交，覆盖订阅、RSS、手动及预提供内容路径。
    """
    setup.service.add_resource(torrent_blacklist_identity(TorrentInfo(info_hash="ab" * 20)))
    arguments = {}
    if entryway == "content":
        arguments["torrent_content"] = setup.content
    elif entryway == "file":
        file = tmp_path / "test.torrent"
        file.write_bytes(setup.content.encode())
        arguments["torrent_file"] = file
    result, reason = setup.chain.download_single(_context(), source=source, return_detail=True, **arguments)
    assert result is None
    assert "黑名单" in reason
    setup.chain.download.assert_not_called()
    setup.chain._record_download_failure.assert_not_called()
    setup.chain._record_rejected_download.assert_not_called()
    setup.chain._settle_accepted_download.assert_not_called()


@pytest.mark.parametrize("field", ["info_hash", "download_hash"])
def test_optional_multi_resource_hash_cannot_break_success_history(setup, db, field):
    """
    下载器真实成功后写入历史时，异常可选元数据或返回哈希不能破坏安全身份结算。
    """
    from app.db.adapters.history.download import TransactionalDownloadHistoryRepository
    from app.db.models.downloadhistory import DownloadFiles, DownloadHistory
    from app.db.session import SessionFactory, async_session_scope

    db.watermark(DownloadHistory, DownloadFiles)
    repository = TransactionalDownloadHistoryRepository(sync_session=SessionFactory, async_session=async_session_scope)
    chain = setup.chain
    del chain._settle_accepted_download
    chain.download_history_repository = repository
    chain._resolve_actual_download_dir = Mock(return_value=Path("/downloads"))
    chain._submit_download_added_task = Mock()
    chain._build_download_notification = Mock(return_value=None)
    context = _context()
    returned_hash = "ab" * 20
    if field == "info_hash":
        context.torrent_info.info_hash = _multiple_hash_magnet()
    else:
        returned_hash = _multiple_hash_magnet()
        chain.download.return_value = ("qb", returned_hash, None, None)

    result, reason = chain.download_single(context, torrent_content=setup.content, return_detail=True)

    assert result is not None
    assert reason is None
    chain.download.assert_called_once()
    history = repository.get_by_hash(returned_hash)
    assert history is not None
    assert history.note["torrent_blacklist"]["hashes"] == ["ab" * 20]
    chain._record_download_failure.assert_not_called()


def test_actual_multi_resource_magnet_is_rejected_before_submission(setup):
    """
    真实提交内容含多个资源时即使黑名单为空也必须先拒绝，不提交或记失败冷却。
    """
    result, reason = setup.chain.download_single(
        _context(), torrent_content=_multiple_hash_magnet(), return_detail=True)
    assert result is None
    assert "资源哈希" in reason
    setup.chain.download.assert_not_called()
    setup.chain._settle_accepted_download.assert_not_called()
    setup.chain._record_download_failure.assert_not_called()
    setup.chain._record_rejected_download.assert_not_called()


def test_last_guard_uses_prepared_resource_and_latest_rules(setup):
    """
    内容准备后新加入的规则立即生效，判断真正提交的资源而不是过时上下文。
    """
    prepared = SimpleNamespace(torrent=TorrentInfo(title="blocked"), torrent_content=setup.content,
                               media=_context().media_info, download_dir=Path("/downloads"), site_downloader=None)
    setup.service.add_rule(BlacklistRule(keyword="blocked"))
    result, reason = setup.chain._submit_prepared_download(
        prepared=prepared, context=_context(), episodes=None, channel=None, source="Manual",
        downloader=None, userid=None, username=None, label=None, custom_words=None, governance=None)
    assert result is None
    assert "黑名单" in reason
    setup.chain.download.assert_not_called()
    setup.chain._record_rejected_download.assert_not_called()


@pytest.mark.parametrize("value", [{}, {"version": 1}, {"entries": []},
                                  {"version": 1, "entries": "invalid"}])
def test_corrupt_config_fails_closed_without_cooldown(setup, value):
    """
    配置损坏必须显式停止批量和单条下载，不写失败冷却。
    """
    setup.config.set(SystemConfigKey.TorrentBlacklist, value)
    result, _ = setup.chain._prepare_batch_download_contexts([_context()], None, None)
    assert result == []
    result, reason = setup.chain.download_single(_context(), return_detail=True)
    assert result is None
    assert "配置无效" in reason
    setup.chain.download.assert_not_called()
    setup.chain._record_download_failure.assert_not_called()
    setup.chain._settle_accepted_download.assert_not_called()


def test_history_note_adds_safe_identity_without_losing_existing_fields(setup):
    """
    成功历史在来源、音乐或插件备注旁保存安全身份，不写下载 URL 和令牌。
    """
    chain = setup.chain
    history_repository = SimpleNamespace(add=Mock(return_value=1))
    chain.download_history_repository = history_repository
    chain._build_download_note = Mock(return_value={"source": "Subscribe|1", "music": {"version": 1}, "custom": [1]})
    chain._submit_download_added_task = Mock()
    chain._build_download_notification = Mock(return_value=None)
    context = _context()
    context.subscription_download = {"source": "test"}
    torrent = TorrentInfo(site=1, torrent_id="123", title="sample",
                          enclosure="https://site.test/download.php?id=123&passkey=secret")
    chain._settle_download_success(
        context=context, media=context.media_info, meta=context.meta_info, torrent=torrent,
        folder_name="folder", file_list=[], download_dir=Path("/downloads"), layout=None,
        downloader="qb", download_hash="ab" * 20, download_episodes=None, episodes=None,
        channel=None, source="Subscribe|1", userid=None, username=None,
        torrent_content=setup.content, custom_words=None)
    history = history_repository.add.call_args.args[0]
    payload = history.to_payload()["note"]
    assert payload["music"] == {"version": 1}
    assert payload["custom"] == [1]
    assert payload["subscription_download"] == {"source": "test"}
    assert payload["torrent_blacklist"]["hashes"] == ["ab" * 20]
    assert "secret" not in str(payload)


def test_invalid_raw_config_never_reaches_downloader(setup):
    """
    启动读取原始坏 JSON 后，单次提交保持拒绝且不产生历史和冷却副作用。
    """
    from sqlalchemy import text

    from app.db.oper.systemconfig import SystemConfigOper
    from app.db.session import SessionFactory

    setup.service.add_rule(BlacklistRule(keyword="old"))
    key = SystemConfigKey.TorrentBlacklist.value
    with SessionFactory() as session:
        session.execute(text("UPDATE systemconfig SET value = 'invalid-json' WHERE key = :key"), {"key": key})
        session.commit()
    try:
        SystemConfigOper().load_snapshot()
        result, reason = setup.chain.download_single(_context(), torrent_content=setup.content, return_detail=True)
        assert result is None
        assert "配置无效" in reason
        setup.chain.download.assert_not_called()
        setup.chain._record_download_failure.assert_not_called()
        setup.chain._settle_accepted_download.assert_not_called()
    finally:
        with SessionFactory() as session:
            session.execute(text("UPDATE systemconfig SET value = NULL WHERE key = :key"), {"key": key})
            session.commit()
        SystemConfigOper().load_snapshot()
