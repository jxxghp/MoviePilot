"""覆盖工作流资源在 RSS 领域对象与搜索/恢复后的 Schema 对象之间流转的契约。"""

from datetime import datetime, timedelta

from app.application.rules import RuleHelper
from app.chain.workflow import _serialize_workflow_context
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.modules.filter import FilterModule
from app.schemas.context import Context as WorkflowContext
from app.schemas.rule import FilterRuleGroup
from app.schemas.types import MediaType
from app.schemas.workflow import ActionContext
from app.workflow.actions import filter_torrents
from app.workflow.actions.filter_torrents import FilterTorrentsAction


def _domain_context(pubdate: str) -> Context:
    """构造 RSS 动作产出的领域资源上下文。"""
    return Context(
        meta_info=MetaInfo("Movie 2024 1080p"),
        media_info=MediaInfo(title="Movie", type=MediaType.MOVIE, tmdb_id=42),
        torrent_info=TorrentInfo(
            title="Movie 2024 1080p",
            enclosure="https://example.com/movie.torrent",
            pubdate=pubdate,
        ),
    )


def test_domain_torrent_context_survives_persist_and_restore():
    """RSS 领域资源写入数据库后应能还原，不能被存成文本导致恢复时整份上下文被丢弃。"""
    context = ActionContext()
    context.torrents.append(_domain_context("2026-10-01 08:00:00"))

    restored = ActionContext.model_validate(_serialize_workflow_context(context))

    torrent = restored.torrents[0]
    assert torrent.torrent_info.title == "Movie 2024 1080p"
    assert torrent.torrent_info.pubdate == "2026-10-01 08:00:00"
    assert torrent.media_info.tmdb_id == 42
    assert torrent.meta_info.title


def test_filter_torrents_restores_schema_context_for_media_and_publish_rules(monkeypatch):
    """搜索或恢复得到的 Schema 资源应能按媒体类型规则组和发布时间规则过滤，并保留原对象与优先级。"""
    recent = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    stale = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    fresh_source = WorkflowContext.model_validate(_domain_context(recent).to_dict())
    stale_source = WorkflowContext.model_validate(_domain_context(stale).to_dict())

    rulehelper = RuleHelper()
    monkeypatch.setattr(rulehelper, "get_rule_groups", lambda: [
        FilterRuleGroup(name="movie", rule_string="PUB", media_type=MediaType.MOVIE.value),
    ])
    module = FilterModule()
    module.rulehelper = rulehelper
    module.rule_set = {"PUB": {"publish_time": "0-120"}}

    class _FilterChain:
        """把动作的过滤请求交给真实过滤模块，绕开模块管理器装配。"""

        @staticmethod
        def filter_torrents(rule_groups, torrent_list, mediainfo=None):
            return module.filter_torrents(rule_groups=rule_groups, torrent_list=torrent_list, mediainfo=mediainfo)

    monkeypatch.setattr(filter_torrents, "ActionChain", _FilterChain)
    monkeypatch.setattr(filter_torrents.runtime_stop_state, "is_workflow_stopped", lambda _wid: False)

    context = ActionContext(torrents=[fresh_source, stale_source])
    result = FilterTorrentsAction("filter").execute(
        workflow_id=1,
        params={"rule_groups": ["movie"]},
        context=context,
    )

    assert result.torrents == [fresh_source]
    assert result.torrents[0] is fresh_source
    assert fresh_source.torrent_info.pri_order == 100
