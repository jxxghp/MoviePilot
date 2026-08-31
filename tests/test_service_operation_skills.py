"""下载器与媒体服务器第三方 API Skill 测试。"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, relative_path: str) -> ModuleType:
    """按文件路径加载带连字符 Skill 中的脚本模块。"""
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def downloader_module() -> ModuleType:
    """加载下载器 Skill 脚本。"""
    return _load_script(
        "moviepilot_downloader_skill",
        "skills/downloader-operation/scripts/mp-downloader.py",
    )


@pytest.fixture
def mediaserver_module() -> ModuleType:
    """加载媒体服务器 Skill 脚本。"""
    return _load_script(
        "moviepilot_mediaserver_skill",
        "skills/mediaserver-operation/scripts/mp-mediaserver.py",
    )


def _downloader_config(provider: str = "qbittorrent") -> SimpleNamespace:
    """构造包含私有配置但仅供脚本内部消费的下载器配置。"""
    return SimpleNamespace(
        name="main",
        type=provider,
        enabled=True,
        default=True,
        path_mapping=[("/media", "/downloads")],
        config={
            "host": "https://private.invalid",
            "username": "admin",
            "password": "secret",
        },
    )


def _mediaserver_config(provider: str = "emby") -> SimpleNamespace:
    """构造包含私有配置但仅供脚本内部消费的媒体服务器配置。"""
    return SimpleNamespace(
        name="living-room",
        type=provider,
        enabled=True,
        sync_libraries=["movies"],
        config={"host": "https://private.invalid", "apikey": "secret"},
    )


def test_downloader_load_configs_bootstraps_runtime_service_reader(
    downloader_module: ModuleType,
    monkeypatch,
) -> None:
    """独立脚本必须用自有短会话加载快照，再读取下载器服务配置。"""
    import app.db.oper.systemconfig as systemconfig_module
    import app.db.session as session_module
    import app.runtime.extensions.service as service_module

    system_config = MagicMock()
    session = MagicMock()
    session_factory = MagicMock()
    session_factory.return_value.__enter__.return_value = session
    configured_configs = [_downloader_config()]
    helper = MagicMock()
    helper.get_downloader_configs.return_value = configured_configs
    configure_reader = MagicMock()
    monkeypatch.setattr(systemconfig_module, "SystemConfigOper", MagicMock(return_value=system_config))
    monkeypatch.setattr(session_module, "SessionFactory", session_factory)
    monkeypatch.setattr(service_module, "ServiceConfigHelper", helper)
    monkeypatch.setattr(service_module, "configure_service_config_reader", configure_reader)

    assert downloader_module._load_configs() == configured_configs

    session_factory.assert_called_once_with()
    system_config.load_snapshot.assert_called_once_with(session)
    configure_reader.assert_called_once_with(system_config.get)
    helper.get_downloader_configs.assert_called_once_with()


def test_mediaserver_load_configs_bootstraps_runtime_service_reader(
    mediaserver_module: ModuleType,
    monkeypatch,
) -> None:
    """独立脚本必须用自有短会话加载快照，再读取媒体服务器服务配置。"""
    import app.db.oper.systemconfig as systemconfig_module
    import app.db.session as session_module
    import app.runtime.extensions.service as service_module

    system_config = MagicMock()
    session = MagicMock()
    session_factory = MagicMock()
    session_factory.return_value.__enter__.return_value = session
    configured_configs = [_mediaserver_config()]
    helper = MagicMock()
    helper.get_mediaserver_configs.return_value = configured_configs
    configure_reader = MagicMock()
    monkeypatch.setattr(systemconfig_module, "SystemConfigOper", MagicMock(return_value=system_config))
    monkeypatch.setattr(session_module, "SessionFactory", session_factory)
    monkeypatch.setattr(service_module, "ServiceConfigHelper", helper)
    monkeypatch.setattr(service_module, "configure_service_config_reader", configure_reader)

    assert mediaserver_module._load_configs() == configured_configs

    session_factory.assert_called_once_with()
    system_config.load_snapshot.assert_called_once_with(session)
    configure_reader.assert_called_once_with(system_config.get)
    helper.get_mediaserver_configs.assert_called_once_with()


@pytest.mark.parametrize(
    ("relative_path", "instances_key"),
    [
        ("skills/downloader-operation/scripts/mp-downloader.py", "instances"),
        ("skills/mediaserver-operation/scripts/mp-mediaserver.py", "instances"),
    ],
)
def test_service_operation_script_loads_configs_without_lifespan(
    relative_path: str,
    instances_key: str,
) -> None:
    """独立 CLI 子进程未启动 lifespan 时也应能从隔离数据库读取配置。"""
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / relative_path), "instances"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout or result.stderr
    # SQLite 首次建引擎会先输出 journal mode 诊断，取随后固定 JSON envelope。
    payload = json.loads(result.stdout[result.stdout.index("{") :])
    assert payload["success"] is True
    assert payload[instances_key] == []


def test_downloader_instances_and_capabilities_do_not_expose_credentials(
    downloader_module: ModuleType,
    monkeypatch,
) -> None:
    """实例发现与能力发现不得输出连接参数或凭据。"""
    monkeypatch.setattr(
        downloader_module,
        "_load_configs",
        lambda: [_downloader_config()],
    )

    instances = downloader_module.list_instances()
    capabilities = downloader_module.list_capabilities("main")

    assert instances["instances"] == [
        {
            "name": "main",
            "provider": "qbittorrent",
            "enabled": True,
            "default": True,
            "path_mapping_count": 1,
        }
    ]
    rendered = str((instances, capabilities))
    assert "private.invalid" not in rendered
    assert "secret" not in rendered
    assert any(item["action"] == "tasks.peers" for item in capabilities["actions"])


def test_downloader_task_list_is_paged_and_normalized(
    downloader_module: ModuleType,
    monkeypatch,
) -> None:
    """任务查询应使用统一分页，并清理结果中的凭据字段。"""
    config = _downloader_config()
    client = MagicMock()
    client.get_torrents.return_value = (
        [
            {"hash": "a", "name": "A", "token": "drop"},
            {"hash": "b", "name": "B"},
        ],
        False,
    )
    monkeypatch.setattr(downloader_module, "_load_configs", lambda: [config])
    monkeypatch.setattr(downloader_module, "_build_client", lambda _config: client)

    result = downloader_module.call_action(
        "main",
        "tasks.list",
        {"offset": 1, "limit": 1, "status": "downloading"},
    )

    assert result["data"]["total"] == 2
    assert result["data"]["items"] == [{"hash": "b", "name": "B"}]
    client.get_torrents.assert_called_once_with(
        ids=None,
        status="downloading",
        tags=None,
    )


def test_downloader_rejects_provider_incompatible_action(
    downloader_module: ModuleType,
    monkeypatch,
) -> None:
    """provider 未声明的 action 必须在连接前拒绝。"""
    monkeypatch.setattr(
        downloader_module,
        "_load_configs",
        lambda: [_downloader_config("rtorrent")],
    )

    with pytest.raises(ValueError, match="不支持 action"):
        downloader_module.call_action(
            "main",
            "tasks.peers",
            {"task_id": "hash"},
        )


def test_downloader_queue_move_uses_fixed_provider_sdk_methods(
    downloader_module: ModuleType,
) -> None:
    """队列动作只能映射到审核过的 provider SDK 方法。"""
    qbc = MagicMock()
    client = SimpleNamespace(qbc=qbc)

    assert downloader_module._queue_move(
        client,
        "qbittorrent",
        {"task_ids": ["a", "b"], "position": "up"},
    )
    qbc.torrents_increase_priority.assert_called_once_with(torrent_hashes=["a", "b"])


def test_downloader_file_selection_uses_normalized_provider_methods(
    downloader_module: ModuleType,
) -> None:
    """文件选择应归一为 wanted/unwanted，且不能执行任意 SDK 方法。"""
    client = MagicMock()

    assert downloader_module._set_file_selection(
        client,
        "transmission",
        {
            "task_id": "hash",
            "wanted_file_ids": [1, 2],
            "unwanted_file_ids": [3],
        },
    )
    client.set_files.assert_called_once_with("hash", [1, 2])
    client.set_unwanted_files.assert_called_once_with("hash", [3])


def test_provider_projection_removes_nested_authentication_fields(
    downloader_module: ModuleType,
    mediaserver_module: ModuleType,
) -> None:
    """第三方返回值中的变体凭据字段也必须被投影层移除。"""
    payload = {
        "name": "safe",
        "access_token": "drop",
        "nested": {"sessionId": "drop", "value": 1},
    }

    assert downloader_module._jsonable(payload) == {
        "name": "safe",
        "nested": {"value": 1},
    }
    assert mediaserver_module._jsonable(payload) == {
        "name": "safe",
        "nested": {"value": 1},
    }


def test_mediaserver_capabilities_are_provider_specific(
    mediaserver_module: ModuleType,
    monkeypatch,
) -> None:
    """能力发现应隐藏配置并过滤 provider 不支持的动作。"""
    monkeypatch.setattr(
        mediaserver_module,
        "_load_configs",
        lambda: [_mediaserver_config("navidrome")],
    )

    result = mediaserver_module.list_capabilities("living-room")
    action_names = {item["action"] for item in result["actions"]}

    assert "items.list" in action_names
    assert "items.music.search" in action_names
    assert "server.users.count" in action_names
    assert "playback.sessions" not in action_names
    assert "metadata.refresh" not in action_names
    assert "private.invalid" not in str(result)
    assert "secret" not in str(result)


def test_mediaserver_items_and_scan_use_fixed_public_methods(
    mediaserver_module: ModuleType,
    monkeypatch,
) -> None:
    """媒体条目查询和扫描应调用固定 client 方法并返回统一 envelope。"""
    config = _mediaserver_config("emby")
    client = MagicMock()
    client.get_items.return_value = [SimpleNamespace(item_id="1", title="Movie")]
    client.refresh_root_library.return_value = True
    monkeypatch.setattr(mediaserver_module, "_load_configs", lambda: [config])
    monkeypatch.setattr(mediaserver_module, "_build_client", lambda _config: client)

    listing = mediaserver_module.call_action(
        "living-room",
        "items.list",
        {"parent": "movies", "offset": 0, "limit": 20},
    )
    scan = mediaserver_module.call_action(
        "living-room",
        "library.scan",
        {},
    )

    assert listing["data"]["items"][0]["item_id"] == "1"
    assert scan["effect"] == "external_side_effect"
    client.get_items.assert_called_once_with(
        parent="movies",
        start_index=0,
        limit=20,
    )
    client.refresh_root_library.assert_called_once_with()


def test_mediaserver_native_search_and_episode_coverage_use_public_methods(
    mediaserver_module: ModuleType,
) -> None:
    """原生搜索和剧集覆盖必须落到已审核的 provider 公共方法。"""
    client = MagicMock()
    client.get_music.return_value = [{"title": "Track"}]
    episode_call = MagicMock(return_value=("series", {1: [1, 2]}))

    def get_tv_episodes(
        item_id=None,
        title=None,
        year=None,
        season=None,
    ):
        """提供带真实签名的 provider 测试替身。"""
        return episode_call(
            item_id=item_id,
            title=title,
            year=year,
            season=season,
        )

    client.get_tv_episodes = get_tv_episodes

    music = mediaserver_module._search_music(
        client,
        "emby",
        {"title": "Track", "artist": "Artist"},
    )
    episodes = mediaserver_module._season_episodes(
        client,
        {"item_id": "series", "season": 1},
    )

    assert music == [{"title": "Track"}]
    assert episodes == ("series", {1: [1, 2]})
    client.get_music.assert_called_once_with(
        title="Track",
        artist="Artist",
        album=None,
    )
    episode_call.assert_called_once_with(
        item_id="series",
        title=None,
        year=None,
        season=1,
    )


def test_skill_docs_forbid_arbitrary_network_and_preserve_high_level_workflows() -> None:
    """Skill 文档必须明确任意网络出口和高层业务边界。"""
    downloader = (PROJECT_ROOT / "skills/downloader-operation/SKILL.md").read_text(encoding="utf-8")
    mediaserver = (PROJECT_ROOT / "skills/mediaserver-operation/SKILL.md").read_text(encoding="utf-8")

    assert "arbitrary URL" in downloader
    assert "download.add" in downloader
    assert "arbitrary SDK methods" in mediaserver
    assert "library.exists" in mediaserver
