"""个人学习技能的批次、写前读取和所有权语义回归。"""

import json

import pytest
from pydantic import ValidationError

from app.agent.learning import skills
from app.agent.learning.schema import ManageSkillsInput
from app.agent.learning.skills import SkillLibrary, validate_document


def document(name='workflow', body='检查输入，再执行，最后验证。'):
    """生成最小但真实可加载的技能文档。"""
    return f'---\nname: {name}\ndescription: 常规工作流程\n---\n{body}\n'


def manage(library, *operations):
    """经过真实工具参数模型执行技能批次。"""
    return library.manage(ManageSkillsInput(operations=list(operations)))


def create(library, name='workflow'):
    """初始化技能供所有权与失败回归使用。"""
    return manage(library, dict(action='create', name=name, content=document(name)))


def test_batch_read_before_write_and_recovery(tmp_path):
    """后台不能使用父对话读取痕迹，重复写冲突不会污染先前操作。"""
    library = SkillLibrary(tmp_path, background=True)
    create(library)
    path = tmp_path / 'workflow/SKILL.md'
    before = path.read_text()
    patch = dict(action='patch', name='workflow', old_string='检查输入', new_string='先校验输入')
    with pytest.raises(ValueError, match='写前'):
        manage(library, patch)
    library.view('workflow')
    with pytest.raises(ValueError, match='重复覆盖'):
        manage(library, patch, dict(action='patch', name='workflow', content=document()))
    assert path.read_text() == before
    manage(library, patch)
    assert '先校验输入' in path.read_text()
    with pytest.raises(ValueError, match='写前'):
        manage(library, dict(action='patch', name='workflow', content=document()))


def test_support_files_and_failed_batch_are_atomic(tmp_path, monkeypatch):
    """写到一半的实际 I/O 故障回滚主文档、支持文件和所有权记录。"""
    library = SkillLibrary(tmp_path, background=True)
    real_write = skills.write_text

    def fail_once(path, content):
        """模拟最后的 sidecar 落盘失败，随后允许补偿写入。"""
        if path.name == '.ownership.json':
            monkeypatch.setattr(skills, 'write_text', real_write)
            raise OSError('disk full')
        real_write(path, content)

    monkeypatch.setattr(skills, 'write_text', fail_once)
    with pytest.raises(OSError, match='disk full'):
        manage(library, dict(action='create', name='workflow', content=document()),
               dict(action='write_file', name='workflow', file_path='references/check.md', file_content='核验回执'))
    assert not (tmp_path / 'workflow/SKILL.md').exists()
    assert not (tmp_path / '.ownership.json').exists()
    # 回滚留下的空目录可以重试；不能接管已有文件的目录。
    create(library)
    assert library.view('workflow')['content'] == document()


def test_public_user_pinned_and_other_users_remain_protected(tmp_path):
    """后台只能更新当前用户 agent 自建项，公共同名项不能被个人库覆盖。"""
    public, alice, bob = (tmp_path / name for name in ('public', 'alice', 'bob'))
    create(SkillLibrary(public), 'shared')
    create(SkillLibrary(bob, background=True), 'bob-only')
    manual = SkillLibrary(alice)
    create(manual, 'manual')
    background = SkillLibrary(alice, public_roots=(public,), background=True)
    assert {item['name'] for item in background.catalog()} == {'shared', 'manual'}
    for name in ('shared', 'manual'):
        background.view(name)
        with pytest.raises(ValueError, match='保护'):
            manage(background, dict(action='delete', name=name))
    create(background)
    owners = json.loads((alice / '.ownership.json').read_text())
    owners['workflow']['pinned'] = True
    (alice / '.ownership.json').write_text(json.dumps(owners))
    background.view('workflow')
    with pytest.raises(ValueError, match='保护'):
        manage(background, dict(action='delete', name='workflow'))


def test_manual_skill_foreground_edit_and_symlink_rejection(tmp_path):
    """没有来源的手工技能允许前台维护，但不能跟随支持文件软链接。"""
    root = tmp_path / 'personal'
    path = root / 'workflow/SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text(document())
    library = SkillLibrary(root)
    manage(library, dict(action='patch', name='workflow', content=document(body='手工更新')))
    assert json.loads((root / '.ownership.json').read_text())['workflow']['created_by'] == 'user'
    (path.parent / 'references').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='符号链接'):
        manage(library, dict(action='write_file', name='workflow', file_path='references/escape', file_content='no'))
    with pytest.raises(ValueError, match='目录'):
        library.view('workflow', 'references/../../escape')


def test_document_validation_and_field_shapes():
    """不接受被解析器截断的描述或通过错误字段逃逸的权限声明。"""
    with pytest.raises(ValueError, match='预算'):
        validate_document('workflow', document().replace('常规工作流程', 'x' * 1025))
    with pytest.raises(ValueError, match='权限'):
        validate_document('workflow', document().replace('description:', 'allowed-tools: []\ndescription:'))
    with pytest.raises(ValidationError):
        ManageSkillsInput(operations=[dict(action='patch', name='workflow', file_content=document())])


def test_fuzzy_patch_preserves_unicode_and_refuses_ambiguity(tmp_path):
    """排版差异匹配保留 Unicode；多义锚点仍拒绝写入。"""
    library = SkillLibrary(tmp_path)
    manage(library, dict(action='create', name='workflow', content=document(body='  使用“旧名称” — 校验\n重复\n重复')))
    manage(library, dict(action='patch', name='workflow', old_string='使用"旧名称" -- 校验', new_string='使用"新名称" -- 校验'))
    assert '使用“新名称” — 校验' in library.view('workflow')['content']
    with pytest.raises(ValueError, match='matches'):
        manage(library, dict(action='patch', name='workflow', old_string='重复', new_string='修改'))


def test_cancelled_commit_rolls_back_and_delete_cleanup_is_truthful(tmp_path, monkeypatch):
    """取消不留下学习半成品，已完成删除的垃圾清理失败不能诱发重复执行。"""
    library = SkillLibrary(tmp_path, background=True)
    create(library)
    library.view('workflow')
    before = (tmp_path / 'workflow/SKILL.md').read_text()
    real_write = skills.write_text

    def cancel_after_write(path, content):
        """在第一份文档落盘后注入前台取消。"""
        real_write(path, content)
        library.cancelled = lambda: True

    monkeypatch.setattr(skills, 'write_text', cancel_after_write)
    with pytest.raises(RuntimeError, match='取消'):
        manage(library, dict(action='patch', name='workflow', content=document(body='不应留下')))
    assert (tmp_path / 'workflow/SKILL.md').read_text() == before
    library.cancelled = lambda: False
    monkeypatch.setattr(skills, 'write_text', real_write)

    def fail_cleanup(_path):
        """模拟隔离目录清理失败。"""
        raise OSError('busy')

    monkeypatch.setattr(skills.shutil, 'rmtree', fail_cleanup)
    result = manage(SkillLibrary(tmp_path), dict(action='delete', name='workflow'))
    assert result['success'] and result['cleanup_pending']
    assert library.catalog() == []


def test_background_consolidation_archives_complete_package_and_restores(tmp_path):
    """后台不能凭空裁剪技能，合并归档可恢复支持文件与来源且不能覆盖公共同名项。"""
    root, public = tmp_path / 'personal', tmp_path / 'public'
    library = SkillLibrary(root, public_roots=(public,), background=True)
    create(library)
    create(library, 'umbrella')
    manage(library, dict(action='write_file', name='workflow', file_path='references/evidence.md', file_content='已验证的操作步骤'))
    library.view('workflow')
    for target in (None, '', 'missing', 'workflow'):
        with pytest.raises(ValueError, match='合并目标'):
            manage(library, dict(action='delete', name='workflow', absorbed_into=target))
    result = manage(library, dict(action='delete', name='workflow', absorbed_into='umbrella'))
    assert result['archived'] == 'workflow'
    assert {item['name'] for item in library.catalog()} == {'umbrella'}
    create(SkillLibrary(public))
    with pytest.raises(ValueError, match='覆盖'):
        library.control('workflow', 'restore')
    manage(SkillLibrary(public), dict(action='delete', name='workflow'))
    assert library.control('workflow', 'restore')['restored'] == 'workflow'
    assert library.view('workflow', 'references/evidence.md')['content'] == '已验证的操作步骤'
    assert json.loads((root / '.ownership.json').read_text())['workflow']['created_by'] == 'agent'
    library.control('workflow', 'pin')
    with pytest.raises(ValueError, match='固定'):
        manage(SkillLibrary(root), dict(action='delete', name='workflow'))


def test_archive_failure_rolls_back_and_main_write_validates(tmp_path, monkeypatch):
    """归档过程中取消恢复原包，主文档 write_file 仍检查完整前言和权限。"""
    library = SkillLibrary(tmp_path, background=True)
    create(library)
    create(library, 'umbrella')
    library.view('workflow')
    manage(library, dict(action='write_file', name='workflow', file_path='SKILL.md', file_content=document(body='新版方法')))
    library.view('workflow')
    real_write = skills.write_text

    def cancel_after_archive_metadata(path, content):
        """归档元数据写入后模拟前台取消，不破坏补偿写入。"""
        real_write(path, content)
        library.cancelled = lambda: True

    monkeypatch.setattr(skills, 'write_text', cancel_after_archive_metadata)
    with pytest.raises(RuntimeError, match='取消'):
        manage(library, dict(action='delete', name='workflow', absorbed_into='umbrella'))
    assert '新版方法' in (tmp_path / 'workflow/SKILL.md').read_text()
    assert not (tmp_path / '.archive/workflow').exists()
    assert 'workflow' in json.loads((tmp_path / '.ownership.json').read_text())
    library.cancelled = lambda: False
    with pytest.raises(ValueError, match='前言'):
        manage(library, dict(action='write_file', name='workflow', file_path='SKILL.md', file_content='无前言'))
