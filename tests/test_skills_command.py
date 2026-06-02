import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

sys.modules.setdefault("qbittorrentapi", ModuleType("qbittorrentapi"))
setattr(sys.modules["qbittorrentapi"], "TorrentFilesList", list)
sys.modules.setdefault("transmission_rpc", ModuleType("transmission_rpc"))
setattr(sys.modules["transmission_rpc"], "File", object)
sys.modules.setdefault("psutil", ModuleType("psutil"))
sys.modules.setdefault("aioshutil", ModuleType("aioshutil"))
sys.modules.setdefault("pyquery", ModuleType("pyquery"))
setattr(sys.modules["pyquery"], "PyQuery", object)
sys.modules.setdefault("dateparser", ModuleType("dateparser"))
setattr(sys.modules["dateparser"], "parse", lambda *args, **kwargs: None)
sys.modules.setdefault("dateutil", ModuleType("dateutil"))
dateutil_parser = ModuleType("dateutil.parser")
setattr(dateutil_parser, "parse", lambda *args, **kwargs: None)
sys.modules.setdefault("dateutil.parser", dateutil_parser)
setattr(sys.modules["dateutil"], "parser", dateutil_parser)

from app.chain.message import MessageChain
from app.chain.skills import SkillsChain, skills_interaction_manager
from app.helper.skill import (
    SkillHelper,
    SkillInfo,
    SkillMarketSource,
    settings as skill_settings,
)
from app.schemas.types import MessageChannel


def _build_skill_zip(skill_dir: str, skill_name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"demo-main/{skill_dir}/SKILL.md",
            (
                f"---\n"
                f"name: {skill_name}\n"
                f"version: 1\n"
                f"description: demo skill\n"
                f"---\n\n"
                f"# {skill_name}\n"
            ),
        )
        zf.writestr(f"demo-main/{skill_dir}/scripts/example.py", "print('ok')\n")
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, payload=None, content: bytes = b"", status_code: int = 200):
        self._payload = payload
        self.content = content
        self.status_code = status_code

    def json(self):
        return self._payload


class TestSkillsCommand(unittest.TestCase):
    def tearDown(self):
        skills_interaction_manager.clear()

    def test_message_routes_text_reply_to_skills_interaction_before_ai(self):
        chain = MessageChain()
        skills_interaction_manager.create_or_replace(
            user_id="10001",
            channel=MessageChannel.Wechat,
            source="wechat-test",
            username="tester",
        )

        with patch.object(chain, "_record_user_message"), patch(
            "app.chain.message.SkillsChain.handle_text_interaction",
            return_value=True,
        ) as handle_text, patch.object(chain, "_handle_ai_message") as handle_ai:
            chain.handle_message(
                channel=MessageChannel.Wechat,
                source="wechat-test",
                userid="10001",
                username="tester",
                text="2",
            )

        handle_text.assert_called_once()
        handle_ai.assert_not_called()

    def test_callback_routes_to_skills_chain(self):
        chain = MessageChain()
        request = skills_interaction_manager.create_or_replace(
            user_id="10001",
            channel=MessageChannel.Telegram,
            source="telegram-test",
            username="tester",
        )

        with patch(
            "app.chain.message.SkillsChain.handle_callback_interaction",
            return_value=True,
        ) as handle_callback:
            chain._handle_callback(
                text=f"CALLBACK:skills:{request.request_id}:market",
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
            )

        handle_callback.assert_called_once()

    def test_skillhelper_install_and_remove_market_skill(self):
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

            with patch.object(
                SkillHelper, "get_user_skills_dir", return_value=user_root
            ), patch.object(
                SkillHelper, "get_bundled_skills_dir", return_value=bundled_root
            ), patch.object(
                helper, "_download_repo_archive", return_value=zip_bytes
            ):
                success, message = helper.install_market_skill(skill)
                self.assertTrue(success, message)
                self.assertTrue((user_root / "demo-skill" / "SKILL.md").exists())
                self.assertTrue(
                    (user_root / "demo-skill" / ".moviepilot-skill-source.json").exists()
                )

                local_skills = helper.list_local_skills()
                self.assertEqual(len(local_skills), 1)
                self.assertEqual(local_skills[0].source_type, "market")
                self.assertTrue(local_skills[0].removable)

                removed, remove_message = helper.remove_local_skill("demo-skill")
                self.assertTrue(removed, remove_message)
                self.assertFalse((user_root / "demo-skill").exists())

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
                self.assertFalse(removed)
                self.assertIn("内置技能", remove_message)

    def test_skillhelper_lists_clawhub_registry_skills(self):
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

        with patch.object(
            helper,
            "_discover_clawhub_runtime_env",
            return_value={"convex_url": "https://wry-manatee-359.convex.cloud"},
        ), patch.object(helper, "_request_convex_query", return_value=response):
            skills = helper._list_market_source_skills("https://clawhub.ai")

        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].id, "weather-forecast")
        self.assertEqual(skills[0].name, "Weather Forecast")
        self.assertEqual(skills[0].source_type, "registry")
        self.assertEqual(skills[0].registry_name, "ClawHub")
        self.assertEqual(skills[0].source_label, "社区注册表 · ClawHub")
        self.assertIn("/openclaw/weather-forecast", skills[0].path)

    def test_skillhelper_filters_market_skills_by_query(self):
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

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].id, "weather-forecast")

    def test_skillhelper_falls_back_to_rest_registry_listing_when_runtime_missing(self):
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

        with patch.object(
            helper, "_discover_clawhub_runtime_env", return_value=None
        ), patch.object(helper, "_request_registry", return_value=response):
            skills = helper._list_market_source_skills("https://clawhub.ai")

        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].id, "weather-forecast")
        self.assertEqual(skills[0].source_type, "registry")
        self.assertEqual(skills[0].registry_name, "ClawHub")
        self.assertEqual(skills[0].source_label, "社区注册表 · ClawHub")
        self.assertIn("/openclaw/weather-forecast", skills[0].path)

    def test_skillhelper_installs_registry_skill(self):
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

            with patch.object(
                SkillHelper, "get_user_skills_dir", return_value=user_root
            ), patch.object(
                SkillHelper, "get_bundled_skills_dir", return_value=bundled_root
            ), patch.object(
                helper, "_request_registry", return_value=_FakeResponse(content=zip_bytes)
            ):
                success, message = helper.install_market_skill(skill)
                self.assertTrue(success, message)
                self.assertTrue((user_root / "registry-demo" / "SKILL.md").exists())
                self.assertTrue(
                    (
                        user_root
                        / "registry-demo"
                        / ".moviepilot-skill-source.json"
                    ).exists()
                )

                local_skills = helper.list_local_skills()
                self.assertEqual(len(local_skills), 1)
                self.assertEqual(local_skills[0].source_type, "registry")
                self.assertEqual(local_skills[0].registry_name, "ClawHub")
                self.assertEqual(local_skills[0].source_label, "社区注册表 · ClawHub")

    def test_skillhelper_lists_market_sources_and_marks_custom_entries(self):
        helper = SkillHelper()

        with patch.object(
            helper,
            "get_market_sources",
            return_value=[
                "https://clawhub.ai",
                "https://github.com/openai/skills",
                "https://github.com/acme/custom-skills",
            ],
        ), patch.object(
            helper,
            "get_default_market_sources",
            return_value=[
                "https://clawhub.ai",
                "https://github.com/openai/skills",
            ],
        ):
            sources = helper.list_market_source_entries()

        self.assertEqual(len(sources), 3)
        self.assertTrue(sources[0].builtin)
        self.assertTrue(sources[1].builtin)
        self.assertFalse(sources[2].builtin)
        self.assertTrue(sources[2].removable)
        self.assertEqual(sources[2].label, "仓库来源 · acme/custom-skills")

    def test_skillhelper_add_custom_market_source_updates_setting(self):
        helper = SkillHelper()

        with patch.object(
            helper,
            "get_market_sources",
            return_value=["https://github.com/openai/skills"],
        ), patch.object(
            type(skill_settings),
            "update_setting",
            return_value=(True, ""),
        ) as update_setting:
            success, message = helper.add_custom_market_source("acme/custom-skills")

        self.assertTrue(success)
        self.assertIn("acme/custom-skills", message)
        update_setting.assert_called_once_with(
            key="SKILL_MARKET",
            value="https://github.com/openai/skills,https://github.com/acme/custom-skills",
        )

    def test_skillhelper_remove_custom_market_source_blocks_builtin(self):
        helper = SkillHelper()

        with patch.object(
            helper,
            "get_default_market_sources",
            return_value=["https://github.com/openai/skills"],
        ):
            success, message = helper.remove_custom_market_source(
                "https://github.com/openai/skills"
            )

        self.assertFalse(success)
        self.assertIn("内置默认源", message)

    def test_skills_chain_market_view_marks_clawhub_as_community_source(self):
        chain = SkillsChain()
        request = skills_interaction_manager.create_or_replace(
            user_id="10001",
            channel=MessageChannel.Telegram,
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

        self.assertEqual(title, "技能市场")
        self.assertIn("社区注册表 · ClawHub", text)
        self.assertIn("社区源，安装前请自行甄别安全性", text)
        self.assertIn("ClawHub 属于社区注册表", text)

    def test_skills_chain_market_view_filters_by_search_query(self):
        chain = SkillsChain()
        request = skills_interaction_manager.create_or_replace(
            user_id="10001",
            channel=MessageChannel.Telegram,
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

        self.assertEqual(title, "技能市场")
        self.assertIn("当前搜索：weather", text)
        self.assertIn("weather-forecast", text)
        self.assertNotIn("github-tools", text)
        self.assertTrue(buttons)
        self.assertEqual(buttons[0][0]["callback_data"], f"skills:{request.request_id}:clear-search")

    def test_skills_chain_root_view_uses_friendly_source_labels(self):
        chain = SkillsChain()
        request = skills_interaction_manager.create_or_replace(
            user_id="10001",
            channel=MessageChannel.Telegram,
            source="telegram-test",
            username="tester",
        )

        with patch.object(chain.skillhelper, "list_local_skills", return_value=[]), patch.object(
            chain.skillhelper, "list_market_skills", return_value=[]
        ), patch.object(
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
        ):
            title, text, _buttons = chain._build_root_view(request=request)

        self.assertEqual(title, "技能管理")
        self.assertIn("社区注册表 · ClawHub", text)
        self.assertIn("官方仓库 · openai/skills", text)
        self.assertIn("仓库来源 · acme/custom-skills", text)
        self.assertIn("3. 管理技能源", text)

    def test_skills_chain_callback_enters_search_input_mode(self):
        chain = SkillsChain()
        request = skills_interaction_manager.create_or_replace(
            user_id="10001",
            channel=MessageChannel.Telegram,
            source="telegram-test",
            username="tester",
        )

        with patch.object(chain, "_render_interaction") as render:
            handled = chain.handle_callback_interaction(
                callback_data=f"skills:{request.request_id}:search",
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
            )

        self.assertTrue(handled)
        self.assertEqual(request.view, "market")
        self.assertEqual(request.awaiting_input, "market-search")
        render.assert_called_once()

    def test_skills_chain_text_search_updates_market_query(self):
        chain = SkillsChain()
        request = skills_interaction_manager.create_or_replace(
            user_id="10001",
            channel=MessageChannel.Telegram,
            source="telegram-test",
            username="tester",
        )
        request.view = "market"

        with patch.object(chain, "_render_interaction") as render:
            handled = chain.handle_text_interaction(
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
                text="搜索 weather",
            )

        self.assertTrue(handled)
        self.assertEqual(request.market_query, "weather")
        self.assertEqual(request.market_page, 0)
        self.assertIsNone(request.awaiting_input)
        render.assert_called_once()

    def test_skills_chain_followup_text_applies_search_when_awaiting_input(self):
        chain = SkillsChain()
        request = skills_interaction_manager.create_or_replace(
            user_id="10001",
            channel=MessageChannel.Telegram,
            source="telegram-test",
            username="tester",
        )
        request.view = "market"
        request.awaiting_input = "market-search"

        with patch.object(chain, "_render_interaction") as render:
            handled = chain.handle_text_interaction(
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
                text="calendar",
            )

        self.assertTrue(handled)
        self.assertEqual(request.market_query, "calendar")
        self.assertIsNone(request.awaiting_input)
        render.assert_called_once()

    def test_skills_chain_callback_enters_source_add_mode(self):
        chain = SkillsChain()
        request = skills_interaction_manager.create_or_replace(
            user_id="10001",
            channel=MessageChannel.Telegram,
            source="telegram-test",
            username="tester",
        )

        with patch.object(chain, "_render_interaction") as render:
            handled = chain.handle_callback_interaction(
                callback_data=f"skills:{request.request_id}:source-add",
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
            )

        self.assertTrue(handled)
        self.assertEqual(request.view, "sources")
        self.assertEqual(request.awaiting_input, "source-add")
        render.assert_called_once()

    def test_skills_chain_followup_text_adds_custom_market_source(self):
        chain = SkillsChain()
        request = skills_interaction_manager.create_or_replace(
            user_id="10001",
            channel=MessageChannel.Telegram,
            source="telegram-test",
            username="tester",
        )
        request.view = "sources"
        request.awaiting_input = "source-add"

        with patch.object(
            chain.skillhelper,
            "add_custom_market_source",
            return_value=(True, "已添加技能源：仓库来源 · acme/custom-skills"),
        ) as add_source, patch.object(chain, "_render_interaction") as render, patch.object(
            chain, "post_message"
        ) as post_message:
            handled = chain.handle_text_interaction(
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
                text="acme/custom-skills",
            )

        self.assertTrue(handled)
        self.assertIsNone(request.awaiting_input)
        add_source.assert_called_once_with("acme/custom-skills")
        post_message.assert_called_once()
        render.assert_called_once()

    def test_skills_chain_text_removes_custom_market_source_by_index(self):
        chain = SkillsChain()
        request = skills_interaction_manager.create_or_replace(
            user_id="10001",
            channel=MessageChannel.Telegram,
            source="telegram-test",
            username="tester",
        )

        with patch.object(
            chain,
            "_remove_market_source",
            return_value=(True, "已删除技能源：仓库来源 · acme/custom-skills"),
        ) as remove_source, patch.object(chain, "_render_interaction") as render, patch.object(
            chain, "post_message"
        ) as post_message:
            handled = chain.handle_text_interaction(
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
                text="删除源 3",
            )

        self.assertTrue(handled)
        self.assertEqual(request.view, "sources")
        remove_source.assert_called_once_with(page_index=3)
        post_message.assert_called_once()
        render.assert_called_once()

    def test_skills_chain_source_view_lists_custom_sources(self):
        chain = SkillsChain()
        request = skills_interaction_manager.create_or_replace(
            user_id="10001",
            channel=MessageChannel.Telegram,
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

        self.assertEqual(title, "技能源管理")
        self.assertIn("社区注册表 · ClawHub", text)
        self.assertIn("仓库来源 · acme/custom-skills", text)
        self.assertIn("删除自定义源", text)
        self.assertTrue(buttons)
        self.assertEqual(
            buttons[1][0]["callback_data"],
            f"skills:{request.request_id}:source-remove:2",
        )

    def test_skills_chain_updates_buttons_via_edit_message(self):
        chain = SkillsChain()
        buttons = [[{"text": "安装 1", "callback_data": "skills:req:install:1"}]]

        with patch.object(chain, "edit_message", return_value=True) as edit_message, patch.object(
            chain, "post_message"
        ) as post_message:
            chain._update_or_post_message(
                channel=MessageChannel.Telegram,
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
            channel=MessageChannel.Telegram,
            source="telegram-test",
            message_id=123,
            chat_id="456",
            title="技能市场",
            text="请选择技能",
            buttons=buttons,
        )
        post_message.assert_not_called()
