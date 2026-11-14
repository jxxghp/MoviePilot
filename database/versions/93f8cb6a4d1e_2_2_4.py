"""
2.2.4 调整数据库索引，补充高频组合索引并移除冗余 id 索引

版本标识： 93f8cb6a4d1e
前置版本： 58edfac72c32
创建时间： 2026-05-09
"""

from alembic import op
import sqlalchemy as sa

# Alembic 使用以下标识连接迁移版本链。
revision = "93f8cb6a4d1e"
down_revision = "58edfac72c32"
branch_labels = None
depends_on = None


# 主键已经提供查询能力，升级时可移除的重复单列 ID 索引。
REDUNDANT_ID_INDEXES = {
    "downloadfiles": [("ix_downloadfiles_id", ["id"])],
    "downloadhistory": [("ix_downloadhistory_id", ["id"])],
    "mediaserveritem": [("ix_mediaserveritem_id", ["id"])],
    "message": [("ix_message_id", ["id"])],
    "passkey": [("ix_passkey_id", ["id"])],
    "plugindata": [("ix_plugindata_id", ["id"])],
    "site": [("ix_site_id", ["id"])],
    "siteicon": [("ix_siteicon_id", ["id"])],
    "sitestatistic": [("ix_sitestatistic_id", ["id"])],
    "siteuserdata": [("ix_siteuserdata_id", ["id"])],
    "subscribe": [("ix_subscribe_id", ["id"])],
    "subscribehistory": [("ix_subscribehistory_id", ["id"])],
    "systemconfig": [("ix_systemconfig_id", ["id"])],
    "transferhistory": [("ix_transferhistory_id", ["id"])],
    "user": [("ix_user_id", ["id"])],
    "userconfig": [("ix_userconfig_id", ["id"])],
    "workflow": [("ix_workflow_id", ["id"])],
}


# 由本次组合索引替代的历史查询索引。
DROP_INDEXES = {
    "plugindata": [
        ("ix_plugindata_plugin_id", ["plugin_id"]),
        ("ix_plugindata_key", ["key"]),
    ],
    "message": [
        ("ix_message_reg_time", ["reg_time"]),
    ],
    "siteuserdata": [
        ("ix_siteuserdata_domain", ["domain"]),
        ("ix_siteuserdata_updated_day", ["updated_day"]),
    ],
    "downloadhistory": [
        ("ix_downloadhistory_download_hash", ["download_hash"]),
    ],
    "downloadfiles": [
        ("ix_downloadfiles_download_hash", ["download_hash"]),
        ("ix_downloadfiles_fullpath", ["fullpath"]),
    ],
    "mediaserveritem": [
        ("ix_mediaserveritem_tmdbid", ["tmdbid"]),
    ],
    "transferhistory": [
        ("ix_transferhistory_date", ["date"]),
    ],
    "userconfig": [
        ("ix_userconfig_username", ["username"]),
        ("ix_userconfig_username_key", ["username", "key"]),
    ],
}


# 匹配高频查询条件和排序顺序的新组合索引。
CREATE_INDEXES = {
    "plugindata": [
        ("ix_plugindata_plugin_id_key", ["plugin_id", "key"]),
    ],
    "message": [
        ("ix_message_reg_time_id", ["reg_time", "id"]),
    ],
    "siteuserdata": [
        ("ix_siteuserdata_updated_day_id", ["updated_day", "id"]),
        (
            "ix_siteuserdata_domain_updated_day_updated_time",
            ["domain", "updated_day", "updated_time"],
        ),
    ],
    "downloadhistory": [
        ("ix_downloadhistory_download_hash_date", ["download_hash", "date"]),
        ("ix_downloadhistory_date_id", ["date", "id"]),
    ],
    "downloadfiles": [
        ("ix_downloadfiles_download_hash_state", ["download_hash", "state"]),
        ("ix_downloadfiles_fullpath_id", ["fullpath", "id"]),
    ],
    "mediaserveritem": [
        ("ix_mediaserveritem_tmdbid_item_type", ["tmdbid", "item_type"]),
    ],
    "subscribe": [
        ("ix_subscribe_username", ["username"]),
        ("ix_subscribe_type_date", ["type", "date"]),
    ],
    "subscribehistory": [
        ("ix_subscribehistory_type_date", ["type", "date"]),
    ],
    "transferhistory": [
        ("ix_transferhistory_status_date", ["status", "date"]),
        ("ix_transferhistory_date_id", ["date", "id"]),
    ],
    "workflow": [
        ("ix_workflow_trigger_type_state", ["trigger_type", "state"]),
    ],
}


# 回退时恢复的旧查询索引，仍按实际存在字段检查。
DOWNGRADE_RESTORE_INDEXES = {
    "plugindata": [
        ("ix_plugindata_plugin_id", ["plugin_id"]),
        ("ix_plugindata_key", ["key"]),
    ],
    "message": [
        ("ix_message_reg_time", ["reg_time"]),
    ],
    "siteuserdata": [
        ("ix_siteuserdata_domain", ["domain"]),
        ("ix_siteuserdata_updated_day", ["updated_day"]),
    ],
    "downloadhistory": [
        ("ix_downloadhistory_download_hash", ["download_hash"]),
    ],
    "downloadfiles": [
        ("ix_downloadfiles_download_hash", ["download_hash"]),
        ("ix_downloadfiles_fullpath", ["fullpath"]),
    ],
    "mediaserveritem": [
        ("ix_mediaserveritem_tmdbid", ["tmdbid"]),
    ],
    "transferhistory": [
        ("ix_transferhistory_date", ["date"]),
    ],
    "userconfig": [
        ("ix_userconfig_username", ["username"]),
        ("ix_userconfig_username_key", ["username", "key"]),
    ],
}


def _load_schema_state(inspector: sa.Inspector):
    """
    一次读取现有表、字段和索引定义，供本次索引迁移判断使用。
    """
    tables = set(inspector.get_table_names())
    table_columns = {
        table_name: {
            column["name"]
            for column in inspector.get_columns(table_name)
        }
        for table_name in tables
    }
    table_indexes = {
        table_name: {
            index["name"]: {
                "columns": tuple(index.get("column_names") or []),
                "unique": bool(index.get("unique")),
            }
            for index in inspector.get_indexes(table_name)
        }
        for table_name in tables
    }
    return tables, table_columns, table_indexes


def _drop_index(
    table_name: str,
    index_name: str,
    tables: set[str],
    table_indexes: dict[str, dict[str, dict[str, object]]],
) -> None:
    """
    索引存在时删除，并同步更新迁移中的结构快照。
    """
    if table_name not in tables:
        return
    if index_name not in table_indexes[table_name]:
        return
    op.drop_index(index_name, table_name=table_name)
    table_indexes[table_name].pop(index_name, None)


def _drop_index_by_signature(
    table_name: str,
    columns: list[str],
    tables: set[str],
    table_indexes: dict[str, dict[str, dict[str, object]]],
    expected_name: str | None = None,
    unique: bool = False,
) -> None:
    """
    优先删除指定名称的索引，否则按字段顺序和唯一性删除首个匹配索引。
    """
    if table_name not in tables:
        return

    target_columns = tuple(columns)
    for index_name, index_meta in list(table_indexes[table_name].items()):
        if expected_name and index_name == expected_name:
            _drop_index(table_name, index_name, tables, table_indexes)
            return
        if index_meta.get("columns") == target_columns and index_meta.get("unique") == unique:
            _drop_index(table_name, index_name, tables, table_indexes)
            return


def _has_index_signature(
    table_name: str,
    columns: list[str],
    tables: set[str],
    table_indexes: dict[str, dict[str, dict[str, object]]],
    unique: bool = False,
) -> bool:
    """
    按字段顺序和唯一性确认是否已存在等价索引。
    """
    if table_name not in tables:
        return False

    target_columns = tuple(columns)
    for index_meta in table_indexes[table_name].values():
        if index_meta.get("columns") == target_columns and index_meta.get("unique") == unique:
            return True
    return False


def _create_index(
    table_name: str,
    index_name: str,
    columns: list[str],
    tables: set[str],
    table_columns: dict[str, set[str]],
    table_indexes: dict[str, dict[str, dict[str, object]]],
) -> None:
    """
    仅在目标表和字段存在且没有等价索引时创建索引。
    """
    if table_name not in tables:
        return
    if not set(columns).issubset(table_columns[table_name]):
        return
    if index_name in table_indexes[table_name]:
        return
    if _has_index_signature(table_name, columns, tables, table_indexes, unique=False):
        return
    op.create_index(index_name, table_name, columns, unique=False)
    table_indexes[table_name][index_name] = {
        "columns": tuple(columns),
        "unique": False,
    }


def upgrade() -> None:
    """
    以字段签名幂等替换 2.2.4 高频查询索引。
    """
    inspector = sa.inspect(op.get_bind())
    tables, table_columns, table_indexes = _load_schema_state(inspector)

    for table_name, index_specs in REDUNDANT_ID_INDEXES.items():
        for index_name, columns in index_specs:
            _drop_index_by_signature(
                table_name,
                columns,
                tables,
                table_indexes,
                expected_name=index_name,
                unique=False,
            )

    for table_name, index_specs in DROP_INDEXES.items():
        for index_name, columns in index_specs:
            _drop_index_by_signature(
                table_name,
                columns,
                tables,
                table_indexes,
                expected_name=index_name,
                unique=False,
            )

    for table_name, index_specs in CREATE_INDEXES.items():
        for index_name, columns in index_specs:
            _create_index(
                table_name,
                index_name,
                columns,
                tables,
                table_columns,
                table_indexes,
            )


def downgrade() -> None:
    """
    移除组合索引并恢复适用于当前表结构的旧索引。
    """
    inspector = sa.inspect(op.get_bind())
    tables, table_columns, table_indexes = _load_schema_state(inspector)

    for table_name, index_specs in CREATE_INDEXES.items():
        for index_name, _ in index_specs:
            _drop_index(table_name, index_name, tables, table_indexes)

    for table_name, index_specs in DOWNGRADE_RESTORE_INDEXES.items():
        for index_name, columns in index_specs:
            _create_index(
                table_name,
                index_name,
                columns,
                tables,
                table_columns,
                table_indexes,
            )

    for table_name, index_specs in REDUNDANT_ID_INDEXES.items():
        for index_name, columns in index_specs:
            _create_index(
                table_name,
                index_name,
                columns,
                tables,
                table_columns,
                table_indexes,
            )
