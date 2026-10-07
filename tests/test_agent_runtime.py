"""Agent 运行时配置与已废弃活动目录的 pytest 回归。"""

import os
import shutil
import sqlite3
import textwrap
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.agent.runtime import AgentRuntimeManager


@pytest.fixture
def context(tmp_path):
    """为每项测试创建独立运行目录及配置管理器工厂。"""
    agent_root = tmp_path / "agent"
    defaults_root = Path(__file__).resolve().parents[1] / "app" / "agent" / "defaults"
    return SimpleNamespace(temp_root=tmp_path, agent_root=agent_root, defaults_root=defaults_root,
                           _manager=lambda: AgentRuntimeManager(agent_root_dir=agent_root, bundled_defaults_dir=defaults_root))


@pytest.fixture
def signature_manager(context):
    """签名测试使用临时配置与默认模板，避免改动仓库内置文件。"""
    defaults_root = context.temp_root / 'defaults'
    shutil.copytree(context.defaults_root, defaults_root)
    manager = AgentRuntimeManager(agent_root_dir=context.agent_root, bundled_defaults_dir=defaults_root)
    manager.ensure_layout()
    return manager


def test_signature_ignores_history_wal_lifecycle(signature_manager, monkeypatch):
    """历史库及 WAL 边车的创建、写入和删除不应改变配置签名。"""
    manager = signature_manager
    monkeypatch.setattr(manager, '_signature_check_interval', 0)
    config = manager.load_runtime_config()
    signature = manager.current_signature()
    database = manager.runtime_dir / 'history' / 'users' / 'test-user' / 'state.db'
    database.parent.mkdir(parents=True)

    for value in ('first message', 'second message'):
        with closing(sqlite3.connect(database)) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE IF NOT EXISTS messages (text TEXT)')
            connection.execute('INSERT INTO messages VALUES (?)', (value,))
            connection.commit()
            assert database.with_name('state.db-wal').exists()
            assert database.with_name('state.db-shm').exists()
            assert manager.current_signature() == signature
            assert manager.load_runtime_config() is config

        assert not database.with_name('state.db-wal').exists()
        assert not database.with_name('state.db-shm').exists()
        assert manager.current_signature() == signature

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 2


def test_signature_does_not_traverse_history_directory(signature_manager, monkeypatch):
    """历史目录应在递归前排除，避免扫描持续增长且并发变化的用户数据。"""
    manager = signature_manager
    signature = manager._build_signature()
    history_root = manager.runtime_dir / 'history'
    (history_root / 'users' / 'test-user').mkdir(parents=True)
    original_scandir = os.scandir

    def guarded_scandir(path):
        """以文件系统边界断言扫描器没有进入历史目录。"""
        assert not Path(path).is_relative_to(history_root)
        return original_scandir(path)

    monkeypatch.setattr(os, 'scandir', guarded_scandir)
    assert manager._build_signature() == signature


@pytest.mark.parametrize('root_name', ['runtime_dir', 'bundled_defaults_dir'])
def test_signature_tolerates_file_removed_after_type_check(signature_manager, monkeypatch, root_name):
    """确定性复现文件类型检查成功、读取元数据前文件被并发删除的竞态。"""
    manager = signature_manager
    signature = manager._build_signature()
    transient = getattr(manager, root_name) / 'transient.md'
    transient.write_text('temporary context', encoding='utf-8')
    original_is_file = Path.is_file
    removed = []

    def remove_after_type_check(path):
        """在扫描器确认文件类型后删除文件，复现 Issue 中的消失时序。"""
        result = original_is_file(path)
        if path == transient and result:
            path.unlink()
            removed.append(path)
        return result

    monkeypatch.setattr(Path, 'is_file', remove_after_type_check)
    assert manager._build_signature() == signature
    assert removed == [transient]


@pytest.mark.parametrize('root_name', ['runtime_dir', 'bundled_defaults_dir'])
@pytest.mark.parametrize('relative_path', [
    'CURRENT_PERSONA.md',
    'personas/default/PERSONA.md',
    'subagents/general-purpose/SUBAGENT.md',
    'extra/history/context.md',
])
def test_signature_tracks_configuration_changes(signature_manager, root_name, relative_path):
    """排除历史数据库后，人格、子代理和任意额外上下文仍参与增改删检测。"""
    manager = signature_manager
    path = getattr(manager, root_name) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    absent = manager._build_signature()
    path.write_text('context', encoding='utf-8')
    created = manager._build_signature()
    assert created != absent
    path.write_text('updated context', encoding='utf-8')
    updated = manager._build_signature()
    assert updated != created
    path.unlink()
    assert manager._build_signature() == absent


def test_default_root_uses_runtime_settings_service(context):
    """未显式传入目录时，管理器应读取组合根提供的配置目录。"""
    config_root = context.temp_root / 'configured'
    service = SimpleNamespace(get=lambda key: config_root if key == 'CONFIG_PATH' else None)
    with patch('app.agent.runtime.get_runtime_settings', return_value=service):
        manager = AgentRuntimeManager(bundled_defaults_dir=context.defaults_root)
    assert manager.agent_root_dir == config_root / 'agent'


def test_load_runtime_config_syncs_defaults_and_parses_sections(context):
    """验证运行时配置、人格选择及原有目录行为。"""
    manager = context._manager()
    runtime_config = manager.load_runtime_config()
    assert runtime_config.active_persona == 'default'
    assert 'professional, concise, restrained' in runtime_config.persona.text
    assert runtime_config.persona.persona_id == 'default'
    assert 'concise' in [persona.persona_id for persona in runtime_config.available_personas]
    assert (context.agent_root / 'runtime' / 'CURRENT_PERSONA.md').exists()
    assert (context.agent_root / 'runtime' / 'personas' / 'default' / 'PERSONA.md').exists()
    assert (context.agent_root / 'runtime' / 'subagents' / 'general-purpose' / 'SUBAGENT.md').exists()
    assert [subagent.subagent_id for subagent in runtime_config.available_subagents] == ['general-purpose']


def test_legacy_root_markdown_is_migrated_to_memory_directory(context):
    """验证运行时配置、人格选择及原有目录行为。"""
    context.agent_root.mkdir(parents=True, exist_ok=True)
    legacy_memory = context.agent_root / 'MEMORY.md'
    legacy_memory.write_text('# Legacy Memory\n', encoding='utf-8')
    legacy_persona = context.agent_root / 'CURRENT_PERSONA.md'
    legacy_persona.write_text(textwrap.dedent('                ---\n                version: 3\n                active_persona: default\n                extra_context_files: []\n                deprecated_phrases: []\n                ---\n                '), encoding='utf-8')
    manager = context._manager()
    manager.ensure_layout()
    assert not legacy_memory.exists()
    assert (context.agent_root / 'memory' / 'MEMORY.md').exists()
    assert not legacy_persona.exists()
    assert (context.agent_root / 'runtime' / 'CURRENT_PERSONA.md').exists()


def test_obsolete_runtime_files_are_deleted_instead_of_migrated(context):
    """验证运行时配置、人格选择及原有目录行为。"""
    context.agent_root.mkdir(parents=True, exist_ok=True)
    obsolete_root = context.agent_root / 'USER_PREFERENCES.md'
    obsolete_root.write_text('# Obsolete\n', encoding='utf-8')
    obsolete_runtime = context.agent_root / 'runtime' / 'system_tasks' / 'SYSTEM_TASKS.md'
    obsolete_runtime.parent.mkdir(parents=True, exist_ok=True)
    obsolete_runtime.write_text('# Obsolete Tasks\n', encoding='utf-8')
    obsolete_persona = context.agent_root / 'runtime' / 'personas' / 'default' / 'AGENT_PROFILE.md'
    obsolete_persona.parent.mkdir(parents=True, exist_ok=True)
    obsolete_persona.write_text('# Obsolete Persona\n', encoding='utf-8')
    manager = context._manager()
    manager.ensure_layout()
    assert not obsolete_root.exists()
    assert not obsolete_runtime.exists()
    assert not obsolete_persona.exists()
    assert not (context.agent_root / 'memory' / 'USER_PREFERENCES.md').exists()


def test_activity_history_is_preserved_without_creating_new_logs(context):
    """旧活动历史保持原地，运行时不再创建新活动目录。"""
    old_activity = context.agent_root / 'activity'
    old_activity.mkdir(parents=True, exist_ok=True)
    old_log = old_activity / '2026-06-18.md'
    old_log.write_text('# 旧活动日志\n', encoding='utf-8')
    manager = context._manager()
    manager.ensure_layout()
    assert not (context.agent_root / 'memory' / 'activity').exists()
    assert old_log.exists()
    assert not hasattr(manager, 'activity_dir')


def test_render_prompt_sections_uses_active_persona(context):
    """验证运行时配置、人格选择及原有目录行为。"""
    manager = context._manager()
    runtime_config = manager.load_runtime_config()
    sections = runtime_config.render_prompt_sections()
    assert '<agent_persona>' in sections
    assert 'Active persona: `default`' in sections
    assert '`persona` with `action=list`' in sections
    assert 'Available personas:' not in sections
    assert 'Available subagents:' not in sections
    assert 'Available subagents:' not in sections


def test_set_active_persona_supports_id_and_alias(context):
    """验证运行时配置、人格选择及原有目录行为。"""
    manager = context._manager()
    manager.load_runtime_config()
    guide_config = manager.set_active_persona('guide')
    assert guide_config.active_persona == 'guide'
    assert guide_config.persona.label == '说明型'
    concise_config = manager.set_active_persona('简洁')
    assert concise_config.active_persona == 'concise'
    assert 'active_persona: concise' in concise_config.current_persona_path.read_text(encoding='utf-8')


def test_invalid_user_runtime_config_falls_back_to_bundled_defaults(context):
    """验证运行时配置、人格选择及原有目录行为。"""
    manager = context._manager()
    manager.ensure_layout()
    invalid_current = context.agent_root / 'runtime' / 'CURRENT_PERSONA.md'
    invalid_current.write_text(textwrap.dedent('                ---\n                version: 3\n                active_persona: broken\n                extra_context_files: []\n                deprecated_phrases: []\n                ---\n                '), encoding='utf-8')
    manager.invalidate_cache()
    runtime_config = manager.load_runtime_config()
    assert runtime_config.used_fallback
    assert runtime_config.active_persona == 'default'
    assert '已回退到内置默认配置' in runtime_config.warnings[0]


def test_deprecated_phrase_warning_is_reported(context):
    """验证运行时配置、人格选择及原有目录行为。"""
    context.agent_root.mkdir(parents=True, exist_ok=True)
    runtime_root = context.agent_root / 'runtime'
    shutil.copytree(context.defaults_root, runtime_root)
    current_persona = runtime_root / 'CURRENT_PERSONA.md'
    current_persona.write_text(textwrap.dedent('                ---\n                version: 3\n                active_persona: default\n                extra_context_files: []\n                deprecated_phrases:\n                  - professional, concise, restrained\n                ---\n                '), encoding='utf-8')
    manager = context._manager()
    manager.invalidate_cache()
    runtime_config = manager.load_runtime_config()
    assert any(('professional, concise, restrained' in warning for warning in runtime_config.warnings))
