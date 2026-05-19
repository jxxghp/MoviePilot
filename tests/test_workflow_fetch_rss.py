import unittest
from datetime import datetime
from unittest.mock import patch

from app.core.context import TorrentInfo
from app.schemas import ActionContext
from app.workflow.actions.fetch_rss import FetchRssAction


class FetchRssActionTest(unittest.TestCase):
    """
    RSS 工作流动作测试。
    """

    def test_execute_builds_core_torrent_info_for_downstream_workflow(self):
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

        with patch("app.workflow.actions.fetch_rss.RssHelper") as rss_helper, \
                patch("app.workflow.actions.fetch_rss.global_vars.is_workflow_stopped", return_value=False):
            rss_helper.return_value.parse.return_value = rss_items

            context = FetchRssAction("fetch-rss").execute(
                workflow_id=1,
                params={"url": "https://example.com/rss.xml"},
                context=ActionContext(),
            )

        torrent_info = context.torrents[0].torrent_info
        self.assertIsInstance(torrent_info, TorrentInfo)
        self.assertIsNone(torrent_info.category)
        self.assertTrue(callable(getattr(torrent_info, "to_dict", None)))
        self.assertEqual("2026-05-19 08:30:00", torrent_info.pubdate)


if __name__ == "__main__":
    unittest.main()
