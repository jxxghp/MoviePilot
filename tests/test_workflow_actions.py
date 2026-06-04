from types import SimpleNamespace

from app.schemas import ActionContext, DownloadTask, FileItem
from app.schemas.workflow import ActionResult
from app.workflow.actions import BaseAction
from app.workflow.actions import fetch_downloads as fetch_downloads_module
from app.workflow.actions import scrape_file as scrape_file_module
from app.workflow.actions.fetch_downloads import FetchDownloadsAction
from app.workflow.actions.scrape_file import ScrapeFileAction
from app.workflow.actions.fetch_rss import FetchRssAction
from app.workflow import WorkFlowManager


def test_fetch_downloads_updates_context_downloads(monkeypatch):
    """获取下载任务动作应更新上游上下文中的下载任务。"""
    calls = []

    class FakeActionChain:
        """模拟下载器查询链。"""

        def list_torrents(self, hashs=None, downloader=None, **kwargs):
            calls.append((hashs, downloader))
            return [SimpleNamespace(path="/downloads/movie.mkv", progress=100)]

    monkeypatch.setattr(fetch_downloads_module, "ActionChain", FakeActionChain)
    monkeypatch.setattr(fetch_downloads_module.global_vars, "is_workflow_stopped", lambda workflow_id: False)

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


def test_scrape_file_keeps_workflow_action_context(monkeypatch):
    """刮削文件动作不应将工作流上下文替换为媒体识别上下文。"""
    scraped = []

    class FakeStorageChain:
        """模拟存储链。"""

        def exists(self, fileitem):
            return True

    class FakeMediaChain:
        """模拟媒体识别和刮削链。"""

        def recognize_by_path(self, path, obtain_images=False):
            return SimpleNamespace(meta_info="meta", media_info="media")

        def scrape_metadata(self, fileitem, meta=None, mediainfo=None):
            scraped.append((fileitem.path, meta, mediainfo))

    monkeypatch.setattr(scrape_file_module, "StorageChain", FakeStorageChain)
    monkeypatch.setattr(scrape_file_module, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(scrape_file_module.global_vars, "is_workflow_stopped", lambda workflow_id: False)
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


def test_execute_with_inputs_maps_contract_inputs_outputs_and_runtime(monkeypatch):
    """新版动作桥接方法应按契约映射输入、输出和运行期信息。"""

    class ContractAction(BaseAction):
        """测试动作契约桥接。"""

        contract = {
            "inputs": [{"name": "torrents", "label": "资源", "kind": "list"}],
            "outputs": [{"name": "downloads", "label": "下载任务", "kind": "list"}],
        }

        @classmethod
        @property
        def name(cls) -> str:
            return "契约动作"

        @classmethod
        @property
        def description(cls) -> str:
            return "测试契约动作"

        @classmethod
        @property
        def data(cls) -> dict:
            return {}

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
    manager = object.__new__(WorkFlowManager)
    manager._actions = {"FetchRssAction": FetchRssAction}

    actions = manager.list_actions()

    assert actions[0]["contract"]["outputs"][0]["name"] == "torrents"
    assert actions[0]["contract"]["condition_fields"][0]["label"] == "资源"
