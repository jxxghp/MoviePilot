"""用户独立的技能维护：原子批次、所有权保护与本轮写前读取。"""

import hashlib
import json
import re
import shutil
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml  # type: ignore[import-untyped]

from app.agent.learning.files import contained_path, mutation_lock, read_text, write_text
from app.agent.learning.matching import format_no_match_hint, fuzzy_find_and_replace
from app.agent.learning.schema import (
    CreateSkill,
    DeleteSkill,
    ManageSkillsInput,
    PatchSkill,
    RemoveSkillFile,
    RewriteSkill,
    SkillOperation,
    WriteSkillFile,
)
from app.agent.learning.threats import first_threat_message
from app.agent.skills.metadata import SkillMetadata, parse_skill_metadata


def validate_document(name: str, content: str, *, new: bool = False) -> SkillMetadata:
    """先验证可被现有加载器识别，拒绝通过技能前言扩展工具或 API 权限。"""
    metadata = parse_skill_metadata(content, f'{name}/SKILL.md', name)
    if metadata is None or metadata['name'] != name:
        raise ValueError('SKILL.md 需要与名称一致的 name、description 及 YAML 前言')
    parts = re.split(r'^---\s*$', content, maxsplit=2, flags=re.MULTILINE)
    if len(parts) != 3 or not parts[2].strip():
        raise ValueError('SKILL.md 需要实际操作说明')
    frontmatter = yaml.safe_load(parts[1])
    description = frontmatter.get('description')
    if not isinstance(description, str) or not isinstance(frontmatter.get('name'), str):
        raise ValueError('技能名称和描述必须是字符串')
    if len(content) > 100_000 or len(description) > (60 if new else 1024):
        raise ValueError('技能超出正文或路由描述预算；新技能 description 最多60字符')
    if 'allowed-tools' in frontmatter or 'allowed-api-operations' in frontmatter:
        raise ValueError('个人学习技能不能声明或扩大工具及 API 权限')
    return metadata


class SkillLibrary:
    """只写当前用户个人库；公共、外部与无来源记录的技能对后台只读。"""

    def __init__(self, root: Path, *, public_roots: tuple[Path, ...] = (), background: bool = False,
                 source: dict[str, Any] | None = None, cancelled: Callable[[], bool] = lambda: False) -> None:
        """宿主绑定目录、执行来源与取消状态，模型参数不参与身份和所有权判断。"""
        self.root = root
        self.public_roots = public_roots
        self.background = background
        self.source = dict(source or {})
        self.cancelled = cancelled
        self._reads: dict[str, str] = {}

    def reset_reads(self) -> None:
        """新前台轮次清除旧读取标记，后台复盘使用新实例单独登记。"""
        self._reads.clear()

    @staticmethod
    def _documents(root: Path) -> list[Path]:
        """只发现技能及一层分类目录，隐藏事务、其他用户和符号链接不进入目录。"""
        if not root.is_dir() or root.is_symlink():
            return []
        result = []
        for path in sorted([*root.glob('*/SKILL.md'), *root.glob('*/*/SKILL.md')]):
            relative = path.relative_to(root)
            if any(part.startswith('.') for part in relative.parts):
                continue
            contained_path(root, relative.as_posix())
            result.append(path)
            if len(result) >= 1000:
                break
        return result

    def catalog(self) -> list[SkillMetadata]:
        """个人技能不覆盖公共同名项，下一次加载即可见到已提交的技能。"""
        result: dict[str, SkillMetadata] = {}
        for root in (*self.public_roots, self.root):
            for path in self._documents(root):
                meta = parse_skill_metadata(read_text(path), str(path), path.parent.name)
                if meta is not None:
                    result.setdefault(meta['name'], meta)
        return list(result.values())

    def _find(self, name: str) -> Path | None:
        """优先公共目录定位同名项，不能以同名个人技能冒充既有授权技能。"""
        for meta in self.catalog():
            if meta['name'] == name:
                return Path(meta['path']).parent
        return None

    @staticmethod
    def _target(directory: Path, file_path: str) -> Path:
        """支持文件仅能位于明确允许的包内目录，所有权 sidecar 不可由工具改写。"""
        parts = Path(file_path).parts
        skill_document = bool(parts and parts[-1] == 'SKILL.md' and len(parts) in {1, 2})
        if not skill_document and (len(parts) < 2 or parts[0] not in {'references', 'templates', 'scripts', 'assets'}):
            raise ValueError('支持文件必须位于 references/templates/scripts/assets')
        return contained_path(directory, file_path)

    def view(self, name: str, file_path: str = 'SKILL.md') -> dict[str, Any]:
        """实际读完整文件后记录版本；主对话曾引用文件不等于本次复盘已读。"""
        with mutation_lock(self.root):
            directory = self._find(name)
            if directory is None:
                raise ValueError('技能不存在')
            target = self._target(directory, file_path)
            content = read_text(target)
            self._reads[str(target)] = hashlib.sha256(content.encode()).hexdigest()
            return dict(success=True, name=name, file_path=file_path, content=content,
                        editable=directory.is_relative_to(self.root), source='personal' if directory.is_relative_to(self.root) else 'public')

    def _ownership(self) -> dict[str, Any]:
        """损坏的来源记录不能被当作空库从而取得后台写入权限。"""
        path = contained_path(self.root, '.ownership.json')
        if not path.exists():
            return {}
        value = json.loads(read_text(path))
        if not isinstance(value, dict):
            raise ValueError('技能来源记录损坏')
        return value

    def _guard(self, name: str, directory: Path, owners: dict[str, Any]) -> None:
        """来源与 pinned 标志由宿主 sidecar 判定；未知来源的后台写入拒绝执行。"""
        if not directory.is_relative_to(self.root):
            raise ValueError('公共或外部技能受保护；不能由个人学习工具修改')
        record = owners.get(name)
        if self.background and (not isinstance(record, dict) or record.get('created_by') != 'agent' or record.get('pinned')):
            raise ValueError('后台只能维护未固定的 agent 自建技能；用户技能受保护')

    def _check_read(self, path: Path) -> None:
        """后台必须本轮读取准确目标，读后被修改的文件也必须重新读取。"""
        if self.background and path.exists():
            digest = hashlib.sha256(read_text(path).encode()).hexdigest()
            if self._reads.get(str(path)) != digest:
                raise ValueError('写前请用 skill_view 重新读取该文件，再基于当前内容修改')

    def _plan(self, operations: list[SkillOperation], owners: dict[str, Any]) -> dict[Path, str | None]:
        """先在内存中完整验证批次，后续失败不应留下前序操作的半成品。"""
        writes: dict[Path, str | None] = {}
        locations = {item['name']: Path(item['path']).parent for item in self.catalog()}
        content: str | None
        for operation in operations:
            if self.cancelled():
                raise RuntimeError('后台学习已被前台任务取消')
            name = operation.name
            directory = locations.get(name)
            if isinstance(operation, CreateSkill):
                if directory is not None:
                    raise ValueError('同名技能已存在，请读取后修改而不是重复创建')
                relative = f'{operation.category}/{name}' if operation.category else name
                directory = contained_path(self.root, relative)
                if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
                    raise ValueError('目标目录已存在，不能自动接管')
                locations[name] = directory
                owners[name] = dict(created_by='agent' if self.background else 'user', pinned=False, source=self.source)
                target, content = directory / 'SKILL.md', operation.content
                validate_document(name, content, new=True)
            else:
                if directory is None:
                    raise ValueError('技能不存在；create 必须位于该技能其他操作之前')
                self._guard(name, directory, owners)
                target = self._target(directory, getattr(operation, 'file_path', None) or 'SKILL.md')
                self._check_read(target)
                content = self._new_content(operation, target, writes)
            if target in writes and not isinstance(operation, PatchSkill):
                raise ValueError('批次中重复覆盖同一文件会丢弃前序修改；请改为局部 patch')
            if content is not None and target == directory / 'SKILL.md':
                validate_document(name, content)
            if content is not None and (threat := first_threat_message(content)):
                raise ValueError(threat)
            writes[target] = content
            record = owners.setdefault(name, dict(created_by='user', pinned=False))
            if not isinstance(record, dict):
                raise ValueError('技能来源记录损坏')
            record['last_source'] = self.source
        return writes

    @staticmethod
    def _new_content(operation: SkillOperation, target: Path, writes: dict[Path, str | None]) -> str | None:
        """依次尝试九级匹配、歧义拒绝和转义保护，保留原文缩进及 Unicode。"""
        if isinstance(operation, RewriteSkill):
            return operation.content
        if isinstance(operation, WriteSkillFile):
            return operation.file_content
        if isinstance(operation, RemoveSkillFile):
            if target.name == 'SKILL.md' or not target.is_file():
                raise ValueError('只能移除已存在的支持文件；整项删除使用 delete')
            return None
        if not isinstance(operation, PatchSkill):
            raise ValueError('不支持的技能操作')
        original = writes[target] if target in writes else read_text(target)
        if original is None:
            raise ValueError('不能修改已被移除的文件')
        updated, count, _, error = fuzzy_find_and_replace(
            original, operation.old_string, operation.new_string, operation.replace_all,
        )
        if error:
            raise ValueError(error + format_no_match_hint(error, count, operation.old_string, original))
        if len(updated) > 100_000:
            raise ValueError('技能文件超过100000字符预算')
        return updated

    def manage(self, request: ManageSkillsInput) -> dict[str, Any]:
        """有序批次共用写锁和回滚边界，只有实际落盘才返回成功回执。"""
        with mutation_lock(self.root):
            owners = self._ownership()
            deletes = [operation for operation in request.operations if isinstance(operation, DeleteSkill)]
            if deletes:
                if len(request.operations) != 1:
                    raise ValueError('delete 必须独占批次')
                return self._delete(deletes[0], owners)
            writes = self._plan(list(request.operations), owners)
            writes[contained_path(self.root, '.ownership.json')] = json.dumps(owners, ensure_ascii=False)
            self._commit(writes)
            return dict(success=True, applied=len(request.operations), skills=list(dict.fromkeys(op.name for op in request.operations)))

    def _commit(self, writes: dict[Path, str | None]) -> None:
        """保留每个目标原文，发生普通 I/O 错误或取消时恢复已写文件。"""
        originals = {path: read_text(path) if path.exists() else None for path in writes}
        new_directories = {parent for path in writes for parent in path.parents
                           if parent.is_relative_to(self.root) and not parent.exists()}
        applied = []
        try:
            for path, content in writes.items():
                if self.cancelled():
                    raise RuntimeError('后台学习已被前台任务取消')
                applied.append(path)
                if content is None:
                    path.unlink()
                else:
                    write_text(path, content)
            if self.cancelled():
                raise RuntimeError('后台学习已被前台任务取消')
        except BaseException:
            for path in reversed(applied):
                previous = originals[path]
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    write_text(path, previous)
            for directory in sorted(new_directories, key=lambda item: len(item.parts), reverse=True):
                with suppress(OSError):
                    directory.rmdir()
            raise

    def _delete(self, operation: DeleteSkill, owners: dict[str, Any]) -> dict[str, Any]:
        """后台只接受有合并目标的可恢复归档，前台删除先移入隔离目录。"""
        directory = self._find(operation.name)
        if directory is None:
            raise ValueError('技能不存在')
        self._guard(operation.name, directory, owners)
        self._check_read(directory / 'SKILL.md')
        if owners.get(operation.name, {}).get('pinned'):
            raise ValueError('固定技能受保护，请先显式 unpin')
        if self.background and not operation.absorbed_into:
            raise ValueError('后台删除必须提供已吸收内容的合并目标 absorbed_into')
        if operation.absorbed_into and (operation.absorbed_into == operation.name or self._find(operation.absorbed_into) is None):
            raise ValueError('合并目标必须是另一项已存在的技能')
        if self.cancelled():
            raise RuntimeError('后台学习已被前台任务取消')
        if self.background:
            return self._archive(operation, directory, owners)
        archive = contained_path(self.root, f'.deleted-{uuid4().hex}')
        directory.rename(archive)
        try:
            owners.pop(operation.name, None)
            write_text(contained_path(self.root, '.ownership.json'), json.dumps(owners, ensure_ascii=False))
        except BaseException:
            archive.rename(directory)
            raise
        try:
            shutil.rmtree(archive)
        except OSError:
            return dict(success=True, deleted=operation.name, absorbed_into=operation.absorbed_into, cleanup_pending=True)
        return dict(success=True, deleted=operation.name, absorbed_into=operation.absorbed_into)

    def _archive(self, operation: DeleteSkill, directory: Path, owners: dict[str, Any]) -> dict[str, Any]:
        """保存完整技能包及原所有权，失败时恢复原目录，不把归档当硬删除。"""
        archive = contained_path(self.root, f'.archive/{operation.name}')
        if archive.exists():
            stamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
            archive = contained_path(self.root, f'.archive/{operation.name}-{stamp}')
        if archive.exists() or (directory / '.archive-owner.json').exists():
            raise ValueError('归档目标已存在，请稍后重试')
        record = owners.pop(operation.name)
        record['absorbed_into'] = operation.absorbed_into
        archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.rename(archive)
        try:
            self._commit({archive / '.archive-owner.json': json.dumps(record, ensure_ascii=False),
                          contained_path(self.root, '.ownership.json'): json.dumps(owners, ensure_ascii=False)})
        except BaseException:
            archive.rename(directory)
            raise
        return dict(success=True, archived=operation.name, absorbed_into=operation.absorbed_into)

    def _restore(self, name: str) -> dict[str, Any]:
        """用户恢复优先精确名称，其次最新时间后缀；拒绝覆盖现有或公共技能。"""
        destination = contained_path(self.root, name)
        if self._find(name) is not None or destination.exists():
            raise ValueError('同名技能或目标目录已存在，不能覆盖恢复')
        archive_root = contained_path(self.root, '.archive')
        exact = contained_path(archive_root, name)
        candidates = [exact] if exact.is_dir() else sorted(
            (path for path in archive_root.glob(f'{name}-*')
             if re.fullmatch(re.escape(name) + r'-\d{14}', path.name)), reverse=True,
        )
        if not candidates:
            raise ValueError('未找到该技能的归档')
        archive = contained_path(archive_root, candidates[0].name)
        record = json.loads(read_text(contained_path(archive, '.archive-owner.json')))
        if not isinstance(record, dict):
            raise ValueError('归档来源记录损坏')
        validate_document(name, read_text(contained_path(archive, 'SKILL.md')))
        owners = self._ownership()
        owners[name] = record
        archive.rename(destination)
        try:
            self._commit({destination / '.archive-owner.json': None,
                          contained_path(self.root, '.ownership.json'): json.dumps(owners, ensure_ascii=False)})
        except BaseException:
            destination.rename(archive)
            raise
        return dict(success=True, restored=name)

    def control(self, name: str, action: str) -> dict[str, Any]:
        """宿主显式用户命令变更个人技能的自动维护资格，公共技能始终不能接管。"""
        with mutation_lock(self.root):
            if action == 'restore':
                return self._restore(name)
            directory = self._find(name)
            if directory is None or not directory.is_relative_to(self.root):
                raise ValueError('只能管理当前用户已有的个人技能')
            owners = self._ownership()
            record = owners.setdefault(name, dict(created_by='user', pinned=False))
            if not isinstance(record, dict):
                raise ValueError('技能来源记录损坏')
            if action == 'adopt':
                record['created_by'] = 'agent'
            elif action in {'pin', 'unpin'}:
                record['pinned'] = action == 'pin'
            else:
                raise ValueError('技能命令支持 adopt/pin/unpin')
            write_text(contained_path(self.root, '.ownership.json'), json.dumps(owners, ensure_ascii=False))
            return dict(success=True, name=name, action=action)
