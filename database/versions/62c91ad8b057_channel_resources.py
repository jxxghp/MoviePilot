"""
将正式版本的渠道数据一次升级为连接、通知规则、交互策略和账号归属的最终结构。

保留最终 revision，使已经使用该结构的手测库无需改写版本号。
"""

import logging

import sqlalchemy as sa
from alembic import op

from database.legacy.channel_resources_v1 import read_channel_resources

revision = "62c91ad8b057"
down_revision = "c4e8f1a29b03"
branch_labels = None
depends_on = None

_DOWNLOAD_OWNER_INDEX = "ix_downloadhistory_owner_date_id"
_DOWNLOAD_REFERENCE_INDEX = "ix_downloadhistory_task_reference"
_OWNERSHIP_TABLES = ("subscribe", "subscribehistory")


def upgrade() -> None:
    """
    在一次迁移中建立最终表结构、隔离含糊群授权并导入正式版本通知配置。
    """
    _quarantine_group_authorizations()
    _create_browser_devices()
    _add_notification_progress()
    _add_download_ownership()
    _add_subscription_ownership()
    _create_inbound_receipts()
    _create_rule_task_grants()
    _migrate_channel_configuration()



def _group_address(channel: str, value: str) -> bool:
    """
    按冻结的历史平台格式判断群地址，不依赖未来宿主代码。

    :param channel: 历史平台显示值
    :param value: 旧发送者、别名或任务归属
    :return: QQ/VoceChat 的群地址时为 True
    """
    return (channel == "QQ" and value.strip().lower().startswith("group:")) or (
        channel == "VoceChat" and value.strip().upper().startswith("GID#")
    )


def _quarantine_group_authorizations() -> None:
    """
    原子隔离群授权、清理群别名和暂停任务，只输出计数诊断。

    不改变用户、订阅和下载数据；重复升级不会恢复停用或拒绝记录。
    """
    bind = op.get_bind()
    metadata = sa.MetaData()
    members = sa.Table("channelauthorization", metadata, autoload_with=bind)
    quarantined = set()
    alias_count = 0
    for row in bind.execute(sa.select(members)).mappings():
        aliases = [alias for alias in (row["aliases"] or []) if not _group_address(row["channel"], alias)]
        values = {}
        if aliases != (row["aliases"] or []):
            values["aliases"] = aliases
            alias_count += 1
        if _group_address(row["channel"], row["sender_id"]):
            quarantined.add(row["id"])
            values.update(role="member", scopes=[], origin="quarantine")
            if row["status"] not in {"disabled", "rejected"}:
                values["status"] = "pending"
        if values:
            bind.execute(members.update().where(members.c.id == row["id"]).values(**values))
    paused = 0
    if "agenttask" in sa.inspect(bind).get_table_names():
        tasks = sa.Table("agenttask", metadata, autoload_with=bind)
        for row in bind.execute(sa.select(tasks)).mappings():
            if row["execution_authorization_id"] in quarantined or _group_address(
                row["channel"] or "", row["user_id"] or ""
            ):
                if row["enabled"]:
                    paused += 1
                bind.execute(tasks.update().where(tasks.c.id == row["id"]).values(enabled=False))
    logging.getLogger("alembic.runtime.migration").info(
        "渠道身份隔离：待处理群记录=%d，清理群别名记录=%d，暂停任务=%d；请按真实发送者重新配置",
        len(quarantined), alias_count, paused,
    )


def _create_browser_devices() -> None:
    """
    幂等创建带账号外键、不可变 owner 和版本的设备表，不推断旧订阅归属。
    """
    if "webpushsubscription" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "webpushsubscription",
            sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
            sa.Column("endpoint_digest", sa.String(64), unique=True, nullable=False),
            sa.Column("endpoint", sa.Text(), nullable=False),
            sa.Column("p256dh", sa.String(256), nullable=False),
            sa.Column("auth", sa.String(256), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
            sa.Column("owner", sa.String(32), nullable=False),
            sa.Column("revision", sa.String(32), nullable=False),
        )
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("webpushsubscription")}
    if "ix_webpushsubscription_owner" not in indexes:
        op.create_index("ix_webpushsubscription_owner", "webpushsubscription", ["owner"])


def _add_notification_progress() -> None:
    """
    增加独立列，业务快照和已有意图保持原格式。
    """
    columns = sa.inspect(op.get_bind()).get_columns("outboxmessage")
    if any(column["name"] == "notification_progress" for column in columns):
        return
    with op.batch_alter_table("outboxmessage") as batch:
        batch.add_column(sa.Column("notification_progress", sa.JSON(), nullable=True))


def _add_download_ownership() -> None:
    """
    新增账号字段与分页索引。旧用户名无法证明归属，历史记录保持空值。
    """
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("downloadhistory")}
    indexes = {index["name"] for index in inspector.get_indexes("downloadhistory")}
    if "owner_identity" not in columns:
        op.add_column("downloadhistory", sa.Column("owner_identity", sa.String(32), nullable=True))
    if "task_reference" not in columns:
        op.add_column("downloadhistory", sa.Column("task_reference", sa.String(64), nullable=True))
    if _DOWNLOAD_OWNER_INDEX not in indexes:
        op.create_index(_DOWNLOAD_OWNER_INDEX, "downloadhistory", ["owner_identity", "date", "id"])
    if _DOWNLOAD_REFERENCE_INDEX not in indexes:
        op.create_index(_DOWNLOAD_REFERENCE_INDEX, "downloadhistory", ["task_reference"], unique=True)


def _add_subscription_ownership() -> None:
    """
    只增加字段和索引，旧用户名留作管理员核对依据。
    """
    for table in _OWNERSHIP_TABLES:
        inspector = sa.inspect(op.get_bind())
        if "owner_identity" not in {column["name"] for column in inspector.get_columns(table)}:
            op.add_column(table, sa.Column("owner_identity", sa.String(32), nullable=True))
        index = f"ix_{table}_owner_identity"
        if index not in {item["name"] for item in inspector.get_indexes(table)}:
            op.create_index(index, table, ["owner_identity"])


def _create_inbound_receipts() -> None:
    """
    创建入站短时认领记录及过期查询索引。
    """
    if not sa.inspect(op.get_bind()).has_table("channelinboundreceipt"):
        op.create_table(
            "channelinboundreceipt",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("expires_at", sa.Float(), nullable=False),
        )
        op.create_index("ix_channelinboundreceipt_expires_at", "channelinboundreceipt", ["expires_at"])


def _create_rule_task_grants() -> None:
    """
    登记成员和规则版本并建立按成员查询的索引。
    """
    if not sa.inspect(op.get_bind()).has_table("channelruletaskgrant"):
        op.create_table(
            "channelruletaskgrant",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("member_id", sa.String(32), nullable=False),
            sa.Column("member_version", sa.Float(), nullable=False),
            sa.Column("policy_revision", sa.Integer(), nullable=False),
            sa.Column("scopes", sa.JSON(), nullable=False),
        )
        op.create_index("ix_channelruletaskgrant_member_id", "channelruletaskgrant", ["member_id"])


def _migrate_channel_configuration() -> None:
    """
    直接导入正式版本通知配置，持久化最终资源而不写入过渡占位或中间策略。
    """
    bind = op.get_bind()
    table = sa.Table("systemconfig", sa.MetaData(), autoload_with=bind)
    existing = bind.execute(sa.select(table.c.value).where(table.c.key == "ChannelSettings")).first()
    legacy = bind.execute(sa.select(table.c.value).where(table.c.key == "Notifications")).scalar_one_or_none()
    stored = existing[0] if existing is not None else None
    updated = read_channel_resources(stored, legacy).model_dump(mode="json")
    if existing is None:
        bind.execute(table.insert().values(key="ChannelSettings", value=updated))
    elif stored != updated:
        bind.execute(table.update().where(table.c.key == "ChannelSettings").values(value=updated))



def downgrade() -> None:
    """
    账号归属与群授权隔离不可逆，回退必须恢复升级前完整数据库备份。

    :raises RuntimeError: 所有自动降级请求
    """
    raise RuntimeError("消息渠道与账号归属不可自动降级，请使用升级前完整数据库备份回退")
