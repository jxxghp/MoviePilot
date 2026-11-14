"""
验证工作流可空集合、动作缺失和事件载荷的执行边界。
"""

import ast
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import BaseModel

from app.chain.workflow import WorkflowChain, WorkflowExecutor
from app.schemas.download import DownloadTask
from app.schemas.event import RecommendSourceEventData
from app.schemas.workflow import Action, ActionContext
from app.workflow import WorkflowManager
from app.workflow.actions.fetch_downloads import FetchDownloadsAction
from app.workflow.actions.fetch_medias import FetchMediasAction
from app.workflow.actions.fetch_rss import FetchRssAction


@pytest.mark.parametrize(("operator", "left", "right", "expected"), [
    (ast.NotEq(), 1, 2, True), (ast.GtE(), 2, 2, True),
    (ast.Lt(), 1, 2, True), (ast.LtE(), 2, 2, True),
    (ast.LtE(), None, 2, False),
])
def test_workflow_comparisons_return_boolean(operator, left, right, expected):
    """
    条件比较保留相等边界，并将不可比较的空值判为不匹配。
    """
    assert WorkflowExecutor.compare_values(left, operator, right) is expected


def test_workflow_condition_rejects_dictionary_unpacking():
    """
    条件字典仅允许显式键值，不执行字典展开或任意对象表达式。
    """
    executor = object.__new__(WorkflowExecutor)
    expression = ast.parse("{**payload}", mode="eval").body
    with pytest.raises(ValueError, match="不支持字典展开"):
        executor.evaluate_condition_node(expression)
    assert executor.evaluate_condition_node(ast.parse("{'key': 1}", mode="eval").body) == {"key": 1}


def test_workflow_execute_event_ignores_non_mapping_payload(monkeypatch):
    """
    无工作流字段的事件不能触发执行，也不会因空载荷抛异常。
    """
    process = Mock()
    monkeypatch.setattr(WorkflowChain, "process", process)
    object.__new__(WorkflowChain).event_process(SimpleNamespace(event_data=None))
    process.assert_not_called()


def test_workflow_conditions_accept_model_payload_and_reject_invalid_payload():
    """
    事件模型使用公开字段参与条件匹配，非字典载荷不能意外满足条件。
    """
    class Payload(BaseModel):
        """
        提供事件条件使用的结构化业务字段。
        """

        status: str

    manager = object.__new__(WorkflowManager)
    workflow = SimpleNamespace(event_conditions={"status": "ready"})
    assert manager._check_event_conditions(workflow, SimpleNamespace(event_data=Payload(status="ready"))) is True
    assert manager._check_event_conditions(workflow, SimpleNamespace(event_data=["ready"])) is False


def test_workflow_missing_action_is_a_failed_attempt():
    """
    动作不存在时单次尝试返回失败，避免索引不存在的动作构造器。
    """
    manager = object.__new__(WorkflowManager)
    manager._actions = {}
    context = ActionContext()
    result = manager._execute_action_once(1, Action(id="missing", type=None), context, {}, {}, None)
    assert result.success is False
    assert result.message == "未找到动作"
    assert result.context is context


def test_download_action_without_hash_does_not_query_downloader(monkeypatch):
    """
    缺少下载标识的旧上下文不会扩大成下载器全量查询。
    """
    chain = Mock(side_effect=AssertionError("缺少标识不应查询下载器"))
    monkeypatch.setattr("app.workflow.actions.fetch_downloads.ActionChain", chain)
    monkeypatch.setattr("app.workflow.actions.fetch_downloads.runtime_stop_state.is_workflow_stopped", Mock(return_value=False))
    context = ActionContext(downloads=[DownloadTask(download_id=None)])
    FetchDownloadsAction("downloads").execute(1, {}, context)
    chain.assert_not_called()
    assert context.downloads[0].download_id is None


def test_rss_action_initializes_nullable_torrent_collection(monkeypatch):
    """
    可空的旧资源集合能够接收新 RSS 结果，未提供体积时按未知大小处理。
    """
    helper = SimpleNamespace(parse=Mock(return_value=[{"title": "Demo.2026", "enclosure": "https://site.test/torrent"}]))
    monkeypatch.setattr("app.workflow.actions.fetch_rss.RssHelper", Mock(return_value=helper))
    monkeypatch.setattr("app.workflow.actions.fetch_rss.runtime_stop_state.is_workflow_stopped", Mock(return_value=False))
    context = ActionContext(torrents=None)
    result = FetchRssAction("rss").execute(1, {"url": "https://site.test/rss", "match_media": False}, context)
    assert result is context
    assert len(context.torrents) == 1
    assert context.torrents[0].torrent_info.size == 0


def test_media_action_initializes_nullable_media_collection(monkeypatch):
    """
    旧上下文媒体列表为空时仍接收榜单结果，结构化推荐事件维持兼容。
    """
    chain = Mock()
    chain.tmdb_trending.return_value = [{"title": "Demo"}]
    monkeypatch.setattr("app.workflow.actions.fetch_medias.RecommendChain", Mock(return_value=chain))
    monkeypatch.setattr("app.workflow.actions.fetch_medias.eventmanager.send_event", Mock(return_value=SimpleNamespace(
        event_data=RecommendSourceEventData(extra_sources=[]),
    )))
    monkeypatch.setattr("app.workflow.actions.fetch_medias.runtime_stop_state.is_workflow_stopped", Mock(return_value=False))
    context = ActionContext(medias=None)
    action = FetchMediasAction("medias")
    assert action.execute(1, {"sources": ["recommend/tmdb_trending"]}, context) is context
    assert [media.title for media in context.medias] == ["Demo"]
    assert action.success is True
