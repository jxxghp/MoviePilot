"""
验证技能管理的消息交互、市场来源、安装和移除。
"""

import io
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.testing.bootstrap import ensure_optional_stub

ensure_optional_stub("qbittorrentapi", TorrentFilesList=list)
ensure_optional_stub("transmission_rpc", File=object)
ensure_optional_stub("psutil")
ensure_optional_stub("aioshutil")
ensure_optional_stub("pyquery", PyQuery=object)

from app.agent.skills.registry import (
    SkillHelper,
    SkillInfo,
    SkillMarketSource,
)

# 可选依赖占位必须先装配，再导入读取运行配置的应用入口。
from app.application.configuration import get_runtime_settings  # noqa: E402
from app.application.messaging.interaction import InteractionContext
from app.application.messaging.skill import SkillInteractionHandler, skill_interaction_manager
from app.chain.message import MessageChain
from app.schemas.types import NotificationChannel


def _build_skill_zip(skill_dir: str, skill_name: str) -> bytes:
    """
    构造包含技能前言和示例脚本的内存 ZIP，避免下载真实资源。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"demo-main/{skill_dir}/SKILL.md",
            (f"---\nname: {skill_name}\nversion: 1\ndescription: demo skill\n---\n\n# {skill_name}\n"),
        )
        zf.writestr(f"demo-main/{skill_dir}/scripts/example.py", "print('ok')\n")
    return buf.getvalue()


class _FakeResponse:
    """
    模拟技能市场接口的 JSON 或文件响应。
    """

    def __init__(self, payload=None, content: bytes = b"", status_code: int = 200):
        """
        保存预设响应载荷、内容和状态码。
        """
        self._payload = payload
        self.content = content
        self.status_code = status_code

    def json(self):
        """
        返回测试预置的 JSON 载荷。
        """
        return self._payload


@pytest.fixture
def _clear_skill_interactions():
    """
    隔离测试之间共享的会话、交互状态和技能市场缓存。
    """
    SkillHelper._list_market_source_skills.cache_clear()
    try:
        yield
    finally:
        skill_interaction_manager.clear()
        SkillHelper._list_market_source_skills.cache_clear()


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_message_routes_text_reply_to_skills_interaction_before_ai() -> None:
    """
    文本回复优先交给已有技能交互会话处理。
    """
    chain = MessageChain()
    skill_interaction_manager.create_or_replace(
        user_id="10001",
        channel=NotificationChannel.Wechat,
        source="wechat-test",
        username="tester",
    )

    with (
        patch.object(chain, "_record_user_message"),
        patch(
            "app.chain.message.SkillInteractionHandler.handle_text_interaction",
            return_value=True,
        ) as handle_text,
        patch.object(chain, "_handle_ai_message") as handle_ai,
    ):
        chain.handle_message(
            channel=NotificationChannel.Wechat,
            source="wechat-test",
            userid="10001",
            username="tester",
            text="2",
        )

    handle_text.assert_called_once()
    handle_ai.assert_not_called()


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skills_text_exit_skips_notification_history() -> None:
    """
    退出技能交互的提示不写入普通通知历史。
    """
    chain = SkillInteractionHandler(messenger=MessageChain())
    skill_interaction_manager.create_or_replace(
        user_id="10001",
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    with patch.object(chain._messenger, "post_message") as post_message:
        handled = chain.handle_text_interaction(
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="退出",
        )

    assert handled
    notification = post_message.call_args.args[0]
    assert notification.title == "技能交互已结束"
    assert not notification.save_history
    assert skill_interaction_manager.get_by_user("10001") is None


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_callback_routes_to_skills_chain() -> None:
    """
    技能按钮回调路由至技能交互处理器。
    """
    chain = MessageChain()
    request = skill_interaction_manager.create_or_replace(
        user_id="10001",
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    with patch(
        "app.chain.message.SkillInteractionHandler.handle_callback_interaction",
        return_value=True,
    ) as handle_callback:
        chain._handle_callback(
            callback_data=f"skills:{request.request_id}:market",
            context=InteractionContext(
                channel=NotificationChannel.Telegram,
                source="telegram-test",
                user_id="10001",
                username="tester",
            ),
        )

    handle_callback.assert_called_once()


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skillhelper_install_and_remove_market_skill() -> None:
    """
    市场技能可安装到临时目录并按管理入口移除。
    """
    helper = SkillHelper()
    skill = SkillInfo(
        id="demo-skill",
        name="demo-skill",
        description="demo",
        source_type="market",
        source_label="市场 · acme/demo",
        repo_url="https://github.com/acme/demo",
        repo_name="acme/demo",
        skill_path="skills/demo-skill",
    )
    zip_bytes = _build_skill_zip("skills/demo-skill", "demo-skill")

    with tempfile.TemporaryDirectory() as tempdir:
        user_root = Path(tempdir) / "user-skills"
        bundled_root = Path(tempdir) / "bundled-skills"
        user_root.mkdir(parents=True, exist_ok=True)
        bundled_root.mkdir(parents=True, exist_ok=True)

        with (
            patch.object(SkillHelper, "get_user_skills_dir", return_value=user_root),
            patch.object(SkillHelper, "get_bundled_skills_dir", return_value=bundled_root),
            patch.object(helper, "_download_repo_archive", return_value=zip_bytes),
        ):
            success, message = helper.install_market_skill(skill)
            assert success, message
            assert (user_root / "demo-skill" / "SKILL.md").exists()
            assert (user_root / "demo-skill" / ".moviepilot-skill-source.json").exists()

            local_skills = helper.list_local_skills()
            assert len(local_skills) == 1
            assert local_skills[0].source_type == "market"
            assert local_skills[0].removable

            removed, remove_message = helper.remove_local_skill("demo-skill")
            assert removed, remove_message
            assert not (user_root / "demo-skill").exists()

            bundled_skill_dir = bundled_root / "builtin-skill"
            bundled_skill_dir.mkdir(parents=True, exist_ok=True)
            (bundled_skill_dir / "SKILL.md").write_text(
                "---\nname: builtin-skill\ndescription: builtin\n---\n",
                encoding="utf-8",
            )
            installed_builtin = user_root / "builtin-skill"
            installed_builtin.mkdir(parents=True, exist_ok=True)
            (installed_builtin / "SKILL.md").write_text(
                "---\nname: builtin-skill\ndescription: builtin\n---\n",
                encoding="utf-8",
            )

            removed, remove_message = helper.remove_local_skill("builtin-skill")
            assert not removed
            assert "内置技能" in remove_message


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skillhelper_lists_clawhub_registry_skills() -> None:
    """
    从 ClawHub 注册服务读取技能列表与来源信息。
    """
    helper = SkillHelper()
    response = _FakeResponse(
        payload={
            "status": "success",
            "value": {
                "hasMore": False,
                "nextCursor": None,
                "page": [
                    {
                        "ownerHandle": "openclaw",
                        "skill": {
                            "slug": "weather-forecast",
                            "displayName": "Weather Forecast",
                            "summary": "Forecast weather from ClawHub",
                        },
                    }
                ],
            },
        }
    )

    with (
        patch.object(
            helper,
            "_discover_clawhub_runtime_env",
            return_value={"convex_url": "https://wry-manatee-359.convex.cloud"},
        ),
        patch.object(helper, "_request_convex_query", return_value=response),
    ):
        skills = helper._list_market_source_skills("https://clawhub.ai")

    assert len(skills) == 1
    assert skills[0].id == "weather-forecast"
    assert skills[0].name == "Weather Forecast"
    assert skills[0].source_type == "registry"
    assert skills[0].registry_name == "ClawHub"
    assert skills[0].source_label == "社区注册表 · ClawHub"
    assert "/openclaw/weather-forecast" in skills[0].path


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skillhelper_filters_market_skills_by_query() -> None:
    """
    市场技能列表按查询文本筛选。
    """
    helper = SkillHelper()
    skills = [
        SkillInfo(
            id="weather-forecast",
            name="Weather Forecast",
            description="Forecast weather from ClawHub",
            source_label="社区注册表 · ClawHub",
        ),
        SkillInfo(
            id="github-tools",
            name="GitHub Tools",
            description="Manage pull requests",
            source_label="官方仓库 · openai/skills",
        ),
    ]

    filtered = helper.filter_market_skills(skills=skills, query="weather clawhub")

    assert len(filtered) == 1
    assert filtered[0].id == "weather-forecast"


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skillhelper_falls_back_to_rest_registry_listing_when_runtime_missing() -> None:
    """
    技能运行时不可用时回退到注册服务 REST 列表。
    """
    helper = SkillHelper()
    response = _FakeResponse(
        payload={
            "items": [
                {
                    "slug": "weather-forecast",
                    "name": "Weather Forecast",
                    "summary": "Forecast weather from ClawHub",
                    "owner": {"handle": "openclaw"},
                }
            ]
        }
    )

    with (
        patch.object(helper, "_discover_clawhub_runtime_env", return_value=None),
        patch.object(helper, "_request_registry", return_value=response) as request_registry,
    ):
        skills = helper._list_market_source_skills("https://clawhub.ai")

    request_registry.assert_called_once()
    assert len(skills) == 1
    assert skills[0].id == "weather-forecast"
    assert skills[0].source_type == "registry"
    assert skills[0].registry_name == "ClawHub"
    assert skills[0].source_label == "社区注册表 · ClawHub"
    assert "/openclaw/weather-forecast" in skills[0].path


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skillhelper_installs_registry_skill() -> None:
    """
    通过注册服务的下载信息安装技能。
    """
    helper = SkillHelper()
    skill = SkillInfo(
        id="registry-demo",
        name="Registry Demo",
        description="registry demo",
        source_type="registry",
        source_label="注册表 · ClawHub",
        registry_url="https://clawhub.ai",
        registry_name="ClawHub",
        registry_slug="registry-demo",
        download_url="https://clawhub.ai/api/v1/download?slug=registry-demo",
    )
    zip_bytes = _build_skill_zip("package", "registry-demo")

    with tempfile.TemporaryDirectory() as tempdir:
        user_root = Path(tempdir) / "user-skills"
        bundled_root = Path(tempdir) / "bundled-skills"
        user_root.mkdir(parents=True, exist_ok=True)
        bundled_root.mkdir(parents=True, exist_ok=True)

        with (
            patch.object(SkillHelper, "get_user_skills_dir", return_value=user_root),
            patch.object(SkillHelper, "get_bundled_skills_dir", return_value=bundled_root),
            patch.object(helper, "_request_registry", return_value=_FakeResponse(content=zip_bytes)),
        ):
            success, message = helper.install_market_skill(skill)
            assert success, message
            assert (user_root / "registry-demo" / "SKILL.md").exists()
            assert (user_root / "registry-demo" / ".moviepilot-skill-source.json").exists()

            local_skills = helper.list_local_skills()
            assert len(local_skills) == 1
            assert local_skills[0].source_type == "registry"
            assert local_skills[0].registry_name == "ClawHub"
            assert local_skills[0].source_label == "社区注册表 · ClawHub"


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skillhelper_lists_market_sources_and_marks_custom_entries() -> None:
    """
    来源列表区分内置与自定义市场来源。
    """
    helper = SkillHelper()

    with (
        patch.object(
            helper,
            "get_market_sources",
            return_value=[
                "https://clawhub.ai",
                "https://github.com/openai/skills",
                "https://github.com/acme/custom-skills",
            ],
        ),
        patch.object(
            helper,
            "get_default_market_sources",
            return_value=[
                "https://clawhub.ai",
                "https://github.com/openai/skills",
            ],
        ),
    ):
        sources = helper.list_market_source_entries()

    assert len(sources) == 3
    assert sources[0].builtin
    assert sources[1].builtin
    assert not sources[2].builtin
    assert sources[2].removable
    assert sources[2].label == "仓库来源 · acme/custom-skills"


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skillhelper_add_custom_market_source_updates_setting() -> None:
    """
    新增自定义市场来源后持久化对应配置。
    """
    helper = SkillHelper()

    with (
        patch.object(
            helper,
            "get_market_sources",
            return_value=["https://github.com/openai/skills"],
        ),
        patch.object(
            get_runtime_settings(),
            "update",
            return_value=(True, ""),
        ) as update_setting,
    ):
        success, message = helper.add_custom_market_source("acme/custom-skills")

    assert success
    assert "acme/custom-skills" in message
    update_setting.assert_called_once_with(
        key="SKILL_MARKET",
        value="https://github.com/openai/skills,https://github.com/acme/custom-skills",
    )


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skillhelper_remove_custom_market_source_blocks_builtin() -> None:
    """
    移除自定义来源入口拒绝删除内置来源。
    """
    helper = SkillHelper()

    with patch.object(
        helper,
        "get_default_market_sources",
        return_value=["https://github.com/openai/skills"],
    ):
        success, message = helper.remove_custom_market_source("https://github.com/openai/skills")

    assert not success
    assert "内置默认源" in message


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skills_chain_market_view_marks_clawhub_as_community_source() -> None:
    """
    市场视图将 ClawHub 标记为社区来源。
    """
    chain = SkillInteractionHandler(messenger=MessageChain())
    request = skill_interaction_manager.create_or_replace(
        user_id="10001",
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        username="tester",
    )
    request.view = "market"

    with patch.object(
        chain.skillhelper,
        "list_market_skills",
        return_value=[
            SkillInfo(
                id="weather-forecast",
                name="Weather Forecast",
                description="Forecast weather from ClawHub",
                source_type="registry",
                source_label="社区注册表 · ClawHub",
                registry_name="ClawHub",
                registry_url="https://clawhub.ai",
                registry_slug="weather-forecast",
            )
        ],
    ):
        title, text, _buttons = chain._build_market_view(request=request)

    assert title == "技能市场"
    assert "社区注册表 · ClawHub" in text
    assert "社区源，安装前请自行甄别安全性" in text
    assert "ClawHub 属于社区注册表" in text


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skills_chain_market_view_filters_by_search_query() -> None:
    """
    市场交互视图只展示匹配当前搜索条件的技能。
    """
    chain = SkillInteractionHandler(messenger=MessageChain())
    request = skill_interaction_manager.create_or_replace(
        user_id="10001",
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        username="tester",
    )
    request.view = "market"
    request.market_query = "weather"

    with patch.object(
        chain.skillhelper,
        "list_market_skills",
        return_value=[
            SkillInfo(
                id="weather-forecast",
                name="Weather Forecast",
                description="Forecast weather from ClawHub",
                source_type="registry",
                source_label="社区注册表 · ClawHub",
                registry_name="ClawHub",
                registry_url="https://clawhub.ai",
                registry_slug="weather-forecast",
            ),
            SkillInfo(
                id="github-tools",
                name="GitHub Tools",
                description="Manage pull requests",
                source_type="market",
                source_label="官方仓库 · openai/skills",
                repo_name="openai/skills",
            ),
        ],
    ):
        title, text, buttons = chain._build_market_view(request=request)

    assert title == "技能市场"
    assert "当前搜索：weather" in text
    assert "weather-forecast" in text
    assert "github-tools" not in text
    assert buttons
    assert buttons[0][0]["callback_data"] == f"skills:{request.request_id}:clear-search"


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skills_chain_root_view_uses_friendly_source_labels() -> None:
    """
    技能首页展示易读的来源名称。
    """
    chain = SkillInteractionHandler(messenger=MessageChain())
    request = skill_interaction_manager.create_or_replace(
        user_id="10001",
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    with (
        patch.object(chain.skillhelper, "list_local_skills", return_value=[]),
        patch.object(chain.skillhelper, "list_market_skills", return_value=[]),
        patch.object(
            chain.skillhelper,
            "list_market_source_entries",
            return_value=[
                SkillMarketSource(
                    source="https://clawhub.ai",
                    label="社区注册表 · ClawHub",
                    builtin=True,
                    removable=False,
                ),
                SkillMarketSource(
                    source="https://github.com/openai/skills",
                    label="官方仓库 · openai/skills",
                    builtin=True,
                    removable=False,
                ),
                SkillMarketSource(
                    source="https://github.com/acme/custom-skills",
                    label="仓库来源 · acme/custom-skills",
                    builtin=False,
                    removable=True,
                ),
            ],
        ),
    ):
        title, text, _buttons = chain._build_root_view(request=request)

    assert title == "技能管理"
    assert "社区注册表 · ClawHub" in text
    assert "官方仓库 · openai/skills" in text
    assert "仓库来源 · acme/custom-skills" in text
    assert "3. 管理技能源" in text


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skills_chain_installed_view_builds_remove_buttons() -> None:
    """
    已安装技能视图为可移除项目生成操作按钮。
    """
    chain = SkillInteractionHandler(messenger=MessageChain())
    request = skill_interaction_manager.create_or_replace(
        user_id="10001",
        channel=NotificationChannel.WebAgent,
        source="web-agent",
        username="tester",
    )

    with patch.object(
        chain.skillhelper,
        "list_local_skills",
        return_value=[
            SkillInfo(
                id="builtin",
                name="Builtin",
                description="Built in skill",
                source_type="builtin",
                source_label="内置",
                removable=False,
            ),
            SkillInfo(
                id="custom",
                name="Custom",
                description="Custom skill",
                source_type="local",
                source_label="本地",
                removable=True,
            ),
        ],
    ):
        title, text, buttons = chain._build_installed_view(request=request)

    assert title == "已安装技能"
    assert "builtin" in text
    assert "custom" in text
    assert buttons
    assert {"text": "删除 2", "callback_data": f"skills:{request.request_id}:remove:2:{request.selection_id}"} in [
        button for row in buttons for button in row
    ]


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skills_chain_callback_enters_search_input_mode() -> None:
    """
    搜索按钮将技能会话切换为等待关键词输入。
    """
    chain = SkillInteractionHandler(messenger=MessageChain())
    request = skill_interaction_manager.create_or_replace(
        user_id="10001",
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    with patch.object(chain, "_render_interaction") as render:
        handled = chain.handle_callback_interaction(
            callback_data=f"skills:{request.request_id}:search",
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
        )

    assert handled
    assert request.view == "market"
    assert request.awaiting_input == "market-search"
    render.assert_called_once()


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skills_chain_text_search_updates_market_query() -> None:
    """
    文本搜索命令更新会话中的市场查询条件。
    """
    chain = SkillInteractionHandler(messenger=MessageChain())
    request = skill_interaction_manager.create_or_replace(
        user_id="10001",
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        username="tester",
    )
    request.view = "market"

    with patch.object(chain, "_render_interaction") as render:
        handled = chain.handle_text_interaction(
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="搜索 weather",
        )

    assert handled
    assert request.market_query == "weather"
    assert request.market_page == 0
    assert request.awaiting_input is None
    render.assert_called_once()


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skills_chain_followup_text_applies_search_when_awaiting_input() -> None:
    """
    等待输入时将后续文本作为技能搜索条件。
    """
    chain = SkillInteractionHandler(messenger=MessageChain())
    request = skill_interaction_manager.create_or_replace(
        user_id="10001",
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        username="tester",
    )
    request.view = "market"
    request.awaiting_input = "market-search"

    with patch.object(chain, "_render_interaction") as render:
        handled = chain.handle_text_interaction(
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="calendar",
        )

    assert handled
    assert request.market_query == "calendar"
    assert request.awaiting_input is None
    render.assert_called_once()


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skills_chain_callback_enters_source_add_mode() -> None:
    """
    添加来源按钮将会话切换为等待来源输入。
    """
    chain = SkillInteractionHandler(messenger=MessageChain())
    request = skill_interaction_manager.create_or_replace(
        user_id="10001",
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    with patch.object(chain, "_render_interaction") as render:
        handled = chain.handle_callback_interaction(
            callback_data=f"skills:{request.request_id}:source-add",
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
        )

    assert handled
    assert request.view == "sources"
    assert request.awaiting_input == "source-add"
    render.assert_called_once()


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skills_chain_followup_text_adds_custom_market_source() -> None:
    """
    等待来源输入时将后续文本保存为自定义来源。
    """
    chain = SkillInteractionHandler(messenger=MessageChain())
    request = skill_interaction_manager.create_or_replace(
        user_id="10001",
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        username="tester",
    )
    request.view = "sources"
    request.awaiting_input = "source-add"

    with (
        patch.object(
            chain.skillhelper,
            "add_custom_market_source",
            return_value=(True, "已添加技能源：仓库来源 · acme/custom-skills"),
        ) as add_source,
        patch.object(chain, "_render_interaction") as render,
        patch.object(chain._messenger, "post_message") as post_message,
    ):
        handled = chain.handle_text_interaction(
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="acme/custom-skills",
        )

    assert handled
    assert request.awaiting_input is None
    add_source.assert_called_once_with("acme/custom-skills")
    post_message.assert_called_once()
    render.assert_called_once()


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skills_chain_text_removes_custom_market_source_by_index() -> None:
    """
    文本命令按展示序号移除指定自定义来源。
    """
    chain = SkillInteractionHandler(messenger=MessageChain())
    request = skill_interaction_manager.create_or_replace(
        user_id="10001",
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    with (
        patch.object(
            chain,
            "_remove_market_source",
            return_value=(True, "已删除技能源：仓库来源 · acme/custom-skills"),
        ) as remove_source,
        patch.object(chain, "_render_interaction") as render,
        patch.object(chain._messenger, "post_message") as post_message,
    ):
        handled = chain.handle_text_interaction(
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="删除源 3",
        )

    assert handled
    assert request.view == "sources"
    remove_source.assert_called_once_with(page_index=3)
    post_message.assert_called_once()
    render.assert_called_once()


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skills_chain_source_view_lists_custom_sources() -> None:
    """
    来源管理视图展示已配置的自定义来源。
    """
    chain = SkillInteractionHandler(messenger=MessageChain())
    request = skill_interaction_manager.create_or_replace(
        user_id="10001",
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        username="tester",
    )
    request.view = "sources"

    with patch.object(
        chain.skillhelper,
        "list_market_source_entries",
        return_value=[
            SkillMarketSource(
                source="https://clawhub.ai",
                label="社区注册表 · ClawHub",
                builtin=True,
                removable=False,
            ),
            SkillMarketSource(
                source="https://github.com/acme/custom-skills",
                label="仓库来源 · acme/custom-skills",
                builtin=False,
                removable=True,
            ),
        ],
    ):
        title, text, buttons = chain._build_sources_view(request=request)

    assert title == "技能源管理"
    assert "社区注册表 · ClawHub" in text
    assert "仓库来源 · acme/custom-skills" in text
    assert "删除自定义源" in text
    assert buttons
    assert buttons[1][0]["callback_data"] == f"skills:{request.request_id}:source-remove:2"


@pytest.mark.usefixtures("_clear_skill_interactions")
def test_skills_chain_updates_buttons_via_edit_message() -> None:
    """
    渠道支持编辑时更新原消息中的技能操作按钮。
    """
    chain = SkillInteractionHandler(messenger=MessageChain())
    buttons = [[{"text": "安装 1", "callback_data": "skills:req:install:1"}]]

    with (
        patch.object(chain._messenger, "edit_message", return_value=True) as edit_message,
        patch.object(chain._messenger, "post_message") as post_message,
    ):
        chain._update_or_post_message(
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            title="技能市场",
            text="请选择技能",
            buttons=buttons,
            original_message_id=123,
            original_chat_id="456",
        )

    edit_message.assert_called_once_with(
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        message_id=123,
        chat_id="456",
        title="技能市场",
        text="请选择技能",
        buttons=buttons,
    )
    post_message.assert_not_called()
