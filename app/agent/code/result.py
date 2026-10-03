"""代码执行的模型输出投影，工具失败与内核状态各自如实报告。"""

import re
from pathlib import Path
from typing import Any

from app.adapters.system.code.output import SPILL_BYTES, STDERR_BYTES, project_stdout, read_cell_stdout
from app.agent.policy.sanitizer import sanitize_archived_text

_ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')


def render_result(payload: dict[str, Any], *, directory: Path, output_directory: Path,
                  reused: bool, state_reset: bool, elapsed: float) -> dict[str, Any]:
    """完整 stdout 脱敏后才落盘，内核退出后仍能从独立输出目录继续读取。"""
    source = read_cell_stdout(payload, directory) + str(payload.get('raw_stdout') or '')
    source_captured = len(source.encode('utf-8'))
    stdout = sanitize_archived_text(_ANSI.sub('', source))
    preview, metadata = project_stdout(stdout, output_directory)
    stderr = sanitize_archived_text(_ANSI.sub('', str(payload.get('stderr') or '') + str(payload.get('raw_stderr') or '')))
    trace = sanitize_archived_text(str(payload.get('traceback') or ''))
    status = payload.get('status', 'error')
    result: dict[str, Any] = {
        'status': 'error' if status == 'error' else 'success',
        'output': preview,
        'exit_code': 1 if status == 'error' else 0,
        'duration_seconds': round(elapsed, 2),
        'kernel': {'mode': 'session', 'reused': reused, 'state_reset': state_reset,
                   'execution_count': int(payload.get('execution_count', 0)), 'ended': status == 'exit'},
        **metadata,
    }
    if stderr or trace:
        errors = (stderr + trace).encode('utf-8', errors='replace')
        raw_errors = str(payload.get('raw_stderr') or '').encode('utf-8')
        truncated = (len(errors) > STDERR_BYTES or bool(payload.get('stderr_clipped'))
                     or int(payload.get('raw_stderr_bytes', 0)) > len(raw_errors))
        result.update(stderr=errors[:STDERR_BYTES].decode('utf-8', errors='ignore'), stderr_truncated=truncated)
        result['output'] += '\n[stderr]\n' + result['stderr']
    if status == 'error':
        result['error'] = result.get('stderr') or 'Python cell 执行异常。'
    captured_total = int(payload.get('stdout_bytes_total', 0)) + int(payload.get('raw_stdout_bytes', 0))
    if payload.get('stdout_clipped') or captured_total > source_captured:
        result['stdout_source_bytes_total'] = captured_total
        result['stdout_spill_capped'] = captured_total > SPILL_BYTES
        result['stdout_capture_incomplete'] = captured_total > source_captured
        if result['stdout_capture_incomplete']:
            result['stdout_truncated'] = True
            result['warning'] = '部分输出超过捕获预算，无法恢复全部正文；现有 spill 仅包含已保留部分。'
    return result
