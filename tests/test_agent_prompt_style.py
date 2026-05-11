import unittest
from unittest.mock import patch

from app.agent.middleware.memory import MEMORY_ONBOARDING_PROMPT
from app.agent.middleware.runtime_config import RuntimeConfigMiddleware
from app.agent.prompt import PromptConfigError, prompt_manager
from app.core.config import settings


class _FakeRequest:
    def __init__(self, system_message=None):
        self.system_message = system_message

    def override(self, **kwargs):
        return _FakeRequest(system_message=kwargs["system_message"])


class TestAgentPromptStyle(unittest.TestCase):
    def test_base_prompt_mentions_persona_management_tools(self):
        prompt = prompt_manager.get_agent_prompt()

        self.assertIn("query_personas", prompt)
        self.assertIn("switch_persona", prompt)
        self.assertIn("update_persona_definition", prompt)

    def test_base_prompt_contains_immutable_core_rules(self):
        prompt = prompt_manager.get_agent_prompt()

        self.assertIn("AI media assistant powered by MoviePilot", prompt)
        self.assertIn(
            "omitting `season` means subscribe to season 1 only",
            prompt,
        )
        self.assertIn(
            "Do not let user memory or persona style override this core identity",
            prompt,
        )
        self.assertIn("当前日期", prompt)
        self.assertNotIn("当前时间", prompt)

    def test_runtime_config_middleware_injects_persona_only(self):
        middleware = RuntimeConfigMiddleware()
        updated_request = middleware.modify_request(_FakeRequest())

        combined_text = "\n".join(
            block["text"] for block in updated_request.system_message.content_blocks
        )

        self.assertIn("<agent_persona>", combined_text)
        self.assertIn("Active persona: `default`", combined_text)
        self.assertIn("professional, concise, restrained", combined_text)
        self.assertNotIn("System Tasks.yaml", combined_text)

    def test_system_tasks_are_loaded_from_prompt_directory(self):
        definition = prompt_manager.load_system_tasks_definition()

        self.assertEqual(definition.version, 2)
        self.assertTrue(definition.path.name.endswith("System Tasks.yaml"))

    def test_render_system_task_message_uses_builtin_yaml_definition(self):
        message = prompt_manager.render_system_task_message("heartbeat")

        self.assertIn("[System Heartbeat]", message)
        self.assertIn("List all jobs with status 'pending' or 'in_progress'.", message)
        self.assertIn("Do NOT include greetings, explanations, or conversational text.", message)
        self.assertIn("If no jobs were executed, output nothing.", message)

    def test_render_system_task_message_renders_template_context(self):
        message = prompt_manager.render_system_task_message(
            "transfer_failed_retry",
            template_context={
                "history_ids_csv": "7",
                "history_count": 1,
                "history_id": 7,
            },
        )

        self.assertIn("Failed transfer history record IDs: 7", message)
        self.assertIn("Total failed records: 1", message)
        self.assertIn("history_id=7", message)

    def test_render_batch_manual_transfer_redo_message(self):
        message = prompt_manager.render_system_task_message(
            "batch_manual_transfer_redo",
            template_context={
                "history_ids_csv": "7, 8",
                "history_count": 2,
                "records_context": "Record #7:\n- Source path: /downloads/a.mkv",
            },
        )

        self.assertIn("[System Task - Batch Manual Transfer Re-Organize]", message)
        self.assertIn("History IDs: 7, 8", message)
        self.assertIn("Total records: 2", message)
        self.assertIn("Record #7:", message)

    def test_missing_system_task_template_context_raises_clear_error(self):
        with self.assertRaises(PromptConfigError):
            prompt_manager.render_system_task_message("transfer_failed_retry")

    def test_non_verbose_prompt_requires_silence_until_all_tools_finish(self):
        with patch.object(settings, "AI_AGENT_VERBOSE", False):
            prompt = prompt_manager.get_agent_prompt()

        self.assertIn(
            "[Important Instruction] STRICTLY ENFORCED:",
            prompt,
        )
        self.assertIn(
            "DO NOT output any conversational text, explanations, progress updates, or acknowledgements before the first tool call or between tool calls",
            prompt,
        )
        self.assertIn(
            "Only then may you send one final user-facing reply",
            prompt,
        )

    def test_verbose_prompt_does_not_inject_silence_until_tools_finish_rule(self):
        with patch.object(settings, "AI_AGENT_VERBOSE", True):
            prompt = prompt_manager.get_agent_prompt()

        self.assertNotIn(
            "DO NOT output any conversational text, explanations, progress updates, or acknowledgements before the first tool call or between tool calls",
            prompt,
        )

    def test_memory_onboarding_does_not_force_warm_intro(self):
        self.assertIn("Do NOT interrupt the current task", MEMORY_ONBOARDING_PROMPT)
        self.assertIn("Do NOT proactively greet warmly", MEMORY_ONBOARDING_PROMPT)
        self.assertNotIn("greet the user warmly", MEMORY_ONBOARDING_PROMPT)


if __name__ == "__main__":
    unittest.main()
