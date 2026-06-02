import asyncio
import importlib.machinery
import sys
import unittest
from types import SimpleNamespace
from types import ModuleType
from unittest.mock import AsyncMock, patch


def _stub_module(name: str, **attrs):
    module = sys.modules.get(name)
    if module is None:
        module = ModuleType(name)
        sys.modules[name] = module
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


_stub_module("qbittorrentapi", TorrentFilesList=list)
_stub_module("transmission_rpc", File=object)
_stub_module(
    "psutil",
    __spec__=importlib.machinery.ModuleSpec("psutil", loader=None),
)

from app.agent.tools.factory import MoviePilotToolFactory
from app.agent import ReplyMode
from app.chain.search import SearchChain
from app.core.config import settings
from app.modules.indexer import IndexerModule
from app.schemas.types import MediaType


def _make_result(title: str, size: int, seeders: int):
    return SimpleNamespace(
        torrent_info=SimpleNamespace(title=title, size=size, seeders=seeders)
    )


class SearchChainAIRecommendTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        SearchChain._ai_recommend_running = False
        SearchChain._ai_recommend_task = None
        SearchChain._current_recommend_request_hash = None
        SearchChain._ai_recommend_result = None
        SearchChain._ai_recommend_error = None

    async def asyncTearDown(self):
        task = SearchChain._ai_recommend_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        SearchChain._ai_recommend_running = False
        SearchChain._ai_recommend_task = None
        SearchChain._current_recommend_request_hash = None
        SearchChain._ai_recommend_result = None
        SearchChain._ai_recommend_error = None

    @staticmethod
    def _make_chain() -> SearchChain:
        chain = object.__new__(SearchChain)
        chain.load_cache = lambda _filename: None
        chain.save_cache = lambda _cache, _filename: None
        chain.remove_cache = lambda _filename: None
        chain.get_search_page_size = IndexerModule.get_search_page_size
        return chain

    async def test_start_recommend_task_restores_original_indices(self):
        chain = self._make_chain()
        saved = []
        chain.save_cache = lambda cache, filename: saved.append((filename, cache))
        results = [_make_result(f"item-{index}", 1024 * (index + 1), index) for index in range(7)]

        with (
            patch.object(settings, "AI_AGENT_ENABLE", True, create=True),
            patch.object(settings, "AI_RECOMMEND_ENABLED", True, create=True),
            patch.object(settings, "AI_RECOMMEND_MAX_ITEMS", 50, create=True),
            patch.object(
                settings,
                "AI_RECOMMEND_USER_PREFERENCE",
                "Prefer high seeders",
                create=True,
            ),
            patch.object(
                SearchChain,
                "_invoke_recommend_llm",
                new=AsyncMock(return_value='[1, 0, 1, "bad", 9]'),
            ),
        ):
            chain.start_recommend_task(
                filtered_indices=[2, 4, 6],
                search_results_count=len(results),
                results=results,
            )
            self.assertIsNotNone(SearchChain._ai_recommend_task)
            await SearchChain._ai_recommend_task

        self.assertEqual([4, 2], SearchChain._ai_recommend_result)
        self.assertEqual(
            [("__ai_recommend_indices__", [4, 2])],
            saved,
        )
        self.assertFalse(SearchChain._ai_recommend_running)
        self.assertIsNone(SearchChain._ai_recommend_task)

    async def test_invoke_recommend_llm_disables_output_message_persistence(self):
        chain = self._make_chain()
        from app.agent import agent_manager
        from app.agent.prompt import prompt_manager

        captured = {}

        async def _fake_run_background_prompt(**kwargs):
            captured.update(kwargs)
            kwargs["output_callback"]("[0, 2]")

        with (
            patch.object(
                prompt_manager,
                "render_system_task_message",
                return_value="PROMPT",
            ),
            patch.object(
                agent_manager,
                "run_background_prompt",
                new=AsyncMock(side_effect=_fake_run_background_prompt),
            ),
        ):
            result = await chain._invoke_recommend_llm("Candidates")

        self.assertEqual("[0, 2]", result)
        self.assertEqual(ReplyMode.CAPTURE_ONLY, captured["reply_mode"])
        self.assertFalse(captured["persist_output_message"])
        self.assertFalse(captured["allow_message_tools"])

    def test_search_by_title_clears_previous_recommend_state_when_caching(self):
        chain = self._make_chain()
        removed = []
        cached = []
        chain.remove_cache = lambda filename: removed.append(filename)
        chain.save_cache = lambda cache, filename: cached.append((filename, cache))
        chain._SearchChain__search_all_sites = lambda keyword, sites, page: [
            SimpleNamespace(title="Test Title", description="Test Desc")
        ]

        SearchChain._current_recommend_request_hash = "stale-hash"
        SearchChain._ai_recommend_result = [3, 1]
        SearchChain._ai_recommend_error = "stale-error"

        results = chain.search_by_title("keyword", cache_local=True)

        self.assertEqual(1, len(results))
        self.assertEqual(["__ai_recommend_indices__"], removed)
        self.assertTrue(any(filename == "__search_result__" for filename, _ in cached))
        self.assertTrue(any(filename == "__search_params__" for filename, _ in cached))
        self.assertIsNone(SearchChain._current_recommend_request_hash)
        self.assertIsNone(SearchChain._ai_recommend_result)
        self.assertIsNone(SearchChain._ai_recommend_error)

    def test_build_search_pages_uses_search_resource_pages_setting(self):
        with patch.object(settings, "SEARCH_RESOURCE_PAGES", 3, create=True):
            self.assertEqual([2, 3, 4], SearchChain._build_search_pages(page=2))

    def test_build_search_pages_falls_back_to_one_page_for_invalid_setting(self):
        with patch.object(settings, "SEARCH_RESOURCE_PAGES", 0, create=True):
            self.assertEqual([0], SearchChain._build_search_pages(page=0))
        with patch.object(settings, "SEARCH_RESOURCE_PAGES", "bad", create=True):
            self.assertEqual([0], SearchChain._build_search_pages(page="bad"))

    def test_search_all_sites_stops_after_short_page(self):
        """
        验证普通站点默认按 100 条判断是否继续翻页。
        """
        chain = self._make_chain()
        requested_pages = []

        def search_torrents(**kwargs):
            """
            模拟前两页满页、第三页不足 100 条，验证不会继续请求第四页。
            """
            page = kwargs["page"]
            requested_pages.append(page)
            count = 100 if page in (0, 1) else 1
            return [
                SimpleNamespace(title=f"Result Page {page}-{index}", description="")
                for index in range(count)
            ]

        chain.search_torrents = search_torrents

        with (
            patch.object(settings, "SEARCH_RESOURCE_PAGES", 4, create=True),
            patch("app.chain.search.SystemConfigOper") as system_config_oper,
            patch("app.chain.search.SitesHelper") as sites_helper,
            patch("app.chain.search.ProgressHelper") as progress_helper,
        ):
            system_config_oper.return_value.get.return_value = [1]
            sites_helper.return_value.get_indexers.return_value = [
                {"id": 1, "name": "测试站点"}
            ]
            progress_helper.return_value = SimpleNamespace(
                start=lambda: None,
                update=lambda **_kwargs: None,
                end=lambda: None,
            )

            results = chain._SearchChain__search_all_sites(
                keyword="keyword",
                sites=None,
                page=0,
            )

        self.assertEqual([0, 1, 2], sorted(requested_pages))
        self.assertEqual(201, len(results))

    def test_search_all_sites_uses_configured_result_num_for_common_site(self):
        """
        验证普通配置站点按 result_num 判断是否继续翻页。
        """
        chain = self._make_chain()
        requested_pages = []

        def search_torrents(**kwargs):
            """
            模拟配置站点每页 50 条，第二页不足 50 条后停止。
            """
            page = kwargs["page"]
            requested_pages.append(page)
            count = 50 if page == 0 else 49
            return [
                SimpleNamespace(title=f"Result Page {page}-{index}", description="")
                for index in range(count)
            ]

        chain.search_torrents = search_torrents

        with (
            patch.object(settings, "SEARCH_RESOURCE_PAGES", 3, create=True),
            patch("app.chain.search.SystemConfigOper") as system_config_oper,
            patch("app.chain.search.SitesHelper") as sites_helper,
            patch("app.chain.search.ProgressHelper") as progress_helper,
        ):
            system_config_oper.return_value.get.return_value = [1]
            sites_helper.return_value.get_indexers.return_value = [
                {"id": 1, "name": "测试站点", "result_num": 50}
            ]
            progress_helper.return_value = SimpleNamespace(
                start=lambda: None,
                update=lambda **_kwargs: None,
                end=lambda: None,
            )

            results = chain._SearchChain__search_all_sites(
                keyword="keyword",
                sites=None,
                page=0,
            )

        self.assertEqual([0, 1], requested_pages)
        self.assertEqual(99, len(results))

    def test_search_all_sites_uses_parser_page_size_for_yema(self):
        """
        验证专用解析器按自身页容量判断，避免 Yema 的 40 条分页被误停。
        """
        chain = self._make_chain()
        requested_pages = []

        def search_torrents(**kwargs):
            """
            模拟 Yema 第一页满 40 条，第二页不足 40 条后停止。
            """
            page = kwargs["page"]
            requested_pages.append(page)
            count = 40 if page == 0 else 39
            return [
                SimpleNamespace(title=f"Result Page {page}-{index}", description="")
                for index in range(count)
            ]

        chain.search_torrents = search_torrents

        with (
            patch.object(settings, "SEARCH_RESOURCE_PAGES", 3, create=True),
            patch("app.chain.search.SystemConfigOper") as system_config_oper,
            patch("app.chain.search.SitesHelper") as sites_helper,
            patch("app.chain.search.ProgressHelper") as progress_helper,
        ):
            system_config_oper.return_value.get.return_value = [1]
            sites_helper.return_value.get_indexers.return_value = [
                {"id": 1, "name": "测试站点", "parser": "Yema"}
            ]
            progress_helper.return_value = SimpleNamespace(
                start=lambda: None,
                update=lambda **_kwargs: None,
                end=lambda: None,
            )

            results = chain._SearchChain__search_all_sites(
                keyword="keyword",
                sites=None,
                page=0,
            )

        self.assertEqual([0, 1], requested_pages)
        self.assertEqual(79, len(results))

    def test_indexer_module_search_page_size_uses_spider_metadata(self):
        """
        验证站点单页容量由索引器模块统一读取，避免搜索链写死 parser 容量。
        """
        self.assertEqual(
            40,
            IndexerModule.get_search_page_size({"parser": "Yema"}, keyword="keyword")
        )
        self.assertEqual(
            50,
            IndexerModule.get_search_page_size({"result_num": 50}, keyword="keyword")
        )
        self.assertIsNone(
            IndexerModule.get_search_page_size({"parser": "Haidan"}, keyword="keyword")
        )
        self.assertIsNone(
            IndexerModule.get_search_page_size({"parser": "TorrentLeech"}, keyword="keyword")
        )

    async def test_async_search_all_sites_stops_after_empty_page(self):
        """
        验证异步搜索遇到空页后停止后续翻页。
        """
        chain = self._make_chain()
        requested_pages = []

        async def async_search_torrents(**kwargs):
            """
            模拟第二页为空，验证异步搜索不会继续请求后续页。
            """
            page = kwargs["page"]
            requested_pages.append(page)
            count = 100 if page == 0 else 0
            return [
                SimpleNamespace(title=f"Result Page {page}-{index}", description="")
                for index in range(count)
            ]

        chain.async_search_torrents = async_search_torrents

        with (
            patch.object(settings, "SEARCH_RESOURCE_PAGES", 4, create=True),
            patch("app.chain.search.SystemConfigOper") as system_config_oper,
            patch("app.chain.search.SitesHelper") as sites_helper,
            patch("app.chain.search.ProgressHelper") as progress_helper,
        ):
            system_config_oper.return_value.get.return_value = [1]
            sites_helper.return_value.async_get_indexers = AsyncMock(
                return_value=[{"id": 1, "name": "测试站点"}]
            )
            progress_helper.return_value = SimpleNamespace(
                start=lambda: None,
                update=lambda **_kwargs: None,
                end=lambda: None,
            )

            results = await chain._SearchChain__async_search_all_sites(
                keyword="keyword",
                sites=None,
                page=0,
            )

        self.assertEqual([0, 1], requested_pages)
        self.assertEqual(100, len(results))

    async def test_async_search_all_sites_stream_stops_after_short_page(self):
        """
        验证渐进式搜索遇到非满页后停止后续翻页。
        """
        chain = self._make_chain()
        requested_pages = []

        async def async_search_torrents(**kwargs):
            """
            模拟渐进式搜索第二页不足 100 条，验证不会继续请求第三页。
            """
            page = kwargs["page"]
            requested_pages.append(page)
            count = 100 if page == 0 else 99
            return [
                SimpleNamespace(title=f"Result Page {page}-{index}", description="")
                for index in range(count)
            ]

        chain.async_search_torrents = async_search_torrents

        with (
            patch.object(settings, "SEARCH_RESOURCE_PAGES", 3, create=True),
            patch("app.chain.search.SystemConfigOper") as system_config_oper,
            patch("app.chain.search.SitesHelper") as sites_helper,
            patch("app.chain.search.ProgressHelper") as progress_helper,
        ):
            system_config_oper.return_value.get.return_value = [1]
            sites_helper.return_value.async_get_indexers = AsyncMock(
                return_value=[{"id": 1, "name": "测试站点"}]
            )
            progress_helper.return_value = SimpleNamespace(
                start=lambda: None,
                update=lambda **_kwargs: None,
                end=lambda: None,
            )

            events = [
                event
                async for event in chain._SearchChain__async_search_all_sites_stream(
                    keyword="keyword",
                    sites=None,
                    page=0,
                )
            ]

        append_events = [event for event in events if event.get("type") == "append"]
        self.assertEqual([0, 1], requested_pages)
        self.assertEqual([0, 1], [event["page"] for event in append_events])
        self.assertEqual(199, append_events[-1]["total_items"])

    def test_search_by_id_caches_replayable_search_params_when_caching(self):
        chain = self._make_chain()
        cached = []
        chain.save_cache = lambda cache, filename: cached.append((filename, cache))
        chain.recognize_media = lambda **_kwargs: SimpleNamespace(title="Test")
        chain.process = lambda **_kwargs: [SimpleNamespace(title="Result")]

        chain.search_by_id(
            tmdbid=123,
            mtype=MediaType.MOVIE,
            area="title",
            season=2,
            sites=[1, 3],
            cache_local=True,
        )

        self.assertIn(
            (
                "__search_params__",
                {
                    "keyword": "tmdb:123",
                    "type": "电影",
                    "area": "title",
                    "title": "",
                    "year": "",
                    "season": "2",
                    "sites": "1,3",
                },
            ),
            cached,
        )
        self.assertTrue(any(filename == "__search_result__" for filename, _ in cached))

    def test_tool_factory_excludes_message_tools_when_disabled(self):
        with patch(
            "app.agent.tools.factory.PluginManager.get_plugin_agent_tools",
            return_value=[],
        ):
            tools = MoviePilotToolFactory.create_tools(
                session_id="test-session",
                user_id="test-user",
                allow_message_tools=False,
            )

        tool_names = {tool.name for tool in tools}
        self.assertNotIn("send_message", tool_names)
        self.assertNotIn("ask_user_choice", tool_names)
        self.assertNotIn("send_local_file", tool_names)
        self.assertNotIn("send_voice_message", tool_names)
