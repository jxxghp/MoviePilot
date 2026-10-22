#!/usr/bin/env python3
"""
为 database-operation 技能提供受控的 MoviePilot 数据库检查和显式写入入口。
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

SCRIPT_PATH = Path(__file__).resolve()


def _find_project_root() -> Optional[Path]:
    """
    从运行环境或目录标记定位 MoviePilot 程序目录。
    """
    candidates = []
    configured_root = os.environ.get("MOVIEPILOT_ROOT", "").strip()
    if configured_root:
        candidates.append(Path(configured_root).expanduser())
    for base in (Path.cwd().resolve(), SCRIPT_PATH.parent):
        candidates.extend((base, *base.parents))
    for candidate in candidates:
        if (candidate / "app" / "runtime" / "config.py").is_file():
            return candidate
    return None


PROJECT_ROOT = _find_project_root()
WRITE_STATEMENT_RE = re.compile(
    r"^\s*(insert|update|delete|drop|alter|truncate|create|replace)\b",
    re.IGNORECASE,
)
WRITE_KEYWORD_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|replace)\b",
    re.IGNORECASE,
)
SELECT_STATEMENT_RE = re.compile(r"^\s*(select|with|explain)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ArgumentSpec:
    """
    单个动作参数的公开调用契约。

    :param name: 动作调用中的精确参数名称
    :param type: 公开模式使用的类型表达式，支持联合类型及数组
    :param description: 供调用方理解参数用途、限制和单位的中文说明
    :param required: 该参数是否必填，默认 False
    :param default: 参数默认值；仅在明确声明默认值时向调用方公开
    :param has_default: 是否将 default 写入公开契约，用于区分未设置和显式 None
    """

    name: str
    type: str
    description: str
    required: bool = False
    default: Any = None
    has_default: bool = False

    def to_dict(self) -> dict[str, Any]:
        """
        返回 MCP 模式生成器使用的公开参数契约。
        """
        result = {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "required": self.required,
        }
        if self.has_default:
            result["default"] = self.default
        return result


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """
    单个允许执行动作的能力、影响范围及参数契约。

    :param description: 公开动作的用途与操作边界说明
    :param effect: 策略分类标识，例如 safe_read、reversible_write 或 destructive_write；保持机器可读原值
    :param arguments: 按声明顺序排列的参数契约，默认无参数
    :param argument_rules: 参数间的组合约束与安全要求，默认无附加规则
    """

    description: str
    effect: str
    arguments: tuple[ArgumentSpec, ...] = ()
    argument_rules: tuple[str, ...] = ()

    @property
    def required(self) -> tuple[str, ...]:
        """
        返回此动作要求的必填参数名称。
        """
        return tuple(argument.name for argument in self.arguments if argument.required)

    def to_dict(self, name: str) -> dict[str, Any]:
        """
        返回不包含实现细节的公开动作契约。
        """
        return {
            "action": name,
            "description": self.description,
            "effect": self.effect,
            "providers": ["sqlite", "postgresql"],
            "required_arguments": list(self.required),
            "arguments": [argument.to_dict() for argument in self.arguments],
            "argument_rules": list(self.argument_rules),
        }


ACTIONS: dict[str, ActionSpec] = {
    "tables": ActionSpec(
        "列出已配置 MoviePilot 数据库中的所有可见表。",
        "safe_read",
    ),
    "schema": ActionSpec(
        "显示指定数据库表的列及是否允许空值。",
        "safe_read",
        arguments=(
            ArgumentSpec(
                "table_name",
                "string",
                "tables 动作返回的精确数据库表名。",
                required=True,
            ),
        ),
    ),
    "query": ActionSpec(
        "执行一条有结果数量限制的 SQL 查询，可显式启用写入模式。",
        "safe_read",
        arguments=(
            ArgumentSpec("sql", "string", "单条 SQL 语句，与 file 互斥。"),
            ArgumentSpec("file", "string", "本地 SQL 文件路径，与 sql 互斥。"),
            ArgumentSpec(
                "limit",
                "integer",
                "为不含 LIMIT 的普通 SELECT 追加的返回行数上限。",
                default=100,
                has_default=True,
            ),
            ArgumentSpec(
                "write",
                "boolean",
                "是否允许 query 执行写入语句；只有获得明确授权时才可设为 true。",
                default=False,
                has_default=True,
            ),
        ),
        argument_rules=(
            "sql 和 file 必须且只能提供一项。",
            "仅允许 SELECT、WITH 和 EXPLAIN；write=true 时才允许写入语句。",
        ),
    ),
    "write": ActionSpec(
        "执行一条经明确授权的 SQL 数据写入或结构变更语句。",
        "destructive_write",
        arguments=(
            ArgumentSpec("sql", "string", "单条数据写入或结构变更语句，与 file 互斥。"),
            ArgumentSpec("file", "string", "本地 SQL 文件路径，与 sql 互斥。"),
        ),
        argument_rules=(
            "sql 和 file 必须且只能提供一项。",
            "拒绝在一次调用中执行多条 SQL 语句。",
        ),
    ),
}


def _ensure_project_import() -> None:
    """
    确保脚本可以从任意工作目录导入 MoviePilot 项目模块。
    """
    if PROJECT_ROOT is None:
        raise RuntimeError(
            "无法定位 MoviePilot 程序目录；请在程序目录执行，或设置 MOVIEPILOT_ROOT="
            "<程序目录> 后重试。"
        )
    project_path = str(PROJECT_ROOT)
    if project_path not in sys.path:
        sys.path.insert(0, project_path)


def _load_settings() -> Any:
    """
    读取 MoviePilot 运行配置。
    """
    _ensure_project_import()
    # 先定位项目并补充导入路径，再读取配置，保证脚本可从任意工作目录启动。
    from app.runtime.config import settings  # pylint: disable=import-outside-toplevel

    return settings


def _build_engine() -> Engine:
    """
    根据 MoviePilot 配置创建同步数据库引擎。
    """
    settings = _load_settings()
    if str(settings.DB_TYPE).lower() == "postgresql":
        return create_engine(
            settings.DB_POSTGRESQL_URL(),
            pool_pre_ping=settings.DB_POOL_PRE_PING,
            pool_recycle=settings.DB_POOL_RECYCLE,
        )
    return create_engine(
        f"sqlite:///{settings.CONFIG_PATH}/user.db",
        pool_pre_ping=settings.DB_POOL_PRE_PING,
        pool_recycle=settings.DB_POOL_RECYCLE,
        connect_args={"timeout": settings.DB_TIMEOUT},
    )


def _normalize_sql(sql: str) -> str:
    """
    去除 SQL 首尾空白及注释前唯一的语句终止符，保留字面值内容。
    """
    sql = sql.strip()
    code = _masked_sql(sql)
    if code is not None and code.count(";") == 1 and code.rstrip().endswith(";"):
        position = code.rfind(";")
        return sql[:position] + sql[position + 1:]
    return sql


def _quoted_end(sql: str, start: int, quote: str, *, escaped: bool = False) -> Optional[int]:
    """
    寻找带成对转义引号的词法项末尾；仅 PostgreSQL E 字符串解析反斜杠。
    """
    index = start + 1
    closing = "]" if quote == "[" else quote
    while index < len(sql):
        if escaped and sql[index] == "\\":
            index += 2
        elif sql[index] == closing:
            if quote != "[" and sql[index:index + 2] == closing * 2:
                index += 2
            else:
                return index + 1
        else:
            index += 1
    return None


def _block_comment_end(sql: str, start: int) -> Optional[int]:
    """
    定位块注释末尾，同时支持 PostgreSQL 的嵌套注释。
    """
    depth = 1
    index = start + 2
    while index < len(sql) and depth:
        if sql[index:index + 2] == "/*":
            depth += 1
            index += 2
        elif sql[index:index + 2] == "*/":
            depth -= 1
            index += 2
        else:
            index += 1
    return index if depth == 0 else None


def _sql_literal_end(sql: str, index: int) -> Optional[int]:
    """
    返回引号或美元引号项的结束位置；不是词法项时返回当前位置。
    """
    char = sql[index]
    if char in "'\"`[":
        escaped = char == "'" and index > 0 and sql[index - 1] in "eE" and (
            index < 2 or not (sql[index - 2].isalnum() or sql[index - 2] == "_")
        )
        return _quoted_end(sql, index, char, escaped=escaped)
    if char == "$":
        match = re.match(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$", sql[index:])
        if match:
            end = sql.find(match.group(), index + len(match.group()))
            return end + len(match.group()) if end >= 0 else None
    return index


def _masked_sql(sql: str) -> Optional[str]:
    """
    用等长空白遮蔽字面值、标识符与注释，未闭合结构按无效 SQL 拒绝。
    """
    code = list(sql)
    index = 0
    while index < len(sql):
        if sql[index:index + 2] == "--":
            end = sql.find("\n", index)
            end = len(sql) if end < 0 else end
        elif sql[index:index + 2] == "/*":
            end = _block_comment_end(sql, index)
        else:
            end = _sql_literal_end(sql, index)
        if end is None:
            return None
        if end > index:
            code[index:end] = " " * (end - index)
            index = end
        else:
            index += 1
    return "".join(code)


def _contains_multiple_statements(sql: str) -> bool:
    """
    判断 SQL 是否包含多语句分隔符。
    """
    code = _masked_sql(sql)
    return code is None or ";" in code.rstrip().removesuffix(";")


def _is_write_statement(sql: str) -> bool:
    """
    判断 SQL 是否为写操作或结构变更操作。
    """
    code = _masked_sql(sql)
    return code is not None and bool(WRITE_STATEMENT_RE.match(code))


def _is_supported_statement(sql: str, allow_write: bool) -> bool:
    """
    判断 SQL 是否在当前权限模式下允许执行。
    """
    if _contains_multiple_statements(sql):
        return False
    code = _masked_sql(sql)
    if code is None or not code.strip().strip(";"):
        return False
    if allow_write:
        return True
    return bool(SELECT_STATEMENT_RE.match(code)) and not WRITE_KEYWORD_RE.search(code)


def _append_limit(sql: str, limit: int) -> str:
    """
    为普通 SELECT 查询追加默认 LIMIT，避免输出过大。
    """
    if limit <= 0:
        return sql
    lowered = (_masked_sql(sql) or "").lower()
    if not lowered.lstrip().startswith("select"):
        return sql
    if re.search(r"\blimit\s+\d+\b", lowered):
        return sql
    return f"{sql}\nLIMIT {limit}"


def _row_to_dict(row: Any) -> dict[str, Any]:
    """
    将 SQLAlchemy 行对象转为普通字典。
    """
    return dict(row._mapping)


def _print_json(payload: Any) -> None:
    """
    输出 JSON 结果。
    """
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def list_tables() -> int:
    """
    列出当前数据库中的数据表。
    """
    engine = _build_engine()
    inspector = inspect(engine)
    _print_json({"tables": sorted(inspector.get_table_names())})
    return 0


def show_schema(table_name: str) -> int:
    """
    显示指定数据表的字段结构。
    """
    engine = _build_engine()
    inspector = inspect(engine)
    columns = [
        {
            "name": column.get("name"),
            "type": str(column.get("type")),
            "nullable": column.get("nullable"),
            "default": column.get("default"),
            "primary_key": bool(column.get("primary_key")),
        }
        for column in inspector.get_columns(table_name)
    ]
    _print_json({"table": table_name, "columns": columns})
    return 0


def run_query(sql: str, *, limit: int = 100, allow_write: bool = False) -> int:
    """
    执行 SQL 语句并输出 JSON 结果。

    :param sql: 要执行的 SQL 语句
    :param limit: 查询语句默认追加的最大行数
    :param allow_write: 是否允许写操作或结构变更操作
    :return: 进程退出码
    """
    normalized_sql = _normalize_sql(sql)
    if not normalized_sql:
        print("Error: SQL is empty", file=sys.stderr)
        return 1
    if not _is_supported_statement(normalized_sql, allow_write):
        print("Error: write statements require --write", file=sys.stderr)
        return 1

    statement_sql = _append_limit(normalized_sql, limit)
    engine = _build_engine()
    try:
        with engine.begin() as connection:
            result = connection.execute(text(statement_sql))
            if result.returns_rows:
                rows = [_row_to_dict(row) for row in result.fetchall()]
                _print_json({"rows": rows, "row_count": len(rows)})
            else:
                _print_json({"row_count": result.rowcount})
    except SQLAlchemyError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    return 0


def _read_sql(sql: Optional[str], sql_file: Optional[str]) -> str:
    """
    从参数或文件读取 SQL 文本。
    """
    if sql:
        return sql
    if sql_file:
        return Path(sql_file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _build_parser() -> argparse.ArgumentParser:
    """
    构建命令行参数解析器。
    """
    parser = argparse.ArgumentParser(description="MoviePilot 数据库操作工具")
    subparsers = parser.add_subparsers(dest="command")

    query_parser = subparsers.add_parser("query", help="执行一条 SQL 语句")
    query_parser.add_argument("sql", nargs="?", help="SQL 语句")
    query_parser.add_argument("--file", dest="sql_file", help="从文件读取 SQL")
    query_parser.add_argument("--limit", type=int, default=100, help="SELECT 默认返回行数上限")
    query_parser.add_argument("--write", action="store_true", help="允许写入语句")

    write_parser = subparsers.add_parser("write", help="执行一条写入语句")
    write_parser.add_argument("sql", nargs="?", help="SQL 语句")
    write_parser.add_argument("--file", dest="sql_file", help="从文件读取 SQL")

    subparsers.add_parser("tables", help="列出数据库表")

    schema_parser = subparsers.add_parser("schema", help="显示表结构")
    schema_parser.add_argument("table_name", help="数据库表名")

    return parser


def main() -> int:
    """
    执行命令行入口。
    """
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "tables":
        return list_tables()
    if args.command == "schema":
        return show_schema(args.table_name)
    if args.command == "query":
        sql = _read_sql(args.sql, args.sql_file)
        return run_query(sql, limit=args.limit, allow_write=args.write)
    if args.command == "write":
        sql = _read_sql(args.sql, args.sql_file)
        return run_query(sql, limit=0, allow_write=True)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
