import unittest
from unittest.mock import patch

from app.agent.middleware.memory import MEMORY_ONBOARDING_PROMPT
from app.agent.middleware.runtime_config import RuntimeConfigMiddleware
from app.agent.prompt import COMMON_SHELL_COMMANDS, PromptConfigError, prompt_manager
from app.core.config import settings


class _FakeRequest:
    def __init__(self, system_message=None):
        self.system_message = system_message

    def override(self, **kwargs):
        return _FakeRequest(system_message=kwargs["system_message"])


class TestAgentPromptStyle(unittest.TestCase):
    def setUp(self):
        """每个用例前清理系统命令缓存，避免本机 PATH 或测试顺序影响断言。"""
        prompt_manager.clear_available_shell_commands_cache()

    def tearDown(self):
        """每个用例后清理系统命令缓存，避免 mock 探测结果泄漏到后续用例。"""
        prompt_manager.clear_available_shell_commands_cache()

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
        self.assertIn(
            "Never directly modify application source code",
            prompt,
        )
        self.assertIn(
            "If the user has not explicitly requested an operation that changes system behavior",
            prompt,
        )
        self.assertIn("<non_negotiable_boundaries>", prompt)
        self.assertIn("<confirmation_policy>", prompt)
        self.assertIn(
            "Treat read-only inspection as allowed",
            prompt,
        )
        self.assertIn(
            "Use `execute_command` only for diagnostics, read-only inspection, or commands the user explicitly asked to run",
            prompt,
        )
        self.assertIn("当前日期", prompt)
        self.assertNotIn("当前时间", prompt)

    def test_base_prompt_requires_parallel_independent_tool_calls(self):
        """核心提示词应明确要求并行执行互不依赖的工具调用。"""
        prompt = prompt_manager.get_agent_prompt()

        self.assertIn("Use parallel tool calls by default", prompt)
        self.assertIn(
            "issue all tool calls that can run without waiting for each other's results",
            prompt,
        )
        self.assertIn(
            "Keep tools sequential only when later arguments depend on earlier output",
            prompt,
        )

    def test_base_prompt_injects_available_shell_commands(self):
        """系统信息应注入 PATH 中已安装的常用命令，帮助 Agent 选择 execute_command。"""
        command_paths = {
            "git": "/usr/bin/git",
            "rg": "/opt/homebrew/bin/rg",
        }
        with patch(
            "app.agent.prompt.shutil.which",
            side_effect=lambda command: command_paths.get(command),
        ):
            prompt = prompt_manager.get_agent_prompt()

        self.assertIn("- 可用系统命令（可通过 `execute_command` 调用）:", prompt)
        self.assertIn("  - git: /usr/bin/git", prompt)
        self.assertIn("  - rg: /opt/homebrew/bin/rg", prompt)
        self.assertIn(
            "When searching files or text, prefer `rg` / `rg --files`",
            prompt,
        )
        self.assertNotIn("  - ssh:", prompt)

    def test_base_prompt_omits_shell_command_section_when_none_available(self):
        """PATH 中没有命中白名单命令时，不注入空的系统命令段落。"""
        with patch("app.agent.prompt.shutil.which", return_value=None):
            prompt = prompt_manager.get_agent_prompt()

        self.assertNotIn("可用系统命令", prompt)

    def test_available_shell_commands_are_cached_after_first_scan(self):
        """常用命令探测应只在首次加载时扫描 PATH，后续提示词复用缓存。"""
        command_paths = {"git": "/usr/bin/git"}
        with patch(
            "app.agent.prompt.shutil.which",
            side_effect=lambda command: command_paths.get(command),
        ) as which_mock:
            first_prompt = prompt_manager.get_agent_prompt()
            second_prompt = prompt_manager.get_agent_prompt()

        self.assertIn("  - git: /usr/bin/git", first_prompt)
        self.assertIn("  - git: /usr/bin/git", second_prompt)
        self.assertEqual(which_mock.call_count, len(COMMON_SHELL_COMMANDS))

    def test_common_shell_commands_skip_linux_basics(self):
        """不影响任务策略的通用命令不进入启动探测列表，避免重复 which。"""
        low_value_commands = {
            "rsync",
            "find",
            "grep",
            "sed",
            "awk",
            "tar",
            "gzip",
            "gunzip",
            "base64",
            "du",
            "df",
            "ps",
            "top",
            "ping",
            "pip",
            "pip3",
            "uv",
            "node",
            "npm",
            "yarn",
            "pnpm",
            "bun",
            "sqlite3",
            "psql",
            "mysql",
            "redis-cli",
            "kubectl",
            "helm",
            "lsof",
            "netstat",
            "ss",
            "traceroute",
            "dig",
            "nslookup",
            "nc",
            "telnet",
            "crontab",
            "systemctl",
            "service",
            "journalctl",
            "launchctl",
            "brew",
            "apt",
            "apk",
            "yum",
            "dnf",
        }

        self.assertFalse(low_value_commands & set(COMMON_SHELL_COMMANDS))

    def test_common_shell_commands_keep_extra_install_runtime_tools(self):
        """需要额外安装且会影响执行方式的运行时工具应保留探测。"""
        expected_commands = {"ssh", "scp", "sftp", "python", "python3"}

        self.assertTrue(expected_commands <= set(COMMON_SHELL_COMMANDS))

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
        self.assertIn("use the `send_message` tool", message)
        self.assertIn("Your final response for heartbeat must be empty", message)
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

    def test_voice_prompt_marks_voice_tool_as_terminal_reply(self):
        """语音回复提示词应说明语音工具会结束当前轮次。"""
        with patch.object(settings, "LLM_SUPPORT_AUDIO_OUTPUT", True):
            prompt = prompt_manager.get_agent_prompt()

        self.assertIn("send_voice_message", prompt)
        self.assertIn("terminal response tool", prompt)
        self.assertIn("do not write a final text reply after it", prompt)
        self.assertIn("text fallback and still completes the reply", prompt)

    def test_core_prompt_describes_voice_input_metadata(self):
        """核心提示词应说明结构化消息中的语音输入元信息。"""
        prompt = prompt_manager.get_agent_prompt()

        self.assertIn("input.mode", prompt)
        self.assertIn("voice", prompt)
        self.assertIn("`message` contains its transcript", prompt)

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
