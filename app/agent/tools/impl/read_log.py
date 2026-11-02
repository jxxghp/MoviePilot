"""
只读日志读取工具。
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.application.security.url import SecurityUtils
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting

# 单次最多返回的记录条数与扫描范围，避免把整份日志灌进模型上下文。
DEFAULT_LOG_TAIL = 200
MAX_LOG_TAIL = 1000
MAX_LOG_SCAN_BYTES = 1024 * 1024
MAX_LOG_RESULT_CHARS = 48 * 1024
# 日志记录以【LEVEL】开头，后续不带级别前缀的行（如堆栈）归属上一条记录。
_LOG_RECORD_PATTERN = re.compile(r"^【(?P<level>[A-Z]+)】")
_LOG_LEVEL_ALIASES = {"WARN": "WARNING", "FATAL": "CRITICAL", "CRIT": "CRITICAL"}


class ReadLogInput(BaseModel):
    """
    只读日志读取工具的输入参数模型。
    """

    logfile: str = Field(
        ...,
        description=(
            "相对于日志目录的日志文件名，例如 moviepilot.log、plugins/插件名.log、"
            "subscriptions/订阅ID.log；不接受绝对路径"
        ),
    )
    tail: Optional[int] = Field(
        DEFAULT_LOG_TAIL,
        description=f"返回最近多少条日志记录，默认 {DEFAULT_LOG_TAIL}，最大 {MAX_LOG_TAIL}",
    )
    level: Optional[str] = Field(
        None,
        description="仅保留指定日志级别，多个级别用逗号分隔，例如 ERROR 或 ERROR,WARNING；省略时返回全部级别",
    )
    keyword: Optional[str] = Field(
        None,
        description="仅保留包含该关键字（不区分大小写）的日志记录；省略时不按关键字过滤",
    )


class ReadLogTool(MoviePilotTool):
    """
    MoviePilot 日志只读读取工具。
    """

    name: str = Field(
        default="read_log",
        description="供模型调用的稳定工具名称",
    )
    tags: list[str] = Field(
        default=[ToolTag.Read, ToolTag.System, ToolTag.Admin],
        description="工具的能力和权限分类标签，初始化时会统一归一化",
    )
    description: str = Field(
        default=(
            "以只读方式读取 MoviePilot 主日志、插件日志或订阅日志，按级别和关键字筛选后返回最近的日志记录，"
            "多行堆栈会随所属记录一并返回。仅允许读取日志目录内的 .log 文件，仅系统管理员可用。"
            "本工具不执行修复，也不会修改任何文件。"
        ),
        description="供模型理解工具用途、输入约束和调用边界的说明",
    )
    require_admin: bool = Field(
        default=True,
        description="是否仅允许系统管理员调用",
    )
    args_schema: Type[BaseModel] = Field(
        default=ReadLogInput,
        description="工具输入参数的验证模型或 JSON Schema",
    )

    def get_tool_message(self, **kwargs: object) -> Optional[str]:
        """
        根据读取参数生成友好的提示消息。
        """
        logfile = str(kwargs.get("logfile") or "日志").strip() or "日志"
        return f"读取日志: {Path(logfile).name}"

    @staticmethod
    def _normalize_levels(level: Optional[str]) -> tuple[str, ...]:
        """
        归一化级别筛选条件，兼容 WARN 等常见简写。

        :param level: 用户提供的级别表达式，多个级别用逗号、斜杠或空格分隔
        :return: 去重后的级别名称元组；未提供时为空元组
        """
        if not level or not str(level).strip():
            return ()
        levels: list[str] = []
        for item in re.split(r"[,/\s]+", str(level).upper()):
            if not item:
                continue
            normalized = _LOG_LEVEL_ALIASES.get(item, item)
            if normalized not in levels:
                levels.append(normalized)
        return tuple(levels)

    @staticmethod
    def _normalize_logfile(logfile: str) -> str:
        """
        校验并归一化日志相对路径。

        :param logfile: 用户提供的日志路径
        :return: 使用正斜杠的日志相对路径
        :raises ValueError: 路径为空、为绝对路径或试图跳出日志目录时抛出
        """
        normalized = str(logfile or "").strip().replace("\\", "/")
        if not normalized:
            raise ValueError("日志文件名不能为空")
        if Path(normalized).is_absolute():
            raise ValueError("请提供相对于日志目录的日志文件名，例如 moviepilot.log 或 plugins/插件名.log")
        return normalized

    @staticmethod
    def _split_records(lines: list[str]) -> list[tuple[Optional[str], str]]:
        """
        把日志行切分为以级别前缀起始的完整记录。

        :param lines: 按行切分的日志文本
        :return: 由级别和整条记录文本组成的列表，级别未知时为 None
        """
        records: list[list[Any]] = []
        for line in lines:
            match = _LOG_RECORD_PATTERN.match(line)
            if match or not records:
                records.append([match.group("level") if match else None, [line]])
            else:
                records[-1][1].append(line)
        return [(level, "\n".join(parts)) for level, parts in records]

    @classmethod
    def _select_records(
        cls,
        records: list[tuple[Optional[str], str]],
        tail: int,
    ) -> tuple[list[tuple[Optional[str], str]], bool]:
        """
        从最新记录向前收集结果，保证总长度不超出输出上限。

        :param records: 已过滤的日志记录
        :param tail: 需要保留的最大记录条数
        :return: 选中的记录列表和是否发生截断
        """
        candidates = records[-tail:]
        selected: list[tuple[Optional[str], str]] = []
        total = 0
        for level, text in reversed(candidates):
            cost = len(text) + (1 if selected else 0)
            if total + cost > MAX_LOG_RESULT_CHARS:
                break
            selected.append((level, text))
            total += cost
        selected.reverse()
        if candidates and not selected:
            # 单条记录本身超限时只保留尾部，截断状态同样需要上报。
            level, text = candidates[-1]
            return [(level, text[-MAX_LOG_RESULT_CHARS:])], True
        return selected, len(selected) < len(candidates)

    @classmethod
    def _read_log_records(
        cls,
        root_path: str,
        logfile: str,
        tail: int,
        levels: tuple[str, ...],
        keyword: Optional[str],
    ) -> dict[str, Any]:
        """
        在线程池中校验路径并读取日志尾部记录。

        :param root_path: 允许读取的日志根目录
        :param logfile: 归一化后的日志相对路径
        :param tail: 需要保留的最大记录条数
        :param levels: 需要保留的日志级别
        :param keyword: 需要匹配的关键字
        :return: 结构化读取结果
        :raises FileNotFoundError: 目标不是日志目录内的现存 .log 文件时抛出
        """
        root = Path(root_path)
        path = root / logfile
        if not SecurityUtils.is_safe_path(base_path=root, user_path=path, allowed_suffixes={".log"}):
            raise FileNotFoundError(logfile)
        if not path.is_file():
            raise FileNotFoundError(logfile)

        with path.open("rb") as file:
            stat = os.fstat(file.fileno())
            scan_start = max(0, stat.st_size - MAX_LOG_SCAN_BYTES)
            file.seek(scan_start)
            raw = file.read()

        scan_truncated = scan_start > 0
        # 从文件中部开始扫描时丢弃首行残片，避免返回不完整记录。
        if scan_truncated:
            raw = raw.partition(b"\n")[2]

        records = cls._split_records(raw.decode("utf-8", errors="replace").splitlines())
        if levels:
            records = [item for item in records if item[0] in levels]
        needle = str(keyword or "").strip().lower()
        if needle:
            records = [item for item in records if needle in item[1].lower()]

        selected, result_truncated = cls._select_records(records, tail)
        return {
            "success": True,
            "logfile": logfile,
            "levels": list(levels),
            "keyword": needle or None,
            "tail": tail,
            "scanned_bytes": len(raw),
            "scan_truncated": scan_truncated,
            "matched_records": len(records),
            "returned_records": len(selected),
            "result_truncated": result_truncated,
            "content": "\n".join(text for _, text in selected),
        }

    async def run(  # type: ignore[override]
        self,
        logfile: str,
        tail: Optional[int] = DEFAULT_LOG_TAIL,
        level: Optional[str] = None,
        keyword: Optional[str] = None,
        **kwargs: object,
    ) -> str:
        """
        读取日志尾部记录并返回 JSON 字符串。
        """
        logger.info(
            f"执行工具: {self.name}, logfile={logfile}, tail={tail}, level={level}, keyword={keyword}"
        )
        try:
            normalized = self._normalize_logfile(logfile)
            try:
                tail_count = int(tail) if tail is not None else DEFAULT_LOG_TAIL
            except (TypeError, ValueError):
                return self._error_result("tail 必须是大于 0 的整数")
            if tail_count < 1:
                return self._error_result("tail 必须是大于 0 的整数")
            tail_count = min(tail_count, MAX_LOG_TAIL)

            payload = await self.run_blocking(
                "default",
                self._read_log_records,
                str(get_runtime_setting("LOG_PATH")),
                normalized,
                tail_count,
                self._normalize_levels(level),
                keyword,
            )
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except ValueError as err:
            return self._error_result(str(err))
        except FileNotFoundError:
            return self._error_result(f"未找到日志 {logfile}，请确认文件名后重试")
        except Exception as err:
            logger.error(f"读取日志 {logfile} 失败: {err}", exc_info=True)
            return self._error_result(f"读取日志时发生错误: {str(err)}")

    @staticmethod
    def _error_result(message: str) -> str:
        """
        生成失败结果 JSON。

        :param message: 展示给模型的失败原因
        :return: JSON 字符串
        """
        return json.dumps({"success": False, "message": message}, ensure_ascii=False)
