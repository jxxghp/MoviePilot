#!/usr/bin/env python3
"""Controlled MoviePilot database helper used by the database-operation Skill."""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

SCRIPT_PATH = Path(__file__).resolve()
PROJECT_ROOT = SCRIPT_PATH.parents[3]
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
    """Describe one public database action argument."""

    name: str
    type: str
    description: str
    required: bool = False
    default: Any = None
    has_default: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the public argument contract used by the MCP schema generator."""
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
    """Describe one stable database helper action."""

    description: str
    effect: str
    arguments: tuple[ArgumentSpec, ...] = ()
    argument_rules: tuple[str, ...] = ()

    @property
    def required(self) -> tuple[str, ...]:
        """Return required argument names for this action."""
        return tuple(argument.name for argument in self.arguments if argument.required)

    def to_dict(self, name: str) -> dict[str, Any]:
        """Return the implementation-independent public action contract."""
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
        "List all tables visible to the configured MoviePilot database.",
        "safe_read",
    ),
    "schema": ActionSpec(
        "Show columns and nullability for one database table.",
        "safe_read",
        arguments=(
            ArgumentSpec(
                "table_name",
                "string",
                "Exact database table name returned by the tables action.",
                required=True,
            ),
        ),
    ),
    "query": ActionSpec(
        "Run one bounded SQL query, optionally enabling the explicit write mode.",
        "safe_read",
        arguments=(
            ArgumentSpec("sql", "string", "One SQL statement; mutually exclusive with file."),
            ArgumentSpec("file", "string", "Local SQL file path; mutually exclusive with sql."),
            ArgumentSpec(
                "limit",
                "integer",
                "Maximum row count appended to a plain SELECT that has no LIMIT.",
                default=100,
                has_default=True,
            ),
            ArgumentSpec(
                "write",
                "boolean",
                "Allow query to execute a write statement; use true only with explicit authorization.",
                default=False,
                has_default=True,
            ),
        ),
        argument_rules=(
            "Provide exactly one of sql and file.",
            "Only SELECT, WITH, and EXPLAIN are allowed unless write=true.",
        ),
    ),
    "write": ActionSpec(
        "Run one explicitly authorized SQL write or schema statement.",
        "destructive_write",
        arguments=(
            ArgumentSpec("sql", "string", "One data-write or schema-change statement; mutually exclusive with file."),
            ArgumentSpec("file", "string", "Local SQL file path; mutually exclusive with sql."),
        ),
        argument_rules=(
            "Provide exactly one of sql and file.",
            "Multiple SQL statements in one call are rejected.",
        ),
    ),
}


def _ensure_project_import() -> None:
    """确保脚本可以从任意工作目录导入 MoviePilot 项目模块。"""
    project_path = str(PROJECT_ROOT)
    if project_path not in sys.path:
        sys.path.insert(0, project_path)


def _load_settings() -> Any:
    """读取 MoviePilot 运行配置。"""
    _ensure_project_import()
    from app.runtime.config import settings  # pylint: disable=import-outside-toplevel

    return settings


def _build_engine() -> Engine:
    """根据 MoviePilot 配置创建同步数据库引擎。"""
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
    """去除 SQL 首尾空白和末尾分号。"""
    return sql.strip().rstrip(";")


def _contains_multiple_statements(sql: str) -> bool:
    """判断 SQL 是否包含多语句分隔符。"""
    return ";" in sql


def _is_write_statement(sql: str) -> bool:
    """判断 SQL 是否为写操作或结构变更操作。"""
    return bool(WRITE_STATEMENT_RE.match(sql))


def _is_supported_statement(sql: str, allow_write: bool) -> bool:
    """判断 SQL 是否在当前权限模式下允许执行。"""
    if _contains_multiple_statements(sql):
        return False
    if allow_write:
        return True
    return bool(SELECT_STATEMENT_RE.match(sql)) and not WRITE_KEYWORD_RE.search(sql)


def _append_limit(sql: str, limit: int) -> str:
    """为普通 SELECT 查询追加默认 LIMIT，避免输出过大。"""
    if limit <= 0:
        return sql
    lowered = sql.lower()
    if not lowered.lstrip().startswith("select"):
        return sql
    if re.search(r"\blimit\s+\d+\b", lowered):
        return sql
    return f"{sql} LIMIT {limit}"


def _row_to_dict(row: Any) -> dict[str, Any]:
    """将 SQLAlchemy 行对象转为普通字典。"""
    return dict(row._mapping)


def _print_json(payload: Any) -> None:
    """输出 JSON 结果。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def list_tables() -> int:
    """列出当前数据库中的数据表。"""
    engine = _build_engine()
    inspector = inspect(engine)
    _print_json({"tables": sorted(inspector.get_table_names())})
    return 0


def show_schema(table_name: str) -> int:
    """显示指定数据表的字段结构。"""
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
    """从参数或文件读取 SQL 文本。"""
    if sql:
        return sql
    if sql_file:
        return Path(sql_file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="MoviePilot database operation helper")
    subparsers = parser.add_subparsers(dest="command")

    query_parser = subparsers.add_parser("query", help="execute a SQL statement")
    query_parser.add_argument("sql", nargs="?", help="SQL statement")
    query_parser.add_argument("--file", dest="sql_file", help="read SQL from file")
    query_parser.add_argument("--limit", type=int, default=100, help="default SELECT row limit")
    query_parser.add_argument("--write", action="store_true", help="allow write statements")

    write_parser = subparsers.add_parser("write", help="execute a write statement")
    write_parser.add_argument("sql", nargs="?", help="SQL statement")
    write_parser.add_argument("--file", dest="sql_file", help="read SQL from file")

    subparsers.add_parser("tables", help="list tables")

    schema_parser = subparsers.add_parser("schema", help="show table schema")
    schema_parser.add_argument("table_name", help="table name")

    return parser


def main() -> int:
    """执行命令行入口。"""
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
