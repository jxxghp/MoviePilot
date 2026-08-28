"""3.0.18 强制用户身份唯一并建立用户从属数据级联约束。

Revision ID: a9d4f2c7e6b1
Revises: f6d8b0c2e4a7
Create Date: 2026-08-28
"""

from collections import defaultdict
from typing import Optional

import sqlalchemy as sa
from alembic import op

revision = "a9d4f2c7e6b1"
down_revision = "f6d8b0c2e4a7"
branch_labels = None
depends_on = None

_TABLE_NAME = "user"
_UNIQUE_INDEX = "ux_user_name"
_LEGACY_INDEX = "ix_user_name"
_REQUIRED_COLUMNS = {"id", "name", "is_active"}
_USER_CONFIG_TABLE = "userconfig"
_USER_CONFIG_FOREIGN_KEY = "fk_userconfig_username_user"
_USER_CONFIG_UNIQUE = "uq_userconfig_username_key"
_PASSKEY_TABLE = "passkey"
_PASSKEY_FOREIGN_KEY = "fk_passkey_user_id_user"
_FOREIGN_KEY_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s_%(column_1_name)s",
}


def _column_names() -> set[str]:
    """返回用户表字段；表不存在时允许迁移空数据库。"""
    inspector = sa.inspect(op.get_bind())
    if _TABLE_NAME not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(_TABLE_NAME)}


def _index_definitions() -> dict[str, dict]:
    """返回用户表按名称索引的当前定义。"""
    return {index["name"]: index for index in sa.inspect(op.get_bind()).get_indexes(_TABLE_NAME) if index.get("name")}


def _constraint_definitions() -> dict[str, dict]:
    """返回用户表按名称索引的唯一约束定义。"""
    return {
        constraint["name"]: constraint
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(_TABLE_NAME)
        if constraint.get("name")
    }


def _table_columns(table_name: str) -> dict[str, dict]:
    """返回指定表的字段定义；表不存在时返回空映射。"""
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return {}
    return {column["name"]: column for column in inspector.get_columns(table_name)}


def _foreign_keys(table_name: str) -> list[dict]:
    """返回指定表的外键定义；表不存在时返回空列表。"""
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return []
    return list(inspector.get_foreign_keys(table_name))


def _unique_constraints(table_name: str) -> list[dict]:
    """返回指定表的唯一约束；表不存在时返回空列表。"""
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return []
    return list(inspector.get_unique_constraints(table_name))


def _foreign_key_matches(
    foreign_key: dict,
    *,
    column: str,
    referred_column: str,
    ondelete: Optional[str],
    onupdate: Optional[str],
) -> bool:
    """判断外键列、目标列与级联动作是否完全符合规范。"""
    options = foreign_key.get("options") or {}
    actual_ondelete = str(options.get("ondelete") or "").upper() or None
    actual_onupdate = str(options.get("onupdate") or "").upper() or None
    return (
        tuple(foreign_key.get("constrained_columns") or ()) == (column,)
        and foreign_key.get("referred_table") == _TABLE_NAME
        and tuple(foreign_key.get("referred_columns") or ()) == (referred_column,)
        and actual_ondelete == ondelete
        and actual_onupdate == onupdate
    )


def _delete_orphans(
    *,
    table_name: str,
    column: str,
    referred_column: str,
    delete_nulls: bool,
) -> None:
    """建立约束前删除无法归属到现有用户的历史从属行。"""
    columns = _table_columns(table_name)
    if column not in columns:
        return
    child = sa.table(table_name, sa.column(column))
    parent = sa.table(_TABLE_NAME, sa.column(referred_column))
    child_column = child.c[column]
    orphaned = ~sa.exists(sa.select(1).select_from(parent).where(parent.c[referred_column] == child_column))
    if delete_nulls:
        orphaned = sa.or_(child_column.is_(None), orphaned)
    op.get_bind().execute(child.delete().where(orphaned))


def _normalize_user_configs() -> None:
    """删除空键和重复配置，仅保留每个用户键最早的历史行。"""
    columns = _table_columns(_USER_CONFIG_TABLE)
    if not {"id", "username", "key"}.issubset(columns):
        return
    configs = sa.table(
        _USER_CONFIG_TABLE,
        sa.column("id", sa.Integer()),
        sa.column("username", sa.String()),
        sa.column("key", sa.String()),
    )
    connection = op.get_bind()
    connection.execute(configs.delete().where(configs.c.key.is_(None)))
    rows = connection.execute(sa.select(configs.c.id, configs.c.username, configs.c.key).order_by(configs.c.id)).all()
    seen: set[tuple[str, str]] = set()
    duplicate_ids: list[int] = []
    for row_id, username, key in rows:
        identity = (username, key)
        if identity in seen:
            duplicate_ids.append(row_id)
        else:
            seen.add(identity)
    if duplicate_ids:
        connection.execute(configs.delete().where(configs.c.id.in_(duplicate_ids)))


def _repair_user_config_unique() -> None:
    """把用户配置键约束修复为命名的非空双列唯一约束。"""
    columns = _table_columns(_USER_CONFIG_TABLE)
    if "key" not in columns:
        return
    constraints = _unique_constraints(_USER_CONFIG_TABLE)
    relevant = [
        constraint
        for constraint in constraints
        if tuple(constraint.get("column_names") or ()) == ("username", "key")
        or constraint.get("name") == _USER_CONFIG_UNIQUE
    ]
    matches = (
        len(relevant) == 1
        and relevant[0].get("name") == _USER_CONFIG_UNIQUE
        and tuple(relevant[0].get("column_names") or ()) == ("username", "key")
    )
    if matches and not bool(columns["key"].get("nullable")):
        return
    with op.batch_alter_table(
        _USER_CONFIG_TABLE,
        naming_convention=_FOREIGN_KEY_NAMING_CONVENTION,
    ) as batch_op:
        for constraint in relevant:
            batch_op.drop_constraint(
                constraint.get("name") or _USER_CONFIG_UNIQUE,
                type_="unique",
            )
        if bool(columns["key"].get("nullable")):
            batch_op.alter_column(
                "key",
                existing_type=columns["key"]["type"],
                existing_nullable=True,
                nullable=False,
            )
        batch_op.create_unique_constraint(
            _USER_CONFIG_UNIQUE,
            ["username", "key"],
        )


def _repair_foreign_key(
    *,
    table_name: str,
    column: str,
    referred_column: str,
    constraint_name: str,
    nullable: bool,
    ondelete: Optional[str],
    onupdate: Optional[str] = None,
) -> None:
    """跨 SQLite/PostgreSQL 精确修复一条用户从属外键。"""
    columns = _table_columns(table_name)
    if column not in columns:
        return
    relevant = [
        foreign_key
        for foreign_key in _foreign_keys(table_name)
        if tuple(foreign_key.get("constrained_columns") or ()) == (column,)
    ]
    matches = len(relevant) == 1 and _foreign_key_matches(
        relevant[0],
        column=column,
        referred_column=referred_column,
        ondelete=ondelete,
        onupdate=onupdate,
    )
    if matches and bool(columns[column].get("nullable")) == nullable:
        return

    with op.batch_alter_table(
        table_name,
        naming_convention=_FOREIGN_KEY_NAMING_CONVENTION,
    ) as batch_op:
        for foreign_key in relevant:
            batch_op.drop_constraint(
                foreign_key.get("name") or constraint_name,
                type_="foreignkey",
            )
        if bool(columns[column].get("nullable")) != nullable:
            batch_op.alter_column(
                column,
                existing_type=columns[column]["type"],
                existing_nullable=bool(columns[column].get("nullable")),
                nullable=nullable,
            )
        batch_op.create_foreign_key(
            constraint_name,
            _TABLE_NAME,
            [column],
            [referred_column],
            ondelete=ondelete,
            onupdate=onupdate,
        )


def _drop_user_config_foreign_key() -> None:
    """降级时移除用户名外键并恢复历史可空字段。"""
    columns = _table_columns(_USER_CONFIG_TABLE)
    if "username" not in columns:
        return
    relevant = [
        foreign_key
        for foreign_key in _foreign_keys(_USER_CONFIG_TABLE)
        if tuple(foreign_key.get("constrained_columns") or ()) == ("username",)
    ]
    key_is_nullable = bool(columns.get("key", {}).get("nullable", True))
    if not relevant and bool(columns["username"].get("nullable")) and key_is_nullable:
        return
    with op.batch_alter_table(
        _USER_CONFIG_TABLE,
        naming_convention=_FOREIGN_KEY_NAMING_CONVENTION,
    ) as batch_op:
        for foreign_key in relevant:
            batch_op.drop_constraint(
                foreign_key.get("name") or _USER_CONFIG_FOREIGN_KEY,
                type_="foreignkey",
            )
        if not bool(columns["username"].get("nullable")):
            batch_op.alter_column(
                "username",
                existing_type=columns["username"]["type"],
                existing_nullable=False,
                nullable=True,
            )
        if "key" in columns and not key_is_nullable:
            batch_op.alter_column(
                "key",
                existing_type=columns["key"]["type"],
                existing_nullable=False,
                nullable=True,
            )


def _replacement_name(
    *,
    original_name: str,
    user_id: int,
    used_names: set[str],
) -> str:
    """生成可重放且不覆盖任何现有用户的重复用户名。"""
    base_name = f"{original_name}__duplicate_{user_id}"
    candidate = base_name
    collision = 1
    while candidate in used_names:
        candidate = f"{base_name}_{collision}"
        collision += 1
    return candidate


def _normalize_duplicate_names() -> None:
    """保留同名最早用户，并就地停用、重命名其余用户。

    迁移不删除或合并用户行，因此按 ``user.id`` 建立的 PassKey 等外键
    仍指向原记录。没有外键语义的历史用户名快照也不做猜测性改写。
    """
    users = sa.table(
        _TABLE_NAME,
        sa.column("id", sa.Integer()),
        sa.column("name", sa.String()),
        sa.column("is_active", sa.Boolean()),
    )
    connection = op.get_bind()
    rows = connection.execute(sa.select(users.c.id, users.c.name).order_by(users.c.id)).mappings().all()
    duplicate_groups: dict[str, list[int]] = defaultdict(list)
    used_names = set()
    for row in rows:
        name = row["name"]
        if name is None:
            raise RuntimeError("用户表存在空用户名，无法建立用户名唯一约束")
        duplicate_groups[name].append(row["id"])
        used_names.add(name)

    for original_name in sorted(duplicate_groups):
        duplicate_ids = duplicate_groups[original_name][1:]
        for user_id in duplicate_ids:
            replacement = _replacement_name(
                original_name=original_name,
                user_id=user_id,
                used_names=used_names,
            )
            connection.execute(users.update().where(users.c.id == user_id).values(name=replacement, is_active=False))
            used_names.add(replacement)


def _repair_unique_index() -> None:
    """把用户名约束修复为精确的单列唯一索引。"""
    constraints = _constraint_definitions()
    if _UNIQUE_INDEX in constraints:
        with op.batch_alter_table(_TABLE_NAME) as batch_op:
            batch_op.drop_constraint(_UNIQUE_INDEX, type_="unique")

    indexes = _index_definitions()
    current = indexes.get(_UNIQUE_INDEX)
    if current is not None and (
        tuple(current.get("column_names") or ()) != ("name",) or not bool(current.get("unique"))
    ):
        op.drop_index(_UNIQUE_INDEX, table_name=_TABLE_NAME)
        current = None
    if current is None:
        op.create_index(
            _UNIQUE_INDEX,
            _TABLE_NAME,
            ["name"],
            unique=True,
        )

    legacy = _index_definitions().get(_LEGACY_INDEX)
    if legacy is not None:
        op.drop_index(_LEGACY_INDEX, table_name=_TABLE_NAME)


def upgrade() -> None:
    """归一用户身份并约束 UserConfig、PassKey 必须归属现有用户。"""
    columns = _column_names()
    if not columns:
        return
    missing_columns = _REQUIRED_COLUMNS - columns
    if missing_columns:
        names = ", ".join(sorted(missing_columns))
        raise RuntimeError(f"用户表缺少迁移必需字段: {names}")
    _normalize_duplicate_names()
    _repair_unique_index()
    _delete_orphans(
        table_name=_USER_CONFIG_TABLE,
        column="username",
        referred_column="name",
        delete_nulls=True,
    )
    _normalize_user_configs()
    _repair_user_config_unique()
    _repair_foreign_key(
        table_name=_USER_CONFIG_TABLE,
        column="username",
        referred_column="name",
        constraint_name=_USER_CONFIG_FOREIGN_KEY,
        nullable=False,
        ondelete="CASCADE",
        onupdate="CASCADE",
    )
    _delete_orphans(
        table_name=_PASSKEY_TABLE,
        column="user_id",
        referred_column="id",
        delete_nulls=True,
    )
    _repair_foreign_key(
        table_name=_PASSKEY_TABLE,
        column="user_id",
        referred_column="id",
        constraint_name=_PASSKEY_FOREIGN_KEY,
        nullable=False,
        ondelete="CASCADE",
    )


def downgrade() -> None:
    """撤销用户名唯一索引，但保留已停用和改名的历史用户。

    自动恢复原重名会覆盖升级后的合法改名，也无法判定共享用户名快照的
    归属，因此降级只恢复旧版非唯一查询索引。
    """
    if not _column_names():
        return
    _drop_user_config_foreign_key()
    _repair_foreign_key(
        table_name=_PASSKEY_TABLE,
        column="user_id",
        referred_column="id",
        constraint_name=_PASSKEY_FOREIGN_KEY,
        nullable=False,
        ondelete=None,
    )
    indexes = _index_definitions()
    if _UNIQUE_INDEX in indexes:
        op.drop_index(_UNIQUE_INDEX, table_name=_TABLE_NAME)
    constraints = _constraint_definitions()
    if _UNIQUE_INDEX in constraints:
        with op.batch_alter_table(_TABLE_NAME) as batch_op:
            batch_op.drop_constraint(_UNIQUE_INDEX, type_="unique")
    indexes = _index_definitions()
    legacy = indexes.get(_LEGACY_INDEX)
    if legacy is not None and (tuple(legacy.get("column_names") or ()) != ("name",) or bool(legacy.get("unique"))):
        op.drop_index(_LEGACY_INDEX, table_name=_TABLE_NAME)
        legacy = None
    if legacy is None:
        op.create_index(
            _LEGACY_INDEX,
            _TABLE_NAME,
            ["name"],
            unique=False,
        )
