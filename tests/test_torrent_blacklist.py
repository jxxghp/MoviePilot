"""
验证永久黑名单的匹配、脱敏身份及原子配置行为。
"""

import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.application.configuration import get_configured_system_config
from app.application.download.blacklist import TorrentBlacklistService
from app.domain.blacklist import (
    BlacklistConfigurationError,
    blacklist_matches,
    history_blacklist_identity,
    parse_blacklist_config,
    torrent_blacklist_identity,
)
from app.domain.context import TorrentInfo
from app.schemas.blacklist import BlacklistConfig, BlacklistEntry, BlacklistRule
from app.schemas.context import TorrentInfo as WireTorrentInfo
from app.schemas.types import SystemConfigKey


def _entry(**kwargs):
    """
    构造最小合法条目以单独验证匹配行为。
    """
    return BlacklistEntry(id="a" * 32, created_at=datetime.now(timezone.utc), **kwargs)


@pytest.fixture
def service():
    """
    每个用例使用独立配置值，并在结束后恢复原值。
    """
    config = get_configured_system_config()
    old = config.get(SystemConfigKey.TorrentBlacklist)
    config.set(SystemConfigKey.TorrentBlacklist, None)
    try:
        yield TorrentBlacklistService(config)
    finally:
        config.set(SystemConfigKey.TorrentBlacklist, old)


def test_manual_rules_normalize_and_require_every_condition():
    """
    单条规则按与匹配，多条按或匹配；未知发布者不满足发布者条件。
    """
    rule = BlacklistRule(keyword="  bad.RELEASE  ", uploader="  ALIce ", site_id=7)
    entry = _entry(kind="manual", rule=rule)
    torrent = TorrentInfo(site=7, title="Good", description="Bad.Release", uploader="alice")
    assert blacklist_matches((entry,), torrent)
    torrent.site = 8
    assert not blacklist_matches((entry,), torrent)
    torrent.site = 7
    torrent.uploader = None
    assert not blacklist_matches((entry,), torrent)
    other = _entry(kind="manual", rule=BlacklistRule(keyword="GOOD"))
    assert blacklist_matches((entry, other), torrent)
    torrent.title = "BadXRelease"
    torrent.description = None
    assert not blacklist_matches((other,), torrent)
    assert not blacklist_matches((_entry(kind="manual", rule=BlacklistRule(keyword="bad.release")),), torrent)
    assert rule.keyword == "bad.release"
    assert rule.uploader == "alice"


@pytest.mark.parametrize("payload", [{}, {"keyword": " "}, {"keyword": "x" * 257},
                                       {"uploader": "x" * 129}, {"site_id": 0}, {"site_id": True}])
def test_invalid_manual_rules_are_rejected(payload):
    """
    空条件及越界条件不得保存。
    """
    with pytest.raises(ValidationError):
        BlacklistRule(**payload)


def test_gazelle_identity_uses_actual_torrent_across_both_urls():
    """
    分组页不能覆盖具体下载 ID，令牌和 URL 用户信息不参与持久化。
    """
    torrent = TorrentInfo(site=1, page_url="https://u:secret@tracker.test/torrents.php?id=12",
                          enclosure="https://tracker.test/torrents.php?action=download&id=39&auth=secret")
    identity = torrent_blacklist_identity(torrent)
    assert len(identity.digests) == 1
    torrent.enclosure = "https://tracker.test/torrents.php?action=download&id=40&auth=secret"
    assert torrent_blacklist_identity(torrent).digests != identity.digests
    torrent.enclosure = "https://tracker.test/torrents.php?action=download&id=39&auth=changed"
    assert torrent_blacklist_identity(torrent).digests == identity.digests
    torrent.enclosure = "https://tracker.test/download.php?id=999&torrentid=39"
    assert torrent_blacklist_identity(torrent).digests == identity.digests
    torrent.page_url = "https://tracker.test/torrents.php?torrent_id=55"
    torrent.enclosure = "https://tracker.test/download.php?torrentid=39"
    explicit = torrent_blacklist_identity(torrent)
    torrent.torrent_id = "55"
    assert torrent_blacklist_identity(torrent).digests == explicit.digests
    assert "secret" not in identity.model_dump_json()
    assert "tracker.test" not in identity.model_dump_json()
    group = TorrentInfo(site=1, page_url="https://tracker.test/torrents.php?id=12")
    assert not torrent_blacklist_identity(group).digests


def test_hostname_fallback_ignores_userinfo():
    """
    未配置站点 ID 时只使用不含认证信息的主机和端口。
    """
    left = TorrentInfo(enclosure="https://alice:token@tracker.test:8443/download.php?id=1")
    right = TorrentInfo(enclosure="https://bob:rotated@tracker.test:8443/download.php?id=1")
    assert torrent_blacklist_identity(left).digests == torrent_blacklist_identity(right).digests


def test_real_hash_aliases_and_cross_site_content_matching():
    """
    标准十六进制、base32 和 v2 截断哈希识别同一内容，跨站点生效。
    """
    digest = "ab" * 20
    encoded = base64.b32encode(bytes.fromhex(digest)).decode()
    original = TorrentInfo(site=1, info_hash=encoded)
    identity = torrent_blacklist_identity(original)
    assert identity.hashes == (digest,)
    entry = _entry(kind="resource", identity=identity)
    assert blacklist_matches((entry,), TorrentInfo(site=2), f"magnet:?xt=urn:btih:{digest}")
    v2 = torrent_blacklist_identity(TorrentInfo(info_hash="cd" * 32))
    assert set(v2.hashes) == {"cd" * 32, "cd" * 20}
    content = b"d4:infod12:meta versioni2e4:name1:xee"
    assert len(torrent_blacklist_identity(TorrentInfo(), content).hashes) == 2
    assert not torrent_blacklist_identity(TorrentInfo(info_hash="invalid")).hashes


def test_hybrid_hash_aliases_preserve_v1_base32_and_v2_identity():
    """
    同一 hybrid 磁力的 v1/base32 等价项、完整 v2 及截断别名全部保留且去重。
    """
    v1, v2 = "ab" * 20, "cd" * 32
    base32 = base64.b32encode(bytes.fromhex(v1)).decode()
    content = f"magnet:?xt=urn:btih:{v1}&xt=urn:btih:{base32}&xt=urn:btmh:1220{v2}"
    identity = torrent_blacklist_identity(TorrentInfo(info_hash=content), content, v2[:40])
    assert set(identity.hashes) == {v1, v2, v2[:40]}


def test_source_metadata_survives_wire_roundtrip():
    """
    来源和插件返回的发布者、种子 ID 与哈希穿过接口序列化后保持不变。
    """
    original = TorrentInfo(site=7, uploader="Alice", torrent_id="39", info_hash="ab" * 20)
    wire = WireTorrentInfo.model_validate(original.to_dict())
    restored = TorrentInfo()
    restored.from_dict(wire.model_dump(mode="json"))
    assert restored.uploader == original.uploader
    assert restored.torrent_id == original.torrent_id
    assert restored.info_hash == original.info_hash


def test_history_snapshot_and_legacy_hash_never_use_title():
    """
    历史备注优先恢复安全身份，旧历史只接受合法哈希。
    """
    identity = torrent_blacklist_identity(TorrentInfo(site=2, torrent_id="9", uploader=" Alice "),
                                         "magnet:?xt=urn:btih:" + "ab" * 20)
    note = {"source": "Subscribe|1", "music": {"version": 1}, "torrent_blacklist": identity.model_dump(mode="json")}
    history = SimpleNamespace(note=note, download_hash=None, torrent_name="unsafe title")
    assert history_blacklist_identity(history) == identity
    assert note["source"] == "Subscribe|1"
    legacy = SimpleNamespace(note={}, download_hash="ab" * 20, torrent_name="a")
    assert history_blacklist_identity(legacy).hashes == ("ab" * 20,)
    legacy.download_hash = "not-a-hash"
    with pytest.raises(ValueError, match="手动"):
        history_blacklist_identity(legacy)


def test_config_snapshot_immutable_and_malformed_config_fails_closed(service):
    """
    返回快照不可修改；损坏配置不能变成允许全部下载的空规则。
    """
    service.add_rule(BlacklistRule(keyword="bad"))
    snapshot = service.snapshot()
    with pytest.raises(ValidationError):
        snapshot.entries[0].rule.keyword = "changed"
    listing = service.list(page=1, count=1)
    assert listing.total == 1
    assert listing.items == snapshot.entries
    get_configured_system_config().set(SystemConfigKey.TorrentBlacklist, {"version": 9, "entries": []})
    with pytest.raises(BlacklistConfigurationError):
        service.snapshot()
    with pytest.raises(BlacklistConfigurationError):
        service.add_rule(BlacklistRule(keyword="anything"))


@pytest.mark.parametrize("value", [{}, {"version": 1}, {"entries": []}])
def test_existing_config_requires_version_and_entries(value):
    """
    已存在的持久化对象必须完整，缺少版本或条目集合时不能依赖模型默认值解除限制。
    """
    with pytest.raises(BlacklistConfigurationError, match="配置无效"):
        parse_blacklist_config(value)


@pytest.mark.parametrize("value", [None, {"version": 1, "entries": []}])
def test_absent_or_explicit_empty_config_remains_valid(value):
    """
    首次未配置与显式完整空配置均表示没有永久限制。
    """
    assert parse_blacklist_config(value) == BlacklistConfig()


def test_atomic_concurrent_additions_and_duplicate_ids(service):
    """
    并发新增不同规则不丢失更新，相同规则保留最早 ID 和时间。
    """
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda index: service.add_rule(BlacklistRule(keyword=f"rule{index % 5}")), range(20)))
    assert len(service.snapshot().entries) == 5
    assert sum(item.created for item in results) == 5
    for index in range(5):
        matching = [result.entry for result in results if result.entry.rule.keyword == f"rule{index}"]
        assert len({item.id for item in matching}) == 1
        assert len({item.created_at for item in matching}) == 1


def test_resource_overlap_merges_without_resetting_identity(service):
    """
    同站身份或内容哈希重叠时合并别名，并保持原创建时间和 ID。
    """
    first = torrent_blacklist_identity(TorrentInfo(site=1, torrent_id="1", info_hash="ab" * 20))
    second = torrent_blacklist_identity(TorrentInfo(site=2, torrent_id="2", info_hash="ab" * 20))
    original = service.add_resource(first)
    duplicate = service.add_resource(second)
    assert not duplicate.created
    assert duplicate.entry.id == original.entry.id
    assert duplicate.entry.created_at == original.entry.created_at
    assert len(duplicate.entry.identity.digests) == 2
    assert service.remove(original.entry.id)
    assert not service.remove(original.entry.id)


def test_resource_alias_bridge_merges_all_overlapping_entries(service):
    """
    新别名连接两条已有资源时合并全部身份，保留最早条目的 ID 和时间。
    """
    first = service.add_resource(torrent_blacklist_identity(TorrentInfo(site=1, torrent_id="1", info_hash="ab" * 20)))
    service.add_resource(torrent_blacklist_identity(TorrentInfo(site=2, torrent_id="2", info_hash="cd" * 20)))
    bridge = torrent_blacklist_identity(TorrentInfo(site=1, torrent_id="1", info_hash="cd" * 20))
    result = service.add_resource(bridge)
    assert not result.created
    assert result.entry.id == first.entry.id
    assert len(service.snapshot().entries) == 1
    assert set(result.entry.identity.hashes) == {"ab" * 20, "cd" * 20}
    assert len(result.entry.identity.digests) == 2


def test_resource_merge_bound_failure_keeps_original_entry(service):
    """
    合并超出别名上限时拒绝写入，保留原 ID、时间及全部已有别名。
    """
    from app.schemas.blacklist import BlacklistIdentity

    identity = BlacklistIdentity(digests=tuple(f"{index:064x}" for index in range(32)), hashes=("ab" * 20,))
    original = service.add_resource(identity)
    overflow = BlacklistIdentity(digests=("ff" * 32,), hashes=("ab" * 20,))
    with pytest.raises(ValidationError):
        service.add_resource(overflow)
    assert service.snapshot().entries == (original.entry,)


def test_capacity_checks_after_deduplication(service):
    """
    满额时重复规则仍可幂等加入，新的规则返回明确错误。
    """
    now = datetime.now(timezone.utc)
    entries = tuple(BlacklistEntry(id=f"{index:032x}", created_at=now, kind="manual",
                                   rule=BlacklistRule(keyword=f"rule{index}")) for index in range(1000))
    config = BlacklistConfig(entries=entries)
    get_configured_system_config().set(SystemConfigKey.TorrentBlacklist, config.model_dump(mode="json"))
    assert not service.add_rule(BlacklistRule(keyword="rule1")).created
    with pytest.raises(ValueError, match="1000"):
        service.add_rule(BlacklistRule(keyword="new"))
    assert len(service.snapshot().entries) == 1000


def test_failed_commit_does_not_publish_rules(service):
    """
    提交失败时数据库和运行时快照都保持旧规则。
    """
    from sqlalchemy.orm import Session

    service.add_rule(BlacklistRule(keyword="old"))
    before = service.snapshot()
    with patch.object(Session, "commit", side_effect=RuntimeError("commit failed")):
        with pytest.raises(RuntimeError, match="commit failed"):
            service.add_rule(BlacklistRule(keyword="new"))
    assert service.snapshot() == before
    assert "new" not in json.dumps(get_configured_system_config().get(SystemConfigKey.TorrentBlacklist))


def test_normalized_text_still_obeys_field_bounds():
    """
    大小写折叠会扩展字符长度，规范化后的值也必须受长度限制。
    """
    with pytest.raises(ValidationError):
        BlacklistRule(keyword="ß" * 129)


@pytest.mark.asyncio
async def test_async_add_and_remove_keep_all_concurrent_changes(service):
    """
    异步同条新增幂等，新增和删除使用最新值而不会覆盖对方。
    """
    original = service.add_rule(BlacklistRule(keyword="old"))
    results = await asyncio.gather(
        service.async_remove(original.entry.id),
        service.async_add_rule(BlacklistRule(keyword="new")),
        service.async_add_rule(BlacklistRule(keyword="NEW")),
    )
    assert results[0] is True
    assert sum(result.created for result in results[1:]) == 1
    assert [entry.rule.keyword for entry in service.snapshot().entries] == ["new"]


def test_invalid_raw_json_is_retained_and_blocks_downloads(service):
    """
    原始 JSON 损坏不能被启动快照归零而意外解除永久限制。
    """
    from sqlalchemy import text

    from app.db.oper.systemconfig import SystemConfigOper
    from app.db.session import SessionFactory

    service.add_rule(BlacklistRule(keyword="old"))
    key = SystemConfigKey.TorrentBlacklist.value
    with SessionFactory() as session:
        session.execute(text("UPDATE systemconfig SET value = :value WHERE key = :key"),
                        {"value": "invalid-json-secret", "key": key})
        session.commit()
    try:
        SystemConfigOper().load_snapshot()

        with pytest.raises(BlacklistConfigurationError):
            service.snapshot()
        assert "配置无效" in service.rejection(TorrentInfo(title="anything"))
        with pytest.raises(BlacklistConfigurationError, match="配置无效"):
            service.add_rule(BlacklistRule(keyword="new"))
        with pytest.raises(BlacklistConfigurationError, match="配置无效"):
            service.remove("a" * 32)
        with pytest.raises(BlacklistConfigurationError, match="配置无效"):
            asyncio.run(service.async_add_resource(
                torrent_blacklist_identity(TorrentInfo(info_hash="ab" * 20))))
        with pytest.raises(BlacklistConfigurationError, match="配置无效"):
            asyncio.run(service.async_remove("a" * 32))
        with SessionFactory() as session:
            raw = session.execute(text("SELECT value FROM systemconfig WHERE key = :key"), {"key": key}).scalar()
        assert raw == "invalid-json-secret"
        assert "invalid-json-secret" not in str(get_configured_system_config().get(SystemConfigKey.TorrentBlacklist))
    finally:
        with SessionFactory() as session:
            session.execute(text("UPDATE systemconfig SET value = NULL WHERE key = :key"), {"key": key})
            session.commit()
        SystemConfigOper().load_snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize("entryway", ["production", "bootstrap", "local_setup"])
async def test_initial_configuration_paths_bind_retention_before_reading(service, monkeypatch, entryway):
    """
    生产、共享测试和离线初始化首次装配新 Oper 前绑定相同策略，坏行不会先被清空。
    """
    from sqlalchemy import text

    from app.db.oper.systemconfig import SystemConfigOper
    from app.db.session import SessionFactory
    from app.startup.composition import configuration as composition
    from app.testing.bootstrap import prepare_backend

    original = SystemConfigOper()
    fresh = object.__new__(SystemConfigOper)
    fresh.__init__()
    service.add_rule(BlacklistRule(keyword="old"))
    key = SystemConfigKey.TorrentBlacklist.value
    with SessionFactory() as session:
        session.execute(text("UPDATE systemconfig SET value = 'invalid-secret' WHERE key = :key"), {"key": key})
        session.commit()
    try:
        if entryway == "production":
            monkeypatch.setattr(composition, "SystemConfigOper", lambda: fresh)
            await composition.compose_configuration(
                executor=SimpleNamespace(run=AsyncMock(side_effect=lambda callback: callback())),
                settings=SimpleNamespace(),
            )
        else:
            monkeypatch.setattr("app.db.oper.systemconfig.SystemConfigOper", lambda: fresh)
            if entryway == "bootstrap":
                prepare_backend()
            else:
                from scripts import local_setup
                monkeypatch.setattr("app.startup.initializers.database.prepare_database", lambda **_kwargs: None)
                monkeypatch.setattr(local_setup, "_ensure_superuser_account_inner", lambda: None)
                local_setup._apply_local_system_config_transactional({})
        for _ in range(2):
            with pytest.raises(BlacklistConfigurationError):
                parse_blacklist_config(fresh.get(SystemConfigKey.TorrentBlacklist))
            fresh.load_snapshot()
        with SessionFactory() as session:
            assert session.execute(text("SELECT value FROM systemconfig WHERE key = :key"),
                                   {"key": key}).scalar_one() == "invalid-secret"
    finally:
        with SessionFactory() as session:
            session.execute(text("UPDATE systemconfig SET value = NULL WHERE key = :key"), {"key": key})
            session.commit()
        original.load_snapshot()
