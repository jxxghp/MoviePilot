"""2.2.4
调整数据库索引，补充高频组合索引并移除冗余 id 索引

Revision ID: 93f8cb6a4d1e
Revises: 58edfac72c32
Create Date: 2026-05-09
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "93f8cb6a4d1e"
down_revision = "58edfac72c32"
branch_labels = None
depends_on = None


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
    tables = set(inspector.get_table_names())
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
    return tables, table_indexes


def _drop_index(
    table_name: str,
    index_name: str,
    tables: set[str],
    table_indexes: dict[str, dict[str, dict[str, object]]],
) -> None:
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
    table_indexes: dict[str, dict[str, dict[str, object]]],
) -> None:
    if table_name not in tables:
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
    inspector = sa.inspect(op.get_bind())
    tables, table_indexes = _load_schema_state(inspector)

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
            _create_index(table_name, index_name, columns, tables, table_indexes)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables, table_indexes = _load_schema_state(inspector)

    for table_name, index_specs in CREATE_INDEXES.items():
        for index_name, _ in index_specs:
            _drop_index(table_name, index_name, tables, table_indexes)

    for table_name, index_specs in DOWNGRADE_RESTORE_INDEXES.items():
        for index_name, columns in index_specs:
            _create_index(table_name, index_name, columns, tables, table_indexes)

    for table_name, index_specs in REDUNDANT_ID_INDEXES.items():
        for index_name, columns in index_specs:
            _create_index(table_name, index_name, columns, tables, table_indexes)
