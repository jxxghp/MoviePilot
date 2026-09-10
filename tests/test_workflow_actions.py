from types import SimpleNamespace

from app.domain.context import Context, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.download import DownloadTask
from app.schemas.file import FileItem
from app.schemas.workflow import ActionContext, ActionResult
from app.workflow import WorkflowManager
from app.workflow.actions import BaseAction
from app.workflow.actions import fetch_downloads as fetch_downloads_module
from app.workflow.actions import fetch_torrents as fetch_torrents_module
from app.workflow.actions import scrape_file as scrape_file_module
from app.workflow.actions.fetch_downloads import FetchDownloadsAction
from app.workflow.actions.fetch_rss import FetchRssAction
from app.workflow.actions.fetch_torrents import FetchTorrentsAction
from app.workflow.actions.scrape_file import ScrapeFileAction


def test_fetch_downloads_updates_context_downloads(monkeypatch):
    """获取下载任务动作应更新上游上下文中的下载任务。"""
    calls = []

    class FakeActionChain:
        """模拟下载器查询链。"""

        def list_torrents(self, hashs=None, downloader=None, **kwargs):
            calls.append((hashs, downloader))
            return [SimpleNamespace(path="/downloads/movie.mkv", progress=100)]

    monkeypatch.setattr(fetch_downloads_module, "ActionChain", FakeActionChain)
    monkeypatch.setattr(fetch_downloads_module.runtime_stop_state, "is_workflow_stopped", lambda workflow_id: False)

    context = ActionContext(
        downloads=[
            DownloadTask(download_id="hash-1", downloader="qbittorrent"),
        ]
    )

    result = FetchDownloadsAction("fetch-downloads").execute(
        workflow_id=1,
        params={},
        context=context,
    )

    assert calls == [(["hash-1"], "qbittorrent")]
    assert result.downloads[0].completed is True
    assert result.downloads[0].path == "/downloads/movie.mkv"


def test_fetch_torrents_filters_special_season_zero(monkeypatch):
    """工作流显式选择季 0 时只能保留特别季资源。"""

    class FakeSearchChain:
        """返回特别季和第一季候选，验证动作层季过滤。"""

        def search_by_title(self, **_kwargs):
            return [
                Context(
                    meta_info=MetaInfo("Test S00"),
                    torrent_info=TorrentInfo(title="Test S00"),
                ),
                Context(
                    meta_info=MetaInfo("Test S01"),
                    torrent_info=TorrentInfo(title="Test S01"),
                ),
            ]

    monkeypatch.setattr(fetch_torrents_module, "SearchChain", FakeSearchChain)
    monkeypatch.setattr(fetch_torrents_module.runtime_stop_state, "is_workflow_stopped", lambda _workflow_id: False)

    action = FetchTorrentsAction("fetch-torrents")
    action.job_done = lambda *_args, **_kwargs: None
    result = action.execute(
        workflow_id=1,
        params={"search_type": "keyword", "name": "Test", "season": 0},
        context=ActionContext(),
    )

    assert [item.meta_info.begin_season for item in result.torrents] == [0]


def test_scrape_file_keeps_workflow_action_context(monkeypatch):
    """刮削文件动作不应将工作流上下文替换为媒体识别上下文。"""
    scraped = []

    class FakeStorageChain:
        """模拟存储链。"""

        def exists(self, fileitem):
            return True

    class FakeMediaChain:
        """模拟媒体识别链。"""

        def recognize_by_path(self, path, obtain_images=False):
            return SimpleNamespace(meta_info="meta", media_info="media")

    class FakeScrapingChain:
        """模拟独立刮削链。"""

        def scrape_metadata(self, fileitem, meta=None, mediainfo=None):
            scraped.append((fileitem.path, meta, mediainfo))

    monkeypatch.setattr(scrape_file_module, "StorageChain", FakeStorageChain)
    monkeypatch.setattr(scrape_file_module, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(scrape_file_module, "ScrapingChain", FakeScrapingChain)
    monkeypatch.setattr(scrape_file_module.runtime_stop_state, "is_workflow_stopped", lambda workflow_id: False)
    monkeypatch.setattr(ScrapeFileAction, "check_cache", lambda self, workflow_id, key: False)
    monkeypatch.setattr(ScrapeFileAction, "save_cache", lambda self, workflow_id, data: None)

    context = ActionContext(
        fileitems=[
            FileItem(path="/library/movie.mkv", storage="local", type="file"),
        ]
    )

    result = ScrapeFileAction("scrape-file").execute(
        workflow_id=1,
        params={},
        context=context,
    )

    assert result is context
    assert result.fileitems[0].path == "/library/movie.mkv"
    assert scraped == [("/library/movie.mkv", "meta", "media")]


def test_scrape_file_does_not_cache_failed_music_scrape(monkeypatch):
    """音乐刮削失败时工作流必须计为失败，不能写入成功缓存。"""
    saved_cache = []

    class FakeStorageChain:
        """模拟存在的音乐文件。"""

        def exists(self, fileitem):
            return True

    class FakeMediaChain:
        """模拟媒体识别成功。"""

        def recognize_by_path(self, path, obtain_images=False):
            return SimpleNamespace(meta_info="music-meta", media_info="music")

    class FakeScrapingChain:
        """模拟音乐产物写入失败。"""

        def scrape_metadata(self, fileitem, meta=None, mediainfo=None):
            return False, "歌词保存失败"

    monkeypatch.setattr(scrape_file_module, "StorageChain", FakeStorageChain)
    monkeypatch.setattr(scrape_file_module, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(scrape_file_module, "ScrapingChain", FakeScrapingChain)
    monkeypatch.setattr(
        scrape_file_module.runtime_stop_state,
        "is_workflow_stopped",
        lambda workflow_id: False,
    )
    monkeypatch.setattr(
        ScrapeFileAction,
        "check_cache",
        lambda self, workflow_id, key: False,
    )
    monkeypatch.setattr(
        ScrapeFileAction,
        "save_cache",
        lambda self, workflow_id, data: saved_cache.append(data),
    )

    action = ScrapeFileAction("scrape-music")
    action.execute(
        workflow_id=1,
        params={},
        context=ActionContext(
            fileitems=[
                FileItem(path="/library/晴天.flac", storage="local", type="file")
            ]
        ),
    )

    assert action.success is False
    assert action._scraped_files == []
    assert saved_cache == []


def test_execute_with_inputs_maps_contract_inputs_outputs_and_runtime(monkeypatch):
    """新版动作桥接方法应按契约映射输入、输出和运行期信息。"""

    class ContractAction(BaseAction):
        """测试动作契约桥接。"""

        contract = {
            "inputs": [{"name": "torrents", "label": "资源", "kind": "list"}],
            "outputs": [{"name": "downloads", "label": "下载任务", "kind": "list"}],
        }

        name = "契约动作"
        description = "测试契约动作"
        data = {}

        @property
        def success(self) -> bool:
            return True

        def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
            """执行测试动作。"""
            _ = workflow_id, params
            context.downloads = [
                DownloadTask(download_id=f"{item}-hash", downloader="qbittorrent")
                for item in context.torrents
            ]
            self.job_done("完成")
            return context

    result = ContractAction("contract").execute_with_inputs(
        workflow_id=1,
        params={},
        inputs={"torrents": ["movie"]},
        runtime={"attempt": 1, "max_attempts": 1, "cancel_token": object()},
        context=ActionContext(),
    )

    assert isinstance(result, ActionResult)
    assert result.outputs["downloads"][0].download_id == "movie-hash"
    assert result.context.runtime_state["current_action_runtime"] == {
        "attempt": 1,
        "max_attempts": 1,
    }

    path_result = ContractAction("contract").execute_with_inputs(
        workflow_id=1,
        params={},
        inputs={"outputs.FetchRssAction.torrents": ["legacy"]},
        runtime={},
        context=ActionContext(),
    )

    assert path_result.outputs["downloads"][0].download_id == "legacy-hash"


def test_workflow_manager_list_actions_exposes_contract():
    """动作列表应返回固定输入输出契约。"""
    manager = object.__new__(WorkflowManager)
    manager._actions = {"FetchRssAction": FetchRssAction}

    actions = manager.list_actions()

    assert actions[0]["name"] == "获取RSS资源"
    assert actions[0]["description"] == "订阅RSS地址获取资源"
    assert isinstance(actions[0]["data"], dict)
    assert actions[0]["contract"]["outputs"][0]["name"] == "torrents"
    assert actions[0]["contract"]["condition_fields"][0]["label"] == "资源"


def test_add_download_only_lack_handles_schema_metainfo(monkeypatch):
    """添加下载动作开启仅下载缺失资源时应支持 Schema MetaInfo 的 season_list 属性并正确过滤已存在剧集。"""
    import app.workflow.actions.add_download as add_download_module
    from app.schemas.context import (
        Context as SchemaContext,
    )
    from app.schemas.context import (
        MediaInfo as SchemaMediaInfo,
    )
    from app.schemas.context import (
        MetaInfo as SchemaMetaInfo,
    )
    from app.schemas.context import (
        TorrentInfo as SchemaTorrentInfo,
    )
    from app.schemas.types import MediaType
    from app.workflow.actions.add_download import AddDownloadAction

    downloaded = []

    class FakeDownloadChain:
        def media_exists(self, mediainfo):
            # 模拟媒体库已存在第 1 季的第 1、2 集
            return SimpleNamespace(seasons={1: [1, 2]})

        def download_single(self, context=None, **kwargs):
            downloaded.append(context.torrent_info.title)
            return f"hash-{context.torrent_info.title}"

    monkeypatch.setattr(add_download_module, "DownloadChain", FakeDownloadChain)
    monkeypatch.setattr(add_download_module.runtime_stop_state, "is_workflow_stopped", lambda _wid: False)

    action = AddDownloadAction("test-add-download")
    action.check_cache = lambda _wid, _key: False
    action.save_cache = lambda _wid, _key: None
    action.job_done = lambda *_args, **_kwargs: None

    # 1. 第 1 季第 1 集（已存在，跳过）
    t1 = SchemaContext(
        meta_info=SchemaMetaInfo(title="Show S01E01", type="电视剧", begin_season=1, episode_list=[1]),
        media_info=SchemaMediaInfo(title="Show", type=MediaType.TV),
        torrent_info=SchemaTorrentInfo(title="Show S01E01", site=1),
    )
    # 2. 第 1 季第 3 集（缺失，应下载）
    t2 = SchemaContext(
        meta_info=SchemaMetaInfo(title="Show S01E03", type="电视剧", begin_season=1, episode_list=[3]),
        media_info=SchemaMediaInfo(title="Show", type=MediaType.TV),
        torrent_info=SchemaTorrentInfo(title="Show S01E03", site=1),
    )
    # 3. 多季资源（有多季，跳过）
    t3 = SchemaContext(
        meta_info=SchemaMetaInfo(title="Show S01-S02", type="电视剧", begin_season=1, end_season=2, episode_list=[1]),
        media_info=SchemaMediaInfo(title="Show", type=MediaType.TV),
        torrent_info=SchemaTorrentInfo(title="Show S01-S02", site=1),
    )
    # 4. 未显式标季但为电视剧（begin_season=None，默认归入第 1 季；第 1 集已存在，跳过）
    t4 = SchemaContext(
        meta_info=SchemaMetaInfo(title="Show Ep01", type="电视剧", begin_season=None, episode_list=[1]),
        media_info=SchemaMediaInfo(title="Show", type=MediaType.TV),
        torrent_info=SchemaTorrentInfo(title="Show Ep01", site=1),
    )
    # 5. 未显式标季但为电视剧（begin_season=None，默认归入第 1 季；第 4 集缺失，应下载）
    t5 = SchemaContext(
        meta_info=SchemaMetaInfo(title="Show Ep04", type="电视剧", begin_season=None, episode_list=[4]),
        media_info=SchemaMediaInfo(title="Show", type=MediaType.TV),
        torrent_info=SchemaTorrentInfo(title="Show Ep04", site=1),
    )

    context = ActionContext(torrents=[t1, t2, t3, t4, t5])
    result = action.execute(workflow_id=1, params={"only_lack": True}, context=context)

    assert downloaded == ["Show S01E03", "Show Ep04"]
    assert len(result.downloads) == 2
    assert [d.download_id for d in result.downloads] == ["hash-Show S01E03", "hash-Show Ep04"]
