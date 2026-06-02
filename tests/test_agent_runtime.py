import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from app.agent.runtime import AgentRuntimeManager


class TestAgentRuntimeConfig(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.temp_root = Path(self._tempdir.name)
        self.agent_root = self.temp_root / "agent"
        self.defaults_root = (
            Path(__file__).resolve().parents[1] / "app" / "agent" / "defaults"
        )

    def _manager(self) -> AgentRuntimeManager:
        return AgentRuntimeManager(
            agent_root_dir=self.agent_root,
            bundled_defaults_dir=self.defaults_root,
        )

    def test_load_runtime_config_syncs_defaults_and_parses_sections(self):
        manager = self._manager()

        runtime_config = manager.load_runtime_config()

        self.assertEqual(runtime_config.active_persona, "default")
        self.assertIn("professional, concise, restrained", runtime_config.persona.text)
        self.assertEqual(runtime_config.persona.persona_id, "default")
        self.assertIn(
            "concise",
            [persona.persona_id for persona in runtime_config.available_personas],
        )
        self.assertTrue((self.agent_root / "runtime" / "CURRENT_PERSONA.md").exists())
        self.assertTrue(
            (
                self.agent_root
                / "runtime"
                / "personas"
                / "default"
                / "PERSONA.md"
            ).exists()
        )

    def test_legacy_root_markdown_is_migrated_to_memory_directory(self):
        self.agent_root.mkdir(parents=True, exist_ok=True)
        legacy_memory = self.agent_root / "MEMORY.md"
        legacy_memory.write_text("# Legacy Memory\n", encoding="utf-8")
        legacy_persona = self.agent_root / "CURRENT_PERSONA.md"
        legacy_persona.write_text(
            textwrap.dedent(
                """\
                ---
                version: 3
                active_persona: default
                extra_context_files: []
                deprecated_phrases: []
                ---
                """
            ),
            encoding="utf-8",
        )

        manager = self._manager()
        manager.ensure_layout()

        self.assertFalse(legacy_memory.exists())
        self.assertTrue((self.agent_root / "memory" / "MEMORY.md").exists())
        self.assertFalse(legacy_persona.exists())
        self.assertTrue((self.agent_root / "runtime" / "CURRENT_PERSONA.md").exists())

    def test_obsolete_runtime_files_are_deleted_instead_of_migrated(self):
        self.agent_root.mkdir(parents=True, exist_ok=True)
        obsolete_root = self.agent_root / "USER_PREFERENCES.md"
        obsolete_root.write_text("# Obsolete\n", encoding="utf-8")

        obsolete_runtime = self.agent_root / "runtime" / "system_tasks" / "SYSTEM_TASKS.md"
        obsolete_runtime.parent.mkdir(parents=True, exist_ok=True)
        obsolete_runtime.write_text("# Obsolete Tasks\n", encoding="utf-8")

        obsolete_persona = (
            self.agent_root
            / "runtime"
            / "personas"
            / "default"
            / "AGENT_PROFILE.md"
        )
        obsolete_persona.parent.mkdir(parents=True, exist_ok=True)
        obsolete_persona.write_text("# Obsolete Persona\n", encoding="utf-8")

        manager = self._manager()
        manager.ensure_layout()

        self.assertFalse(obsolete_root.exists())
        self.assertFalse(obsolete_runtime.exists())
        self.assertFalse(obsolete_persona.exists())
        self.assertFalse((self.agent_root / "memory" / "USER_PREFERENCES.md").exists())

    def test_render_prompt_sections_uses_active_persona(self):
        manager = self._manager()
        runtime_config = manager.load_runtime_config()

        sections = runtime_config.render_prompt_sections()

        self.assertIn("<agent_persona>", sections)
        self.assertIn("Active persona: `default`", sections)
        self.assertIn("`guide`", sections)

    def test_set_active_persona_supports_id_and_alias(self):
        manager = self._manager()
        manager.load_runtime_config()

        guide_config = manager.set_active_persona("guide")
        self.assertEqual(guide_config.active_persona, "guide")
        self.assertEqual(guide_config.persona.label, "说明型")

        concise_config = manager.set_active_persona("简洁")
        self.assertEqual(concise_config.active_persona, "concise")
        self.assertIn("active_persona: concise", concise_config.current_persona_path.read_text(encoding="utf-8"))

    def test_invalid_user_runtime_config_falls_back_to_bundled_defaults(self):
        manager = self._manager()
        manager.ensure_layout()
        invalid_current = self.agent_root / "runtime" / "CURRENT_PERSONA.md"
        invalid_current.write_text(
            textwrap.dedent(
                """\
                ---
                version: 3
                active_persona: broken
                extra_context_files: []
                deprecated_phrases: []
                ---
                """
            ),
            encoding="utf-8",
        )
        manager.invalidate_cache()

        runtime_config = manager.load_runtime_config()

        self.assertTrue(runtime_config.used_fallback)
        self.assertEqual(runtime_config.active_persona, "default")
        self.assertIn("已回退到内置默认配置", runtime_config.warnings[0])

    def test_deprecated_phrase_warning_is_reported(self):
        self.agent_root.mkdir(parents=True, exist_ok=True)
        runtime_root = self.agent_root / "runtime"
        shutil.copytree(self.defaults_root, runtime_root)
        current_persona = runtime_root / "CURRENT_PERSONA.md"
        current_persona.write_text(
            textwrap.dedent(
                """\
                ---
                version: 3
                active_persona: default
                extra_context_files: []
                deprecated_phrases:
                  - professional, concise, restrained
                ---
                """
            ),
            encoding="utf-8",
        )

        manager = self._manager()
        manager.invalidate_cache()
        runtime_config = manager.load_runtime_config()

        self.assertTrue(
            any(
                "professional, concise, restrained" in warning
                for warning in runtime_config.warnings
            )
        )
