import textwrap
from pathlib import Path

import app.agent.middleware.subagents as subagent_module
from app.agent.runtime import AgentRuntimeManager


def _write_current_persona(defaults_root: Path) -> None:
    """写入最小可用的人格激活配置。"""
    defaults_root.mkdir(parents=True, exist_ok=True)
    (defaults_root / "CURRENT_PERSONA.md").write_text(
        textwrap.dedent(
            """\
            ---
            version: 3
            active_persona: default
            extra_context_files: []
            deprecated_phrases: []
            ---
            # CURRENT_PERSONA
            """
        ),
        encoding="utf-8",
    )
    persona_dir = defaults_root / "personas" / "default"
    persona_dir.mkdir(parents=True, exist_ok=True)
    (persona_dir / "PERSONA.md").write_text(
        textwrap.dedent(
            """\
            ---
            version: 1
            persona_id: default
            label: 默认
            description: 默认人格
            aliases: []
            ---
            # PERSONA

            测试人格。
            """
        ),
        encoding="utf-8",
    )


def _write_subagent(
    root: Path,
    subagent_id: str,
    *,
    version: int = 1,
    description: str = "测试子代理",
    body: str = "测试子代理提示。",
) -> Path:
    """写入一个子代理定义文件。"""
    subagent_dir = root / "subagents" / subagent_id
    subagent_dir.mkdir(parents=True, exist_ok=True)
    path = subagent_dir / "SUBAGENT.md"
    path.write_text(
        textwrap.dedent(
            f"""\
            ---
            version: {version}
            subagent_id: {subagent_id}
            label: 测试
            description: {description}
            include_tags:
              - media
              - web
            exclude_tags:
              - write
              - message
            ---
            # SUBAGENT

            {body}
            """
        ),
        encoding="utf-8",
    )
    return path


def test_runtime_syncs_and_parses_subagent_definitions(tmp_path):
    """运行时应同步并解析 defaults/subagents 下的子代理定义。"""
    defaults_root = tmp_path / "defaults"
    _write_current_persona(defaults_root)
    _write_subagent(
        defaults_root,
        "custom-reader",
        description="Custom reader subagent.",
        body="Only inspect custom media signals.",
    )

    manager = AgentRuntimeManager(
        agent_root_dir=tmp_path / "agent",
        bundled_defaults_dir=defaults_root,
    )
    runtime_config = manager.load_runtime_config()

    copied_path = (
        tmp_path
        / "agent"
        / "runtime"
        / "subagents"
        / "custom-reader"
        / "SUBAGENT.md"
    )
    subagent = runtime_config.available_subagents[0]
    assert copied_path.exists()
    assert subagent.subagent_id == "custom-reader"
    assert subagent.description == "Custom reader subagent."
    assert subagent.include_tags == ["media", "web"]
    assert subagent.exclude_tags == ["write", "message"]
    assert "Only inspect custom media signals." in subagent.text


def test_runtime_updates_bundled_subagent_when_version_increases(tmp_path):
    """内置子代理版本升高时应覆盖用户目录里的旧版副本。"""
    defaults_root = tmp_path / "defaults"
    _write_current_persona(defaults_root)
    _write_subagent(
        defaults_root,
        "custom-reader",
        version=1,
        body="version one",
    )

    manager = AgentRuntimeManager(
        agent_root_dir=tmp_path / "agent",
        bundled_defaults_dir=defaults_root,
    )
    manager.ensure_layout()

    _write_subagent(
        defaults_root,
        "custom-reader",
        version=2,
        body="version two",
    )
    manager.invalidate_cache()
    runtime_config = manager.load_runtime_config()

    subagent = runtime_config.available_subagents[0]
    assert subagent.version == 2
    assert "version two" in subagent.text


def test_middleware_profiles_are_loaded_from_runtime_config(monkeypatch, tmp_path):
    """子代理中间件应从运行时 YAML 定义生成 profile。"""
    defaults_root = tmp_path / "defaults"
    _write_current_persona(defaults_root)
    _write_subagent(
        defaults_root,
        "custom-reader",
        description="Runtime custom reader.",
        body="Runtime-only prompt.",
    )
    manager = AgentRuntimeManager(
        agent_root_dir=tmp_path / "agent",
        bundled_defaults_dir=defaults_root,
    )

    monkeypatch.setattr(subagent_module, "agent_runtime_manager", manager)
    subagent_module._builtin_subagent_profiles.cache_clear()
    subagent_module.builtin_subagent_names.cache_clear()

    profiles = subagent_module._builtin_subagent_profiles()

    assert [profile.name for profile in profiles] == ["custom-reader"]
    assert profiles[0].description == "Runtime custom reader."
    assert profiles[0].include_tags == frozenset({"media", "web"})
    assert "Runtime-only prompt." in profiles[0].prompt
