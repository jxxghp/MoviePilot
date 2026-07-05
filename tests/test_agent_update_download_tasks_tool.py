import asyncio
import json
from unittest.mock import MagicMock, patch

from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.update_download_tasks import UpdateDownloadTasksTool
from app.schemas import DownloaderTorrent, TransferDirectoryConf


def _download_dirs():
    """
    构造允许下载任务修改保存目录的测试下载目录配置。
    """
    return [
        TransferDirectoryConf(
            name="本地下载",
            priority=1,
            storage="local",
            download_path="/downloads",
        ),
    ]


def test_update_download_tasks_resolves_downloader_and_updates_all_supported_fields(monkeypatch):
    """
    未显式传下载器时，应先按 Hash 解析任务所属下载器，再一次性执行多项修改。
    """
    monkeypatch.setattr(
        "app.helper.directory.DirectoryHelper.get_download_dirs",
        lambda _self: _download_dirs(),
    )
    hash_value = "a" * 40
    download_chain = MagicMock()
    download_chain.list_torrents.return_value = [
        DownloaderTorrent(downloader="qb", hash=hash_value, title="Demo")
    ]
    download_chain.set_torrents_tag.return_value = True
    download_chain.set_downloading.return_value = True
    download_chain.update_torrent.return_value = {
        "limits": True,
        "trackers": True,
        "save_path": True,
        "category": True,
    }

    with patch(
        "app.agent.tools.impl.update_download_tasks.DownloadChain",
        return_value=download_chain,
    ):
        result = UpdateDownloadTasksTool._update_download_sync(
            hash_value=hash_value,
            action="stop",
            tags=["movie", "hd"],
            download_limit=1024,
            upload_limit=512,
            trackers=["https://tracker.example/announce"],
            save_path="/downloads/new",
            category="电影",
            ratio_limit=2.5,
            seeding_time_limit=1440,
        )

    assert result["downloader"] == "qb"
    assert {item["operation"] for item in result["results"]} == {
        "tags",
        "stop",
        "limits",
        "trackers",
        "save_path",
        "category",
    }
    assert all(item["success"] for item in result["results"])
    download_chain.list_torrents.assert_called_once_with(
        hashs=[hash_value],
        include_all_tags=True,
    )
    download_chain.set_torrents_tag.assert_called_once_with(
        hashs=[hash_value],
        tags=["movie", "hd"],
        downloader="qb",
    )
    download_chain.set_downloading.assert_called_once_with(
        hash_str=hash_value,
        oper="stop",
        name="qb",
    )
    download_chain.update_torrent.assert_called_once_with(
        hash_string=hash_value,
        downloader="qb",
        download_limit=1024,
        upload_limit=512,
        tracker_list=["https://tracker.example/announce"],
        save_path="/downloads/new",
        category="电影",
        ratio_limit=2.5,
        seeding_time_limit=1440,
    )


def test_update_download_tasks_skips_property_update_when_only_action_is_requested():
    """
    仅开始或暂停任务时，不应额外调用属性修改接口。
    """
    hash_value = "e" * 40
    download_chain = MagicMock()
    download_chain.list_torrents.return_value = [
        DownloaderTorrent(downloader="tr", hash=hash_value, title="Demo")
    ]
    download_chain.set_downloading.return_value = True

    with patch(
        "app.agent.tools.impl.update_download_tasks.DownloadChain",
        return_value=download_chain,
    ):
        result = UpdateDownloadTasksTool._update_download_sync(
            hash_value=hash_value,
            action="start",
        )

    assert result["results"] == [
        {"operation": "start", "success": True, "message": "成功开始下载任务"}
    ]
    download_chain.update_torrent.assert_not_called()


def test_update_download_tasks_reports_missing_task_when_downloader_cannot_be_resolved():
    """
    找不到任务时，应返回明确的解析失败结果。
    """
    hash_value = "b" * 40
    download_chain = MagicMock()
    download_chain.list_torrents.return_value = []

    with patch(
        "app.agent.tools.impl.update_download_tasks.DownloadChain",
        return_value=download_chain,
    ):
        result = UpdateDownloadTasksTool._update_download_sync(
            hash_value=hash_value,
            action="start",
        )

    assert result["results"] == [
        {
            "operation": "resolve_downloader",
            "success": False,
            "message": "未找到下载任务或下载器不可用",
        }
    ]
    download_chain.set_downloading.assert_not_called()
    download_chain.update_torrent.assert_not_called()


def test_update_download_tasks_run_rejects_empty_update():
    """
    没有任何修改字段时，应拒绝调用下载器。
    """
    result = asyncio.run(
        UpdateDownloadTasksTool(session_id="session-1", user_id="10001").run(
            hash="c" * 40
        )
    )

    assert "至少需要指定一个要更新的字段" in result


def test_update_download_tasks_run_outputs_structured_result():
    """
    工具运行结果应是结构化 JSON，方便 Agent 判断每项修改是否成功。
    """
    with patch.object(
        UpdateDownloadTasksTool,
        "_update_download_sync",
        return_value={
            "hash": "d" * 40,
            "downloader": "tr",
            "results": [
                {"operation": "limits", "success": True, "message": "限速/做种策略修改成功"}
            ],
        },
    ):
        result = asyncio.run(
            UpdateDownloadTasksTool(session_id="session-1", user_id="10001").run(
                hash="d" * 40,
                download_limit=100,
            )
        )

    payload = json.loads(result)
    assert payload["downloader"] == "tr"
    assert payload["results"][0]["operation"] == "limits"


def test_factory_registers_update_download_tasks_without_old_modify_name():
    """
    工具工厂应只暴露统一后的下载任务更新工具名。
    """
    with patch(
        "app.agent.tools.factory.PluginManager.get_plugin_agent_tools",
        return_value=[],
    ):
        tools = MoviePilotToolFactory.create_tools(
            session_id="download-task-session",
            user_id="10001",
        )

    tool_names = {tool.name for tool in tools}
    assert "query_download_tasks" in tool_names
    assert "update_download_tasks" in tool_names
    assert "modify_download" not in tool_names
