"""
验证 RSS 工作流动作的输入解析与结果输出。
"""

from datetime import datetime
from unittest.mock import patch

from app.domain.context import TorrentInfo
from app.schemas.workflow import ActionContext
from app.workflow.actions.fetch_rss import FetchRssAction


def test_execute_builds_core_torrent_info_for_downstream_workflow() -> None:
    """
    工作流产出的种子信息应使用 core TorrentInfo，避免下载事件下游收到缺少运行时接口的 schema 对象。
    """
    rss_items = [
        {
            "title": "Example RSS Torrent",
            "enclosure": "https://example.com/example.torrent",
            "link": "https://example.com/details",
            "size": 1024,
            "pubdate": datetime(2026, 5, 19, 8, 30, 0),
        }
    ]

    with (
        patch("app.workflow.actions.fetch_rss.RssHelper") as rss_helper,
        patch("app.workflow.actions.fetch_rss.runtime_stop_state.is_workflow_stopped", return_value=False),
    ):
        rss_helper.return_value.parse.return_value = rss_items

        context = FetchRssAction("fetch-rss").execute(
            workflow_id=1,
            params={"url": "https://example.com/rss.xml"},
            context=ActionContext(),
        )

    torrent_info = context.torrents[0].torrent_info
    assert isinstance(torrent_info, TorrentInfo)
    assert torrent_info.category is None
    assert callable(getattr(torrent_info, "to_dict", None))
    assert "2026-05-19 08:30:00" == torrent_info.pubdate
