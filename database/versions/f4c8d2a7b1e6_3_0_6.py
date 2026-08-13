"""3.0.6
新增 Agent 自主任务逐次执行记录

Revision ID: f4c8d2a7b1e6
Revises: b3d7e9f1a2c4
Create Date: 2026-08-13
"""

from alembic import op
import sqlalchemy as sa


revision = "f4c8d2a7b1e6"
down_revision = "b3d7e9f1a2c4"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    """返回使用当前迁移连接的数据库检查器。"""
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    """检查表是否存在。"""
    return table_name in _inspector().get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    """检查表字段是否存在。"""
    if not _has_table(table_name):
        return False
    return column_name in {
        column["name"] for column in _inspector().get_columns(table_name)
    }


def _has_index(table_name: str, index_name: str) -> bool:
    """检查表索引或唯一约束是否存在。"""
    if not _has_table(table_name):
        return False
    inspector = _inspector()
    names = {index.get("name") for index in inspector.get_indexes(table_name)}
    names.update(
        constraint.get("name")
        for constraint in inspector.get_unique_constraints(table_name)
    )
    return index_name in names


def _id_column(dialect_name: str) -> sa.Column:
    """生成与当前 ORM 一致的自增主键定义。"""
    if dialect_name == "postgresql":
        return sa.Column(
            "id",
            sa.Integer(),
            sa.Identity(start=1, cycle=True),
            primary_key=True,
        )
    return sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True)


def upgrade() -> None:
    """建立运行历史表及 AgentTask 最新运行指针。"""
    if _has_table("agenttask") and not _has_column("agenttask", "last_run_id"):
        with op.batch_alter_table("agenttask") as batch_op:
            batch_op.add_column(sa.Column("last_run_id", sa.String(), nullable=True))

    if not _has_table("agenttaskrun"):
        dialect_name = op.get_bind().dialect.name
        op.create_table(
            "agenttaskrun",
            _id_column(dialect_name),
            sa.Column("run_id", sa.String(), nullable=False),
            sa.Column("task_id", sa.Integer(), nullable=False),
            sa.Column("trigger_source", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("trigger_type", sa.String(), nullable=False),
            sa.Column("cron_expression", sa.String()),
            sa.Column("run_at", sa.String()),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("username", sa.String()),
            sa.Column("session_id", sa.String(), nullable=False),
            sa.Column("channel", sa.String()),
            sa.Column("message_source", sa.String()),
            sa.Column("original_chat_id", sa.String()),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("started_at", sa.String(), nullable=False),
            sa.Column("finished_at", sa.String()),
            sa.Column("result", sa.Text()),
        )
    if not _has_index("agenttaskrun", "ix_agenttaskrun_run_id"):
        op.create_index(
            "ix_agenttaskrun_run_id",
            "agenttaskrun",
            ["run_id"],
            unique=True,
        )
    if not _has_index("agenttaskrun", "ix_agenttaskrun_task_started"):
        op.create_index(
            "ix_agenttaskrun_task_started",
            "agenttaskrun",
            ["task_id", "started_at", "id"],
        )


def downgrade() -> None:
    """删除运行历史并移除 AgentTask 最新运行指针。"""
    if _has_table("agenttaskrun"):
        if _has_index("agenttaskrun", "ix_agenttaskrun_task_started"):
            op.drop_index("ix_agenttaskrun_task_started", table_name="agenttaskrun")
        if _has_index("agenttaskrun", "ix_agenttaskrun_run_id"):
            op.drop_index("ix_agenttaskrun_run_id", table_name="agenttaskrun")
        op.drop_table("agenttaskrun")
    if _has_column("agenttask", "last_run_id"):
        with op.batch_alter_table("agenttask") as batch_op:
            batch_op.drop_column("last_run_id")
