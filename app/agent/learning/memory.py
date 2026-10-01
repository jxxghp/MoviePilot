"""有界稳定记忆：整条修订、原子批次、后台删除提案和磁盘漂移保护。"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.agent.learning.files import contained_path, mutation_lock, read_text, write_text
from app.agent.learning.schema import MemoryInput, MemoryOperation
from app.agent.learning.threats import first_threat_message

ENTRY_DELIMITER = '\n§\n'
LIMITS = {'memory': 2200, 'user': 1375}


def parse_entries(raw: str) -> list[str]:
    """仅按完整分隔符拆条，正文中单独出现的 § 不会切断原文。"""
    return [part.strip() for part in raw.split(ENTRY_DELIMITER) if part.strip()]


def locate(entries: list[str], old_text: str, pinned: str | None = None) -> int:
    """整条精确匹配优先；不同内容的多处子串命中拒绝猜测，审批匹配原始整条。"""
    if pinned is not None:
        if pinned not in entries:
            raise ValueError('待确认的原记忆已发生变化，提案保留，请重新核对')
        return entries.index(pinned)
    if not old_text.strip():
        raise ValueError('replace/remove 需要 old_text，且 replace 会替换整条记忆')
    indexes = [i for i, entry in enumerate(entries) if entry == old_text]
    indexes = indexes or [i for i, entry in enumerate(entries) if old_text in entry]
    if not indexes or len({entries[i] for i in indexes}) > 1:
        raise ValueError('old_text 未唯一匹配，请重新读取记忆并使用准确片段')
    return indexes[0]


def apply_operations(entries: list[str], operations: list[MemoryOperation],
                     pinned: list[str | None] | None = None) -> tuple[list[str], list[str | None]]:
    """在副本上顺序验证所有操作，返回新列表与每次删除或替换的完整旧条目。"""
    working = list(entries)
    previous: list[str | None] = []
    for i, operation in enumerate(operations):
        content = (operation.content if operation.content is not None else operation.new_text or '').strip()
        if operation.action != 'remove':
            if not content:
                raise ValueError('add/replace 需要非空 content；删除请使用 remove')
            if threat := first_threat_message(content, scope='strict'):
                raise ValueError(threat)
        if operation.action == 'add':
            if content not in working:
                working.append(content)
            previous.append(None)
        else:
            index = locate(working, (operation.old_text or '').strip(), pinned[i] if pinned else None)
            previous.append(working[index])
            working[index:index + 1] = [content] if operation.action == 'replace' else []
    return working, previous


class MemoryStore:
    """一用户一目录；复盘只有新增直接生效，破坏性操作保留精确原文等待人工决定。"""

    def __init__(self, root: Path, *, background: bool = False,
                 cancelled: Callable[[], bool] = lambda: False) -> None:
        """身份、后台标志与取消令牌由宿主绑定，工具参数不能改写。"""
        self.root, self.background, self.cancelled = root, background, cancelled
        self.failures = 0
        self._snapshot: dict[str, str] | None = None

    def reset_turn(self) -> None:
        """只在真实新轮次重置连续失败预算。"""
        self.failures = 0

    def _path(self, target: str) -> Path:
        """只允许两个稳定记忆目标，不将模型字符串当文件路径使用。"""
        if target not in LIMITS:
            raise ValueError('记忆目标必须是 memory 或 user')
        return contained_path(self.root, 'USER.md' if target == 'user' else 'MEMORY.md')

    def _read(self, target: str) -> tuple[str, list[str]]:
        """读取失败必须保留原文件，不能把异常当空列表覆盖。"""
        path = self._path(target)
        raw = read_text(path) if path.exists() else ''
        return raw, list(dict.fromkeys(parse_entries(raw)))

    def snapshot(self) -> dict[str, str]:
        """冻结正文由调用方持有；污染条目只从注入快照屏蔽，磁盘保留供人工清理。"""
        if self._snapshot is not None:
            return dict(self._snapshot)
        with mutation_lock(self.root):
            result = {}
            for target in LIMITS:
                _, entries = self._read(target)
                safe = ['[BLOCKED: 记忆条目命中持久化注入模式，请人工核对]' if first_threat_message(entry) else entry
                        for entry in entries]
                result[str(self._path(target))] = ENTRY_DELIMITER.join(safe)
            self._snapshot = result
            return dict(result)

    @staticmethod
    def _operations(request: MemoryInput) -> list[MemoryOperation]:
        """批次字段优先，一次调用中的所有操作必须属于同一目标。"""
        if request.operations:
            return request.operations
        return [MemoryOperation(action=request.action, content=request.content, old_text=request.old_text, new_text=request.new_text)]

    def _validate(self, target: str, raw: str, entries: list[str], operations: list[MemoryOperation],
                  batch: bool, pinned: list[str | None] | None = None) -> tuple[list[str], list[str | None]]:
        """按最终状态校验预算；整批清空与不可往返的外部编辑均不得默默丢失。"""
        if any(op.action != 'add' for op in operations):
            parsed = parse_entries(raw)
            if raw.strip() and (raw.strip() != ENTRY_DELIMITER.join(parsed) or max(map(len, parsed), default=0) > LIMITS[target]):
                backup = self._path(target).with_suffix(f'.md.bak.{uuid4().hex}')
                write_text(backup, raw)
                raise ValueError(f'磁盘内容无法按记忆条目无损往返；原文备份到 {backup.name}，请先人工整理')
        working, previous = apply_operations(entries, operations, pinned)
        if batch and entries and not working:
            raise ValueError('批次不能清空全部记忆；需要明确清除时使用单次 remove')
        if len(ENTRY_DELIMITER.join(working)) > LIMITS[target]:
            raise ValueError(f'记忆超出 {LIMITS[target]} 字符预算；在同一批次合并或缩短条目后再试')
        return working, previous

    def _pending_path(self, identifier: str) -> Path:
        """提案 ID 必须是宿主生成的 UUID，不能用于路径穿越。"""
        if len(identifier) != 32 or any(char not in '0123456789abcdef' for char in identifier):
            raise ValueError('无效的记忆提案 ID')
        return contained_path(self.root, f'.pending/{identifier}.json')

    def _stage(self, request: MemoryInput, operations: list[MemoryOperation], previous: list[str | None]) -> dict[str, Any]:
        """保存当前整条原文以防审批时子串误命中新内容；失败不会退化成直接删除。"""
        directory = contained_path(self.root, '.pending')
        if directory.exists() and sum(1 for _ in directory.glob('*.json')) >= 100:
            raise ValueError('待处理记忆提案已达100项，请先处理旧提案')
        identifier = uuid4().hex
        payload = dict(target=request.target, operations=[op.model_dump() for op in operations],
                       previous=previous, batch=bool(request.operations))
        if self.cancelled():
            raise RuntimeError('后台学习已取消')
        serialized = json.dumps(payload, ensure_ascii=False)
        if len(serialized.encode('utf-8')) > 64 * 1024:
            raise ValueError('提案超过64KiB，请拆成更小的可审阅变更')
        write_text(self._pending_path(identifier), serialized)
        return dict(success=True, staged=True, proposal_staged=True, pending_id=identifier,
                    message=f'后台不会自动删除已有记忆。用 /memory pending 查看，/memory approve {identifier} 应用，或 /memory discard {identifier} 丢弃。')

    def pending(self, identifier: str | None = None) -> dict[str, Any]:
        """枚举至多100条简短索引，指定 ID 才读取完整提案，避免反复装入全部正文。"""
        if identifier:
            return dict(success=True, proposal=json.loads(read_text(self._pending_path(identifier))))
        directory = contained_path(self.root, '.pending')
        proposals = []
        for path in sorted(directory.glob('*.json'))[:100]:
            payload = json.loads(read_text(self._pending_path(path.stem)))
            proposals.append(dict(id=path.stem, target=payload['target'], operations=len(payload['operations']),
                                  preview=str(payload['operations'][0].get('content') or payload['operations'][0].get('old_text') or '')[:120]))
        return dict(success=True, proposals=proposals, detail_command='/memory pending ID')

    def manage(self, request: MemoryInput) -> dict[str, Any]:
        """连续四次失败返回终止回执，成功写入不回显全库以免诱发无限整理。"""
        if request.action == 'pending':
            return self.pending()
        with mutation_lock(self.root):
            try:
                return self._manage(request)
            except ValueError as error:
                self.failures += 1
                if self.failures > 3:
                    return dict(success=False, done=True, error='本轮记忆整理已连续失败四次，请停止重试并继续回复用户')
                _, entries = self._read(request.target)
                return dict(success=False, error=str(error), **({} if request.operations else {'current_entries': entries}))

    def _manage(self, request: MemoryInput) -> dict[str, Any]:
        """锁内重新读盘并一次提交，后台破坏性批次整体保存为提案。"""
        operations = self._operations(request)
        raw, entries = self._read(request.target)
        working, previous = self._validate(request.target, raw, entries, operations, bool(request.operations))
        if self.background and any(op.action != 'add' for op in operations):
            return self._stage(request, operations, previous)
        if self.cancelled():
            raise RuntimeError('后台学习已取消')
        changed = working != entries
        if changed:
            write_text(self._path(request.target), ENTRY_DELIMITER.join(working))
        self.failures = 0
        return dict(success=True, done=True, changed=changed, target=request.target, entry_count=len(working),
                    usage=f'{len(ENTRY_DELIMITER.join(working))}/{LIMITS[request.target]}', previous_entries=previous,
                    note='写入已完成，请勿重复本次操作')

    def resolve(self, identifier: str, *, approve: bool) -> dict[str, Any]:
        """仅供宿主显式用户命令调用；模型工具不暴露此入口。"""
        with mutation_lock(self.root):
            path = self._pending_path(identifier)
            payload = json.loads(read_text(path))
            if approve:
                operations = [MemoryOperation(**op) for op in payload['operations']]
                raw, entries = self._read(payload['target'])
                working, _ = self._validate(payload['target'], raw, entries, operations, payload['batch'], payload['previous'])
                write_text(self._path(payload['target']), ENTRY_DELIMITER.join(working))
            try:
                path.unlink()
            except OSError:
                return dict(success=True, approved=approve, pending_id=identifier, cleanup_pending=True)
            return dict(success=True, approved=approve, pending_id=identifier)
