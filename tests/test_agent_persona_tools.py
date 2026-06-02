import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent.runtime import AgentRuntimeManager
from app.agent.tools.impl.query_personas import QueryPersonasTool
from app.agent.tools.impl.switch_persona import SwitchPersonaTool
from app.agent.tools.impl.update_persona_definition import UpdatePersonaDefinitionTool


class TestAgentPersonaTools(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.temp_root = Path(self._tempdir.name)
        self.agent_root = self.temp_root / "agent"
        defaults_root = (
            Path(__file__).resolve().parents[1] / "app" / "agent" / "defaults"
        )
        self.runtime_manager = AgentRuntimeManager(
            agent_root_dir=self.agent_root,
            bundled_defaults_dir=defaults_root,
        )
        self.runtime_manager.ensure_layout()

    def test_query_personas_returns_available_personas_and_active_state(self):
        tool = QueryPersonasTool(session_id="session-1", user_id="10001")

        with patch(
            "app.agent.tools.impl.query_personas.agent_runtime_manager",
            self.runtime_manager,
        ):
            result = asyncio.run(tool.run())

        payload = json.loads(result)
        self.assertEqual(payload["active_persona"], "default")
        self.assertGreaterEqual(payload["count"], 9)
        self.assertTrue(any(persona["persona_id"] == "concise" for persona in payload["personas"]))
        self.assertTrue(any(persona["persona_id"] == "catgirl" for persona in payload["personas"]))
        self.assertTrue(any(persona["is_active"] for persona in payload["personas"]))

    def test_switch_persona_updates_runtime_by_alias(self):
        tool = SwitchPersonaTool(session_id="session-1", user_id="10001")

        with patch(
            "app.agent.tools.impl.switch_persona.agent_runtime_manager",
            self.runtime_manager,
        ):
            result = asyncio.run(tool.run(persona_id="讲解"))

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["active_persona"], "guide")
        self.assertEqual(self.runtime_manager.load_runtime_config().active_persona, "guide")

    def test_update_persona_definition_updates_existing_persona(self):
        tool = UpdatePersonaDefinitionTool(session_id="session-1", user_id="10001")

        with patch(
            "app.agent.tools.impl.update_persona_definition.agent_runtime_manager",
            self.runtime_manager,
        ):
            result = asyncio.run(
                tool.run(
                    persona_id="default",
                    description="更偏执行导向的默认人格。",
                    append_instructions=["Prefer action-first responses."],
                )
            )

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertFalse(payload["created"])
        runtime_config = self.runtime_manager.load_runtime_config()
        default_persona = next(
            persona
            for persona in runtime_config.available_personas
            if persona.persona_id == "default"
        )
        self.assertEqual(default_persona.description, "更偏执行导向的默认人格。")
        self.assertIn("Prefer action-first responses.", default_persona.text)

    def test_update_persona_definition_can_create_new_persona(self):
        tool = UpdatePersonaDefinitionTool(session_id="session-1", user_id="10001")

        with patch(
            "app.agent.tools.impl.update_persona_definition.agent_runtime_manager",
            self.runtime_manager,
        ):
            result = asyncio.run(
                tool.run(
                    persona_id="analysis",
                    label="分析型",
                    description="更适合解释复杂问题。",
                    aliases=["分析", "推理"],
                    instructions=(
                        "- Tone: analytical and structured.\n"
                        "- For complex tasks, explain the key tradeoff briefly."
                    ),
                    create_if_missing=True,
                )
            )

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertTrue(payload["created"])
        runtime_config = self.runtime_manager.load_runtime_config()
        created_persona = next(
            persona
            for persona in runtime_config.available_personas
            if persona.persona_id == "analysis"
        )
        self.assertEqual(created_persona.label, "分析型")
        self.assertIn("推理", created_persona.aliases)
        self.assertIn("analytical and structured", created_persona.text)
